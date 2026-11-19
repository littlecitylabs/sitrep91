"""
MC5000 capture tool.

Replaces the charger-cli + PowerShell watchdog + log-scraping pipeline with
a single Python tool that speaks the BLE protocol directly.

WHAT IT DOES BETTER THAN THE OLD PIPELINE
-----------------------------------------
  * no 600-second timeout: charger-cli has a hardcoded 10-minute limit on
    every command including `monitor`, which is what fragmented every log
  * structured binary in, structured CSV out - no console-text scraping
  * captures fields the old pipeline could not see at all: internal
    resistance and the device's own capacity accumulator
  * validates every frame against a protocol fingerprint, so a firmware
    change surfaces as a loud error rather than quietly wrong numbers
  * writes each sample to disk as it arrives - a crash costs you the last
    row, not the run
  * can start multiple slots at once (config-only, no 0x93)

FIRMWARE BEHAVIOR THIS TOOL ENCODES (all verified on FW 2.02)
-------------------------------------------------------------
  * sending a 0x94 config STARTS the slot; there is no inert "configure now,
    start later" step. Only send a config when you mean to begin.
  * 0x93 does NOT stop a run. Measured with any channel value, including the
    documented 0x00 stop-all and the slot's own bitmask, it is indistinguishable
    from sending nothing. Two things do stop a run: 0xFE addressed to ONE
    slot's bitmask stops that slot alone (see stop_our_slots, which is what
    automatic teardown uses), and the init sequence, which stops EVERY slot and
    is therefore guarded - if a slot we did not start is busy, nothing is
    stopped. See stop_all_guarded, kept as the fallback.
  * init therefore also DESTROYS a run in progress, so it is NEVER sent
    except as a deliberate stop, and never on the start path or a reconnect.
    Measured with the probe_init probe: a slot pulling 1747mA at 494s elapsed
    went to 0mA / 0mAh / 0s within two seconds of init, voltage rebounding as
    the load came off. The device ACKs all three commands, so it is deliberate.
  * slot configs persist across power cycles, so always write a fresh config
    rather than relying on whatever the slot is holding.
  * every config carries a charger-enforced safety timer, which is the only
    protection that does not depend on this software behaving correctly.

EMBEDDING IT
------------
  Capture emits a Sample for every accepted reading. Anything that wants live
  data - a plot, a database writer - registers a callback rather than reading
  the CSV back:

      cap = Capture(name="bench", slots=[1, 2])
      cap.subscribe(lambda s: print(s.slot, s.status.voltage_v))

  Arguments are keyword-only. Capture.from_args builds one from parsed
  command-line arguments instead.

  The CSV is written regardless and is not routed through this mechanism; see
  Capture.subscribe for why that asymmetry is deliberate.

USAGE
-----
  Attach to something already running (started from the panel or elsewhere):
      python capture.py --attach --slots 1 --name thrunite_test

  Start and capture a discharge:
      python capture.py --start discharge --slots 1 \
          --current 500 --capacity 3400 --name thrunite_discharge

  Start two slots at once:
      python capture.py --start discharge --slots 1,2 \
          --current 500 --capacity 3000 --name pair_test
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import functools
import json
import math
import queue
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakDeviceNotFoundError, BleakError

from drift import FIRMWARE_FINGERPRINTS, Fingerprint, FrameValidator
from protocol import (
    build_temperature_query, parse_temperature_response,
    CHEMISTRY_DEFAULTS, Chemistry, DeviceInfo, KEEP_VOLTAGE_GATES_MV,
    KEEP_VOLTAGE_OVER_BLE, Mode,
    NICKEL_CHEMISTRIES, SlotStatus, STORAGE_VOLTAGE_MV, VOLTAGE_GATES,
    build_config, build_info_query, build_slot_stop, build_status_query,
    init_sequence, parse_info_response, parse_status_response,
)
# Files the tool keeps for itself live in the portable home folder, never
# relative to the working directory: a shortcut with a different "Start in"
# must not be able to lose them (that is how the old baseline.json gate could
# go silently off). settings.ini - which also holds the per-charger firmware
# records - lives there too; see runfiles.py.
from runfiles import (
    ACTIONS, APP_DIR, APP_VERSION, HOME_DIR, default_runs_dir, read_setting,
    run_csv_name, run_folder_name, session_log_path, unique_run_dir,
    write_setting,
)

CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
NAME_HINTS = ("charger", "mc5000")

# Firmware versions this toolkit has been verified against, and so may START
# runs on. An exact ALLOWLIST, deliberately not a minimum: "2.02 or newer"
# would let 2.03 start automatically the day SkyRC ships it, and every
# firmware finding this tool relies on is specific to the image that was
# traced. Verifying a new version means adding it here AND giving it a
# reference in drift.FIRMWARE_FINGERPRINTS.
SUPPORTED_FIRMWARE = frozenset({"2.02"})
# The embedded fingerprint a capture validates against before (or without)
# reading the device's version.
REFERENCE_FIRMWARE = "2.02"


class _BuiltinBaseline:
    """Sentinel: validate against the fingerprint embedded in code."""

    def __repr__(self):
        return "BUILTIN_BASELINE"


BUILTIN_BASELINE = _BuiltinBaseline()

# Consecutive "device not found" reconnect failures before we tell the user
# the charger is probably holding a stale link. Each failure is a ~30 s scan
# plus the 3 s retry pause, so this is ~3.3 min. The evidence, all measured:
#   - a Windows radio toggle left the charger not advertising and produced 15
#     in a row over 9+ minutes (unrecoverable without a power cycle);
#   - a USB dongle replug produced exactly ONE, then reconnected;
#   - after a CLEAN disconnect the charger takes ~2 minutes to advertise again
#     (two sessions) - about four misses.
# It was 3 (~1.5 min), which the third finding makes wrong: a drop that
# behaves like a clean disconnect would be told to power-cycle a charger that
# was about to come back by itself. 6 clears ~2 min with two attempts of
# margin and still warns well inside the 15 of the genuinely stuck case.
STALE_LINK_AFTER = 6

# The first connection of a session: how long to keep scanning before giving
# up. Measured: after a clean disconnect the
# charger takes ~2 minutes to start advertising again, so a user who closes
# the app and reopens it promptly would otherwise get "not found". 150 s is
# that ~2 min plus margin. Scans are SCAN_TIMEOUT_S each, so this is a count
# of scans, not a clock - which also keeps it testable.
SCAN_TIMEOUT_S = 10.0
CONNECT_WAIT_S = 150.0
REAPPEAR_NOTE = ("charger not found yet - the charger can take a couple of "
                 "minutes to reappear after it was last disconnected; "
                 "still looking")

# Seconds between 0x55 temperature polls. 30, down from 60, once both
# objections to polling faster were closed: the backlight timer is off on a
# stock unit, and the transmit buffer has 328 bytes of clearance.
TEMP_INTERVAL_S = 30.0

# The app-side temperature cutoff. APP-SIDE
# because the firmware parses the 0x94 TempCut field and then DISCARDS it - it
# never reaches the config struct. So
# this is a line the app watches and acts on, and it exists only while the app
# is connected; the charger's safety timer and its own 70 C battery limit stay
# the backstop. Below ~30 a warm charger's idle bay would trip it at once; the
# top stays under the charger's own 70.
TEMP_CUTOFF_MIN_C, TEMP_CUTOFF_MAX_C = 30, 65
# No temperature reading for this long, on a running slot with a cutoff set,
# and the cutoff is BLIND: it cannot trip on a value it never sees. Five
# minutes is ten polls - far beyond the ~3 % of replies that go missing, so
# ordinary drops never raise it.
CUTOFF_BLIND_AFTER_S = 300.0
# A per-slot stop from the cutoff that has not taken after this long is sent
# again, up to CUTOFF_STOP_TRIES in all.
CUTOFF_STOP_RETRY_S = 10.0
CUTOFF_STOP_TRIES = 3

# Confirming that a start TOOK. A config goes out as a write-without-response,
# and one can simply be lost: on the bench, slot 4's start
# was logged as sent and the slot never moved (status 0, no IR) while the
# three beside it started; a retry worked. So every start is watched until the
# charger shows the slot alive. A slot that really started shows run status 3
# and its IR within ~3 s. The config is re-sent ONLY on positive evidence it
# did not take: START_CONFIRM_S after the send, a reply that arrived at least
# START_FRESH_S after it still shows the slot wholly idle. No fresh reply at
# all is not evidence, so nothing is re-sent on silence; after START_GIVEUP_S
# the start is declared failed instead. Module-level so tests can shorten them.
START_CONFIRM_S = 6.0
START_FRESH_S = 5.0
START_TRIES = 2
START_GIVEUP_S = 30.0

# While a session does not know the firmware version, how often it re-asks.
# The version is read only on a wholly idle charger (0x57 is proven harmless
# there and nowhere else), so an attach beside a busy slot waits for a gap
# between runs and reads it then. Module-level so tests can shorten it.
FIRMWARE_RETRY_S = 60.0

CHEMISTRY_BY_NAME = {
    "liion": Chemistry.LIION, "liionhv": Chemistry.LIION_HV,
    "lifepo4": Chemistry.LIFEPO4, "nimh": Chemistry.NIMH,
    "nicd": Chemistry.NICD, "eneloop": Chemistry.ENELOOP,
    "nizn": Chemistry.NIZN, "ram": Chemistry.RAM,
    "lto": Chemistry.LTO, "naion": Chemistry.NAION,
}

# Reverse of CHEMISTRY_BY_NAME, which is one-to-one. meta.json records the name
# as spelled on the command line, NOT the enum member name: the user types
# "liionhv" while the member is LIION_HV, so deriving the string from the enum
# would write "liion_hv" and silently change the on-disk format for past runs.
CHEMISTRY_NAME = {v: k for k, v in CHEMISTRY_BY_NAME.items()}

# Responses that only ever arrive because we sent control traffic: the init
# sequence (0x74/0x65/0xfe) and a config (0x94). An attach sends neither, so
# their absence from the baseline comparison says nothing about the firmware,
# and drift.py is told to expect it. 0x94 matters since the baseline was
# regenerated from a run that started a slot - without it, every attach would
# report "command no longer seen: 0x94".
# The init trio, plus the per-slot stop that shares its 0xfe opcode. A run
# that only STARTS slots never sends any of these: starting is a 0x94 config
# and nothing else. They go out only on a deliberate stop.
INIT_RESPONSE_COMMANDS = ("0x74", "0x65", "0xfe")
# Everything a controlling session may trigger. An attach sends none of it.
CONTROL_RESPONSE_COMMANDS = INIT_RESPONSE_COMMANDS + ("0x94", "0x57")

# How many rounds of status polling to try before deciding a slot is silent.
# More than one because the first write after connecting is swallowed by the
# device's greeting frame; see the poll loop in session().
POLL_ATTEMPTS = 4

CSV_COLUMNS = [
    "wall_time", "elapsed_s", "slot", "voltage_v", "current_ma",
    "device_capacity_mah", "device_elapsed_s", "resistance_mohm",
    "state", "checksum_ok", "drift_severity", "note",
    # The device's OWN view of the run, from status-frame bytes 18-20 (the
    # firmware builder's byte map is in protocol.py). `state` above is still
    # ours, inferred; these are the charger's. Recorded rather than acted on -
    # notably `device_alert` is how a run that ended on the safety timer says so
    # itself, instead of us inferring teardown from counters resetting.
    # Appended at the end so older readers keyed on column NAMES are unaffected.
    "device_status", "device_mode", "device_alert",
    # The slot's temperature sensor in whole degrees C, from undocumented
    # opcode 0x55. A BAY sensor, not the cell: it reads about
    # 5 C above the cell surface once warm. SPARSE ON PURPOSE: a value appears
    # only on the row nearest the reading that produced it, and is blank
    # everywhere else. It is NOT carried forward, because that would invent a
    # measurement for a timestamp where none was taken and make staleness
    # invisible. Forward-fill at analysis time if you want it.
    # Older runs name it cell_temp_c; readers accept both - see
    # analyze.slot_temp_of.
    "slot_temp_c",
]


@dataclass
class RunConfig:
    """
    What the run was asked to do.

    Capture records this in meta.json and acts on none of it - the values that
    actually drive the charger go out in a config packet built by the caller.
    Keeping it separate from Capture's own parameters makes that distinction
    visible: everything here is provenance, not behavior.
    """

    mode: str = "attach"
    current_ma: int | None = None
    capacity_mah: int | None = None
    safety_timer_min: int | None = None
    cutoff_mv: int | None = None
    target_mv: int | None = None
    storage_mv: int | None = None
    charge_cutoff_current_ma: int | None = None
    discharge_cutoff_current_ma: int | None = None
    # The Advanced settings (None = the charger's default). Always None for a
    # run this program did not start.
    delta_v_mv: int | None = None
    trickle_ma: int | None = None
    keep_mv: int | None = None
    # The app-side temperature cutoff, if one was set. Provenance like the
    # rest; the capture's own record of what it did is in meta.json's
    # temp_cutoff_trips.
    temp_cutoff_c: int | None = None


@dataclass
class Sample:
    """
    One accepted reading from one slot - the unit a capture emits.

    `status` is the SlotStatus that drift.py already produced while validating
    the frame; it is carried through by reference and never re-parsed. Each
    frame is decoded by protocol.py exactly once, and this object is how that
    single result travels to everyone who needs it.

    Treat instances as read-only. Every subscriber receives the same object,
    so mutating one corrupts the data for everything downstream of it.
    """

    wall_time: datetime
    elapsed_s: float
    slot: int
    status: SlotStatus
    drift_severity: str
    note: str
    # The 0x55 temperature written on THIS row, else None - sparse, exactly as
    # the CSV column is. An int already decoded from its own frame family, so
    # carrying it here re-parses nothing.
    slot_temp_c: int | None = None


@dataclass(frozen=True)
class RunGroup:
    """
    Slots started together as one (a GUI group start, or a multi-slot CLI
    --start). Nothing structural: each member still gets its own run folder;
    the members share `started`, so their folders carry the same timestamp and
    sort together, and the id is recorded in each member's meta.json.
    """

    id: str
    slots: tuple[int, ...]
    started: datetime

    def to_meta(self) -> dict:
        return {"id": self.id, "slots": list(self.slots)}


def request_snapshot(request: RunRequest | None) -> dict | None:
    """Every field of a RunRequest, as plain JSON values - the full config a
    run was started with, recorded in its meta.json rather than a reference to
    something that can change afterward."""
    if request is None:
        return None
    out = {}
    for name, value in asdict(request).items():
        if isinstance(value, Mode):
            value = value.name.lower()
        elif isinstance(value, Chemistry):
            value = CHEMISTRY_NAME.get(value, value.name.lower())
        out[name] = value
    return out


@dataclass
class SlotRun:
    """
    One slot's one run: its own folder, CSV and meta.json.

    Opened when this program starts the slot (Capture.begin_slot_run), when a
    slot is seen running something it did not start ("external"), or - for a
    CLI --attach - on the first frame from each watched slot. Once a run has
    FINISHED its folder keeps receiving the slot's rows (the rested voltage
    after a run is worth having) until the next run on that slot opens its own.

    Everything per-run lives here rather than in per-slot dicts on Capture, so
    a meta.json describes exactly its own run and never the one after it.
    """

    slot: int
    action: str
    cell_id: str | None
    started: datetime
    name: str
    ours: bool = False
    request: RunRequest | None = None
    group: RunGroup | None = None
    dir: Path | None = None
    fh: object = None
    writer: object = None
    rows: int = 0
    t0: datetime | None = None
    drift_errors: int = 0
    peak_temp_c: int | None = None
    temp_cutoff_c: int | None = None
    cutoff_trip_c: int | None = None
    cutoff_stopped: bool = False
    stop_sent: bool = False
    ir_samples: list | None = None
    final: SlotStatus | None = None
    # The highest device elapsed this run's frames ever reached. `final` cannot
    # stand in for it: a finished run keeps receiving its slot's rows, so a
    # NEW run's first frame (elapsed 1, before current flows) overwrote `final`
    # and the new run no longer looked like "the counter went backward".
    peak_elapsed_s: int = 0
    alert: str | None = None
    finished: bool = False
    # False from a reconnect until the run is next seen live. A run that is
    # first seen ENDED after a link loss ended unwatched, for a reason the
    # app cannot know - a power cut reads exactly like a completed run.
    live_since_link: bool = True
    ended_unseen: bool = False
    end_reason: str | None = None
    closed: bool = False
    gaps_from: int = 0
    gaps_to: int | None = None
    attempts_from: int = 0
    attempts_to: int | None = None
    # Each time a write failure moved this run to the local default folder.
    relocations: list = field(default_factory=list)
    # A plain-language warning for the slot's card, else None.
    warning: str | None = None

    @property
    def csv_path(self) -> Path | None:
        return self.dir / run_csv_name(self.dir) if self.dir else None

    @property
    def meta_path(self) -> Path | None:
        return self.dir / "meta.json" if self.dir else None


def parse_cells(spec: str | None, slots: list[int]) -> dict[int, str | None]:
    """
    Turn "1=ENL-A-01,2=ENL-A-02" into {1: "ENL-A-01", 2: "ENL-A-02"}.

    Cell identity is per SLOT, not per run - a two-slot capture holds two
    different cells. Slots with no cell given map to None, so the recorded
    mapping always covers every slot being captured and downstream readers
    never have to guess whether a slot was omitted or simply unlabeled.

    Deliberately does NOT check the id against the database: db.py is opt-in,
    and capture.py must keep working with no database present.
    """
    cells: dict[int, str | None] = {s: None for s in slots}
    if not spec:
        return cells

    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"--cells entry {part!r} is not SLOT=CELL_ID "
                f"(e.g. --cells 1=ENL-A-01,2=ENL-A-02)")
        slot_text, cell_id = part.split("=", 1)
        slot_text, cell_id = slot_text.strip(), cell_id.strip()
        try:
            slot = int(slot_text)
        except ValueError:
            raise ValueError(
                f"--cells entry {part!r} has a non-numeric slot {slot_text!r}"
            ) from None
        if slot not in cells:
            raise ValueError(
                f"--cells names slot {slot}, which is not in --slots "
                f"{sorted(cells)}")
        if not cell_id:
            raise ValueError(f"--cells entry {part!r} has an empty cell id")
        cells[slot] = cell_id

    return cells


MODE_BY_NAME = {
    "discharge": Mode.DISCHARGE, "charge": Mode.CHARGE,
    "break_in": Mode.BREAK_IN, "refresh": Mode.REFRESH, "cycle": Mode.CYCLE,
}


@dataclass
class SessionOptions:
    """
    How to drive a BLE session - everything session() needs beyond the Capture.

    Field names deliberately match the command-line argument names, because
    session() reads them as plain attributes: the CLI hands it parsed args, an
    embedder hands it one of these, and session() cannot tell the difference.

    The defaults describe an ATTACH: start nothing, disturb nothing. An
    embedder that constructs SessionOptions() and changes nothing gets a
    monitor that cannot touch a running operation - the safe configuration is
    the zero-effort one.
    """

    start: str | None = None      # None = attach; "discharge" | "charge" starts
    mode: str | None = None       # override operation; see MODE_BY_NAME
    current: int = 500
    capacity: int = 3000
    safety_timer: int = 240
    cutoff_mv: int | None = None
    target_mv: int | None = None
    storage_mv: int | None = None
    ir_samples: int = 2
    charge_cutoff_current: int | None = None
    discharge_cutoff_current: int | None = None
    interval: float = 2.0
    report_every: float = 30.0
    # How long ONE connection records before the capture ends. None = no cap
    # (the GUI's default: it is meant to stay open). The cap only ever ends the
    # RECORDING; it never stops a slot - see session().
    max_hours: float | None = 6.0
    stop_on_drift: bool = False
    stop_on_exit: bool = True
    fresh: bool = True
    allow_unvalidated_firmware: bool = False
    temp_cutoff: int | None = None   # app-side cutoff in C; None = off

# Modes whose current mapping has been verified on hardware. build_config takes
# a charge current and a discharge current; for a plain charge or discharge only
# one of them is meaningful, and which is obvious. For cycle, refresh and
# break-in it is not - a cycle plainly needs both, and break-in conventionally
# runs at a fixed fraction of rated capacity that the charger may set itself.
#
# The old inline code sent `current if mode == CHARGE else 0` and
# `current if mode == DISCHARGE else 0`, which meant those three modes were
# configured with ZERO current in both directions. Refusing is better than
# silently starting an operation configured with nothing.
#
# Independent corroboration from skyrc-mc-rs's PROTOCOL.md: mode 0x05
# (Break-In) "causes device to abort after ~42 seconds" and is really mode
# 0x02 (Discharge) with specific parameters; Cycle/Refresh need a whole extra
# block the config carries but we do not populate (per-phase resting times at
# offsets 16-19, cycle count at 20, cycle DIRECTION at 21: 0=C>D, 1=D>C,
# 2=C>D>C, 3=D>C>D). So these modes need a hardware probe to establish their
# real parameter set, not a guess - the refusal stands until then.
# Storage is verified (the probe_storage probe): it
# drives a cell toward ~3.8 V from either side and idles at the target. Unlike
# charge/discharge it needs BOTH currents set - it may charge OR discharge to
# reach the target - which build_start_packets handles below.
VERIFIED_MODES = (Mode.CHARGE, Mode.DISCHARGE, Mode.STORAGE)

# Chemistry x mode combinations the toolkit will START. This is stricter than
# VERIFIED_MODES: a combination is here only if it has been run on the author's
# own charger (firmware 2.02). Everything else is MONITOR-ONLY - it still shows
# live and records normally, but starting it is refused with a plain message.
# Why a table of combinations and not just a list of modes: the mode byte, the
# voltage windows and the defaults all differ per chemistry, and a chemistry
# that has never run has never had its config checked against a real cell.
#
#   Li-ion   discharge (including four slots at once), charge, storage
#   NiMH     discharge, charge
#   Eneloop  discharge, charge
#
# Li-ion STORAGE was verified by a run started from the GUI; to take it out,
# delete Mode.STORAGE from the Li-ion row and nothing else. ROADMAP.md's table
# repeats this one, and test_capture.py checks the two agree.
# Never run: Li-ion HV, LiFePO4, LTO, Na-ion, NiCd, NiZn, RAM, and every Cycle,
# Refresh and Break-In mode.
VERIFIED_CONTROL = {
    Chemistry.LIION: (Mode.CHARGE, Mode.DISCHARGE, Mode.STORAGE),
    Chemistry.NIMH: (Mode.CHARGE, Mode.DISCHARGE),
    Chemistry.ENELOOP: (Mode.CHARGE, Mode.DISCHARGE),
}


def control_verified(chemistry, mode) -> bool:
    """Whether this chemistry and mode may be started (see VERIFIED_CONTROL)."""
    return mode in VERIFIED_CONTROL.get(chemistry, ())


def monitor_only_message(chemistry, mode=None) -> str:
    """The plain-words refusal for a combination that is not in the table."""
    name = CHEMISTRY_NAME.get(chemistry, chemistry.name.lower())
    what = (f"{name} {mode.name.lower().replace('_', '-')}"
            if mode is not None else name)
    allowed = "; ".join(
        f"{CHEMISTRY_NAME.get(c, c.name.lower())} "
        f"({', '.join(m.name.lower() for m in modes)})"
        for c, modes in VERIFIED_CONTROL.items())
    return (f"{what} is monitor-only in this version. Control is enabled only "
            f"for what has been run on the author's own charger: {allowed}. "
            f"You can still start it at the charger's front panel and watch "
            f"and record it here.")

# The charger honors a BLE charge-voltage target ONLY in this range - Li-ion's,
# the panel's own 4.05-4.25 V. Hardware-confirmed (a 4.100 V target was
# regulated to exactly 4.100 V). Out-of-range values are SILENTLY dropped to the
# per-chemistry default (~4.2 V) by the firmware - the exact trap that produced
# the long-wrong "target_mv ignored" verdict (earlier tests used 3.5/3.8 V, below
# the floor). So target_mv is refused outside this range, and on chemistries /
# modes where it is not verified, rather than let a charge run at a voltage the
# user did not ask for. Only Li-ion is confirmed; widen when others are tested.
# Both voltage fields are RANGE-GATED by the firmware: a value outside the
# per-chemistry window is silently replaced with the chemistry default, with no
# error and nothing observable over BLE. protocol.VOLTAGE_GATES holds the
# windows read out of the fw-2.02 normalizer; these two names are kept because
# the Li-ion target ceiling is what --target-mv is for.
TARGET_MV_MIN, TARGET_MV_MAX = VOLTAGE_GATES[Chemistry.LIION]["target_mv"]

# The firmware normalizer's ceiling on the Capacity field (config+0x02). Above
# it, the value is silently replaced with the chemistry default.
CAPACITY_MAH_MAX = 10000

# The normalizer's fixed bounds on the two run currents (fw 2.02, config +0x54
# Chg_Cur and +0x68 DChg_Cur). Like every other numeric field, a value outside
# is SILENTLY replaced with a default the firmware picks, so the run would not
# use the current that was asked for. Storage sends its one current as BOTH the
# charge and the discharge current, so it is held to the lower, discharge limit.
CHARGE_CURRENT_MA_MAX = 5000
DISCHARGE_CURRENT_MA_MAX = 2000
# TimeCut (config +0x108) is gated at 1440 minutes - 24 hours.
SAFETY_TIMER_MIN_MAX = 1440

# The LOWEST values the charger's own panel offers, read off its Charge,
# Discharge and Storage pages for every chemistry: Current
# 0.05 A and Capacity 100 mAh, the same on every page, except Eneloop, whose
# page has no Capacity line at all (its Size preset sets it). Nothing below them
# is ever sent - the firmware's own gates sit lower (a bare 1 mA), but a value
# the panel itself will not take is one nobody has run. Eneloop's capacity,
# which the app does send, is held to the same 100 mAh. The panel's Capacity
# line also offers OFF; it is deliberately not offered here (unmeasured).
CURRENT_MA_MIN = 50
CAPACITY_MAH_MIN = 100

# Retry's carried-over runs (Capture.carry_forward). A run the previous session
# started is claimed by the new one only if the device's elapsed counter matches
# where that run WOULD be by now - the elapsed it last showed plus the wall-clock
# time since it was last seen - to within this many seconds, AND the device
# reports the same mode. A panel run that began later cannot match: it would
# have to have started within this margin of the old run's own start, which a
# slot running that old run cannot have done. The margin covers what the
# comparison cannot know: the poll interval (2 s) and frame latency on each
# side, the counter's 1 s granularity, and the two clocks' tiny rate
# difference. A miss is fail-safe: the run is treated as a panel run (never
# stopped, no cutoff), which the safety timer still bounds.
CARRY_MARGIN_S = 30

# The Advanced settings' ranges, from the manual's "Parameters" table (the
# firmware gates every one of these numeric fields too, and silently replaces an
# out-of-range value with a default - see RunRequest.problem). Cut-off currents
# 10-990 mA; -dV 3-12 mV (NiMH/NiCd charge); trickle 50-300 mA in 10 mA steps
# (0 = off). Keep voltage windows are protocol.KEEP_VOLTAGE_GATES_MV (0 = off).
CUTOFF_CURRENT_MA_RANGE = (10, 990)
DELTA_V_MV_RANGE = (3, 12)
TRICKLE_MA_RANGE = (50, 300)
TRICKLE_STEP_MA = 10


# --- Configure's starting values -------------------------------------------
#
# What the Configure dialog fills in when a chemistry is chosen. The values are
# THE CHARGER'S OWN: read off the MC5000's front panel (CHARGE SETTINGS pages,
# slot 1) for Li-ion Charge/Discharge/Storage and for NiMH and
# Eneloop (Std AA) Charge/Discharge, and checked where they overlap against the
# manual's "Battery Voltage" table (target and cut-off voltages) and the
# firmware's own default table (every cut-off below is CHEMISTRY_DEFAULTS).
# The panel was not factory-reset first, so "default" means what an untouched
# page showed; the values are round and the manual's Li-ion walkthrough screen
# shows the same 5000 mAh / 3.00 A.
#
# ONE deliberate departure: the safety timer. The panel's default is OFF on every
# chemistry and mode; the timer is the only protection that survives this
# software failing, so it is never left off - see suggest_safety_timer_min.
#
# Notes on individual values:
#  * Li-ion capacity 5000 mAh, charge 3.00 A and discharge 2.00 A are the panel's
#    and suit a 5000 mAh cell. For a smaller cell, set Capacity first; the 1C
#    warning (c_rate_warning) says when a current is too high for it.
#  * Eneloop has no Capacity line on the panel - the Size preset sets it - and
#    this app sends Size = Std AA, whose capacity is 2000 mAh.
#  * Storage has two currents on the panel (charge 3.00 A, discharge 2.00 A).
#    "current_ma" here is the CHARGE one; the discharge one is in
#    advanced_defaults. A request that names no discharge current still sends
#    its one current both ways, as before the Advanced section existed.
#  * Nickel charge has no charge cut-off current on the panel, and a nickel
#    discharge's D cut-off current is the 100 mA the app already sends.
CONFIGURE_DEFAULTS = {
    Chemistry.LIION: {
        "capacity_mah": 5000,
        "current_ma": {Mode.CHARGE: 3000, Mode.DISCHARGE: 2000,
                       Mode.STORAGE: 3000},
    },
    Chemistry.NIMH: {
        "capacity_mah": 3000,
        "current_ma": {Mode.CHARGE: 1000, Mode.DISCHARGE: 1000},
    },
    Chemistry.ENELOOP: {
        "capacity_mah": 2000,
        "current_ma": {Mode.CHARGE: 1000, Mode.DISCHARGE: 1000},
    },
}


# --- Configure's starting CURRENTS: worked out from the capacity ---------------
#
# The panel's own currents (CONFIGURE_DEFAULTS above) suit a 5000 mAh cell and
# are wrong for most others: a Li-ion 800 mAh cell opened at 3 A. So Configure
# starts with the capacity BLANK and derives each current from it, capped at the
# panel's own default and rounded to CURRENT_STEP_MA. A value the user types
# over one is kept.
#
# The rates, and why:
#  * Li-ion CHARGE 0.5C: the usual "standard charge" rate on lithium-ion cell
#    datasheets; gentler than the panel's 3 A and well inside any 18650's
#    maximum.
#  * Li-ion DISCHARGE 0.2C: the rate lithium-ion rated capacity is specified at
#    (IEC 61960, 0.2 It A).
#  * NiMH / Eneloop CHARGE 0.5C: deliberately not lower. A -dV termination is
#    unreliable at low charge rates because the end-of-charge voltage drop gets
#    small, so 0.5C keeps it dependable (IEC 61951-2's own
#    standard charge is a slow 0.1C for 16 h, which this is not).
#  * NiMH / Eneloop DISCHARGE 0.2C: the rate nickel-metal-hydride rated capacity
#    is specified at (IEC 61951-2, 0.2 It A).
#  * Li-ion STORAGE: each leg is the same
#    operation as the plain run, so the charge leg (the main Current field) is
#    0.5C and the discharge leg (the first Advanced row) is 0.2C, each capped at
#    the panel's own storage default (3000 / 2000 mA). Storage moves the cell
#    toward the storage voltage in whichever direction it starts, so both legs
#    can run. Lower than the panel's defaults everywhere below their caps, and
#    the storage run measured on the bench (500 mA, 3.5 Ah) was slower than either.
# CURRENT_STEP_MA is 10: the firmware's gate takes any whole number of mA, so
# the step only keeps the figures readable (a 2300 mAh cell reads 1150, not
# 1149).
CHARGE_RATE = (1, 2)            # 0.5C as a fraction, so the maths is exact
DISCHARGE_RATE = (1, 5)         # 0.2C
CURRENT_STEP_MA = 10


def derive_current_ma(chemistry, mode, capacity_mah) -> int | None:
    """The starting current for `mode` on a cell of `capacity_mah`: 0.5C to
    charge, 0.2C to discharge, rounded to the nearest CURRENT_STEP_MA, never
    below one step and never above the panel's own default for that chemistry
    and action. For Storage this is the CHARGE leg (the main Current field); the
    discharge leg is derive_storage_discharge_ma. None where there is nothing
    to derive: no capacity yet, or an action this does not cover (Cycle,
    Refresh, Break-In; monitor-only chemistries have none)."""
    row = CONFIGURE_DEFAULTS.get(chemistry)
    if row is None or capacity_mah is None or capacity_mah <= 0:
        return None
    num, den = {Mode.CHARGE: CHARGE_RATE, Mode.STORAGE: CHARGE_RATE,
                Mode.DISCHARGE: DISCHARGE_RATE}.get(mode, (None, None))
    cap = row["current_ma"].get(mode)
    if num is None or cap is None:
        return None
    # nearest step, halves rounding up, in integers
    steps = (2 * capacity_mah * num + den * CURRENT_STEP_MA) // (
        2 * den * CURRENT_STEP_MA)
    return max(CURRENT_MA_MIN, min(cap, steps * CURRENT_STEP_MA))


def derive_storage_discharge_ma(chemistry, capacity_mah) -> int | None:
    """Storage's DISCHARGE current: the same 0.2C a Discharge run gets, rounded
    the same way and capped at the panel's storage-discharge default. None
    without a capacity, or for a chemistry with no storage run."""
    if (CONFIGURE_DEFAULTS.get(chemistry, {}).get("current_ma", {})
            .get(Mode.STORAGE) is None):
        return None
    got = derive_current_ma(chemistry, Mode.DISCHARGE, capacity_mah)
    return None if got is None else min(got, DISCHARGE_CURRENT_MA_DEFAULT)


def configure_defaults(chemistry, mode) -> dict | None:
    """The starting values for `chemistry` and `mode`, or None where the charger
    has none we have read (every monitor-only chemistry, and Cycle, Refresh and
    Break-In). Keys: current_ma, capacity_mah, cutoff_mv."""
    row = CONFIGURE_DEFAULTS.get(chemistry)
    if row is None or mode not in row["current_ma"]:
        return None
    return {"current_ma": row["current_ma"][mode],
            "capacity_mah": row["capacity_mah"],
            "cutoff_mv": CHEMISTRY_DEFAULTS[chemistry]["cutoff_mv"]}


# Safety-timer rule. minutes = capacity / current x 60 x margin, rounded UP to
# the next 10 and held to 30-1440 (the charger gates TimeCut at 1440).
# The margin is 1.5 everywhere except a NICKEL CHARGE, where it is 2.5: a nickel
# cell takes about 1.4x its capacity to fill (charge efficiency is below one,
# and the capacity counter itself under-reports a nickel charge by ~9.5 %), and
# the front panel's own timer for an Eneloop charge, set by hand, was 300 min -
# which is this rule's answer for 2000 mAh at 1000 mA. 1.5 covers the Li-ion
# charge measured at 1.34x ideal (1701 mAh at 1500 mA took 91 min against 68)
# and a nickel discharge, whose CV tail is already inside its 1.0x.
# Why a rule at all: a fixed 240 min ends a 3000 mAh discharge at 500 mA (6 h)
# at about 2000 mAh, reading as a capacity shortfall.
# STORAGE (cover the worse case plus margin): the worse case
# is a full cell discharged to the storage voltage or an empty one charged to
# it, and neither moves more than the whole capacity. The dialog passes the
# SLOWER of the two currents, so the rule is capacity / slower current x 1.5 -
# the whole capacity at the slow leg, not the roughly half a real storage run
# moves, with the 1.5 on top. That is already the case it needs to cover (the
# 500 mA run on a 3.5 Ah cell took 2.9 h against the 10.5 h this gives).
TIMER_MARGIN = (3, 2)            # 1.5 as a fraction, so the maths is exact
TIMER_MARGIN_NICKEL_CHARGE = (5, 2)
TIMER_STEP_MIN = 10
TIMER_FLOOR_MIN = 30


def suggest_safety_timer_min(chemistry, mode, capacity_mah, current_ma):
    """(minutes, capped). `capped` is True when the rule wanted more than the
    charger's 24 h limit and was held to it. None where capacity or current is
    not a positive number."""
    if capacity_mah <= 0 or current_ma <= 0:
        return None
    num, den = (TIMER_MARGIN_NICKEL_CHARGE
                if chemistry in NICKEL_CHEMISTRIES and mode is Mode.CHARGE
                else TIMER_MARGIN)
    # ceil(capacity * 60 * num / (current * den) / step) * step, in integers
    wanted = -(-(capacity_mah * 60 * num) // (current_ma * den * TIMER_STEP_MIN)) \
        * TIMER_STEP_MIN
    return (max(TIMER_FLOOR_MIN, min(SAFETY_TIMER_MIN_MAX, wanted)),
            wanted > SAFETY_TIMER_MIN_MAX)


def c_rate_warning(mode, current_ma, capacity_mah) -> str | None:
    """A plain warning when the current is above 1C for the capacity entered, or
    None. Warns, never refuses: it is a datasheet question the charger cannot
    answer, and the default currents are sized for the default capacity."""
    if current_ma <= 0 or capacity_mah <= 0 or current_ma <= capacity_mah:
        return None
    what = {Mode.CHARGE: "Charge", Mode.DISCHARGE: "Discharge",
            Mode.STORAGE: "Storage"}.get(mode, "The")
    return (f"{what} current {current_ma} mA is above 1C for {capacity_mah} mAh "
            f"(1C is {capacity_mah} mA). Check your cell's datasheet.")


# --- The Advanced settings -------------------------------------------------
#
# Which of them the charger's own pages show for each chemistry and action (the
# CHARGE SETTINGS pages read off slot 1), in the order the panel
# lists them. The names are RunRequest fields, so the dialog, the checks and the
# tests all read the one table. Everything not listed is not a setting of that
# page: nickel charge has no charge cut-off current, a discharge page has only
# its own cut-off current, and trickle and -dV exist only on nickel charge.
# The nickel charge page ALSO shows a Keep Voltage line that is not here on
# purpose: the firmware discards it over Bluetooth for nickel (protocol.
# KEEP_VOLTAGE_OVER_BLE), so it can only be set at the panel.
# (Storage's CHARGE current is the main Current field; its discharge current is
# the first Advanced row.)
_ADV_LIION = {
    Mode.CHARGE: ("target_mv", "charge_cutoff_current_ma", "keep_mv"),
    Mode.DISCHARGE: ("discharge_cutoff_current_ma",),
    Mode.STORAGE: ("storage_mv", "discharge_current_ma",
                   "charge_cutoff_current_ma", "discharge_cutoff_current_ma"),
}
_ADV_NICKEL = {
    Mode.CHARGE: ("delta_v_mv", "target_mv", "trickle_ma"),
    Mode.DISCHARGE: ("discharge_cutoff_current_ma",),
}


def advanced_fields(chemistry, mode) -> tuple:
    """The RunRequest field names Configure's Advanced section offers for this
    chemistry and action, in panel order; empty where the charger has none we
    can send (every monitor-only combination)."""
    if not control_verified(chemistry, mode):
        return ()
    table = _ADV_NICKEL if chemistry in NICKEL_CHEMISTRIES else _ADV_LIION
    return table.get(mode, ())


def advanced_defaults(chemistry, mode) -> dict:
    """The panel's own value for each Advanced field of this chemistry and
    action (zero = OFF for trickle and keep voltage, as the panel shows). Only
    the fields advanced_fields names; a request leaves a field None when it is
    at this value, meaning 'the charger's default', exactly as before."""
    values = {
        "target_mv": CHEMISTRY_DEFAULTS[chemistry]["target_mv"],
        "charge_cutoff_current_ma": 100,
        "discharge_cutoff_current_ma": 100,
        "delta_v_mv": 6,
        "trickle_ma": 0,
        "keep_mv": 0,
        "storage_mv": STORAGE_VOLTAGE_MV.get(chemistry, 0),
        "discharge_current_ma": DISCHARGE_CURRENT_MA_DEFAULT,
    }
    return {name: values[name] for name in advanced_fields(chemistry, mode)}


# The panel's Li-ion Storage discharge current. Kept here, beside the Advanced
# defaults, because Storage is the only place a second current exists.
DISCHARGE_CURRENT_MA_DEFAULT = 2000


@dataclass
class RunRequest:
    """
    What to ask a slot to do. Validated before anything is sent.

    Separate from RunConfig, which is the record of what a run WAS asked to do.
    This one is the instruction; that one is the provenance.
    """

    mode: Mode
    chemistry: Chemistry
    current_ma: int
    capacity_mah: int
    safety_timer_min: int
    cutoff_mv: int | None = None
    target_mv: int | None = None
    storage_mv: int | None = None
    charge_cutoff_current_ma: int | None = None
    discharge_cutoff_current_ma: int | None = None
    # App-side temperature cutoff in whole degrees C, or None for off. NOT sent
    # to the charger - the firmware discards TempCut (see TEMP_CUTOFF_MIN_C) -
    # so it plays no part in build_start_packets; the capture watches it. A
    # plain field so it is serialized with everything else.
    temp_cutoff_c: int | None = None
    # The Advanced settings (all None = the charger's own default, which is what
    # every request sent before they existed). discharge_current_ma is Storage's
    # second current: Storage then charges at current_ma and discharges at this;
    # None sends current_ma both ways. delta_v_mv and trickle_ma are nickel
    # charge only; trickle_ma and keep_mv treat 0 as off.
    discharge_current_ma: int | None = None
    delta_v_mv: int | None = None
    trickle_ma: int | None = None
    keep_mv: int | None = None

    def problem(self) -> str | None:
        """Why this must not be sent, or None if it is safe to build."""
        if self.mode not in VERIFIED_MODES:
            return (
                f"{self.mode.name.lower()} is not supported: its charge and "
                f"discharge current mapping has never been verified on "
                f"hardware, and configuring it would send 0mA in both "
                f"directions. Use charge or discharge.")
        # Deliberate limit: no field, flag or option lets a combination outside
        # VERIFIED_CONTROL through, so what can be started is exactly what was
        # run on the author's charger.
        if not control_verified(self.chemistry, self.mode):
            return monitor_only_message(self.chemistry, self.mode)
        if self.current_ma < CURRENT_MA_MIN:
            return (f"current {self.current_ma}mA is below the {CURRENT_MA_MIN}mA "
                    f"the charger's own panel allows as its lowest. SITREP91 "
                    f"sends nothing below what the panel offers.")
        if self.capacity_mah < CAPACITY_MAH_MIN:
            return (f"rated capacity {self.capacity_mah}mAh is below the "
                    f"{CAPACITY_MAH_MIN}mAh the charger's own panel allows as "
                    f"its lowest. SITREP91 sends nothing below what the panel "
                    f"offers.")
        if self.capacity_mah > CAPACITY_MAH_MAX:
            # Same silent-substitution trap as the voltages, and this field is
            # not just metadata: the charger derives a capacity ceiling from it
            # (runtime+0x1c, 1.2x rated on one branch) and TEARS THE SLOT DOWN
            # when delivered capacity passes it. A wrong value here ends runs.
            return (f"rated capacity {self.capacity_mah}mAh is above the "
                    f"{CAPACITY_MAH_MAX}mAh the charger accepts. Out-of-range "
                    f"values are SILENTLY replaced with a per-chemistry default "
                    f"by the firmware, and the charger stops a run once "
                    f"delivered capacity passes ~1.2x whatever it ends up "
                    f"holding - so this would cut the run short.")
        # Storage with a named discharge current charges at current_ma and
        # discharges at that one, so each has its own limit; without one it
        # sends current_ma both ways and is held to the lower, discharge limit.
        split = self.mode is Mode.STORAGE and self.discharge_current_ma is not None
        limit = (CHARGE_CURRENT_MA_MAX if self.mode is Mode.CHARGE or split
                 else DISCHARGE_CURRENT_MA_MAX)
        if self.current_ma > limit:
            kind = ("charge" if self.mode is Mode.CHARGE or split else
                    "discharge" if self.mode is Mode.DISCHARGE else "storage")
            why = (" (storage sends its one current as both the charge and "
                   "the discharge current)"
                   if self.mode is Mode.STORAGE and not split else "")
            return (f"{kind} current {self.current_ma}mA is above the {limit}mA "
                    f"the charger accepts{why}. Out-of-range values are "
                    f"SILENTLY replaced with a default by the firmware, so the "
                    f"run would not use the current you asked for.")
        if self.discharge_current_ma is not None:
            if self.mode is not Mode.STORAGE:
                return ("a separate discharge current only applies to a storage "
                        "run, which has two currents. Drop it.")
            if self.discharge_current_ma < CURRENT_MA_MIN:
                return (f"discharge current {self.discharge_current_ma}mA is "
                        f"below the {CURRENT_MA_MIN}mA the charger's own panel "
                        f"allows as its lowest. SITREP91 sends nothing below "
                        f"what the panel offers.")
            if self.discharge_current_ma > DISCHARGE_CURRENT_MA_MAX:
                return (f"discharge current {self.discharge_current_ma}mA is "
                        f"above the {DISCHARGE_CURRENT_MA_MAX}mA the charger "
                        f"accepts. Out-of-range values are SILENTLY replaced "
                        f"with a default by the firmware, so the run would not "
                        f"use the current you asked for.")
        if self.safety_timer_min > SAFETY_TIMER_MIN_MAX:
            return (f"safety timer {self.safety_timer_min} min is above the "
                    f"{SAFETY_TIMER_MIN_MAX} min (24 h) the charger accepts. "
                    f"Out-of-range values are SILENTLY replaced with a default "
                    f"by the firmware, so the timer would not be what you set.")
        if self.safety_timer_min < 0:
            return f"safety timer cannot be negative, got {self.safety_timer_min}"
        if self.safety_timer_min == 0:
            # There is no way to switch the timer off.
            return ("refusing to run with no safety timer. It is the only "
                    "protection that survives this software crashing, the BLE "
                    "link dropping or the machine sleeping, so a run always "
                    "has one.")
        if self.target_mv is not None:
            # target_mv is a CHARGE ceiling, verified only for Li-ion. Refuse it
            # anywhere else, and refuse out-of-range values loudly - the firmware
            # silently drops those to the default, which is not what was asked.
            if self.mode is not Mode.CHARGE:
                return (f"target voltage is a charge ceiling; it does not apply "
                        f"to {self.mode.name.lower()}. Drop the target voltage.")
            # The window is per chemistry (VOLTAGE_GATES): Li-ion's was measured
            # on hardware; the nickel one is the firmware's, read from the image
            # and not yet seen on the panel (the Advanced bench script checks).
            lo, hi = VOLTAGE_GATES[self.chemistry]["target_mv"]
            if not (lo <= self.target_mv <= hi):
                name = CHEMISTRY_NAME.get(self.chemistry, self.chemistry.name)
                dflt = CHEMISTRY_DEFAULTS[self.chemistry]["target_mv"]
                return (f"target voltage {self.target_mv}mV is outside the "
                        f"{lo}-{hi}mV range the charger honors for {name}. "
                        f"Out-of-range values are SILENTLY dropped to the "
                        f"{dflt}mV default by the firmware, so this is refused "
                        f"rather than charging to a voltage you did not ask for.")
        if self.cutoff_mv is not None:
            # Same trap as target_mv, and this one is the discharge floor: the
            # firmware silently swaps an out-of-range cutoff for the chemistry
            # default, so the run ends at a voltage nobody asked for. Refuse
            # instead of discharging to a guess.
            lo, hi = VOLTAGE_GATES[self.chemistry]["cutoff_mv"]
            if not (lo <= self.cutoff_mv <= hi):
                name = CHEMISTRY_NAME.get(self.chemistry, self.chemistry.name)
                dflt = CHEMISTRY_DEFAULTS[self.chemistry]["cutoff_mv"]
                return (f"cutoff voltage {self.cutoff_mv}mV is outside the "
                        f"{lo}-{hi}mV range the charger honors for {name}. "
                        f"Out-of-range values are SILENTLY replaced with the "
                        f"{dflt}mV default by the firmware, so the run would "
                        f"end at a voltage you did not ask for.")
        if self.storage_mv is not None:
            # Li_Sto_Volt (config+0xa4), gated per chemistry like the others.
            # A (0, 0) window means the chemistry has no storage target at all.
            if self.mode is not Mode.STORAGE:
                return (f"storage voltage only applies to a storage run, not "
                        f"{self.mode.name.lower()}. Drop the storage voltage.")
            lo, hi = VOLTAGE_GATES[self.chemistry]["storage_mv"]
            name = CHEMISTRY_NAME.get(self.chemistry, self.chemistry.name)
            if (lo, hi) == (0, 0):
                return (f"{name} has no storage-voltage target on this "
                        f"firmware - the field is gated to zero and whatever "
                        f"is sent is discarded. Drop the storage voltage.")
            if not (lo <= self.storage_mv <= hi):
                return (f"storage voltage {self.storage_mv}mV is outside the "
                        f"{lo}-{hi}mV range the charger honors for {name}. "
                        f"Out-of-range values are SILENTLY replaced with the "
                        f"default by the firmware, so the cell would park at a "
                        f"voltage you did not ask for.")
        advanced = self._advanced_problem()
        if advanced:
            return advanced
        return temp_cutoff_problem(self.temp_cutoff_c)

    def _advanced_problem(self) -> str | None:
        """The remaining Advanced settings: where each applies, and its range.
        Same shape as the voltage checks above - an out-of-range value is
        silently replaced by the firmware, so it is refused with the window."""
        name = CHEMISTRY_NAME.get(self.chemistry, self.chemistry.name)
        nickel = self.chemistry in NICKEL_CHEMISTRIES
        lo, hi = CUTOFF_CURRENT_MA_RANGE
        for label, value, applies, where in (
            ("charge cut-off current", self.charge_cutoff_current_ma,
             (self.mode is Mode.CHARGE and not nickel) or self.mode is Mode.STORAGE,
             "a lithium charge or storage run (a nickel charge has none)"),
            ("discharge cut-off current", self.discharge_cutoff_current_ma,
             self.mode in (Mode.DISCHARGE, Mode.STORAGE),
             "a discharge or storage run"),
        ):
            if value is None:
                continue
            if not applies:
                return (f"the {label} only applies to {where}, not "
                        f"{name} {self.mode.name.lower()}. Drop it.")
            if not (lo <= value <= hi):
                return (f"{label} {value}mA is outside the {lo}-{hi}mA range "
                        f"the charger honors. Out-of-range values are SILENTLY "
                        f"replaced with a default by the firmware, so the run "
                        f"would end at a current you did not ask for.")
        if self.delta_v_mv is not None:
            if not (nickel and self.mode is Mode.CHARGE):
                return (f"-dV only applies to a nickel charge, not "
                        f"{name} {self.mode.name.lower()}. Drop it.")
            dlo, dhi = DELTA_V_MV_RANGE
            if not (dlo <= self.delta_v_mv <= dhi):
                return (f"-dV {self.delta_v_mv}mV is outside the {dlo}-{dhi}mV "
                        f"range the charger honors. Out-of-range values are "
                        f"SILENTLY replaced with a default by the firmware, so "
                        f"the charge would not end on the drop you asked for.")
        if self.trickle_ma is not None:
            if not (nickel and self.mode is Mode.CHARGE):
                return (f"trickle charge only applies to a nickel charge, not "
                        f"{name} {self.mode.name.lower()}. Drop it.")
            tlo, thi = TRICKLE_MA_RANGE
            if self.trickle_ma != 0 and not (
                    tlo <= self.trickle_ma <= thi
                    and self.trickle_ma % TRICKLE_STEP_MA == 0):
                return (f"trickle {self.trickle_ma}mA must be 0 (off) or "
                        f"{tlo}-{thi}mA in {TRICKLE_STEP_MA}mA steps. "
                        f"Out-of-range values are SILENTLY replaced with a "
                        f"default by the firmware.")
        if self.keep_mv is not None:
            if self.keep_mv and self.chemistry not in KEEP_VOLTAGE_OVER_BLE:
                return (f"the charger ignores a keep voltage sent over "
                        f"Bluetooth for {name} (its firmware discards the "
                        f"value, measured on hardware). Drop it, or set it at "
                        f"the charger's own panel - SITREP91 will watch and "
                        f"log that run.")
            if self.mode is not Mode.CHARGE:
                return (f"keep voltage only applies to a charge, not "
                        f"{self.mode.name.lower()}. Drop it.")
            klo, khi = KEEP_VOLTAGE_GATES_MV[self.chemistry]
            if self.keep_mv != 0 and not (klo <= self.keep_mv <= khi):
                return (f"keep voltage {self.keep_mv}mV must be 0 (off) or "
                        f"{klo}-{khi}mV for {name}. Out-of-range values are "
                        f"SILENTLY replaced with a default by the firmware.")
        return None

    def effective_cutoff_mv(self) -> int:
        return self.cutoff_mv or CHEMISTRY_DEFAULTS[self.chemistry]["cutoff_mv"]

    def mode_byte(self) -> int:
        from protocol import mode_byte_for_chemistry
        return mode_byte_for_chemistry(self.mode, self.chemistry)


def temp_cutoff_problem(value) -> str | None:
    """Why `value` is not a usable temperature cutoff, or None (None = off).
    Shared by RunRequest.problem and the CLI's early check."""
    if value is None:
        return None
    if not (TEMP_CUTOFF_MIN_C <= value <= TEMP_CUTOFF_MAX_C):
        return (f"temperature cutoff {value} C is outside "
                f"{TEMP_CUTOFF_MIN_C}-{TEMP_CUTOFF_MAX_C} C. Below that a warm "
                f"charger's idle bay would trip it at once; above it, it would "
                f"sit on top of the charger's own 70 C battery limit.")
    return None


def build_start_packets(slots: list[int], req: RunRequest) -> list[tuple[int, bytes]]:
    """
    Build the config packet for each slot. One place, used by the CLI and by
    anything else that starts a run, so no caller assembles packets itself.

    On this firmware a config packet IS the start command, so calling this means
    committing to starting. Raises ValueError if the request is not safe to
    send - callers should surface req.problem() before getting here.
    """
    problem = req.problem()
    if problem:
        raise ValueError(problem)

    packets = []
    for s in slots:
        packets.append((s, build_config(
            slot=s,
            mode=req.mode,
            chemistry=req.chemistry,
            # Storage may charge OR discharge to reach the target, so it needs
            # both currents (as probe_storage sent them); charge/discharge need
            # only their own direction.
            charge_current_ma=(req.current_ma
                               if req.mode in (Mode.CHARGE, Mode.STORAGE) else 0),
            discharge_current_ma=(
                (req.discharge_current_ma or req.current_ma)
                if req.mode is Mode.STORAGE else
                req.current_ma if req.mode is Mode.DISCHARGE else 0),
            capacity_mah=req.capacity_mah,
            cutoff_timer_min=req.safety_timer_min,
            **({"cutoff_mv": req.cutoff_mv} if req.cutoff_mv else {}),
            **({"target_mv": req.target_mv} if req.target_mv else {}),
            **({"storage_mv": req.storage_mv} if req.storage_mv else {}),
            **({"charge_cutoff_current_ma": req.charge_cutoff_current_ma}
               if req.charge_cutoff_current_ma else {}),
            **({"discharge_cutoff_current_ma": req.discharge_cutoff_current_ma}
               if req.discharge_cutoff_current_ma else {}),
            **({"delta_peak_mv": req.delta_v_mv} if req.delta_v_mv else {}),
            **({"trickle_ma": req.trickle_ma} if req.trickle_ma else {}),
            **({"keep_mv": req.keep_mv} if req.keep_mv else {}),
        )))
    return packets


@dataclass
class StartInjection:
    """
    One armed slot's start, handed from a front end into the live session.

    `config` is the 0x94 packet actually written; `request` is carried alongside
    only so the session can log honest numbers and a demo source can render the
    slot coming alive without re-parsing the packet. Build one with
    `StartInjection.build(slot, request)`.

    `cell_id` and `group` travel with the start because they belong to the RUN
    it begins: the run's folder is named after the cell, and a group start is
    recorded as one in each member's meta.json (see RunGroup).
    """

    slot: int
    config: bytes
    request: RunRequest
    cell_id: str | None = None
    group: RunGroup | None = None

    @classmethod
    def build(cls, slot: int, request: RunRequest, cell_id: str | None = None,
              group: RunGroup | None = None) -> "StartInjection":
        (_, cfg), = build_start_packets([slot], request)
        return cls(slot=slot, config=cfg, request=request, cell_id=cell_id,
                   group=group)


# The SkyRC product number the MC5000 answers to 0x57 with (SK-100214). It is a
# MODEL identifier, constant for every unit (the MC3000 answers 100083), so it
# is what tells an MC5000 from another SkyRC charger whose name also contains
# "charger" and whose firmware could read 2.02 too. Starting requires it.
SUPPORTED_PRODUCT = "100214"
_NOT_CHECKED = object()


def firmware_refusal(observed_fw: str | None, remembered_fw: str | None,
                     allow_unvalidated: bool,
                     product=_NOT_CHECKED) -> str | None:
    """
    Why a run must not be started on this firmware, or None if it may.

    The invariant: never START on firmware that is not in SUPPORTED_FIRMWARE.
    Watching is always permitted - this gate is only consulted on the start
    path. A run may start if the version was read just now and is supported,
    or - when it could not be read now (0x57 is only sent to a wholly idle
    charger, and may go unanswered) - if this charger was last read as a
    supported version (remembered_firmware). A version read now always wins
    over the remembered one, so a charger that was updated is caught.

    Unlike the gate this replaces, an unreadable version with nothing
    remembered REFUSES: absence of evidence used to mean "start anyway", which
    let any stranger's charger run whenever a slot happened to be busy.
    """
    if allow_unvalidated:
        return None
    fw = observed_fw or remembered_fw
    if fw in SUPPORTED_FIRMWARE and product is not _NOT_CHECKED:
        # The model must be the MC5000's too: any device whose name contains
        # "charger" and whose reply decodes to a supported version used to pass.
        if product is None:
            return (f"Couldn't confirm this charger's model (its product "
                    f"code), and it has not been read on this computer "
                    f"before. This version supports the SkyRC MC5000 (product "
                    f"code {SUPPORTED_PRODUCT}). Watching is available; "
                    f"starting runs is not until the model is known. It is "
                    f"only read while every slot is idle - let running slots "
                    f"finish, then reconnect.")
        if product != SUPPORTED_PRODUCT:
            return (f"This charger reports product code {product}, not "
                    f"{SUPPORTED_PRODUCT} (the SkyRC MC5000). This version "
                    f"supports the MC5000 only. Watching is available; "
                    f"starting runs is not.")
    if fw in SUPPORTED_FIRMWARE:
        return None
    supported = ", ".join(sorted(SUPPORTED_FIRMWARE))
    if fw is None:
        return (f"Couldn't read this charger's firmware version, and it has "
                f"not been read on this computer before. This version supports "
                f"firmware {supported}. Watching is available; starting runs "
                f"is not until the version is known. The version is only read "
                f"while every slot is idle - let running slots finish, then "
                f"reconnect.")
    return (f"Firmware {fw} found; this version supports {supported}. "
            f"Watching is available; starting runs is not. Please report "
            f"firmware {fw} so it can be verified.")


def _charger_section(address: str) -> str:
    return f"charger {address.upper()}"


def remembered_firmware(address: str | None) -> str | None:
    """The firmware version last read from the charger at `address`, from
    settings.ini, or None. Never raises: a missing or damaged file simply
    means nothing is remembered, and the gate then refuses rather than guess."""
    if not address:
        return None
    return read_setting(_charger_section(address), "firmware")


def remembered_product(address: str | None) -> str | None:
    """The product code last read from the charger at `address`, or None."""
    if not address:
        return None
    return read_setting(_charger_section(address), "product_code")


def _observed_product(cap):
    """The product code read this session, or _NOT_CHECKED when none was: the
    watching-only verdict holds a charger to the MC5000 only on evidence read
    now, never on a remembered record (see product_for for starts)."""
    info = getattr(cap, "device_info", None)
    code = getattr(info, "product_code", None) if info is not None else None
    return code if code else _NOT_CHECKED


def product_for(cap) -> str | None:
    """The product code to hold a start to: the one read this session, else the
    one remembered for this charger, else None (unknown)."""
    info = getattr(cap, "device_info", None)
    return ((getattr(info, "product_code", None) if info is not None
             else None)
            or remembered_product(getattr(cap, "address", None)))


def remember_firmware(address: str | None, firmware: str | None,
                      log=None, product_code: str | None = None) -> None:
    """Record the version just read from this charger, supported or not, so a
    later session that cannot read it (a busy slot) knows what it is. A write
    failure is logged, never raised: losing the record costs a refusal later,
    not the run."""
    if not address or not firmware:
        return
    values = {"firmware": firmware,
              "read_at": datetime.now().isoformat(timespec="seconds")}
    if product_code:
        values["product_code"] = product_code
    write_setting(_charger_section(address), values, log)


def refuse_reason(slots: list[int], latest: dict, *,
                  for_gui: bool = False) -> str | None:
    """
    Why a run must not be started, or None if it is safe to proceed.

    `for_gui` only rewords the busy-slot message (the CLI's "Use --attach" is no
    use to someone in the window, which is already watching); the refusal itself
    is the same either way.

    `latest` maps slot number to the most recent SlotStatus, gathered by polling
    BEFORE anything is written to the device. That ordering is the whole point:
    init stops running operations, so these checks are worthless if they run
    after it. Once, a mistyped --start killed a live discharge and the
    guardrail then correctly reported a run it had itself just ended.

    A pure function so it can be tested offline, and so the GUI can ask the same
    question before offering a start button rather than reimplementing it.
    """
    missing = [s for s in slots if s not in latest]
    if missing:
        return (f"slot(s) {missing} did not report status, so their state is "
                f"unknown and starting would be a guess")

    busy = [s for s in slots if latest[s].current_ma > 0]
    if busy:
        if for_gui:
            who = "it is" if len(busy) == 1 else f"slots {busy} are"
            return (f"{who} already running something. If it was started at "
                    f"the charger, SITREP91 is only watching it. If a break-in "
                    f"has finished at the panel, clear its Done message there, "
                    f"then start.")
        return (f"slot(s) {busy} already running. "
                f"Use --attach to capture them instead.")

    empty = [s for s in slots if latest[s].state == "empty"]
    if empty:
        return f"slot(s) {empty} read 0.000V (no cell)"

    return None


def sample_to_row(s: Sample) -> dict:
    """Map a Sample onto CSV_COLUMNS. The only place that mapping is defined.

    The temperature comes from Sample.slot_temp_c, an int the 0x55 frame
    already yielded - a DIFFERENT frame family from the 0x91 status frame in
    Sample.status, so carrying it re-parses nothing: protocol.py still decodes
    each frame exactly once.
    """
    return {
        "wall_time": s.wall_time.isoformat(timespec="seconds"),
        "elapsed_s": round(s.elapsed_s, 1),
        "slot": s.slot,
        "voltage_v": s.status.voltage_v,
        "current_ma": s.status.current_ma,
        "device_capacity_mah": s.status.capacity_mah,
        "device_elapsed_s": s.status.elapsed_seconds,
        "resistance_mohm": s.status.resistance_mohm,
        "state": s.status.state,
        "checksum_ok": s.status.checksum_ok,
        "drift_severity": s.drift_severity,
        "note": s.note,
        "device_status": s.status.device_status,
        "device_mode": s.status.mode_code,
        # the name, not the number - a CSV is read by people
        "device_alert": s.status.alert,
        "slot_temp_c": s.slot_temp_c,
    }


class Log:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, flush=True)
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()


class Capture:
    """
    One capture session: validates frames, writes each slot's runs to their own
    folders (see SlotRun), tracks slot state.

    A session is one BLE connection's worth of watching, possibly many runs
    long; the RECORD is per slot per run - one folder, one CSV, one meta.json
    each, named by runfiles.run_folder_name. A frame is written to its slot's
    open run; a slot with no run open (idle, and nothing started or seen
    running on it yet) is still reported to subscribers but has no file.

    Takes explicit values rather than a parsed-args object, so anything that is
    not the command line - the GUI, a test, any future embedding - does not have
    to fabricate a fake Namespace to construct one. Capture.from_args builds one
    from parsed command-line arguments.

    Arguments are keyword-only. There are enough of them, and enough share a
    type, that positional calls would be easy to get silently wrong.

    `outdir` is the logs folder, read afresh for every run it opens, so a
    front end that changes it mid-session changes it from the next run.
    `fallback_dir`, when given, is where a run moves if writing to `outdir`
    fails (the public app's rule: warn and carry on); without it a write
    failure is left to surface as a crash, as the CLI always has.
    `record_idle` opens a run for every watched slot at its first frame, idle
    or not (a CLI --attach, which has always recorded idle slots).
    `external_runs` opens a run when a slot is seen running something this
    capture did not start.
    """

    def __init__(
        self,
        *,
        name: str,
        slots: list[int],
        chemistry: Chemistry = Chemistry.LIION,
        outdir: str | Path | None = None,
        fallback_dir: str | Path | None = None,
        baseline: str | Path | None = BUILTIN_BASELINE,
        lenient: bool = False,
        done_after: int = 5,
        cells: dict[int, str | None] | None = None,
        run_config: RunConfig | None = None,
        temp_interval_s: float | None = None,
        sends_control: bool = True,
        record_idle: bool = False,
        external_runs: bool = True,
        folder_label: str | None = None,
        log: Log | None = None,
    ):
        self.log = log or Log()
        self.name = name
        self.slots = list(slots)
        self.chemistry = chemistry
        self.done_after = done_after
        self.cells = dict(cells) if cells else {s: None for s in self.slots}
        self.run_config = run_config or RunConfig()

        self.started_at = datetime.now()
        self.outdir = Path(outdir) if outdir else default_runs_dir()
        self.fallback_dir = Path(fallback_dir) if fallback_dir else None
        self.record_idle = record_idle
        self.external_runs = external_runs
        # Appended to every run folder's name (the CLI's --name); None for
        # the GUI, whose folders follow the spec exactly.
        self.folder_label = folder_label
        # Every run this capture opened, in order, and the one currently
        # receiving each slot's rows.
        self.runs: list[SlotRun] = []
        self.open_runs: dict[int, SlotRun] = {}
        # The CLI's session log, if it keeps one; recorded in each meta.json.
        self.session_log: Path | None = None

        # The protocol reference. By default the fingerprint embedded in code
        # for REFERENCE_FIRMWARE, swapped for the device's own entry once 0x57
        # names a version that has one. A file is read ONLY when a developer
        # passes one explicitly (--baseline, when validating new firmware), and
        # nothing ever writes one. None means no reference at all (tests).
        loaded = None
        self._builtin_reference = baseline is BUILTIN_BASELINE
        if self._builtin_reference:
            loaded = Fingerprint.builtin(REFERENCE_FIRMWARE)
        elif baseline and Path(baseline).exists():
            loaded = Fingerprint.load(baseline)
            self.log(f"loaded protocol baseline from {baseline} "
                     f"(fw {loaded.firmware})")
        self.validator = FrameValidator(
            baseline=loaded, strict=not lenient,
            # An attach triggers no control responses at all. A start
            # triggers 0x94/0x57 but no init - starting stopped sending it -
            # so the init trio's absence is not evidence of drift either.
            expected_absent=(INIT_RESPONSE_COMMANDS if sends_control
                             else CONTROL_RESPONSE_COMMANDS))

        self.t0 = None
        self.rows = 0
        # One entry (a timestamp) per LINK LOSS: a session that had connected
        # and then dropped. Reconnect ATTEMPTS are counted separately - a
        # 2-minute outage retries dozens of times, and counting those reported
        # 53 "gaps" for 2 real drops.
        self.gaps = []
        self.reconnect_attempts = 0
        # Set by session() once notifications are flowing; run_capture reads it
        # to tell a lost link from a reconnect attempt that never got one.
        self.link_up = False
        # True only while a session is connected and polling, and False the
        # moment it ends - unlike link_up, which stays set until the next
        # attempt begins. Front ends refuse Start, Stop and ALL STOP while it
        # is False, so no command can be queued for a link that is not there.
        self.link_ok = False
        # A plain-language warning while the charger looks stuck holding a
        # stale link (see run_capture), else None. Front ends display it.
        self.stale_link_warning: str | None = None
        # BLE address of the charger, set by run_capture. Keys the per-charger
        # firmware record in settings.ini.
        self.address: str | None = None
        # The charger's advertised name (e.g. "#Charger-1A2B") when this capture
        # found it by scanning; None when it was handed an address. A label for
        # the window's CONNECTED marker - never used to identify or select.
        self.device_name: str | None = None
        # Whether this session's firmware is verified: None until decided on
        # the first connection, then True/False. False means watching only,
        # with no 0x55 temperature polling.
        self.firmware_verified: bool | None = None
        self.drift_errors = 0
        # Frames whose HANDLING raised (see guarded_notify), and the distinct
        # errors already reported, so one bug is written up once, not per frame.
        self.frame_errors = 0
        self._frame_error_keys: set = set()
        self.device_info: DeviceInfo | None = None
        self.latest = {}        # only the slots this run is capturing
        self.latest_any = {}    # every slot that has answered - see on_notify
        # Last reading per slot while the run still had counters, i.e. the
        # result. Stopping zeroes elapsed and capacity, and the confirmation
        # polls afterward would otherwise overwrite the figures the summary
        # reports - claiming 0mAh for a run that really delivered 20.
        self.final_by_slot = {}
        self.ever_active = set()
        self.idle_streak = {s: 0 for s in self.slots}
        self.finished = set()
        # Slots whose device counters were zeroed underneath us while current
        # was still being reported - see the teardown note in on_notify. Latched,
        # because the condition itself latches frame after frame.
        self.torn_down = set()
        # Pre-flight IR sampling. While `measuring_ir` is non-empty the capture
        # is SUSPENDED: frames are still parsed (so the sampler can read them)
        # but nothing is recorded. Those arms are throwaway - letting them
        # reach the CSV would put phantom rows in the run's own record and feed
        # the teardown detector a counter reset per sample.
        self.measuring_ir: set[int] = set()
        self.ir_samples: dict[int, list[int]] = {}
        # The charger's OWN reason a slot stopped, from status-frame byte 20.
        # Recorded per slot, and reported when the slot is called FINISHED.
        self.alert_by_slot: dict[int, str] = {}
        # Per-cell battery temperature from opcode 0x55, polled on its own
        # slower cadence. None disables it entirely. The requested value is
        # kept apart, because unverified firmware switches polling off and a
        # later successful version read may switch it back on.
        self.temp_interval_s = temp_interval_s
        self.requested_temp_interval_s = temp_interval_s
        # Latest known reading per slot, for anything that wants a current
        # value (the GUI). Survives until replaced.
        self.temp_by_slot: dict[int, int] = {}
        # A reading waiting to be written. CONSUMED by the next CSV row for
        # that slot, which is what makes the column sparse. Cleared on
        # reconnect, because a value stranded across a gap would land on a row
        # minutes or hours after it was measured.
        self.pending_temp: dict[int, int] = {}
        # When each slot's latest reading arrived (time.monotonic), for its age
        # on screen and the blind-cutoff check. Monotonic rather than wall
        # time: a clock change must not make a reading look fresh or stale.
        self.temp_at: dict[int, float] = {}
        # The highest reading of each slot's CURRENT run - reset when a run is
        # started on the slot (begin_slot_run). Shown on a finished card and
        # recorded in meta.json.
        self.peak_temp_by_slot: dict[int, int] = {}
        # The slots whose run THIS capture started (a CLI --start, or a GUI
        # injection). The cutoff may stop only these; on any other it warns.
        # `started_by_us` below is the older whole-capture flag, kept because
        # stop-on-exit keys on it.
        self.started_slots: set[int] = set()
        self.started_by_us = False
        # Runs a PREVIOUS session of this app started that may still be going
        # when this one connects (the GUI's Retry): slot -> {"elapsed", "action",
        # "temp_cutoff_c"}. A slot's first live frame claims its entry only if
        # the device's counters carry on from where the old session last saw
        # them - the same test that tells a new run from a continuing one -
        # and the entry is dropped the moment the slot shows idle, so a panel
        # run that comes later is never mistaken for ours. See inherit_runs.
        self.inherited: dict[int, dict] = {}
        # Wall-clock time (time.time) each slot's last status frame arrived -
        # what a carried-over run's elapsed is checked against.
        self.seen_wall: dict[int, float] = {}
        # Once-per-capture setup, tracked by what has HAPPENED rather than by
        # which connection attempt this is. A session that fails to connect and
        # a later one that succeeds used to leave the setup - and the start
        # itself - unrun for good, with nothing said (seen twice on the bench).
        # `setup_done`: the attach announcement / firmware read / firmware
        # verdict have run. `start_attempted`: a start's configs are about to
        # go out, so nothing may send them again.
        self.setup_done = False
        self.start_attempted = False
        # The app-side temperature cutoff (see TEMP_CUTOFF_MIN_C): slot -> C.
        self.temp_cutoff_by_slot: dict[int, int] = {}
        # When each cutoff started counting toward "blind" (monotonic).
        self._cutoff_since: dict[int, float] = {}
        # slot -> the reading that met the cutoff, once it has. One trip per
        # run: a stopped slot has nothing more to stop.
        self.cutoff_trips: dict[int, int] = {}
        # Of those, the ones we stopped (ours) - the rest were warned about.
        self.cutoff_stopped: set[int] = set()
        # Slots whose current run a per-slot stop from this app ended (the
        # card's Stop) - so the card can say "Stopped by you" and keep the
        # run's last readings, as it does for a run the charger ended. Cleared
        # when the slot's next run opens.
        self.user_stopped: set[int] = set()
        # Stops the cutoff has asked for and the session has yet to send.
        # Filled on bleak's thread by on_notify, drained by the session.
        self.pending_cutoff_stops: set[int] = set()
        # slot -> [monotonic time of last send, sends so far]
        self._cutoff_stop_sends: dict[int, list] = {}
        # Slots whose cutoff is currently blind (no reading for too long).
        self.cutoff_blind: set[int] = set()
        # When each slot last answered a status poll (monotonic), any slot -
        # the in-session firmware read needs FRESH evidence that every slot
        # is idle, not the remembered answer from minutes ago.
        self.answered_at: dict[int, float] = {}
        # Starts sent and not yet seen to take: slot -> {"config", "sent_at",
        # "first_at", "tries"} (see START_CONFIRM_S, confirm_starts).
        self.start_pending: dict[int, dict] = {}
        # Slots whose start was declared failed. A front end that re-arms the
        # slot clears it; a new start (expect_start) clears it too.
        self.start_failed: set[int] = set()
        # Slots whose start the charger took and then ended AT ONCE with a
        # normal reason (a charge on a cell that is already full): done, not
        # failed. Cleared by a new start (expect_start).
        self.ended_at_once: set[int] = set()

        self._subscribers = []
        self.subscriber_errors = 0

    @classmethod
    def from_args(cls, args, log: Log | None = None) -> "Capture":
        """
        Build a Capture from parsed command-line arguments.

        All the CLI-shaped conversions live here - splitting "1,2" into slots,
        mapping a chemistry name onto the enum, parsing the --cells spec - so
        the constructor itself deals only in typed values.
        """
        slots = [int(s) for s in args.slots.split(",")]
        return cls(
            name=args.name,
            slots=slots,
            chemistry=CHEMISTRY_BY_NAME[args.chemistry.lower()],
            outdir=args.outdir if args.outdir else default_runs_dir(),
            # An attach records every watched slot from its first frame, idle
            # or not (the idle-voltage findings came from exactly that). A
            # --start records the runs it starts, and nothing else.
            record_idle=not args.start,
            external_runs=not args.start,
            # --name stays findable in the folder name.
            folder_label=args.name,
            baseline=args.baseline if args.baseline else BUILTIN_BASELINE,
            lenient=args.lenient,
            done_after=args.done_after,
            cells=parse_cells(getattr(args, "cells", None), slots),
            temp_interval_s=(getattr(args, "temp_interval", None)
                             if getattr(args, "temps", True) else None),
            run_config=RunConfig(
                mode=args.start or "attach",
                current_ma=args.current,
                capacity_mah=args.capacity,
                safety_timer_min=args.safety_timer,
                cutoff_mv=args.cutoff_mv,
                target_mv=args.target_mv,
                storage_mv=getattr(args, "storage_mv", None),
                charge_cutoff_current_ma=getattr(args, "charge_cutoff_current", None),
                discharge_cutoff_current_ma=getattr(args, "discharge_cutoff_current", None),
                temp_cutoff_c=getattr(args, "temp_cutoff", None),
            ),
            # Only a run we start sends control packets at all.
            sends_control=bool(args.start),
            log=log,
        )

    # ---------- sample subscribers ----------

    def subscribe(self, fn):
        """
        Register a callback to receive every Sample this capture accepts.

        `fn` is called with one Sample, in arrival order, once that sample is
        already written to its run's CSV and flushed to disk (a slot with no
        run open has no CSV, and its samples still arrive). Subscribers are optional
        observers - the CSV is the product of a run and is written whether
        anything is listening or not.

        Two constraints on `fn`:
          * it must be fast and must not block. on_notify runs inside bleak's
            BLE event loop, so a slow subscriber stalls frame reception for the
            entire capture.
          * it must not mutate the Sample; every subscriber gets the same one.

        Subscribers see only accepted samples - every one that becomes a CSV
        row, plus an idle slot's samples before any run is open on it.
        Unparseable frames and frames for slots we did not ask for never
        arrive here.
        """
        self._subscribers.append(fn)

    def _emit(self, sample: Sample):
        """
        Hand a sample to every subscriber.

        A subscriber that raises is logged and skipped: a broken consumer must
        not be able to kill a six-hour run. Note the deliberate asymmetry with
        the CSV write, which is NOT protected this way - losing the data is a
        real failure, losing a plot update is not.
        """
        for fn in self._subscribers:
            try:
                fn(sample)
            except Exception as e:
                self.subscriber_errors += 1
                self.log(f"  subscriber {getattr(fn, '__name__', fn)!r} raised: {e!r}")

    # ---------- data handling ----------

    def guarded_notify(self, sender, data: bytearray):
        """What the BLE stack is handed as its notification callback. bleak runs
        it as an asyncio callback, where an exception is dropped silently: no
        crash log, no message, the cards just stop updating (a CSV write that
        failed even in the fallback folder looked exactly like that). So the
        error is caught here, written to the crash log and the session log once
        per distinct error, and the capture carries on with the next frame."""
        try:
            self.on_notify(sender, data)
        except Exception as e:                      # noqa: BLE001
            self.frame_failed(e)

    def frame_failed(self, e: Exception) -> None:
        self.frame_errors += 1
        key = (type(e).__name__, str(e)[:120])
        if key not in self._frame_error_keys and len(self._frame_error_keys) < 20:
            self._frame_error_keys.add(key)
            where = ""
            try:
                import crashlog
                path = crashlog.record(type(e), e, e.__traceback__,
                                       "frame handler")
                where = f" Details: {path}" if path else ""
            except Exception:                       # noqa: BLE001
                pass
            self.log(f"!!! handling a charger frame failed: {e!r}. That frame "
                     f"was skipped and the capture carries on.{where}")
        elif self.frame_errors % 200 == 0:
            self.log(f"!!! {self.frame_errors} frames have failed to be "
                     f"handled so far (last: {e!r})")

    def on_notify(self, _sender, data: bytearray):
        raw = bytes(data)
        result = self.validator.check(raw)

        if result.severity == "error":
            self.drift_errors += 1
            for m in result.messages:
                self.log(f"  DRIFT/ERROR: {m}")

        if len(data) > 2 and data[2] == 0x57 and self.device_info is None:
            try:
                info = parse_info_response(bytes(data))
            except ValueError:
                pass
            else:
                self.device_info = info
                # The drift fingerprint finally gets its identity fields, so
                # the recorded fingerprint can say what it measured.
                self.validator.current.set_device_info(
                    info.firmware, info.hardware, info.product_code)
                # Validate against this version's own embedded reference when
                # it has one. An unknown version keeps the 2.02 reference, so
                # a differently-shaped frame is flagged rather than absorbed.
                if (self._builtin_reference
                        and info.firmware in FIRMWARE_FINGERPRINTS):
                    self.validator.baseline = Fingerprint.builtin(info.firmware)
                self.log(f"  device: product code {info.product_code}, "
                         f"firmware {info.firmware}, hardware {info.hardware}")
            return

        # 0x55 - per-cell battery temperature. A different frame family from
        # 0x91, so it never becomes a Sample: it is stashed here and the next
        # CSV row for that slot carries it.
        if len(raw) > 2 and raw[2] == 0x55:
            try:
                t = parse_temperature_response(raw)
            except ValueError:
                return
            # temperature_c is None unless the charger says the slot is
            # RUNNING. The firmware does not clear its transmit buffer, so a
            # non-running slot's byte 14 is whatever an earlier frame left
            # there. Never second-guess that guard.
            if t.temperature_c is not None:
                self._record_temperature(t.slot, t.temperature_c)
            return

        st = result.parsed
        if st is None:
            return

        # Every slot that answers is remembered here, including ones this run is
        # not capturing. Nothing downstream uses it - no CSV row, no Sample - but
        # stopping needs it: the only mechanism that stops a run is the init
        # sequence, which stops EVERY slot, so we must be able to see whether a
        # slot we did not start is busy before sending it.
        self.latest_any[st.slot] = st
        self.answered_at[st.slot] = time.monotonic()
        self.seen_wall[st.slot] = time.time()

        if st.slot not in self.slots:
            return

        # Suspended: the pre-flight sampler is arming and stopping this slot on
        # purpose. latest_any above is already updated, which is all the
        # sampler reads, so drop the frame before it can become a CSV row or
        # touch the finished/teardown bookkeeping.
        if st.slot in self.measuring_ir:
            return

        note = ""
        # A frame with current flowing but the device's counters zeroed means
        # the slot was torn down underneath us. The firmware's per-slot teardown
        # zeroes capacity, elapsed and resistance while voltage,
        # current and `state` keep echoing their last pre-teardown values - so
        # the reading is a phantom, not a measurement, and recording it as live
        # would inject it into any integration.
        #
        # It is also how a SAFETY-TIMER (TimeCut) termination looks from here,
        # and unlike a stop it is not one frame: measured on hardware, a
        # timer-ended slot sat at a dead-flat 4.138V / 502mA / active for five
        # minutes. So the condition is LATCHED once seen, and only the counters
        # advancing again (a genuinely new run on that slot) clears it. Without
        # the latch only the first frame is tagged and every one after it resets
        # the idle streak, so the run is never detected as finished at all.
        #
        # A normal termination - voltage cutoff, End_Cur taper - looks nothing
        # like this: current goes to 0 and `state` to `done` with the counters
        # intact, and the current-based path below handles it. Verified against
        # the tails of recorded voltage-cutoff and End_Cur runs.
        # The charger names the reason itself in byte 20, and that reason
        # SURVIVES the teardown - measured on hardware: the alert appeared in the very frame the counters reset and persisted
        # on every latched frame until an explicit stop cleared it. So a
        # standing alert is the device saying "this slot is done, and here is
        # why". A slot whose counters are advancing has no standing alert, so
        # a live frame clears any stale one.
        if st.alert_code:
            self.alert_by_slot[st.slot] = st.alert
        elif st.elapsed_seconds > 0:
            self.alert_by_slot.pop(st.slot, None)

        prev = self.latest.get(st.slot)
        if (st.current_ma > 0 and st.elapsed_seconds == 0
                and st.capacity_mah == 0):
            if prev is not None and prev.elapsed_seconds > 0:
                self.torn_down.add(st.slot)
            elif st.alert_code:
                # An ATTACH that connected after the teardown never saw the
                # counters advance, so it used to be unable to tell a phantom
                # from a live run and deliberately did not guess. With a
                # standing alert it no longer has to: the charger is stating
                # that the slot terminated. Only the alert lifts that
                # abstention - a phantom-shaped frame with no alert still does
                # not guess, exactly as before.
                self.torn_down.add(st.slot)
            if st.slot in self.torn_down:
                note = "transition_artifact"
        else:
            # Any frame that is NOT that shape means the phantom is over: the
            # counters advanced again (a genuinely new run) or the current went
            # to 0 (an explicit stop cleared the latch, which is the only thing
            # that does). Clearing only on the first of those left a stopped
            # slot marked torn-down forever - on the bench, slot 1
            # sat reading "latched" at 0mA long after its stop.
            self.torn_down.discard(st.slot)

        # An empty bay has no temperature. Its last reading belongs to a cell
        # that has been taken out, and a card that kept showing it read as a
        # live figure with nothing marking it old (seen on the bench: 36 C on
        # an empty S3). Cleared like the other readings; the run's own peak lives
        # on the SlotRun and its meta.json is unaffected.
        if st.state == "empty":
            self.temp_by_slot.pop(st.slot, None)
            self.temp_at.pop(st.slot, None)
            self.pending_temp.pop(st.slot, None)
            self.peak_temp_by_slot.pop(st.slot, None)

        # Which run this frame belongs to - possibly one this frame opens.
        run = self._run_for_frame(st, note)

        now = datetime.now()
        if self.t0 is None:
            self.t0 = now

        sample = Sample(
            wall_time=now,
            elapsed_s=(now - self.t0).total_seconds(),
            slot=st.slot,
            status=st,
            drift_severity=result.severity,
            note=note,
            # CONSUMED here, which is what makes the column sparse.
            slot_temp_c=self.pending_temp.pop(st.slot, None),
        )

        # CSV first. It is the point of the run, so a failure to write it is
        # never absorbed: from the CLI it surfaces as a crash, and in the public
        # app (fallback_dir set) the run moves to the local default folder and
        # says so loudly - see _write_row. Flushed per row: a crash costs the
        # last row, not the run.
        if run is not None:
            row = sample_to_row(sample)
            # elapsed_s in a run's own file counts from that run's first row,
            # so every file starts at 0; the Sample keeps session time for the
            # live chart, which overlays slots started at different moments.
            if run.rows == 0:
                run.t0 = now
            row["elapsed_s"] = round((now - run.t0).total_seconds(), 1)
            self._write_row(run, row)
            self.rows += 1
            if result.severity == "error":
                run.drift_errors += 1
            if st.elapsed_seconds > 0:
                run.final = st
                run.peak_elapsed_s = max(run.peak_elapsed_s, st.elapsed_seconds)
            if st.alert_code:
                run.alert = st.alert

        self.latest[st.slot] = st
        if st.elapsed_seconds > 0:
            self.final_by_slot[st.slot] = st
        if st.current_ma > 0 and not note:
            self.ever_active.add(st.slot)
            self.idle_streak[st.slot] = 0
            # A FINISHED slot drawing current with its counters advancing is
            # running again - a new run, from the GUI or the panel - so it is
            # no longer finished, and its end will be detected and reported
            # afresh. Without this a slot re-run in one session (the GUI's
            # normal use) stayed "finished" from its first run: its second end
            # was never logged, and all_done could fire mid-run (seen on the
            # bench). The counters test keeps both phantoms
            # out: a teardown latch has them at 0 (and is tagged, so `note`
            # already excludes it), and a normal end has current at 0.
            if st.slot in self.finished and st.elapsed_seconds > 0:
                self.finished.discard(st.slot)
            if run is not None and st.elapsed_seconds > 0:
                run.live_since_link = True
        elif st.slot in self.ever_active:
            self.idle_streak[st.slot] += 1
            if self.idle_streak[st.slot] >= self.done_after and st.slot not in self.finished:
                self.finished.add(st.slot)
                # Report from final_by_slot, not the current frame: a torn-down
                # slot reports 0mAh over 0s, which would misreport a real run.
                last = self.final_by_slot.get(st.slot, st)
                why = self.alert_by_slot.get(st.slot)
                if why:
                    # The charger's own words beat our inference.
                    how = f" - charger reports {why!r}"
                elif st.slot in self.torn_down:
                    how = " (counters torn down - safety timer or external stop)"
                else:
                    how = ""
                # Seen ended only after a link loss: the END was not watched,
                # whether or not the charger says why it ended. That is a fact
                # about this program's record, not about the charger - the
                # figures are the last readings received, so capacity and time
                # are a floor (on the bench, a run that timed out during a
                # PC sleep was recorded 'done' with 9 mAh / 70 s of a 180 s,
                # ~25 mAh run). end_reason says how the run ended FROM OUR SIDE;
                # `alert` keeps the charger's reason either way.
                unseen = (run is not None and not run.finished
                          and not run.live_since_link)
                if unseen:
                    how += (" - first seen ended after a link loss, so the "
                            "figures are the last readings received"
                            + ("" if why else " and why it ended is unknown"))
                self.log(f"slot {st.slot} appears FINISHED{how} "
                         f"({'last reading received: ' if unseen else ''}"
                         f"{last.voltage_v:.3f}V, device capacity "
                         f"{last.capacity_mah}mAh over {last.elapsed_seconds}s)")
                if run is not None and not run.finished:
                    run.finished = True
                    run.ended_unseen = unseen
                    if why:
                        run.alert = why
                    # Written now, so the run's record is complete even if
                    # this program never gets to close it cleanly.
                    self._write_meta(run)

        # Observers last, so they see durable data and fully updated capture
        # state (latest / ever_active / finished) rather than a half-applied view.
        self._emit(sample)

    def all_done(self) -> bool:
        return bool(self.ever_active) and self.finished >= set(self.ever_active)

    # ---------- temperature and the app-side cutoff ----------

    def _record_temperature(self, slot: int, temp_c: int):
        """A 0x55 reading from a RUNNING slot (the parser's guard has passed)."""
        self.temp_by_slot[slot] = temp_c
        self.pending_temp[slot] = temp_c
        self.temp_at[slot] = time.monotonic()
        if temp_c > self.peak_temp_by_slot.get(slot, -1000):
            self.peak_temp_by_slot[slot] = temp_c
        # The run's own peak. 0x55 only reports a RUNNING slot, so a reading
        # while the slot's run has already finished belongs to the next run,
        # which is opening; _open_external_run credits it there.
        run = self.open_runs.get(slot)
        if run is not None and not run.finished:
            if run.peak_temp_c is None or temp_c > run.peak_temp_c:
                run.peak_temp_c = temp_c
        self._check_cutoff(slot, temp_c)

    def set_temp_cutoff(self, slot: int, limit_c: int | None):
        """Arm (or clear, with None) the app-side cutoff for one slot. Validate
        with temp_cutoff_problem first; this only records it."""
        run = self.open_runs.get(slot)
        if run is not None:
            run.temp_cutoff_c = limit_c
        if limit_c is None:
            self.temp_cutoff_by_slot.pop(slot, None)
            self._cutoff_since.pop(slot, None)
            self.cutoff_blind.discard(slot)
            return
        self.temp_cutoff_by_slot[slot] = limit_c
        self._cutoff_since[slot] = time.monotonic()

    def expect_start(self, slot: int, config: bytes):
        """A start config has just been written to `slot`: watch for it to
        take (confirm_starts). Kept so the identical packet can be re-sent."""
        now = time.monotonic()
        self.start_failed.discard(slot)
        self.ended_at_once.discard(slot)
        # The alert standing BEFORE this start, so confirm_starts can tell the
        # charger's answer to this start from a leftover from an earlier run.
        self.start_pending[slot] = {"config": bytes(config), "sent_at": now,
                                    "first_at": now, "tries": 1,
                                    "alert_before": self.alert_by_slot.get(slot)}

    def begin_slot_run(self, slot: int, request: RunRequest | None = None, *,
                       cell_id: str | None = None,
                       group: RunGroup | None = None,
                       temp_cutoff_c: int | None = None) -> SlotRun:
        """
        This capture has just started a run on `slot`: open the run's own
        folder (named for the cell and the action) and reset the per-run
        temperature state - the last run's reading, its peak and any cutoff
        trip describe a run that is over - then arm the new run's cutoff.

        `request` is the full config the run was started with, recorded in its
        meta.json; its temp_cutoff_c is the cutoff. `temp_cutoff_c` is only for
        callers with no request (tests). `cell_id` overrides the capture's cell
        for this slot (the GUI's Configure can change it between runs).
        """
        self.started_slots.add(slot)
        self.started_by_us = True
        self.temp_by_slot.pop(slot, None)
        self.temp_at.pop(slot, None)
        self.pending_temp.pop(slot, None)
        self._reset_run_temperature(slot)
        if cell_id is not None:
            self.cells[slot] = cell_id
        if request is not None:
            action = request.mode.name.lower()
            temp_cutoff_c = request.temp_cutoff_c
        elif self.run_config.mode in ACTIONS:
            action = self.run_config.mode
        else:
            action = "external"
        run = self._open_run(slot, action, ours=True, request=request,
                             group=group)
        # The pre-flight IR samples were taken for exactly this run.
        run.ir_samples = self.ir_samples.get(slot)
        self.set_temp_cutoff(slot, temp_cutoff_c)
        self._write_meta(run)
        return run

    def _reset_run_temperature(self, slot: int):
        """Forget the per-run temperature figures of the slot's last run."""
        self.peak_temp_by_slot.pop(slot, None)
        self.cutoff_trips.pop(slot, None)
        self.cutoff_stopped.discard(slot)
        self.pending_cutoff_stops.discard(slot)
        self._cutoff_stop_sends.pop(slot, None)

    def mark_cutoff_stopped(self, slot: int):
        """The temperature cutoff's stop went to `slot` - recorded for the
        session and on the run itself, whose meta.json says it ended so."""
        self.cutoff_stopped.add(slot)
        run = self.open_runs.get(slot)
        if run is not None:
            run.cutoff_stopped = True

    def link_restored(self):
        """A reconnect: every run still going must be seen live again before
        its end can be taken as watched (see SlotRun.live_since_link)."""
        for run in self.open_runs.values():
            if not run.finished:
                run.live_since_link = False

    def unwatched_end(self, slot: int) -> bool:
        """This slot's run ended while the link was down (first seen finished
        after a reconnect), so its figures are the last readings RECEIVED and
        capacity and time are a floor. What the GUI card and the final summary
        ask; end_reason in meta.json says the same as `ended_while_disconnected`."""
        run = self.open_runs.get(slot)
        if run is None:
            run = next((r for r in reversed(self.runs) if r.slot == slot), None)
        return bool(run is not None and run.ended_unseen)

    def note_stop_sent(self, slot: int):
        """A per-slot stop went to `slot`: its run ended by a stop, not on its
        own, which its meta.json must say (analysis treats the two apart)."""
        run = self.open_runs.get(slot)
        if run is not None and not run.finished:
            run.stop_sent = True
            self.user_stopped.add(slot)

    # ---------- runs and their folders ----------

    def _run_for_frame(self, st: SlotStatus, note: str) -> SlotRun | None:
        """
        The run a frame from `st.slot` belongs to, opening one if the frame
        shows a run this capture has no folder for.

        A NEW run is recognized by current flowing with the device's counters
        advancing (not a teardown phantom) on a slot with no run open, or
        whose run has finished AND whose elapsed counter went backward - a
        genuinely new run restarts it, while a multi-phase panel operation
        (a break-in's discharge after its rest) carries it on, and so stays in
        one folder.
        """
        run = self.open_runs.get(st.slot)
        live = st.current_ma > 0 and st.elapsed_seconds > 0 and not note
        if st.slot in self.inherited and _reports_idle(st):
            # Idle before it was ever claimed: the old run is over, and
            # anything that starts here later is somebody else's.
            del self.inherited[st.slot]
        if live and self.external_runs:
            if run is None:
                return (self._open_inherited_run(st)
                        or self._open_external_run(st.slot))
            if run.finished:
                last = run.peak_elapsed_s or None
                if last is None or st.elapsed_seconds < last:
                    return self._open_external_run(st.slot)
        if run is None and self.record_idle:
            return self._open_external_run(st.slot)
        return run

    def carry_forward(self) -> dict[int, dict]:
        """What a NEW session must know to go on owning the runs this one
        started and left going (the GUI's Retry): for each slot this capture
        started that was last seen with current flowing and counters advancing,
        what it last showed (device elapsed, when, device mode), the action,
        the config it was started with and its temperature cutoff. A slot last
        seen idle, torn down or finished carries nothing.

        Runs this session INHERITED but never saw again are carried on
        unchanged, so a second Retry (the charger was not found the first
        time) keeps them; their original last-seen time is what the claim is
        later measured against."""
        out = {}
        for slot in sorted(self.started_slots):
            st = self.latest_any.get(slot)
            run = self.open_runs.get(slot)
            seen = self.seen_wall.get(slot)
            if (st is None or seen is None or st.current_ma <= 0
                    or st.elapsed_seconds <= 0
                    or slot in self.torn_down or slot in self.finished
                    or (run is not None and run.finished)):
                continue
            out[slot] = {"elapsed": st.elapsed_seconds, "seen_wall": seen,
                         "mode_code": st.mode_code,
                         "action": run.action if run is not None else "external",
                         "request": run.request if run is not None else None,
                         "temp_cutoff_c": self.temp_cutoff_by_slot.get(slot)}
        for slot, inh in self.inherited.items():
            if slot not in out and slot not in self.started_slots:
                out[slot] = dict(inh)
        return out

    def inherit_runs(self, carried: dict[int, dict]) -> None:
        """Take over runs an earlier session started (see carry_forward)."""
        self.inherited = {s: dict(v) for s, v in (carried or {}).items()}

    def _open_inherited_run(self, st: SlotStatus) -> SlotRun | None:
        """A live frame on a slot an earlier session started. It is the same
        run - ours, with its temperature cutoff and its config - only if the
        device's elapsed matches where that run would be by now (the elapsed it
        last showed plus the wall-clock time since, within CARRY_MARGIN_S) and
        the device reports the same mode. Anything else is not ours (None: the
        caller opens it as external, the fail-safe)."""
        slot = st.slot
        inh = self.inherited.pop(slot, None)
        if inh is None:
            return None
        expected = inh["elapsed"] + (time.time() - inh["seen_wall"])
        off = st.elapsed_seconds - expected
        same_mode = (inh.get("mode_code") is not None
                     and st.mode_code == inh["mode_code"])
        if abs(off) > CARRY_MARGIN_S or not same_mode:
            self.log(f"slot {slot}: running, but not the run the previous "
                     f"session started (elapsed {st.elapsed_seconds}s, "
                     f"expected about {round(expected)}s; mode "
                     f"{st.mode_code}, expected {inh.get('mode_code')}); "
                     f"treating it as a run started elsewhere")
            return None
        self.started_slots.add(slot)
        self.started_by_us = True
        self._reset_run_temperature(slot)
        run = self._open_run(slot, inh["action"], ours=True,
                             request=inh.get("request"))
        run.temp_cutoff_c = inh.get("temp_cutoff_c")
        self.set_temp_cutoff(slot, inh.get("temp_cutoff_c"))
        self._write_meta(run)
        self.log(f"slot {slot}: still running from the previous session; it "
                 f"stays ours (temperature cutoff kept, closing stops it)")
        return run

    def _open_external_run(self, slot: int) -> SlotRun:
        # Something this capture did not start. It is not ours to stop - not by
        # the temperature cutoff, not on exit - even if an earlier run on this
        # slot was.
        self.started_slots.discard(slot)
        pending = self.pending_temp.get(slot)
        self._reset_run_temperature(slot)
        if pending is not None:
            self.peak_temp_by_slot[slot] = pending
        run = self._open_run(slot, "external")
        run.temp_cutoff_c = self.temp_cutoff_by_slot.get(slot)
        run.peak_temp_c = pending
        self._write_meta(run)
        return run

    def _open_run(self, slot: int, action: str, *, ours: bool = False,
                  request: RunRequest | None = None,
                  group: RunGroup | None = None) -> SlotRun:
        """Close the slot's previous run (if any) and open a new one in the
        logs folder - or, when that cannot be written, the fallback folder."""
        self.user_stopped.discard(slot)
        prev = self.open_runs.get(slot)
        if prev is not None:
            self._close_run(prev)
        started = group.started if group else datetime.now()
        cell = self.cells.get(slot)
        run = SlotRun(slot=slot, action=action, cell_id=cell, started=started,
                      name=run_folder_name(started, slot, cell, action,
                                           self.folder_label),
                      ours=ours, request=request, group=group,
                      gaps_from=len(self.gaps),
                      attempts_from=self.reconnect_attempts)
        # Read afresh per run: a front end that changed the logs folder changes
        # it from the next run on.
        target = self.outdir
        try:
            self._create_run_files(run, target)
        except OSError as e:
            if not self._can_fall_back(target):
                raise
            self._relocate(run, e, failed=target)
        self.runs.append(run)
        self.open_runs[slot] = run
        self.log(f"  slot {slot}: recording to {run.dir}")
        return run

    def _create_run_files(self, run: SlotRun, parent: Path):
        parent.mkdir(parents=True, exist_ok=True)
        run_dir = unique_run_dir(parent, run.name)
        run_dir.mkdir()
        fh = open(run_dir / run_csv_name(run_dir), "w", newline="",
                  encoding="utf-8")
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        fh.flush()
        run.dir, run.fh, run.writer = run_dir, fh, writer

    def _can_fall_back(self, failed: Path | None) -> bool:
        if self.fallback_dir is None or failed is None:
            return False
        try:
            return self.fallback_dir.resolve() != Path(failed).resolve()
        except OSError:
            return True

    def _relocate(self, run: SlotRun, error: Exception, failed: Path):
        """
        Writing this run's files failed: carry the run on in the fallback
        folder, and say so loudly - on the card (run.warning), in the log and
        in meta.json. The public app's amendment to "CSV failures crash": a
        flash drive pulled or a share gone mid-run costs the rest of the run
        nothing. A failure in the fallback too is left to raise.
        """
        old_dir = run.dir
        if run.fh is not None:
            try:
                run.fh.close()
            except OSError:
                pass
        self._create_run_files(run, self.fallback_dir)
        run.relocations.append({
            "from": str(old_dir or failed),
            "to": str(run.dir),
            "at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(error).__name__}: {error}",
        })
        where = old_dir or failed
        run.warning = (f"Could not write to {where} ({error}). This run "
                       f"continues in {run.dir}"
                       + ("; rows recorded before the failure stay in the old "
                          "folder" if old_dir and run.rows else "") + ".")
        self.log(f"!!! slot {run.slot}: {run.warning}")

    def _write_row(self, run: SlotRun, row: dict):
        try:
            run.writer.writerow(row)
            run.fh.flush()
        except OSError as e:
            failed = run.dir.parent if run.dir else self.outdir
            if run.relocations or not self._can_fall_back(failed):
                raise
            self._relocate(run, e, failed=failed)
            run.writer.writerow(row)
            run.fh.flush()
        run.rows += 1

    def _end_reason(self, run: SlotRun, session_reason: str | None) -> str | None:
        """Why this run ended, as its meta.json records it. The charger ending
        it is "done"; a stop - ours, or the temperature cutoff's - is not,
        whatever the session later ended as, so analysis never reads a
        stopped run's capacity as a full measurement.

        Two values exist so a reader is not misled:
          ended_while_disconnected  first seen ended on the frames after a
              reconnect, with no reason from the charger - a power cut and a
              completed run look identical from here
          recording_ended  a run still going when the session ended - one this
              app did not start, or one the session-length cap cut off: only
              the RECORDING stopped, not the run
        Readers treat anything but "done"/"duration" as incomplete, so both
        land on the safe side of analyze.py and dblog.py unchanged. A
        "duration" session never labels an unfinished run "duration": that
        reads as a complete capture, and the run was not complete."""
        if run.cutoff_stopped:
            return "temp_cutoff"
        if run.stop_sent:
            return "stopped"
        if run.finished:
            return "ended_while_disconnected" if run.ended_unseen else "done"
        if session_reason == "duration":
            return "recording_ended"
        if not run.ours and session_reason not in ("replaced", "drift"):
            return "recording_ended"
        return session_reason

    def _close_run(self, run: SlotRun, session_reason: str | None = "replaced"):
        """A new run is opening on this slot (or the capture is ending): write
        the old one's final meta.json and close its file."""
        run.gaps_to = len(self.gaps)
        run.attempts_to = self.reconnect_attempts
        run.end_reason = self._end_reason(run, session_reason)
        self._write_meta(run)
        if run.fh is not None:
            run.fh.close()
        run.closed = True
        if self.open_runs.get(run.slot) is run:
            del self.open_runs[run.slot]

    def _check_cutoff(self, slot: int, temp_c: int):
        """
        Trip on the FIRST reading at or above the slot's cutoff. Readings are
        whole degrees and a spurious value has never been seen, and a false
        trip costs a restart, not a cell - so no confirmation reading.

        Only a run this capture STARTED is stopped. Any other gets a warning
        and nothing else: never disturb a slot we did not start.
        """
        limit = self.temp_cutoff_by_slot.get(slot)
        if limit is None or slot in self.cutoff_trips or temp_c < limit:
            return
        self.cutoff_trips[slot] = temp_c
        run = self.open_runs.get(slot)
        if run is not None:
            run.cutoff_trip_c = temp_c
        if slot in self.started_slots:
            self.pending_cutoff_stops.add(slot)
            self.log(f"TEMPERATURE CUTOFF: slot {slot} read {temp_c} C, at or "
                     f"above your {limit} C cutoff - stopping slot {slot}")
        else:
            self.log(f"TEMPERATURE CUTOFF: slot {slot} read {temp_c} C, at or "
                     f"above your {limit} C cutoff - NOT stopping it: this "
                     f"run was not started here, and a slot we did not start "
                     f"is never disturbed. Stop it at the charger if needed.")

    def slot_running(self, slot: int) -> bool:
        """Current is genuinely flowing on `slot` - not a teardown phantom."""
        st = self.latest.get(slot)
        return bool(st and st.current_ma > 0 and slot not in self.torn_down)

    def update_cutoff_blind(self, now: float | None = None) -> None:
        """
        Re-evaluate which cutoffs are blind: a running slot, cutoff set and not
        yet tripped, and no reading for CUTOFF_BLIND_AFTER_S - counted from the
        latest reading, or from when the cutoff was armed if there has been
        none. Logs each change of state once. `now` is time.monotonic() unless
        a test supplies one.
        """
        now = time.monotonic() if now is None else now
        for slot, limit in self.temp_cutoff_by_slot.items():
            last = max(self.temp_at.get(slot, float("-inf")),
                       self._cutoff_since.get(slot, now))
            blind = (slot not in self.cutoff_trips
                     and self.slot_running(slot)
                     and now - last >= CUTOFF_BLIND_AFTER_S)
            if blind and slot not in self.cutoff_blind:
                self.cutoff_blind.add(slot)
                self.log(f"CUTOFF BLIND: no temperature reading from slot "
                         f"{slot} for {int(now - last) // 60} min - its "
                         f"{limit} C cutoff cannot trip until one arrives")
            elif not blind and slot in self.cutoff_blind:
                self.cutoff_blind.discard(slot)
                self.log(f"  slot {slot}: temperature readings are back; "
                         f"its cutoff is watching again")

    def temperature_note(self) -> str | None:
        """
        Why temperature is NOT being read, in plain words - or None when it is
        (or when that is not decided yet). Front ends show this rather than a
        silent blank.
        """
        if self.temp_interval_s is not None:
            return None
        if self.requested_temp_interval_s is None:
            return "temperature polling is switched off"
        if self.firmware_verified is None:
            return None             # still connecting; not decided
        fw = self.device_info.firmware if self.device_info else None
        if fw is None:
            return ("temperature available once the firmware is checked - "
                    "the version is read only while every slot is idle")
        return (f"no temperature: firmware {fw} is not verified "
                f"(supported: {', '.join(sorted(SUPPORTED_FIRMWARE))})")

    # ---------- lifecycle ----------

    def _run_meta(self, run: SlotRun) -> dict:
        """
        One run's meta.json: everything about THAT run and nothing about the
        slot's run before or after it. Per-slot values keep the shapes older
        runs used ({"<slot>": value}), so readers need not tell them apart.
        """
        req = run.request
        rc = self.run_config
        s = str(run.slot)
        gaps = self.gaps[run.gaps_from:run.gaps_to]
        attempts_to = (run.attempts_to if run.attempts_to is not None
                       else self.reconnect_attempts)

        def cfg(name):
            # A run we started records what IT was sent; an external one has
            # only the capture's own options (all empty for an attach).
            return getattr(req, name) if req is not None else getattr(rc, name)

        return {
            # The session label (the CLI's --name); the run's own identity is
            # its folder.
            "name": self.name,
            # Which version of this program recorded the run.
            "app_version": APP_VERSION,
            "folder": run.dir.name if run.dir else run.name,
            "created": run.started.isoformat(timespec="seconds"),
            "slot": run.slot,
            "cell": run.cell_id,
            "action": run.action,
            # Kept in the multi-slot shapes older runs used, which analyze.py
            # and dblog.py read. JSON keys are strings; slots are ints elsewhere.
            "slots": [run.slot],
            "cells": {s: run.cell_id},
            # The typed name, not the enum member name - see CHEMISTRY_NAME.
            "chemistry": CHEMISTRY_NAME[req.chemistry if req is not None
                                        else self.chemistry],
            "group": run.group.to_meta() if run.group else None,
            # Which physical device produced this run. None when the session
            # never read it (0x57 goes only to a wholly idle charger).
            "device": ({k: getattr(self.device_info, k, None)
                        for k in ("product_code", "firmware", "hardware")}
                       if self.device_info else None),
            "mode": req.mode.name.lower() if req is not None else rc.mode,
            "current_ma": cfg("current_ma"),
            "capacity_mah": cfg("capacity_mah"),
            "safety_timer_min": cfg("safety_timer_min"),
            "cutoff_mv": cfg("cutoff_mv"),
            "target_mv": cfg("target_mv"),
            "storage_mv": cfg("storage_mv"),
            "charge_cutoff_current_ma": cfg("charge_cutoff_current_ma"),
            "discharge_cutoff_current_ma": cfg("discharge_cutoff_current_ma"),
            # Added with the Advanced section; absent from older runs, null
            # when the charger's own default was used.
            "delta_v_mv": cfg("delta_v_mv"),
            "trickle_ma": cfg("trickle_ma"),
            "keep_mv": cfg("keep_mv"),
            # The FULL config this run was started with - every field, never a
            # reference to something that can change later. None for a run
            # this program did not start.
            "config": request_snapshot(req),
            # [mOhm, ...] from the pre-flight, so the run's own latched IR can
            # be judged against something.
            "ir_samples": {s: run.ir_samples} if run.ir_samples else {},
            # Whether 0x55 was polled for slot temperature, and how often. null
            # means it was not, so a run with no slot_temp_c values can be told
            # from one where the feature was simply off.
            "temp_interval_s": self.temp_interval_s,
            # The highest slot temperature in THIS run's file.
            "peak_slot_temp_c": ({s: run.peak_temp_c}
                                 if run.peak_temp_c is not None else {}),
            # The app-side cutoff: what was set, and what it did. A trip is
            # {"temp_c": the reading that met it, "action": "stopped" for a
            # run we started, "warned" for one we did not}.
            "temp_cutoff_c": ({s: run.temp_cutoff_c}
                              if run.temp_cutoff_c is not None else {}),
            "temp_cutoff_trips": (
                {s: {"temp_c": run.cutoff_trip_c,
                     "action": "stopped" if run.cutoff_stopped else "warned"}}
                if run.cutoff_trip_c is not None else {}),
            "samples": run.rows,
            # Link losses while this run was being recorded, one timestamp
            # each. Older runs listed every reconnect ATTEMPT here instead;
            # zero meant zero either way.
            "gaps": gaps,
            "reconnect_attempts": attempts_to - run.attempts_from,
            # False = watched on firmware not in SUPPORTED_FIRMWARE (no 0x55);
            # null = never decided (the session never connected).
            "firmware_verified": self.firmware_verified,
            # Rows of THIS run flagged as drift errors.
            "drift_errors": run.drift_errors,
            "finished": run.finished,
            # The charger's own last reason for stopping (status byte 20).
            "alert": run.alert,
            "final": ({"voltage_v": run.final.voltage_v,
                       "capacity_mah": run.final.capacity_mah,
                       "elapsed_s": run.final.elapsed_seconds,
                       "resistance_mohm": run.final.resistance_mohm}
                      if run.final else None),
            # Each time writing failed and the run moved to the local default.
            "relocated": run.relocations,
            "session": {
                "name": self.name,
                "started": self.started_at.isoformat(timespec="seconds"),
                "log": str(self.session_log) if self.session_log else None,
                "drift_errors": self.drift_errors,
            },
            "protocol_fingerprint": asdict(self.validator.current),
            "end_reason": run.end_reason,
        }

    def _write_meta(self, run: SlotRun, extra: dict | None = None):
        for attempt in (1, 2):
            meta = self._run_meta(run)
            if extra:
                meta.update(extra)
            try:
                with open(run.meta_path, "w", encoding="utf-8") as fh:
                    json.dump(meta, fh, indent=2)
                return
            except OSError as e:
                failed = run.dir.parent if run.dir else self.outdir
                if (attempt == 2 or run.closed or run.relocations
                        or not self._can_fall_back(failed)):
                    raise
                self._relocate(run, e, failed=failed)

    def save_meta(self, extra: dict | None = None):
        """
        Write meta.json for every run still open. `extra["end_reason"]` is the
        SESSION's end reason: each open run records its own - "done" if the
        charger ended it, "stopped"/"temp_cutoff" if a stop did, otherwise the
        session's. Runs already closed were written when they closed.
        """
        extra = dict(extra or {})
        session_reason = extra.pop("end_reason", None)
        for run in list(self.open_runs.values()):
            if session_reason is not None:
                run.end_reason = self._end_reason(run, session_reason)
            self._write_meta(run, extra)

    def close(self):
        """Close every open run's CSV. The runs stay the slots' current ones,
        so save_meta still describes them afterward."""
        for run in self.open_runs.values():
            if run.fh is not None:
                try:
                    run.fh.close()
                except OSError:
                    pass
                run.fh = None

    def run_dirs(self) -> list[Path]:
        """Every run folder this capture wrote, in the order they opened."""
        return [r.dir for r in self.runs if r.dir]

    def warning_for(self, slot: int) -> str | None:
        """The file warning for a slot's current run, if any (for its card)."""
        run = self.open_runs.get(slot)
        return run.warning if run else None


ALL_SLOTS = (1, 2, 3, 4)


def any_slot_busy(latest_any: dict) -> bool:
    """
    Whether ANY slot that answered is drawing current.

    The question 0x57 has to pass: it is verified harmless on idle slots and
    nowhere else, so it is only ever aimed at a wholly idle charger.
    """
    return any(st.current_ma > 0 for st in latest_any.values())


def busy_beyond_latch(latest_any: dict) -> bool:
    """
    Whether any slot that answered is drawing current, NOT counting a slot the
    charger has ended and is only echoing: run status 0 with a standing alert
    (the teardown latch - a timer-ended slot keeps its last current for as
    long as nobody clears it; measured on hardware, repeatedly).

    Used ONLY by the mid-session firmware read. Without it an attach beside a
    panel run that ended on its TIMER never learned the firmware, so
    temperature stayed off for the rest of the session. The device has stopped that slot itself, so 0x57 is aimed at
    a charger with nothing running. The start path keeps any_slot_busy.
    """
    return any(st.current_ma > 0
               and not (getattr(st, "device_status", None) == 0
                        and getattr(st, "alert_code", None))
               for st in latest_any.values())


def foreign_busy(our_slots, latest_any: dict) -> list[int]:
    """
    Slots drawing current that this run did not start.

    Pure so the all-slot stop's guard can be tested offline. `latest_any` is
    Capture.latest_any, which deliberately remembers every slot that answered,
    including ones this run is not capturing - it exists for exactly this.
    """
    return [s for s in ALL_SLOTS
            if s not in our_slots
            and s in latest_any
            and latest_any[s].current_ma > 0]


def _reports_idle(st) -> bool:
    """The charger says this slot is idle: run status 0 AND no current. A slot
    never heard from, or a frame without the status byte, is NOT idle."""
    return (st is not None and st.current_ma <= 0
            and getattr(st, "device_status", None) == 0)


async def stop_our_slots(client, char, cap, log) -> bool:
    """
    Stop the slots this run started, and nothing else.

    Sends the per-slot stop - 0xFE addressed to one slot's bitmask - to each
    slot we started. Measured (the probe_slot_stop probe): with two
    slots both discharging, 0xFE 0x01 took slot 1 to 0 mA while slot 2 kept
    running at 500 mA. It needs no init and cannot reach a slot it is not
    addressed to, so unlike the all-slot teardown it never has to decline.

    Falls back to stop_all_guarded only if a slot we started is STILL drawing
    current afterward - a real failure, where the blunt instrument is worth
    reaching for and its own guard then decides whether it may fire.

    WHICH slots: the ones this capture STARTED (Capture.started_slots), not
    every slot it monitors. In the CLI those are the same set, but a GUI
    watching four slots that started a run on slot 3 used to send 0xFE to all
    four on exit - stopping a front-panel run on slot 1 it never started.
    A capture without started_slots (older callers, test doubles) falls back to
    cap.slots, the previous behavior.

    Of those, a slot the charger itself reports IDLE (status byte 0, 0 mA) is
    skipped: it has nothing to stop. Zero current alone is NOT enough - a slot
    started seconds ago reads 0 mA while the charger measures its IR, and
    skipping it would leave that run going unrecorded after we exit.

    Returns True if every slot we started went idle.
    """
    started = getattr(cap, "started_slots", None)
    ours = [s for s in cap.slots if started is None or s in started]
    targets = [s for s in ours if not _reports_idle(cap.latest_any.get(s))]
    skipped = sorted(set(ours) - set(targets))
    if skipped:
        log(f"  slot(s) {skipped} already idle (charger reports status 0); "
            f"nothing to stop")
    for s in targets:
        await client.write_gatt_char(char, build_slot_stop(s), response=False)
        # Recorded as a stop: without this the run was labeled "done" once the
        # slot went idle, and analysis read its capacity as a full measurement.
        note = getattr(cap, "note_stop_sent", None)   # test doubles lack it
        if note is not None:
            note(s)
        log(f"  per-slot stop sent to slot {s} (0xFE, its bitmask alone)")
        await asyncio.sleep(0.4)
    if not targets:
        return True
    await asyncio.sleep(1.5)

    for s in targets:
        await client.write_gatt_char(char, build_status_query(s), response=False)
        await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)

    still = [s for s in targets
             if s in cap.latest_any and cap.latest_any[s].current_ma > 0]
    if not still:
        log("stopped; slots idle (no other slot was touched)")
        return True

    log(f"per-slot stop did not take on slot(s) {still}; falling back to the "
        f"all-slot init teardown, which may decline")
    return await stop_all_guarded(client, char, cap, log)


async def stop_all_guarded(client, char, cap, log) -> bool:
    """
    Stop the runs we started - unless that would disturb one we did not.

    The ONLY thing measured to stop a run on FW 2.02 is the init sequence.
    0x93 does nothing, with any channel value: tested against a live 500mA
    discharge with channel 0x00 and with the slot's own bitmask, and both were
    indistinguishable from sending nothing at all (the probe_stop probe).

    Init is all-or-nothing - it stops every slot - so this cannot be selective.
    It can only decline. Every slot is polled first, including ones this run is
    not capturing, and if any slot we did not start is drawing current we stop
    NOTHING and say so. Our own run then ends on the charger's safety timer,
    which is a far better outcome than silently killing a test someone set up by
    hand.

    Because stopping IS init, it no longer matters whether init was sent earlier
    in this session, so this works after a reconnect too.

    Returns True if the stop was sent.
    """
    for s in ALL_SLOTS:
        await client.write_gatt_char(char, build_status_query(s), response=False)
        await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)

    # "Ours" means STARTED by this capture, not merely monitored: a GUI
    # watching a panel run on slot 1 must count that run as foreign, or init
    # would kill it. Falls back to cap.slots where started_slots is absent.
    started = getattr(cap, "started_slots", None)
    ours = [s for s in cap.slots if started is None or s in started]
    foreign = foreign_busy(ours, cap.latest_any)

    if foreign:
        log(f"NOT stopping: slot(s) {foreign} are running and we did not start "
            f"them. The only working stop on this firmware halts every slot, so "
            f"stopping ours would kill theirs too.")
        log(f"  slot(s) {ours} are left running; the charger's "
            f"{cap.run_config.safety_timer_min}-minute safety timer will end "
            f"them.")
        return False

    log("stopping (init sequence - the only mechanism that works on this "
        "firmware; 0x93 does nothing)")
    for pkt in init_sequence():
        await client.write_gatt_char(char, pkt, response=False)
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)

    for s in cap.slots:
        await client.write_gatt_char(char, build_status_query(s), response=False)
        await asyncio.sleep(0.15)
    await asyncio.sleep(1.0)

    still = [s for s in cap.slots
             if s in cap.latest_any and cap.latest_any[s].current_ma > 0]
    if still:
        log(f"*** STOP FAILED: slot(s) {still} still drawing current. "
            f"Stop them from the panel. ***")
    else:
        log("stopped; slots idle")
    return True


async def stop_all_mode(fresh: bool = True, log=None) -> int:
    """
    Halt every slot, whatever started it. The panic stop.

    Deliberately UNGUARDED, and the only place in this tool that is. Everywhere
    else refuses to disturb a slot it did not start, because an automatic action
    has no business reaching that far. This is not automatic: you asked for it,
    naming every slot, and that is a different question.

    NOT A SAFETY DEVICE. It needs a working Bluetooth link, a responsive
    charger and about fifteen seconds. If a cell is hot or venting, use the
    charger's own controls or remove it - do not stand there watching a laptop
    scan. The charger's per-run safety timer is the protection that survives
    this software failing, and it is why every config carries one.

    Reports what it found and what actually went idle afterward, because a
    stop command being sent proves nothing: 0x93 was sent for months and never
    stopped anything.
    """
    log = log or Log()
    # Try harder than a capture would: a single scan can come back empty, and
    # "not found" is a poor answer to "stop everything".
    dev = await find_device(log, attempts=3)
    if dev is None:
        print("no MC5000 found after 3 scans - if something is running, "
              "stop it from the charger's panel")
        return 1

    latest: dict[int, SlotStatus] = {}

    def on_notify(_sender, data: bytearray):
        raw = bytes(data)
        if len(raw) < 4 or raw[2] != 0x91:
            return
        try:
            st = parse_status_response(raw)
        except ValueError:
            return
        if st.slot:
            latest[st.slot] = st

    kwargs = {"winrt": {"use_cached_services": False}} if fresh else {}
    client = BleakClient(dev.address, **kwargs)

    try:
        await client.connect()
        log("connected")
        await asyncio.sleep(1.0)

        char = None
        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid.lower() == CHAR_UUID:
                    char = ch
        if char is None:
            log("characteristic not found")
            return 1

        await client.start_notify(char, on_notify)
        await asyncio.sleep(1.0)

        # More than one round: the first write after connecting is swallowed.
        for _ in range(POLL_ATTEMPTS):
            for s in ALL_SLOTS:
                await client.write_gatt_char(char, build_status_query(s),
                                             response=False)
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.6)
            if len(latest) == len(ALL_SLOTS):
                break

        active = [s for s in ALL_SLOTS
                  if s in latest and latest[s].current_ma > 0]
        for s in ALL_SLOTS:
            st = latest.get(s)
            if st is None:
                log(f"  slot {s}: no answer")
            else:
                log(f"  slot {s}: {st.voltage_v:.3f}V {st.current_ma:5d}mA "
                    f"{st.capacity_mah:5d}mAh {st.elapsed_seconds:5d}s"
                    f"{'   ACTIVE' if st.current_ma > 0 else ''}")

        if not active:
            log("nothing is drawing current; sending the stop anyway")
        else:
            log(f"stopping slot(s) {active}")

        for pkt in init_sequence():
            await client.write_gatt_char(char, pkt, response=False)
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.5)

        latest.clear()
        for _ in range(POLL_ATTEMPTS):
            for s in ALL_SLOTS:
                await client.write_gatt_char(char, build_status_query(s),
                                             response=False)
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.6)
            if len(latest) == len(ALL_SLOTS):
                break

        still = [s for s in ALL_SLOTS
                 if s in latest and latest[s].current_ma > 0]
        log("")
        if still:
            log(f"*** STOP FAILED: slot(s) {still} still drawing current. "
                f"STOP THEM FROM THE PANEL. ***")
            return 1

        unseen = [s for s in ALL_SLOTS if s not in latest]
        if unseen:
            log(f"slot(s) {unseen} did not answer, so their state is unknown; "
                f"check the panel")
        log("all slots that answered are idle")
        return 0

    finally:
        try:
            if client.is_connected:
                await client.disconnect()
        except Exception:
            pass


async def send_all_stop(client, char, cap, log) -> bool:
    """
    The All Stop: halt every slot, whatever started it, over the LIVE session.

    Deliberately unguarded - the one action allowed to reach beyond runs this
    tool owns, because it is only ever triggered by an explicit human request
    (the GUI's All Stop button), never automatically. Automatic teardown uses
    stop_our_slots, which reaches only the slots this run started.

    Reports what actually went idle rather than assuming: a stop being sent
    proves nothing on this firmware. Returns True if every slot that answered
    is idle afterward.
    """
    before = [s for s in ALL_SLOTS
              if s in cap.latest_any and cap.latest_any[s].current_ma > 0]
    log(f"ALL STOP: halting slot(s) {before or '(none visibly active)'} "
        f"with the init sequence")
    for pkt in init_sequence():
        await client.write_gatt_char(char, pkt, response=False)
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.5)

    # Fresh reads only: stale latest_any entries from before the stop would
    # read as still-active and produce a false FAILED.
    cap.latest_any.clear()
    for _ in range(2):
        for s in ALL_SLOTS:
            await client.write_gatt_char(char, build_status_query(s),
                                         response=False)
            await asyncio.sleep(0.15)
        await asyncio.sleep(0.8)

    still = [s for s in ALL_SLOTS
             if s in cap.latest_any and cap.latest_any[s].current_ma > 0]
    unseen = [s for s in ALL_SLOTS if s not in cap.latest_any]
    if still:
        log(f"*** ALL STOP FAILED: slot(s) {still} still drawing current. "
            f"STOP THEM FROM THE PANEL. ***")
        return False
    if unseen:
        log(f"all stop: slot(s) {unseen} did not answer; check the panel")
    log("all stop complete; every slot that answered is idle")
    return True


# The pre-flight arms send the run's OWN config, unmodified.
#
# An earlier version sent a 1 mAh rated capacity as a tight backstop, since the
# charger derives a termination ceiling from capacity_mah (runtime+0x1c, ~1.2x
# rated). It worked as a backstop and broke the run: sent 0.6 s after the
# pre-flight's last per-slot stop, the REAL config's capacity did not take, the
# slot kept the 1 mAh ceiling, and the actual run was torn down after 15 s at
# 1 mAh with alert 'capacity limit' (seen on the bench). A config
# value can outlive the stop that should have ended its run.
#
# So: write nothing the real run could inherit. The trick bought a reduction in
# crash exposure from ~8 mAh to ~1.2 mAh and risked destroying the run it was
# protecting - a bad trade. With the real config, a crashed sampler leaves the
# slot on exactly the backstop every normal run already relies on: its own
# safety timer. No new risk class, and nothing hostile to inherit.
#
# The settle below is the other half of that lesson: give the slot time between
# the last stop and the real config, rather than racing it.
IR_PREFLIGHT_SETTLE_S = 1.5
# How long to wait for the charger to publish a resistance. It has one about a
# second after the slot starts counting (measured: device elapsed 1 s on two
# bench runs), so this is generous.
IR_PREFLIGHT_TIMEOUT_S = 8.0
IR_PREFLIGHT_POLL_S = 0.3
# Samples this close together are treated as agreeing, and sampling stops early.
IR_AGREEMENT_MOHM = 3


async def measure_ir_preflight(client, char, cap, req, slots, log,
                               samples: int = 2) -> dict[int, list[int]]:
    """
    Sample internal resistance a few times before the real run starts.

    The charger measures IR ONCE per arm and then holds it for the whole run,
    so a bad sample poisons the run's only figure. Re-arming is the only way to
    make it measure again, so this arms and stops the slot a few times and
    reports what it saw.

    Why this is cheap, and why the configured current does not matter: the
    measurement applies its OWN fixed 100 mA load (`MeaRes_DisCharge_Init`,
    a hardcoded 0x64) and completes BEFORE the run's configured current starts
    - measured, at device elapsed 1 s, with current still reading 0. So a slot
    stopped as soon as resistance appears never sees the configured current at
    all, only that brief pulse.

    Throwaway arms are made safe by the two charger-enforced backstops, not by
    this loop finishing: a 1 mAh capacity ceiling and a 1-minute timer. If this
    process dies mid-sample the charger ends the arm by itself.

    Returns {slot: [ir, ...]}. Never raises for a slot that simply refuses to
    report - an absent reading is reported as such and the real run proceeds.
    """
    out: dict[int, list[int]] = {}
    for slot in slots:
        vals: list[int] = []
        for i in range(samples):
            # The run's own config, verbatim - see the note on
            # IR_PREFLIGHT_SETTLE_S for why nothing here is special-cased.
            problem = req.problem()
            if problem:
                log(f"  IR pre-flight skipped on slot {slot}: {problem}")
                break
            cfg = build_start_packets([slot], req)[0][1]
            cap.measuring_ir.add(slot)
            # Ownership BEFORE the write: if this crashes between the two, the
            # stop-on-exit path must still know the slot is ours to stop.
            cap.started_by_us = True
            try:
                await client.write_gatt_char(char, cfg, response=False)
                got = None
                deadline = asyncio.get_event_loop().time() + IR_PREFLIGHT_TIMEOUT_S
                while asyncio.get_event_loop().time() < deadline:
                    await asyncio.sleep(IR_PREFLIGHT_POLL_S)
                    await client.write_gatt_char(
                        char, build_status_query(slot), response=False)
                    st = cap.latest_any.get(slot)
                    if st is not None and st.resistance_mohm > 0:
                        got = st.resistance_mohm
                        break
            finally:
                # Always stop the arm, even if the read failed or timed out.
                await client.write_gatt_char(
                    char, build_slot_stop(slot), response=False)
                await asyncio.sleep(0.6)
                cap.measuring_ir.discard(slot)
            if got is None:
                log(f"  IR pre-flight slot {slot}: no reading (sample {i + 1})")
                continue
            vals.append(got)
            if len(vals) >= 2 and max(vals) - min(vals) <= IR_AGREEMENT_MOHM:
                # They agree; another sample would tell us nothing.
                break
        # Let the slot settle before anything else configures it. Without
        # this the real run's config lands on a slot still tearing down.
        await asyncio.sleep(IR_PREFLIGHT_SETTLE_S)
        if vals:
            spread = max(vals) - min(vals)
            log(f"  IR pre-flight slot {slot}: {vals} mOhm"
                + (f" (spread {spread})" if len(vals) > 1 else ""))
            out[slot] = vals
    cap.ir_samples = out
    return out


def flush_pending_commands(cap, inject_queue, stop_queue, all_stop_event,
                           log) -> int:
    """
    Throw away every command that was waiting for a link that is gone (or not
    yet back). Returns how many were dropped.

    A Start pressed while the link was down used to sit in the queue and be
    written whenever a session next connected - minutes or hours later, after
    a sleep or a power cycle, possibly with a different cell in the bay. No
    command may outlive the link it was given on: the user is told to use the
    charger's panel instead, and presses again once connected. Covers the
    start and stop queues, a pending ALL STOP, and the start-confirmation
    state (which could otherwise RE-SEND a start config after a reconnect).
    The temperature cutoff's own stops are not touched: that is a safety stop
    the app owes the cell, and it is still right after a reconnect.
    """
    dropped = []
    for q, what in ((inject_queue, "start"), (stop_queue, "stop")):
        if q is None:
            continue
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                break
            slot = getattr(item, "slot", item)
            dropped.append(f"{what} slot {slot}")
    if all_stop_event is not None and all_stop_event.is_set():
        all_stop_event.clear()
        dropped.append("ALL STOP")
    for slot in sorted(cap.start_pending):
        dropped.append(f"start confirmation slot {slot}")
    cap.start_pending.clear()
    if dropped:
        log(f"  dropped {len(dropped)} command(s) from before the link was "
            f"lost: {', '.join(dropped)}. Nothing is sent late - if you still "
            f"want it, press it again now.")
    return len(dropped)


async def drain_start_injections(queue_obj, client, char, cap, log) -> None:
    """
    Start armed slots whose configs the front end has dropped into the queue,
    over the LIVE session. Each item is a (slot, config_bytes) pair.

    This is per-slot independent START, made safe by probe_independent.py:
    a 0x94 config starts an idle slot WITHOUT init, and slots
    already running are undisturbed. So each config is sent on its own - never
    the init sequence, which would stop every slot. (Per-slot STOP has no known
    mechanism on FW 2.02; this is start-only.)

    Two guards fire at the instant of the write, not when the slot was armed:

    - Idle check (refuse_reason): something may have grabbed the slot between
      arming and this cycle. Never start onto a slot that is not idle now.
    - Firmware gate (firmware_refusal): the same bar as a CLI start, and with
      NO override. Deliberate limit: the GUI has none, so a start only ever
      goes to firmware that passed the gate. If the version is still
      unread and every slot is now idle, it is read here first; otherwise the
      per-charger record in settings.ini decides, and with neither the start
      is refused.

    A refused injection is dropped with a logged reason; the rest proceed.
    """
    while True:
        try:
            item: StartInjection = queue_obj.get_nowait()
        except queue.Empty:
            return
        slot = item.slot

        refusal = refuse_reason([slot], cap.latest)
        if refusal:
            log(f"  start injection for slot {slot} refused: {refusal}")
            continue

        # A monitor that connected beside a busy slot never got a safe window
        # for 0x57. If the charger has since gone wholly idle, take it now.
        if cap.device_info is None and not any_slot_busy(cap.latest_any):
            await read_firmware(client, char, cap, log)
        observed_fw = cap.device_info.firmware if cap.device_info else None
        fw_refusal = firmware_refusal(observed_fw,
                                      remembered_firmware(cap.address), False,
                                      product=product_for(cap))
        if fw_refusal:
            log(f"  start injection for slot {slot} refused: {fw_refusal}")
            continue
        if cap.firmware_verified is False:
            # Verified after all (read late, or remembered): lift the
            # watching-only restrictions for the rest of the session.
            apply_firmware_verdict(cap, True, log)

        await client.write_gatt_char(char, item.config, response=False)
        # We now own this slot: stop-on-exit's guarded teardown applies to it,
        # while still never touching a slot we did not start - and so may the
        # temperature cutoff, which this arms from the request.
        cap.begin_slot_run(slot, item.request, cell_id=item.cell_id,
                           group=item.group)
        cap.expect_start(slot, item.config)
        log(f"  slot {slot} started by injection: "
            f"{item.request.mode.name.lower()} {item.request.current_ma} mA "
            f"(config, no init)")
        await asyncio.sleep(1.5)


async def drain_stop_injections(queue_obj, client, char, cap, log) -> None:
    """
    Stop individual slots the front end has queued, over the LIVE session.
    Each item is a slot number.

    The counterpart to drain_start_injections, using the per-slot stop found
    by probe_slot_stop.py: 0xFE addressed to one slot's bitmask stops
    that slot alone, with no init and no effect on the others. Unlike All Stop
    (which is the init sequence and halts everything), this is surgical, so it
    needs no guard against touching other slots - it cannot. It is still only
    ever triggered by an explicit human action (a slot's Stop button), never
    automatically.

    The slot then reports 0 mA; finished-detection sees it go idle and records
    the stop in the CSV, exactly as it does after an All Stop.
    """
    while True:
        try:
            slot = queue_obj.get_nowait()
        except queue.Empty:
            return
        await client.write_gatt_char(char, build_slot_stop(slot),
                                     response=False)
        cap.note_stop_sent(slot)
        log(f"  slot {slot} stopped (0xFE to its bitmask, no init; "
            f"other slots untouched)")
        await asyncio.sleep(1.0)


async def drain_cutoff_stops(client, char, cap, log) -> None:
    """
    Send the per-slot stops the temperature cutoff has asked for.

    The same 0xFE-to-one-bitmask stop as a slot's Stop button (and as
    stop_our_slots uses): it reaches that slot alone, no init. Capture only
    queues a slot this capture started, so nothing here can touch a run we did
    not start. A stop that has not taken after CUTOFF_STOP_RETRY_S is sent
    again, up to CUTOFF_STOP_TRIES; after that it says so loudly rather than
    reaching for the all-slot init, which would stop everyone's runs.
    """
    now = time.monotonic()
    for slot in sorted(cap.pending_cutoff_stops):
        cap.pending_cutoff_stops.discard(slot)
        await client.write_gatt_char(char, build_slot_stop(slot),
                                     response=False)
        cap.mark_cutoff_stopped(slot)
        cap._cutoff_stop_sends[slot] = [now, 1]
        log(f"  slot {slot} stopped by the temperature cutoff (0xFE to its "
            f"bitmask; other slots untouched)")
        await asyncio.sleep(0.4)
    for slot, sends in list(cap._cutoff_stop_sends.items()):
        if not cap.slot_running(slot):
            del cap._cutoff_stop_sends[slot]        # it took
            continue
        last, tries = sends
        if now - last < CUTOFF_STOP_RETRY_S:
            continue
        if tries >= CUTOFF_STOP_TRIES:
            log(f"*** slot {slot}: the temperature cutoff's stop did not take "
                f"after {tries} tries - STOP IT AT THE CHARGER ***")
            del cap._cutoff_stop_sends[slot]
            continue
        await client.write_gatt_char(char, build_slot_stop(slot),
                                     response=False)
        sends[:] = [now, tries + 1]
        log(f"  slot {slot} still running after the cutoff's stop; sent it "
            f"again (try {tries + 1} of {CUTOFF_STOP_TRIES})")
        await asyncio.sleep(0.4)


def _shows_started(st) -> bool:
    """Any sign the charger has taken a start: a run status other than 0, an
    IR reading, elapsed time or current. A torn-down phantom (current echoing
    after a run) cannot be mistaken for this, because refuse_reason never lets
    a start be sent to a slot drawing current."""
    return (st.current_ma > 0 or st.resistance_mohm > 0
            or st.elapsed_seconds > 0
            or getattr(st, "device_status", None) not in (None, 0))


# Reasons the charger gives for ending a run AT ONCE that are a normal end, not
# a failed start: a charge sent to a cell that is already full has nothing to
# do. Matched by the alert's NAME (protocol.ALERT_NAMES). "fully charged" is
# wire code 13, seen on hardware (a charge whose target sat below the cell's
# resting voltage).
ENDED_AT_ONCE_NORMAL = ("fully charged",)


def _ended_at_once(cap, slot: int, alert: str, log) -> None:
    """The charger answered a start with the reason it ended, and no sign the
    run ever began (see confirm_starts). A normal reason is DONE; any other is
    a refusal or fault, so the start is declared failed - but neither is
    re-sent, because the charger did hear the config."""
    run = cap.open_runs.get(slot)
    if run is not None:
        run.alert = alert
    if alert in ENDED_AT_ONCE_NORMAL:
        cap.ended_at_once.add(slot)
        cap.finished.add(slot)
        if run is not None:
            run.finished = True
        log(f"  slot {slot}: the charger reports {alert!r} and ended the run "
            f"at once. That is a normal end, not a failed start - there was "
            f"nothing for it to do.")
    else:
        cap.start_failed.add(slot)
        log(f"START ENDED AT ONCE on slot {slot}: the charger reports "
            f"{alert!r}. Nothing is running on slot {slot}.")
    if run is not None:
        cap._write_meta(run)


async def confirm_starts(client, char, cap, log, now: float | None = None):
    """
    Watch every start in cap.start_pending until the charger shows it took.

    Confirmed: a status reply received AFTER the send shows the slot alive.
    Ended at once: a reply after the send carries an alert the slot did not have
    before, with no sign of life - the charger took the config and gave its
    reason. Never re-sent (_ended_at_once).
    Re-sent (the identical config, up to START_TRIES in all): only when a reply
    that arrived at least START_FRESH_S after the send still shows the slot
    wholly idle - positive evidence the write was lost. A config is never
    re-sent on silence, because a slot that started but whose replies were
    lost must not receive a second config.
    Failed: after START_TRIES, or START_GIVEUP_S with no usable reply. The
    slot goes into cap.start_failed and the log says so plainly.
    """
    now = time.monotonic() if now is None else now
    for slot, p in list(cap.start_pending.items()):
        st = cap.latest_any.get(slot)
        at = cap.answered_at.get(slot, float("-inf"))
        if st is not None and at > p["sent_at"] and _shows_started(st):
            del cap.start_pending[slot]
            if p["tries"] > 1:
                log(f"  slot {slot}: start confirmed on try {p['tries']}")
            continue
        if (st is not None and at > p["sent_at"] and st.alert_code
                and st.alert != p.get("alert_before")):
            del cap.start_pending[slot]
            _ended_at_once(cap, slot, st.alert, log)
            continue
        if now - p["sent_at"] < START_CONFIRM_S:
            continue
        idle_now = (st is not None and at >= p["sent_at"] + START_FRESH_S
                    and _reports_idle(st) and not _shows_started(st))
        if idle_now and p["tries"] < START_TRIES:
            await client.write_gatt_char(char, p["config"], response=False)
            p["sent_at"], p["tries"] = time.monotonic(), p["tries"] + 1
            log(f"  slot {slot} did not start (the charger still reports it "
                f"idle); sending its config again (try {p['tries']} of "
                f"{START_TRIES})")
            await asyncio.sleep(1.5)
        elif idle_now or now - p["first_at"] >= START_GIVEUP_S:
            del cap.start_pending[slot]
            cap.start_failed.add(slot)
            why = (f"after {p['tries']} tries the charger still reports it idle"
                   if idle_now else "the charger has not confirmed it")
            log(f"START DID NOT TAKE on slot {slot}: {why}. Nothing is running "
                f"on slot {slot}; start it again.")


async def read_firmware_when_idle(client, char, cap, log, override=False) -> bool:
    """
    Learn the firmware version mid-session, if - and only if - the charger is
    wholly idle RIGHT NOW. Returns True if a version was read.

    For an attach that connected beside a busy slot: 0x57 is proven harmless
    on an idle charger only, so it waits for a gap between runs. Every slot is
    polled first (a status query is read-only), and the read goes out only if
    ALL FOUR answered that poll and none is drawing current. A slot that did
    not answer counts as unknown, not idle - stricter than the first-connect
    poll, and affordable because this is simply tried again later. A slot
    the charger has ended and is only echoing (status 0 with an alert - the
    teardown latch) counts as idle here; see busy_beyond_latch.

    A version read now is remembered for this charger and decides the verdict:
    a supported one switches temperature polling on for the rest of the
    session; an unsupported one switches it off (a charger updated since it was
    last remembered is caught here).
    """
    asked = time.monotonic()
    for s in ALL_SLOTS:
        await client.write_gatt_char(char, build_status_query(s),
                                     response=False)
        await asyncio.sleep(0.15)
    await asyncio.sleep(0.8)
    fresh = all(cap.answered_at.get(s, float("-inf")) >= asked
                for s in ALL_SLOTS)
    if not fresh or busy_beyond_latch(cap.latest_any):
        return False
    log("charger is idle; reading its firmware version (0x57)")
    await read_firmware(client, char, cap, log)
    if cap.device_info is None or not cap.device_info.firmware:
        log("  device info (0x57) did not answer; will try again later")
        return False
    log(f"  firmware {cap.device_info.firmware}")
    apply_firmware_verdict(
        cap, firmware_refusal(cap.device_info.firmware, None, False,
                              product=cap.device_info.product_code) is None,
        log, override=override)
    return True


async def read_firmware(client, char, cap, log) -> None:
    """
    Ask the charger for its identity (0x57) and remember the version.

    Callers must only call this on a wholly idle charger: 0x57 is verified
    harmless on idle slots and nowhere else. The version read
    is recorded in settings.ini against this charger's address - supported or
    not - so a later session that cannot read it knows what it is.
    """
    for _ in range(3):
        await client.write_gatt_char(char, build_info_query(), response=False)
        await asyncio.sleep(0.8)
        if cap.device_info is not None:
            break
    if cap.device_info is not None and cap.device_info.firmware:
        remember_firmware(cap.address, cap.device_info.firmware, log,
                          product_code=cap.device_info.product_code)


def apply_firmware_verdict(cap, verified: bool, log,
                           override: bool = False) -> None:
    """
    Record whether this session's firmware is verified, and apply what follows.

    Unverified firmware is WATCH-ONLY: runs cannot be started (the start paths
    enforce that), and 0x55 temperature polling is switched off, because that
    opcode is only proven safe on the image that was traced. The run is labeled
    "unverified firmware" in the log, in meta.json and by the GUI.

    `override` is the allow_unvalidated_firmware setting, which no
    command-line option or GUI control sets (it is always False there). When
    True, a developer is exploring an untraced image on purpose, so 0x55 stays
    ON (the run is still labeled unverified).
    """
    cap.firmware_verified = verified
    if verified:
        if cap.temp_interval_s is None and cap.requested_temp_interval_s is not None:
            cap.temp_interval_s = cap.requested_temp_interval_s
            log("  firmware verified; temperature polling (0x55) on")
        return
    fw = cap.device_info.firmware if cap.device_info else None
    remembered = remembered_firmware(cap.address)
    what = fw or (f"{remembered} (remembered)" if remembered else "unknown")
    if override:
        how = ("started anyway under --allow-unvalidated-firmware"
               if cap.started_by_us else "--allow-unvalidated-firmware given")
    else:
        how = "watching only"
    log(f"UNVERIFIED FIRMWARE ({what}): {how}. Supported: "
        f"{', '.join(sorted(SUPPORTED_FIRMWARE))}.")
    if override:
        if cap.temp_interval_s is not None:
            log("  temperature polling (0x55) stays ON under "
                "--allow-unvalidated-firmware - unproven on this image")
        return
    if cap.temp_interval_s is not None:
        cap.temp_interval_s = None
        log("  temperature polling (0x55) is off on unverified firmware")
    note = cap.temperature_note()
    if note and fw is None:
        log(f"  {note}; it will be read in the next gap between runs")
    # Not polling it, so its absence says nothing about the firmware.
    if "0x55" not in cap.validator.expected_absent:
        cap.validator.expected_absent += ("0x55",)


async def find_device(log, attempts: int = 1, wait_s: float | None = None,
                      stop_requested=None):
    """
    Scan for the charger. attempts > 1 retries, for callers that should try
    harder than a capture would - a scan can come back empty transiently
    (observed on the bench), and giving up on the first miss is the wrong
    behavior when the user has asked to stop everything.

    `wait_s` keeps scanning for about that long instead (whole scans of
    SCAN_TIMEOUT_S), saying why after the first miss. It exists because after a
    clean disconnect the charger takes ~2 minutes to advertise again
    (measured): a user who closes the app and reopens it would
    otherwise be told the charger is not there. `stop_requested` (a callable)
    abandons the wait between scans.
    """
    if wait_s is not None:
        attempts = max(attempts, math.ceil(wait_s / SCAN_TIMEOUT_S))
    for attempt in range(1, attempts + 1):
        if stop_requested is not None and stop_requested():
            return None
        if attempt == 1:
            log("scanning ...")
        elif wait_s is None:
            log(f"scanning (attempt {attempt} of {attempts}) ...")
        elif attempt == 2:
            log(f"{REAPPEAR_NOTE} (up to {wait_s / 60:.1f} min)")
        else:
            waited = int((attempt - 1) * SCAN_TIMEOUT_S)
            log(f"still looking for the charger ({waited} s so far)")
        for d in await BleakScanner.discover(timeout=SCAN_TIMEOUT_S):
            if any(h in (d.name or "").lower() for h in NAME_HINTS):
                log(f"found {d.name} [{d.address}]")
                return d
    return None


async def session(cap: Capture, args, address: str, first: bool,
                  stop_requested=None, all_stop_event=None,
                  inject_queue=None, stop_queue=None,
                  run_until_stopped=False) -> str:
    """
    One BLE session. Returns why it ended:
      "done"         all active slots finished naturally
      "duration"     the --max-hours cap was reached. The cap ends the
                     RECORDING only: it never stops a slot, so a run still
                     going carries on under its own safety timer and its
                     meta.json says `recording_ended`
      "stopped"      a deliberate stop (Stop button / All Stop / Ctrl-C)
      "refused"      a guardrail declined to start; nothing was sent
      "start_failed" every start was sent but none took (see confirm_starts)
      "drift"        aborted mid-run on protocol drift errors
      "disconnected" BLE dropped; run_capture retries this one
    Only "done"/"duration" are a complete capture (a baseline may be written).
    """
    log = cap.log
    kwargs = {"winrt": {"use_cached_services": False}} if args.fresh else {}
    client = BleakClient(address, **kwargs)
    char = None
    cap_reached = False     # the time cap ended this session: stop NOTHING
    # The user deliberately ended the session (Stop, window close, Ctrl-C).
    # ONLY then does the exit below stop the runs this app started. Every
    # other way out - a failed write, a dropped link, drift, a refusal - leaves
    # the runs alone: a transient Bluetooth error must never stop a multi-hour
    # test, and the reconnect that follows picks the session up again.
    deliberate_stop = False

    # A temperature measured before a dropped link must not be written onto a
    # row after it. Clearing here covers the reconnect case, which is the only
    # way a pending reading can outlive the moment it describes. `last_temp`
    # must be bound before the poll loop reads it - session() is entered afresh
    # on every reconnect, so both belong here rather than further in.
    cap.pending_temp.clear()
    last_temp = 0.0

    try:
        await client.connect()
        log("connected")
        await asyncio.sleep(1.0)

        for svc in client.services:
            for ch in svc.characteristics:
                if ch.uuid.lower() == CHAR_UUID:
                    char = ch
        if char is None:
            log("characteristic not found")
            return "disconnected"

        await client.start_notify(char, cap.guarded_notify)
        # From here a drop is a LOST LINK (a reconnect gap), not a failed
        # attempt to get one - see run_capture.
        # Anything queued for an earlier link is dead: drop it BEFORE the link
        # is declared usable, so nothing fires late (see flush_pending_commands).
        flush_pending_commands(cap, inject_queue, stop_queue, all_stop_event,
                               log)
        cap.link_up = True
        cap.link_ok = True
        if not first:
            cap.link_restored()
        await asyncio.sleep(2.0)

        setup_pending = not getattr(cap, "setup_done", False)
        if setup_pending and not args.start:
            log("attach: sending no init, so a running operation is "
                "left undisturbed")

        # LOOK BEFORE TOUCHING ANYTHING.
        #
        # Status polling answers without init (measured), and so does the
        # 0x94 config that actually starts a run,
        # so the slots can be inspected while the device is still undisturbed.
        # This ordering is load-bearing: init STOPS whatever is running, so
        # sending it before the checks below would destroy the very run the
        # checks exist to protect. That is exactly what happened once -
        # a mistyped --start killed a live discharge, and the guardrail then
        # correctly reported a run it had already ended.
        #
        # Poll until every slot has answered, not just once.
        #
        # The FIRST write after connecting collides with the device's greeting
        # frame and is silently dropped - whichever packet it happens to be.
        # That used to be an init command, so the warm-up poll appeared
        # reliable; now that init no longer goes first, the poll itself takes
        # the hit. A single unanswered poll would otherwise be indistinguishable
        # from a slot that is genuinely not responding, and the guardrails below
        # would refuse a perfectly good run - observed on the bench.
        # Every slot is polled, not just ours. A status query is read-only and
        # cannot disturb anything, and the answers land in cap.latest_any, which
        # is what lets the checks below SEE a run we did not start. This matters
        # more since --start stopped sending init: init used to halt a foreign
        # run before anything else could reach it, so nothing downstream ever
        # met a busy foreign slot. Now they can, and must cope.
        #
        # The wait is for EVERY slot to answer, not just ours. Breaking as soon
        # as our own slots had answered defeated the widening entirely: the
        # first write after connecting is silently dropped (see below), that
        # write is now a foreign slot's query, and one round is all it took to
        # leave that slot permanently unknown. Measured on the bench:
        # a live 501mA discharge on slot 1 was invisible to the
        # 0x57 gate below, which then sent the read anyway. Falling through
        # after POLL_ATTEMPTS costs a few seconds and refuses nothing.
        for attempt in range(POLL_ATTEMPTS):
            for s in ALL_SLOTS:
                await client.write_gatt_char(char, build_status_query(s),
                                             response=False)
                await asyncio.sleep(0.15)
            await asyncio.sleep(0.8)
            if all(s in cap.latest_any for s in ALL_SLOTS):
                break
        else:
            silent = [s for s in cap.slots if s not in cap.latest]
            if silent:
                log(f"slot(s) {silent} did not answer in {POLL_ATTEMPTS} rounds")
            # A slot that never answers is treated as idle by the checks below,
            # and that is a deliberate choice rather than an oversight: the
            # alternative - refusing to read 0x57 whenever any bay is silent -
            # would let one unresponsive empty bay switch off the firmware gate
            # on every run. Silence is logged so the reader can judge it.
            unknown = [s for s in ALL_SLOTS if s not in cap.latest_any]
            if unknown:
                log(f"  slot(s) {unknown} never answered; assuming idle")

        # On an attach, read the firmware too - not just on the start path.
        # It decides whether a later per-slot start injection may go ahead,
        # AND whether temperature is polled at all: without a version (read
        # now, or remembered for this charger) an attach used to watch with
        # no temperature for its whole life, hiding the headline from anyone
        # watching a panel-started run. 0x57 is verified harmless on IDLE
        # slots only, so it goes out now only if every slot is idle; beside a
        # busy slot it is skipped, and the polling loop retries in the next
        # gap between runs (read_firmware_when_idle). That gate is load-bearing
        # - do not drop it to get temperature sooner.
        if setup_pending and not args.start and cap.device_info is None:
            if not any_slot_busy(cap.latest_any):
                await read_firmware(client, char, cap, log)
                if cap.device_info and cap.device_info.firmware:
                    log(f"  firmware {cap.device_info.firmware}, product code "
                        f"{cap.device_info.product_code}")

        # The start goes out in the first session that gets this far - which is
        # not always the first connection attempt. Keyed on whether a start has
        # been SENT, never on the attempt number: a failed first connect used to
        # make the later, successful session a silent watcher that never
        # started the run. And never sent twice: once any config is on its way
        # a reconnect only watches.
        start_pending = bool(args.start) and not getattr(cap, "start_attempted",
                                                         False)
        if args.start and not start_pending:
            log("reconnected: this capture's start was already sent and is "
                "not sent again - watching only")
        elif start_pending and not first:
            log("the start had not been sent yet (earlier connection attempts "
                "did not get through) - starting now")
        if start_pending:
            refusal = refuse_reason(cap.slots, cap.latest)
            if refusal:
                log(f"REFUSING TO START: {refusal}")
                log("nothing was sent to the charger; any running operation "
                    "is untouched")
                return "refused"

            # Read the device's identity before committing anything - the
            # firmware gate. 0x57 is verified harmless on IDLE slots only, and
            # that is the whole of what is known about it. refuse_reason has
            # just proved OUR slots idle, but a slot we did not start may be
            # running - a case this path could not previously reach, because
            # init would have killed that run before we got here. So ask the
            # same question the attach path asks, and skip the read rather than
            # aim an unproven opcode at someone else's run.
            busy = foreign_busy(cap.slots, cap.latest_any)
            if busy:
                log(f"  slot(s) {busy} are running and are not ours; skipping "
                    f"the 0x57 firmware read (harmless only on idle slots)")
                log("  they will not be touched - starting sends a config to "
                    "our slot(s) and nothing else")
                log("  firmware therefore not read now - not unanswered, "
                    "unasked; falling back to this charger's last reading")
            else:
                await read_firmware(client, char, cap, log)
                if cap.device_info is None:
                    log("  device info (0x57) did not answer; falling back to "
                        "this charger's last reading")

            observed_fw = cap.device_info.firmware if cap.device_info else None
            remembered_fw = remembered_firmware(cap.address)
            refusal = firmware_refusal(observed_fw, remembered_fw,
                                       args.allow_unvalidated_firmware,
                                       product=product_for(cap))
            if refusal:
                log(f"REFUSING TO START: {refusal}")
                log("nothing was sent to the charger")
                return "refused"
            if observed_fw is None and remembered_fw:
                log(f"  firmware {remembered_fw}, as last read from this "
                    f"charger")

            req = RunRequest(
                mode=MODE_BY_NAME[args.mode] if args.mode else {
                    "discharge": Mode.DISCHARGE,
                    "charge": Mode.CHARGE,
                    "storage": Mode.STORAGE,
                }[args.start],
                chemistry=cap.chemistry,
                current_ma=args.current,
                capacity_mah=args.capacity,
                safety_timer_min=args.safety_timer,
                cutoff_mv=args.cutoff_mv,
                target_mv=args.target_mv,
                storage_mv=getattr(args, "storage_mv", None),
                charge_cutoff_current_ma=getattr(args, "charge_cutoff_current", None),
                discharge_cutoff_current_ma=getattr(args, "discharge_cutoff_current", None),
                temp_cutoff_c=getattr(args, "temp_cutoff", None),
            )
            problem = req.problem()
            if problem:
                log(f"REFUSING TO START: {problem}")
                log("nothing was sent to the charger")
                return "refused"

            packets = build_start_packets(cap.slots, req)

            # From here a config may reach the charger (the IR pre-flight arms
            # the slot with the run's own config), so a later session must
            # never start this run again - whatever happens to this link.
            cap.start_attempted = True
            log("")
            log(f"starting {req.mode.name.lower()} on slot(s) {cap.slots} "
                f"@ {req.current_ma}mA, safety timer "
                f"{req.safety_timer_min} min")
            log(f"  chemistry {CHEMISTRY_NAME[cap.chemistry]}, cutoff "
                f"{req.effective_cutoff_mv()}mV, mode byte "
                f"0x{req.mode_byte():02x}")
            log("(a config packet IS the start command on this firmware)")

            # NOTHING is sent before the configs. Starting used to send the
            # init sequence here, justified as "required before 0x93 stop" -
            # but 0x93 does not stop anything on this firmware (black-box
            # measured, then confirmed in the disassembly: the command
            # dispatcher has no 0x93 case at all), and a 0x94 config starts a
            # slot with no init whatsoever (the probe_independent probe: on a
            # bare connection, a config to an idle slot started it). So the
            # send accomplished nothing while carrying init's full cost - it
            # STOPS EVERY SLOT. Starting slot 3 killed a discharge on slot 1,
            # and the pre-start guard could not catch it because it only
            # inspects the slots you asked to start.
            #
            # With the send gone, --start cannot reach a slot it was not aimed
            # at, and the guard stays narrow on purpose: starting one slot
            # alongside a run you did not start is now safe, and refusing it
            # would throw away the per-slot independence this firmware allows.
            # Pre-flight IR sampling, before the run's own config. The
            # charger measures IR once per arm and holds it, so the run's
            # single figure has no corroboration unless we take some first.
            # Deliberately after every refusal guard above: this arms slots,
            # so it must not run on anything we were not cleared to start.
            ir_samples = getattr(args, "ir_samples", 2)
            if ir_samples:
                log(f"  sampling internal resistance ({ir_samples}x) "
                    f"before the run ...")
                await measure_ir_preflight(client, char, cap, req,
                                           [s for s, _ in packets], log,
                                           samples=ir_samples)

            # Slots started together are one group: their folders share a
            # timestamp and each meta.json names the others.
            group = None
            if len(packets) > 1:
                started = datetime.now()
                group = RunGroup(id=f"{started:%Y-%m-%d_%H%M%S}",
                                 slots=tuple(s for s, _ in packets),
                                 started=started)
            for s, cfg in packets:
                await client.write_gatt_char(char, cfg, response=False)
                log(f"  config sent to slot {s}")
                cap.begin_slot_run(s, req, group=group)
                cap.expect_start(s, cfg)
                await asyncio.sleep(1.5)
            cap.started_by_us = True
            if req.temp_cutoff_c is not None:
                log(f"  temperature cutoff {req.temp_cutoff_c} C - watched by "
                    f"this program, ACTIVE ONLY WHILE IT IS CONNECTED; the "
                    f"charger's safety timer and 70 C limit stay the backstop")
            log("")

        # The firmware verdict, once per capture: verified if read now (or
        # remembered for this charger) as a supported version. Attaching is
        # allowed on ANY firmware, but unverified firmware is watch-only and
        # gets no 0x55 - see apply_firmware_verdict.
        if setup_pending and not args.start and getattr(args, "temp_cutoff", None):
            limit = args.temp_cutoff
            for s in cap.slots:
                cap.set_temp_cutoff(s, limit)
            log(f"  temperature cutoff {limit} C on slot(s) {cap.slots}: "
                f"WARN ONLY - this attach started nothing, so it will never "
                f"stop a slot, only say so")
        if setup_pending:
            observed_fw = cap.device_info.firmware if cap.device_info else None
            apply_firmware_verdict(
                cap, firmware_refusal(observed_fw,
                                      remembered_firmware(cap.address),
                                      False,
                                      product=_observed_product(cap)) is None,
                log, override=bool(getattr(args, "allow_unvalidated_firmware",
                                           False)))
            cap.setup_done = True

        # ---- polling loop ----
        loop = asyncio.get_event_loop()
        max_hours = getattr(args, "max_hours", None)
        hard_deadline = (float("inf") if max_hours is None
                         else loop.time() + max_hours * 3600)
        last_report = 0.0
        last_fw_try = time.monotonic()
        # Slots already finished when the firmware read was last tried. A slot
        # finishing is the moment the charger may have just gone idle, so it
        # triggers a try at once rather than waiting out FIRMWARE_RETRY_S.
        fw_tried_finished = set(cap.finished)
        override = bool(getattr(args, "allow_unvalidated_firmware", False))

        while loop.time() < hard_deadline:
            # The CLI stops via Ctrl-C; an embedder has no equivalent on a
            # background thread, so it hands in a callable instead (typically
            # threading.Event.is_set). Checked once per polling cycle, so a
            # stop takes effect within ~interval seconds.
            if stop_requested is not None and stop_requested():
                deliberate_stop = True
                return "stopped"
            # The All Stop travels over this live session, because a second
            # BLE connection cannot exist while this one holds the device.
            # After it fires the capture keeps watching: the slots go idle,
            # finished-detection notices, and the run ends "done" - so the
            # CSV records the halt itself.
            if all_stop_event is not None and all_stop_event.is_set():
                all_stop_event.clear()
                await send_all_stop(client, char, cap, log)
            # Per-slot start injection travels the same live session: the front
            # end drops an armed slot's config here and it is sent with NO init,
            # so any slot already running is undisturbed. Start-only - per-slot
            # stop has no known mechanism. See drain_start_injections.
            if inject_queue is not None:
                await drain_start_injections(inject_queue, client, char, cap, log)
            # Per-slot stop travels the same live session: 0xFE to one slot's
            # bitmask, halting it alone with no init. See drain_stop_injections.
            if stop_queue is not None:
                await drain_stop_injections(stop_queue, client, char, cap, log)
            if not client.is_connected:
                return "disconnected"

            # The is_connected check above is necessary but NOT sufficient: the
            # link can drop between that check and the write, and a WinRT write
            # can fail outright ("Operation aborted", WinError -2147467260).
            # Measured 9.5 hours into a 37-hour break-in attach: the
            # OSError propagated out of session(), straight past the reconnect
            # loop in run_capture() - which only ever inspected a RETURNED
            # reason - and killed the process. The retry machinery existed and
            # was simply unreachable for a raised error. Returning the same
            # "disconnected" the check above returns puts a transport failure
            # back on the path built for it.
            try:
                # The temperature cutoff's stops, queued by on_notify on this
                # same loop. Inside the try: they are writes like any other.
                await drain_cutoff_stops(client, char, cap, log)
                # Starts that have not yet been seen to take.
                await confirm_starts(client, char, cap, log)
                # Still no firmware version: try again in a gap between runs -
                # on the timer, or at once when a slot has just finished.
                if cap.device_info is None and (
                        time.monotonic() - last_fw_try >= FIRMWARE_RETRY_S
                        or cap.finished - fw_tried_finished):
                    last_fw_try = time.monotonic()
                    fw_tried_finished = set(cap.finished)
                    await read_firmware_when_idle(client, char, cap, log,
                                                  override=override)
                for s in cap.slots:
                    await client.write_gatt_char(
                        char, build_status_query(s), response=False)
                    await asyncio.sleep(0.15)
                # Temperature on its OWN, much slower cadence. Cell
                # temperature moves over minutes, so putting 0x55 on the 2 s
                # status cadence would be pure traffic for no extra signal.
                now_mono = time.monotonic()
                # `is not None`, not truthiness: None means the feature is
                # off, but 0 is a legitimate interval meaning "every poll".
                # Testing truthiness silently turned --temp-interval 0 into
                # --no-temps, which the session smoke test caught.
                if (cap.temp_interval_s is not None
                        and now_mono - last_temp >= cap.temp_interval_s):
                    last_temp = now_mono
                    for s in cap.slots:
                        await client.write_gatt_char(
                            char, build_temperature_query(s), response=False)
                        await asyncio.sleep(0.15)
            except (BleakError, OSError) as e:
                log(f"status query failed ({type(e).__name__}: {e}); reconnecting")
                return "disconnected"
            await asyncio.sleep(max(0.2, args.interval - 0.15 * len(cap.slots)))

            awake = getattr(cap, "awake", None)
            if awake is not None:
                awake.update(anything_busy(cap))

            if cap.drift_errors and args.stop_on_drift:
                log("stopping because of protocol drift errors")
                return "drift"

            cap.update_cutoff_blind()

            # A CLI capture is the recording of a run, so it ends when every
            # slot it started has finished. An interactive monitor is a persistent
            # control surface - stopping every slot must NOT end it, or there is
            # no live session left to start the next slot into (observed on the
            # bench: stopping both slots ended the session, and a
            # follow-up start then found nothing connected). So the GUI runs with
            # run_until_stopped and ends only on an explicit stop, a drop, or the
            # duration cap.
            # Every slot this CLI capture set out to start failed to start:
            # nothing is running and nothing ever will, so say so and end
            # rather than poll an idle charger until --max-hours.
            if (not run_until_stopped and not cap.start_pending
                    and (cap.start_failed or cap.ended_at_once)
                    and (cap.start_failed | cap.ended_at_once)
                    >= set(cap.slots)):
                if cap.start_failed:
                    log("no slot started, so there is nothing to capture")
                    return "start_failed"
                # Every slot was ended at once by a normal reason: the capture
                # is complete, with nothing more to record.
                log("the charger ended every run at once; nothing more to "
                    "capture")
                return "done"

            if cap.all_done() and not run_until_stopped:
                # A capture that ends when its runs do - a CLI attach watching
                # a panel run, typically - reaches this a few seconds after the
                # last slot goes idle, well inside FIRMWARE_RETRY_S, so the
                # in-loop retry never fired and the version was never read or
                # remembered: the NEXT attach had no temperature either. The
                # charger may be idle now, so try once on the way out. Same
                # idle-only gate; beside another busy slot it sends no 0x57.
                if cap.device_info is None:
                    try:
                        await read_firmware_when_idle(client, char, cap, log,
                                                      override=override)
                    except (BleakError, OSError) as e:
                        log(f"  firmware read on the way out failed "
                            f"({type(e).__name__}: {e}); not remembered")
                return "done"

            now = loop.time()
            if now - last_report >= args.report_every:
                last_report = now
                parts = []
                for s in cap.slots:
                    st = cap.latest.get(s)
                    if st:
                        t = cap.temp_by_slot.get(s)
                        parts.append(f"S{s} {st.voltage_v:.3f}V {st.current_ma:5d}mA "
                                     f"{st.capacity_mah:5d}mAh"
                                     + (f" {t}C" if t is not None else ""))
                # The sample count is deliberately NOT here: the GUI shows a live
                # count in its health line, and a second, 30s-snapshot count in
                # this heartbeat only disagreed with it. The
                # per-slot readings are the heartbeat; the count has one home.
                log(f"  {'  |  '.join(parts)}")

        # The cap is a limit on how long we RECORD, not on how long a run may
        # last (it counts from connect, so it could land
        # hours into someone's run). It must never reach the charger: say so,
        # and let the exit path below skip its stop. The charger's own safety
        # timer is what bounds the run.
        cap_reached = True
        going = sorted(s for s, r in cap.open_runs.items() if not r.finished)
        if going:
            names = ", ".join(f"slot {s}" for s in going)
            log(f"the {max_hours:g} h session cap was reached: recording ends "
                f"here. The run on {names} carries on on the charger under "
                f"its own safety timer - nothing was stopped. Reopen the app "
                f"and attach to record the rest.")
        else:
            log(f"the {max_hours:g} h session cap was reached: recording ends "
                f"here (no run was going).")
        return "duration"

    except (asyncio.CancelledError, KeyboardInterrupt):
        # Ctrl-C in the CLI cancels the task: that is the user ending it.
        deliberate_stop = True
        raise
    finally:
        # The link is over for commands the moment we leave, however we leave.
        cap.link_ok = False
        flush_pending_commands(cap, inject_queue, stop_queue, all_stop_event,
                               log)
        try:
            if client.is_connected:
                if (char is not None and args.stop_on_exit
                        and cap.started_by_us and not cap_reached
                        and deliberate_stop):
                    # Narrow stop first: it reaches only the slots we started,
                    # so it never has to decline the way the all-slot teardown
                    # does when someone else's run is going. That fallback is
                    # still there, one layer down, if 0xFE does not take.
                    await stop_our_slots(client, char, cap, log)
                await client.disconnect()
        except Exception:
            pass


# SetThreadExecutionState flags. CONTINUOUS makes the request last until it is
# cleared (or the thread or process ends, so a crash cannot leave the PC held
# awake); SYSTEM_REQUIRED blocks idle sleep and hibernate. DISPLAY_REQUIRED is
# deliberately NOT asked for: the screen may still turn off.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> int:
    """The Windows call, behind a seam so the tests can stand in for it. Returns
    0 when Windows refused it."""
    import ctypes
    fn = ctypes.windll.kernel32.SetThreadExecutionState
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = ctypes.c_uint32
    return fn(flags)


# How long after the last busy moment the request is kept, so the gap between
# one run ending and the next being started (or a rest phase the charger reports
# as 0 mA) does not let the PC drop off to sleep.
AWAKE_GRACE_S = 180.0


class AwakeGuard:
    """
    Ask Windows not to sleep or hibernate WHILE A SLOT IS BUSY, and let go when
    none has been for AWAKE_GRACE_S.

    This replaces holding the request for as long as the window was open, which
    kept an idle PC awake overnight. Called from the
    session's polling loop with whether anything is busy; the request belongs
    to the CALLING THREAD, so it is set and cleared on the one thread asyncio
    keeps a whole capture on (the GUI's capture thread, or the CLI's main one).
    While the link is down update() is not called, so a hold taken for a run
    stays through the reconnect - a sleeping PC cannot reconnect.

    Never lets a failure reach the capture: a keep-awake that cannot be had
    costs a warning, not the run. It cannot stop the user closing a laptop lid
    or choosing Sleep, and it does not keep the display on. Not a Windows
    machine: does nothing.

    No sleep test has been done on hardware. The reason for holding it is that a
    PC that sleeps mid-run loses the link and records nothing for as long as it
    is asleep, and a run that ends in the meantime loses its ending.
    """

    def __init__(self, log, clock=time.monotonic):
        self.log = log
        self.clock = clock
        self.held = False
        self._last_busy = None
        # A refused or failed request is not retried (and re-logged) on every
        # poll of the same busy spell; the next busy spell tries again.
        self._gave_up = False

    def update(self, busy: bool) -> None:
        now = self.clock()
        if busy:
            self._last_busy = now
            if not self.held and not self._gave_up:
                self._acquire()
        elif not self.held:
            self._gave_up = False
        elif (self.held and self._last_busy is not None
                and now - self._last_busy >= AWAKE_GRACE_S):
            self.release("no slot has been busy for "
                         f"{AWAKE_GRACE_S / 60:g} minutes")

    def _acquire(self) -> None:
        if sys.platform != "win32":
            return
        try:
            ok = bool(_set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
        except Exception as e:                      # noqa: BLE001
            self.log(f"could not ask Windows to stay awake ({e!r}); if the PC "
                     f"sleeps during this run, nothing is recorded while it "
                     f"sleeps")
            self._gave_up = True
            return
        if ok:
            self.held = True
            self.log("a slot is busy: asking Windows not to sleep while it "
                     "is")
        else:
            self.log("Windows refused the request to stay awake; if the PC "
                     "sleeps during this run, nothing is recorded while it "
                     "sleeps")
            self._gave_up = True

    def release(self, why: str = "the capture ended") -> None:
        if not self.held:
            return
        self.held = False
        try:
            _set_execution_state(ES_CONTINUOUS)
        except Exception:                           # noqa: BLE001
            pass
        self.log(f"{why}: no longer asking Windows to stay awake")


def anything_busy(cap) -> bool:
    """Whether any slot is doing something the PC must stay awake for: a run
    open and not finished (which covers a rest phase that reads 0 mA), a start
    still being confirmed, or current flowing that is not a torn-down slot's
    phantom reading."""
    if cap.start_pending:
        return True
    # A start that did not take (or ended at once with a fault) leaves its run
    # open and never finished, but nothing is running: it must not hold the PC
    # awake until the slot's next run.
    failed = getattr(cap, "start_failed", ())
    if any(not r.finished and getattr(r, "slot", None) not in failed
           for r in cap.open_runs.values()):
        return True
    torn = getattr(cap, "torn_down", ())
    return any(st.current_ma > 0 and s not in torn
               for s, st in cap.latest_any.items())


def keeps_awake(fn):
    """Give a capture coroutine an AwakeGuard (cap.awake, fed by the session's
    polling loop) and release whatever it holds however the capture ends. A
    decorator rather than a wrapper function so run_capture's own source stays
    what the audits in test_capture.py read (inspect.getsource follows
    __wrapped__)."""
    @functools.wraps(fn)
    async def wrapper(cap, *args, **kwargs):
        guard = AwakeGuard(cap.log)
        cap.awake = guard
        try:
            return await fn(cap, *args, **kwargs)
        finally:
            guard.release()
    return wrapper


@keeps_awake
async def run_capture(cap: Capture, opts, address: str | None = None,
                      stop_requested=None, all_stop_event=None,
                      inject_queue=None, stop_queue=None,
                      run_until_stopped=False) -> str:
    """
    The full capture lifecycle: find the charger, run sessions, reconnect on
    dropouts. Returns the final end reason.

    This is the function an embedder calls - the GUI's BLE source runs exactly
    this on its own thread's event loop, so the command line and the GUI cannot
    drift apart. `opts` is parsed CLI args or a SessionOptions; session() reads
    the same attribute names off either.

    `address` skips the scan when the caller already found the device (the CLI
    scans early so a missing charger fails before the run directory exists).

    A reconnect is not retried once stop has been requested - tearing down is
    not a dropout to recover from.
    """
    log = cap.log
    if address is None:
        dev = await find_device(log, wait_s=CONNECT_WAIT_S,
                                stop_requested=stop_requested)
        if dev is None:
            if stop_requested is not None and stop_requested():
                return "stopped"
            log(f"no MC5000 found after {CONNECT_WAIT_S / 60:.1f} min of "
                f"looking")
            return "no-device"
        address = dev.address
        cap.device_name = dev.name or None

    cap.address = address

    reason = "stopped"
    attempt = 0
    not_found = 0       # consecutive "device not found" reconnect failures
    while True:
        attempt += 1
        if attempt > 1:
            cap.reconnect_attempts += 1
            log(f"--- reconnecting (attempt {cap.reconnect_attempts}; "
                f"{len(cap.gaps)} link loss{'es' if len(cap.gaps) != 1 else ''}"
                f" so far) ---")
        cap.link_up = False
        error = None
        try:
            reason = await session(cap, opts, address, first=(attempt == 1),
                                   stop_requested=stop_requested,
                                   all_stop_event=all_stop_event,
                                   inject_queue=inject_queue,
                                   stop_queue=stop_queue,
                                   run_until_stopped=run_until_stopped)
        except (BleakError, OSError) as e:
            # Backstop for the same class of failure anywhere else in a session,
            # not just the status-query write that was measured. A transport
            # error must never be able to end a multi-day capture: this is the
            # layer that owns reconnection, so it is the layer that decides, and
            # nothing below it should be able to route around that by raising.
            log(f"session raised {type(e).__name__}: {e}")
            reason = "disconnected"
            error = e

        # A reconnect GAP is a lost link: a session that got as far as
        # receiving data and then dropped. A retry that never connected is not
        # another gap - counting those reported 53 for 2 real drops.
        if cap.link_up:
            not_found = 0
            if cap.stale_link_warning:
                cap.stale_link_warning = None
                log("the charger is back; the stale-link warning no longer "
                    "applies")
            if reason == "disconnected":
                mark = datetime.now().isoformat(timespec="seconds")
                cap.gaps.append(mark)
                log(f"--- link lost (gap #{len(cap.gaps)} at {mark}) ---")
        elif isinstance(error, BleakDeviceNotFoundError):
            not_found += 1
            if not_found == STALE_LINK_AFTER:
                cap.stale_link_warning = stale_link_message(not_found)
                for line in cap.stale_link_warning.split("\n"):
                    log(f"!!! {line}")
            elif not_found > STALE_LINK_AFTER and not_found % 10 == 0:
                log(f"!!! still not found after {not_found} attempts - the "
                    f"charger probably needs a POWER CYCLE (see above)")
        else:
            # Anything else - notably "no Bluetooth adapter" while a USB
            # dongle is unplugged - is a different situation with a different
            # cure, so it neither builds nor keeps the stale-link verdict.
            not_found = 0

        if reason in ("done", "duration", "drift", "refused", "stopped",
                      "start_failed"):
            break
        if stop_requested is not None and stop_requested():
            reason = "stopped"
            break
        # Still alive and reconnecting, so not "ended".
        what = {"disconnected": "connection lost",
                "no-device": "charger not found"}.get(
                    reason, f"connection problem ({reason})")
        log(f"{what}; retrying in 3s")
        await asyncio.sleep(3.0)
    return reason


def peak_temp_text(peak: int | None, unwatched: bool) -> str:
    """
    The peak-temperature tail of a slot's final summary line. The peak only
    covers the readings received, so after an unwatched end (Capture.
    unwatched_end) it is the highest SEEN, a floor - like the capacity and
    time beside it.
    """
    if peak is None:
        return ""
    if unwatched:
        return f", highest slot temp seen {peak} C (may have gone higher)"
    return f", peak slot temp {peak} C"


def stale_link_message(attempts: int) -> str:
    """
    What to tell the user when the charger has stopped being found.

    Measured once: after the Windows Bluetooth radio was toggled, the
    charger kept holding the severed link, stopped advertising, and stayed that
    way for 9+ minutes with a healthy adapter beside it - only a power cycle of
    the charger brought it back. A USB dongle replug, by contrast, recovered on
    its own. So this names the cure for the stuck case without promising that
    waiting will help, and without calling the other cases fatal.
    """
    return (
        f"The charger has not been found for {attempts} attempts in a row.\n"
        f"A charger that has only just dropped the link normally reappears "
        f"within about 2 minutes; this one has not.\n"
        f"It is probably still holding the old Bluetooth link - this happens "
        f"after the PC's Bluetooth is switched off and on, or the PC sleeps - "
        f"and it will NOT come back on its own.\n"
        f"To recover: POWER-CYCLE THE CHARGER. That ends any run on it; until "
        f"then the run carries on by itself, protected by its safety timer, "
        f"but nothing is being recorded.\n"
        f"Restarting this program or using --attach will not help: both need "
        f"the charger to be advertising, and it is not.\n"
        f"(If the charger is simply switched off or out of range, switch it "
        f"on or move closer instead.) Still retrying.")


def resolve_start_args(args) -> str | None:
    """--start needs --capacity, and works out whatever else was not given the
    way Configure does (derive_current_ma: same rates, same minimums, same
    caps; suggest_safety_timer_min: same rule). Fills args.current and
    args.safety_timer in place. Returns why it cannot, or None. An attach, or
    anything without --start, is left alone."""
    if not getattr(args, "start", None):
        return None
    if args.capacity is None:
        return ("--capacity is required with --start: the current and the "
                "safety timer are worked out from the cell's rated capacity, "
                "and nothing here assumes one.")
    mode = MODE_BY_NAME[args.mode] if getattr(args, "mode", None) else {
        "discharge": Mode.DISCHARGE, "charge": Mode.CHARGE,
        "storage": Mode.STORAGE}[args.start]
    chem = CHEMISTRY_BY_NAME[args.chemistry.lower()]
    if args.current is None:
        if mode is Mode.STORAGE:
            # One current both ways on this tool: the slower, discharge-leg one.
            derived = derive_storage_discharge_ma(chem, args.capacity)
        else:
            derived = derive_current_ma(chem, mode, args.capacity)
        if derived is None:
            return (f"No current can be worked out for "
                    f"{CHEMISTRY_NAME[chem]} {mode.name.lower()} from the "
                    f"capacity; pass --current.")
        args.current = derived
        print(f"--current not given: {derived} mA from {args.capacity} mAh "
              f"(as Configure works it out)")
    if args.safety_timer is None:
        got = suggest_safety_timer_min(chem, mode, args.capacity, args.current)
        if got is None:
            return ("The safety timer cannot be worked out from this capacity "
                    "and current; pass --safety-timer.")
        args.safety_timer = got[0]
        print(f"--safety-timer not given: {got[0]} min from {args.capacity} "
              f"mAh at {args.current} mA"
              + (" (held at the charger's 24 h limit)" if got[1] else ""))
    return None


async def main():
    ap = argparse.ArgumentParser()
    # Not required=True: --stop-all starts no run, so it has nothing to label.
    # Enforced below instead.
    ap.add_argument("--name", help="label for this session: it ends each "
                    "run folder's name (<date>_<time>_S<n>_<cell>_<action>"
                    "_<name>), names the session log (runs/logs/) and is "
                    "recorded in each run's meta.json")
    ap.add_argument("--stop-all", action="store_true",
                    help="halt every slot, whatever started it, then exit. "
                         "The one unguarded action in this tool - everything "
                         "else refuses to disturb a slot it did not start. "
                         "Not a safety device: it needs a working Bluetooth "
                         "link. The charger's safety timer is the protection "
                         "that survives this software failing.")
    ap.add_argument("--slots", default="1", help="comma separated, e.g. 1 or 1,2")
    ap.add_argument("--cells", default=None,
                    help="which cell is in which slot, as SLOT=CELL_ID pairs "
                         "(e.g. 1=ENL-A-01,2=ENL-A-02). Recorded in meta.json. "
                         "Optional, but a run captured without it can never be "
                         "linked back to the physical cell afterward.")
    ap.add_argument("--start", choices=["discharge", "charge", "storage"],
                    help="start the run (omit to attach to something already running)")
    ap.add_argument("--attach", action="store_true",
                    help="capture something already running without disturbing "
                         "it. Sends only status queries - no init, so a run "
                         "started from the panel keeps its timers and capacity "
                         "counters. Explicit opposite of --start.")
    ap.add_argument("--chemistry", default="liion", choices=sorted(CHEMISTRY_BY_NAME))
    ap.add_argument("--current", type=int, default=None,
                    help="mA. Omit to work it out from --capacity as the "
                         "app's Configure does: 0.5C to charge, 0.2C to "
                         "discharge, never below 50 mA or above the panel's "
                         "own default (Storage uses the 0.2C figure both "
                         "ways, since this tool has one current).")
    ap.add_argument("--capacity", type=int, default=None,
                    help="rated capacity mAh. REQUIRED with --start: the "
                         "current and the safety timer are worked out from "
                         "it, and nothing assumes a cell's capacity.")
    ap.add_argument("--cutoff-mv", type=int, default=None,
                    help="discharge cutoff in mV per cell. Omit to use the "
                         "chemistry default (Li-ion 3200, NiMH 900). For NiMH "
                         "capacity testing 1000-1100 is the usual convention.")
    ap.add_argument("--target-mv", type=int, default=None,
                    help="Li-ion charge ceiling in mV per cell; omit for the "
                         "chemistry default (~4200). HONORED over BLE on FW 2.02 for "
                         "IN-RANGE values only: 4050-4250 mV. Confirmed on hardware: "
                         "target_mv=4100 kneed and CV-regulated at exactly "
                         "4.100 V. OUT-OF-RANGE values are silently dropped to the "
                         "chemistry default by the firmware (what made it look "
                         "'ignored' in earlier 3.5/3.8 V tests), so values outside "
                         "4050-4250 are REFUSED here rather than sent, and it is "
                         "accepted only for a Li-ion charge.")
    ap.add_argument("--ir-samples", type=int, default=2,
                    help="how many extra internal-resistance samples to take "
                         "before starting the run (0 disables). The charger "
                         "measures IR ONCE per arm and holds it for the whole "
                         "run, so one bad sample poisons the run's only "
                         "figure; each extra sample re-arms the slot briefly. "
                         "Costs about a second each - the measurement uses its "
                         "own 100mA pulse and finishes before the configured "
                         "current starts, so the cell barely notices. Stops "
                         "early once two samples agree.")
    ap.add_argument("--storage-mv", type=int, default=None,
                    help="storage-mode park voltage in mV per cell (the "
                         "Li_Sto_Volt field). Omit for the chemistry default "
                         "(Li-ion 3800). Range-gated by the firmware per "
                         "chemistry (Li-ion 3700-3850) - out-of-range values "
                         "are silently replaced with the default, so they are "
                         "REFUSED here. Nickel chemistries have no storage "
                         "target at all (window is 0-0).")
    ap.add_argument("--charge-cutoff-current", type=int, default=None,
                    help="charge termination current (End_Cur) in mA; omit for the "
                         "100 mA default. The CC/CV charge self-terminates when the "
                         "CV-phase current tapers to this value. Confirmed honored "
                         "over BLE on fw 2.02: a real working control.")
    ap.add_argument("--discharge-cutoff-current", type=int, default=None,
                    help="discharge termination current (End_DCur, config+0x90) in "
                         "mA; omit for the 100 mA default. Once the cell reaches the "
                         "cutoff voltage the discharge does a CV taper and "
                         "self-terminates when current falls to this value. Twin of "
                         "--charge-cutoff-current.")
    ap.add_argument("--delta-peak-mv", type=int, default=None,
                    help="NiMH/NiCd charge termination sensitivity (-dV). "
                         "Reference implementation always sends 6.")
    ap.add_argument("--mode", default=None,
                    choices=["discharge", "charge", "break_in", "refresh", "cycle"],
                    help="override the operation. NOTE: on nickel chemistries "
                         "the mode byte differs from lithium - break_in and "
                         "discharge are NOT interchangeable.")
    ap.add_argument("--safety-timer", type=int, default=None,
                    help="charger-enforced cutoff in minutes. This is the only "
                         "protection that survives this software crashing or "
                         "the BLE link dropping. Omit to work it out from the "
                         "capacity and current, as Configure does.")
    # Deliberate limit: research overrides are NOT options of the released
    # tools, so what starts is only what was tested. Two do not exist in the
    # code at all: starting a chemistry and mode outside VERIFIED_CONTROL, and
    # running with no safety timer. The
    # third, starting on firmware outside SUPPORTED_FIRMWARE, is a parameter the
    # session code and its tests still read, always False from here.
    ap.set_defaults(allow_unvalidated_firmware=False)
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between polls")
    # Per-cell battery temperature via undocumented opcode 0x55. ON by
    # default: the opcode is fully mapped, its handler writes only into the
    # transmit buffer, and the buffer was measured with 328 bytes of clearance
    # for a 36-byte frame. The byte map was read from
    # firmware 2.02; on another version drift.py's baseline check will flag a
    # differently-shaped frame rather than let a wrong number through.
    ap.add_argument("--temps", action="store_true", default=True,
                    help="poll per-cell battery temperature (the default)")
    ap.add_argument("--no-temps", dest="temps", action="store_false",
                    help="do not poll temperature; no slot_temp_c values")
    ap.add_argument("--temp-interval", type=float, default=TEMP_INTERVAL_S,
                    help=f"seconds between temperature polls (default "
                         f"{TEMP_INTERVAL_S:g}). The signal moves over minutes; "
                         f"do not set this near the status interval.")
    ap.add_argument("--temp-cutoff", type=int, default=None,
                    help=f"stop a slot this program started when its slot "
                         f"temperature reads at or above this many degrees C "
                         f"({TEMP_CUTOFF_MIN_C}-{TEMP_CUTOFF_MAX_C}). A line "
                         f"you choose, below the charger's own 70 C limit. "
                         f"ACTIVE ONLY WHILE THIS PROGRAM IS CONNECTED - the "
                         f"charger does not enforce it; its safety timer and "
                         f"thermal limit remain the backstop. On --attach it "
                         f"only warns: a run this program did not start is "
                         f"never stopped. Warns if no reading has arrived for "
                         f"{CUTOFF_BLIND_AFTER_S / 60:g} minutes.")
    ap.add_argument("--report-every", type=float, default=30.0)
    ap.add_argument("--done-after", type=int, default=5,
                    help="consecutive idle readings before calling a slot finished")
    ap.add_argument("--max-hours", type=float, default=6.0,
                    help="stop RECORDING after this long (default 6). This "
                         "ends the capture only: a run still going is left "
                         "going on the charger under its own safety timer, "
                         "never stopped, and its meta.json records "
                         "recording_ended.")
    ap.add_argument("--outdir", default=None,
                    help="where run folders go (default: runs/ in the app "
                         "folder, whatever the working directory; one "
                         "folder per slot per run)")
    ap.add_argument("--baseline", default=None,
                    help="developers only: a protocol fingerprint file to "
                         "validate against instead of the one built in for the "
                         "firmware. Never written by this tool - a clean run's "
                         "meta.json carries its observed protocol_fingerprint.")
    ap.add_argument("--lenient", action="store_true",
                    help="downgrade unknown frame shapes to warnings")
    ap.add_argument("--stop-on-drift", action="store_true",
                    help="abort the capture if drift errors appear")
    ap.add_argument("--no-stop-on-exit", dest="stop_on_exit", action="store_false",
                    help="leave the operation running when capture ends")
    # Fresh service discovery is the default: Windows caches GATT handles per
    # adapter, and stale ones surface as "Not connected" at start_notify after
    # a dongle replug or a firmware reconnect. Re-discovering costs a second or
    # two; getting it wrong costs the run.
    ap.add_argument("--fresh", action="store_true",
                    help="bypass the Windows GATT service cache (the default; "
                         "accepted so existing commands keep working)")
    ap.add_argument("--cached", dest="fresh", action="store_false",
                    help="reuse Windows' cached GATT services. Slightly faster "
                         "to connect, but stale handles cause 'Not connected'.")
    ap.set_defaults(fresh=True)
    args = ap.parse_args()

    if args.stop_all:
        if args.start or args.attach:
            print("--stop-all starts and captures nothing; "
                  "do not combine it with --start or --attach")
            sys.exit(2)
        sys.exit(await stop_all_mode(fresh=args.fresh))

    if not args.name:
        print("--name is required (it labels the run's output directory)")
        sys.exit(2)

    if args.attach and args.start:
        print("--attach and --start are mutually exclusive")
        sys.exit(2)

    problem = resolve_start_args(args)
    if problem:
        print(problem)
        sys.exit(2)

    if args.temp_cutoff is not None:
        problem = temp_cutoff_problem(args.temp_cutoff)
        if problem:
            print(problem)
            sys.exit(2)
        if not args.temps:
            print("--temp-cutoff needs temperature readings; drop --no-temps")
            sys.exit(2)

    # Validate --cells before scanning. Capture is not built until after device
    # discovery, so without this a typo costs a 10-second scan before failing.
    try:
        parse_cells(args.cells, [int(s) for s in args.slots.split(",")])
    except ValueError as e:
        print(e)
        sys.exit(2)

    stamp_log = Log()
    dev = await find_device(stamp_log, wait_s=CONNECT_WAIT_S)
    if dev is None:
        print(f"no MC5000 found after {CONNECT_WAIT_S / 60:.1f} min of looking "
              f"- is it switched on and in range?")
        sys.exit(1)

    cap = Capture.from_args(args, stamp_log)
    # One running log per session, beside the run folders rather than in one
    # of them: a session can hold several runs, each with its own folder.
    cap.session_log = session_log_path(cap.outdir, cap.started_at, args.name)
    cap.session_log.parent.mkdir(parents=True, exist_ok=True)
    cap.log = Log(cap.session_log)
    log = cap.log

    log("=" * 68)
    log(f"CAPTURE: {args.name}")
    log(f"  slots {cap.slots}, {args.chemistry}, "
        f"{'START ' + args.start if args.start else 'ATTACH (start nothing)'}")
    log("  cells " + ", ".join(
        f"{s}={cap.cells[s] or '(unlabeled)'}" for s in cap.slots))
    if not any(cap.cells.values()):
        log("  NOTE: no --cells given; this run cannot be linked to a "
            "physical cell later")
    if args.start:
        log(f"  {args.current}mA, rated {args.capacity}mAh, "
            f"safety timer {args.safety_timer} min")
    log(f"  writing to {cap.outdir} (one folder per slot per run)")
    log(f"  session log {cap.session_log}")
    log("=" * 68)

    reason = "stopped"
    try:
        reason = await run_capture(cap, args, address=dev.address)
    except KeyboardInterrupt:
        reason = "stopped"
        log("stopped by user (Ctrl-C)")
    finally:
        log("")
        log("=" * 68)
        log(f"finished: {reason}")
        log(f"samples written : {cap.rows}")
        # Link losses, not retries - see Capture.gaps.
        log(f"reconnect gaps  : {len(cap.gaps)}"
            + (f" (link losses; {cap.reconnect_attempts} reconnect attempts)"
               if cap.reconnect_attempts else ""))
        if cap.firmware_verified is False:
            log("firmware        : UNVERIFIED (watching only, no temperature)")
        note = cap.temperature_note()
        if note:
            log(f"temperature     : {note}")
        for s, t in sorted(cap.cutoff_trips.items()):
            how = ("stopped" if s in cap.cutoff_stopped
                   else "warned only - not our run")
            log(f"temp cutoff     : slot {s} reached {t} C ({how})")
        log(f"drift errors    : {cap.drift_errors}")
        for s in cap.slots:
            # final_by_slot, not latest: stopping zeroes the counters, so
            # latest holds the aftermath rather than the result.
            st = cap.final_by_slot.get(s) or cap.latest.get(s)
            if st:
                peak = cap.peak_temp_by_slot.get(s)
                log(f"  slot {s} final: {st.voltage_v:.3f}V, device reported "
                    f"{st.capacity_mah}mAh over {st.elapsed_seconds}s"
                    + (" (last reading received: this run ended while the "
                       "link was down, so capacity and time are a floor)"
                       if cap.unwatched_end(s) else "")
                    + peak_temp_text(peak, cap.unwatched_end(s)))
        log("")
        log(cap.validator.report())

        cap.save_meta({"end_reason": reason})

        # No baseline is ever written here any more. It used to be, whenever
        # none existed and the run ended cleanly - from whatever charger was
        # connected, which let unknown firmware certify itself after one run.
        # The reference now lives in code (drift.FIRMWARE_FINGERPRINTS); what a
        # run observed is still recorded, in meta.json's protocol_fingerprint.

        for d in cap.run_dirs():
            log(f"data: {d}")
        if not cap.runs:
            log("data: none - no run was recorded")
        for run in cap.runs:
            if run.warning:
                log(f"!!! slot {run.slot}: {run.warning}")
        cap.close()
        log.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
