"""
MC5000 BLE protocol - core encode/decode logic.

Based on the reverse-engineered protocol documented at:
https://github.com/rssdev10/skyrc-mc-rs/blob/main/docs/PROTOCOL.md
(MIT; see THIRD_PARTY_NOTICES.md for what was used.)

This module is pure logic - no BLE I/O - so it can be validated against
the documented worked examples without needing the real charger connected.
"""

from dataclasses import dataclass
from enum import IntEnum


class Chemistry(IntEnum):
    LIION = 0x00
    LIION_HV = 0x01
    LIFEPO4 = 0x02
    NIMH = 0x03
    NICD = 0x04
    ENELOOP = 0x05
    NIZN = 0x06
    RAM = 0x07
    LTO = 0x08
    NAION = 0x09


CHEMISTRY_DEFAULTS = {
    Chemistry.LIION: {"target_mv": 4200, "cutoff_mv": 3200},
    Chemistry.LIION_HV: {"target_mv": 4350, "cutoff_mv": 3400},
    Chemistry.LIFEPO4: {"target_mv": 3650, "cutoff_mv": 2900},
    Chemistry.NIMH: {"target_mv": 1650, "cutoff_mv": 900},
    Chemistry.NICD: {"target_mv": 1650, "cutoff_mv": 900},
    Chemistry.ENELOOP: {"target_mv": 1650, "cutoff_mv": 900},
    Chemistry.NIZN: {"target_mv": 1900, "cutoff_mv": 1100},
    Chemistry.RAM: {"target_mv": 1650, "cutoff_mv": 900},
    Chemistry.LTO: {"target_mv": 2850, "cutoff_mv": 1800},
    Chemistry.NAION: {"target_mv": 4000, "cutoff_mv": 2000},
}


# Firmware-derived RANGE GATES for the two voltage fields (fw 2.02).
#
# The 0x94 config parser stores these verbatim, but it then calls a normalizer
# that validates EVERY numeric config field against a bound and,
# if the value falls outside, SILENTLY REPLACES IT with the per-chemistry
# default above. There is no error and no ACK difference - an out-of-range
# request is indistinguishable over BLE from one that was honored.
#
# Bounds come from a per-chemistry table (17 halfwords per
# chemistry): target_mv uses elements 0/1 as (lower, span), cutoff_mv uses
# elements 4/5. The defaults the normalizer falls back to are stored per
# chemistry and match CHEMISTRY_DEFAULTS above exactly, all 20 - which is what
# validates this reading. The Li-ion target_mv gate derived here (4050-4250)
# is also exactly the range measured on hardware, twice.
#
# This is the target_mv trap generalized: a field looks "ignored" when every
# value you ever tried happened to be out of range. Callers must clamp.
# storage_mv is the Li_Sto_Volt field (config+0xa4, table elements 8/9). A
# (0, 0) window means the chemistry has no storage target at all - every nickel
# chemistry - so any value sent there is discarded.
VOLTAGE_GATES = {
    #                        target_mv         cutoff_mv           storage_mv
    Chemistry.LIION:    {"target_mv": (4050, 4250), "cutoff_mv": (2500, 3300), "storage_mv": (3700, 3850)},
    Chemistry.LIION_HV: {"target_mv": (4250, 4400), "cutoff_mv": (2600, 3800), "storage_mv": (3850, 3950)},
    Chemistry.LIFEPO4:  {"target_mv": (3580, 3700), "cutoff_mv": (2600, 3000), "storage_mv": (3250, 3400)},
    Chemistry.NIMH:     {"target_mv": (1470, 1800), "cutoff_mv": (600, 1000),  "storage_mv": (0, 0)},
    Chemistry.NICD:     {"target_mv": (1470, 1800), "cutoff_mv": (600, 1000),  "storage_mv": (0, 0)},
    Chemistry.ENELOOP:  {"target_mv": (1470, 1800), "cutoff_mv": (600, 1000),  "storage_mv": (0, 0)},
    Chemistry.NIZN:     {"target_mv": (1200, 2000), "cutoff_mv": (900, 1600),  "storage_mv": (0, 0)},
    Chemistry.RAM:      {"target_mv": (1400, 1700), "cutoff_mv": (500, 1300),  "storage_mv": (0, 0)},
    Chemistry.LTO:      {"target_mv": (2600, 2900), "cutoff_mv": (1500, 2300), "storage_mv": (2250, 2650)},
    Chemistry.NAION:    {"target_mv": (3200, 4150), "cutoff_mv": (1500, 3500), "storage_mv": (3400, 3600)},
}


# Keep Voltage windows (mV), the field the firmware stores at config +0x40
# (RST_Volt, a 2-byte value; packet bytes 24-25). Zero means OFF, which is the
# panel's default on every chemistry. SOURCE: the manual's "Battery Voltage"
# table, row "Keep voltage". The Li-ion window is also the one the firmware
# normalizer gates +0x40 to (3980-4180, read from the image), so for Li-ion the
# manual and the image agree; the other rows are the manual's alone. Like every
# numeric field, a value outside the window is silently replaced by the firmware.
KEEP_VOLTAGE_GATES_MV = {
    Chemistry.LIION:    (3980, 4180),
    Chemistry.LIION_HV: (4080, 4330),
    Chemistry.LIFEPO4:  (3380, 3580),
    Chemistry.NIMH:     (1300, 1450),
    Chemistry.NICD:     (1300, 1450),
    Chemistry.ENELOOP:  (1300, 1450),
    Chemistry.NIZN:     (1500, 1880),
    Chemistry.RAM:      (1400, 1500),
    Chemistry.LTO:      (2580, 2830),
    Chemistry.NAION:    (3180, 3980),
}

# ...BUT ONLY THESE CHEMISTRIES TAKE IT OVER BLUETOOTH. The 0x94 parser reads
# the two bytes for every chemistry, yet its charge-mode copy block stores
# RST_Volt into the config only when the chemistry index is 0, 1, 2, 8 or 9
# (its one store sits inside that branch); NiMH, NiCd, Eneloop, NiZn
# and RAM jump past it, so the value is DISCARDED, exactly like TempCut. The
# window above is real (the normalizer gates it) but nothing ever puts a nickel
# value there from a BLE config. MEASURED on hardware on NiMH and Eneloop from
# SITREP91 (1400 mV, trickle 0 and 120: panel Keep Voltage OFF both times) and
# again from the vendor SkyCharger app (1.40 V: panel OFF, while -dV, target
# and trickle from the same screen all showed). The panel's own editor can set
# it, which is why the panel has the line. NiZn and RAM are firmware-read only.
KEEP_VOLTAGE_OVER_BLE = frozenset({
    Chemistry.LIION, Chemistry.LIION_HV, Chemistry.LIFEPO4,
    Chemistry.LTO, Chemistry.NAION,
})


class Mode(IntEnum):
    CHARGE = 0x00
    STORAGE = 0x01
    DISCHARGE = 0x02
    CYCLE = 0x03
    REFRESH = 0x04
    BREAK_IN = 0x05


# Nickel-family chemistries use a DIFFERENT mode byte numbering than lithium,
# because they have extra modes (Refresh, Break-In) that shift the values.
#
# This matters a great deal: sending mode 0x02 to a NiMH cell selects
# BREAK-IN, not Discharge. A discharge test would silently run the wrong
# operation.
#
# (This also resolves an ambiguity the protocol write-up flagged but could
# not explain: it observed the phone app sending 0x02 for a NiMH "Break-In"
# and wondered whether break-in was secretly implemented as discharge. It
# wasn't - for nickel chemistries, 0x02 simply *is* Break-In.)
#
# CONFIRMED in firmware (fw 2.02, disassembly of the 0x94 config
# parser and its predicates). The firmware gates Break-In on
# *chemistry in {NiMH, NiCd, Eneloop} AND mode-byte == 2*;
# Charge is mode 0 for every chemistry; nickel mode 1 is Refresh
# The nickel group {3,4,5} the parser tests is exactly this tuple.
# So CHARGE:0/REFRESH:1/BREAK_IN:2 below are firmware-verified for nickel, and
# lithium mode 2 = discharge (the break-in predicate is false off-nickel).
# DISCHARGE:3/CYCLE:4 for nickel have no dedicated parser branch - they're
# consistent with the phone app's encoding but decided in the run executor,
# which was not disassembled.
NICKEL_CHEMISTRIES = (
    Chemistry.NIMH,
    Chemistry.NICD,
    Chemistry.ENELOOP,
    Chemistry.NIZN,
)

_MODE_BYTE_NICKEL = {
    Mode.CHARGE: 0x00,
    Mode.REFRESH: 0x01,
    Mode.BREAK_IN: 0x02,
    Mode.DISCHARGE: 0x03,
    Mode.CYCLE: 0x04,
    Mode.STORAGE: 0x01,  # not typical for nickel
}

_MODE_BYTE_LITHIUM = {
    Mode.CHARGE: 0x00,
    Mode.STORAGE: 0x01,
    Mode.DISCHARGE: 0x02,
    Mode.CYCLE: 0x03,
    Mode.BREAK_IN: 0x02,  # unavailable for lithium; fallback
    Mode.REFRESH: 0x03,   # unavailable for lithium; fallback
}


def mode_byte_for_chemistry(mode: Mode, chemistry: Chemistry) -> int:
    """The 0x94 mode byte is chemistry-dependent. See NICKEL_CHEMISTRIES note."""
    table = _MODE_BYTE_NICKEL if chemistry in NICKEL_CHEMISTRIES else _MODE_BYTE_LITHIUM
    return table[mode]


STORAGE_VOLTAGE_MV = {
    Chemistry.LIION: 3800,
    Chemistry.LIION_HV: 3900,
    Chemistry.LIFEPO4: 3300,
    Chemistry.LTO: 2400,
    Chemistry.NAION: 3500,
}


SLOT_BITMASK = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x08}


def checksum(payload_from_cmd: bytes) -> int:
    """SUM (mod 256) of all bytes from command byte through end of data payload."""
    return sum(payload_from_cmd) % 256


def build_packet(cmd: int, data: bytes) -> bytes:
    """
    Wrap a command + data payload in the full packet structure:
    [0x0F][length][cmd][data...][checksum]
    length = len(data) + 2 (cmd byte + checksum byte)
    """
    body = bytes([cmd]) + data
    length = len(body) + 1  # +1 for checksum byte
    chk = checksum(body)
    return bytes([0x0F, length]) + body + bytes([chk])


def build_status_query(slot: int) -> bytes:
    """0x91 - request live status for a single slot."""
    channel = SLOT_BITMASK[slot]
    return build_packet(0x91, bytes([channel]))


def build_start_stop(slots: list[int] | None) -> bytes:
    """
    0x93 - start/stop, as documented. See the warning below before using it.

    slots=None or [] -> stop all (0x00)
    slots=[1,2,3,4] -> combined bitmask covering all given slots

    *** DOES NOT STOP A RUN ON FW 2.02. ***

    Measured on hardware with the probe_stop probe against a live 500mA
    discharge, on a connection that had sent the init sequence, using both the
    documented stop-all channel 0x00 and the slot's own bitmask. Neither had any
    effect: the readings after were indistinguishable from a control that sent
    nothing at all.

    So the documented "REPLACES the entire active slot set" semantics do not
    hold here, in either direction. What actually stops a run is the init
    sequence - which stops every slot at once. capture.py's stop_all_guarded
    uses that, and refuses when a slot it did not start is busy.

    Kept because it is what the protocol write-up specifies, it may work on
    other firmware, and the probe_stop probe needs it to keep testing.
    """
    if not slots:
        channel = 0x00
    else:
        channel = 0
        for s in slots:
            channel |= SLOT_BITMASK[s]
    return build_packet(0x93, bytes([channel]))


def build_config(
    slot: int,
    mode: Mode,
    chemistry: Chemistry,
    charge_current_ma: int,
    discharge_current_ma: int,
    capacity_mah: int,
    target_mv: int | None = None,
    cutoff_mv: int | None = None,
    storage_mv: int | None = None,
    charge_cutoff_current_ma: int = 100,
    discharge_cutoff_current_ma: int = 100,
    max_time_min: int = 300,
    cutoff_timer_min: int = 0,
    temp_cut_c: int = 60,
    delta_peak_mv: int = 6,
    trickle_ma: int = 0,
    keep_mv: int = 0,
) -> bytes:
    """
    0x94 - configure a slot. Must be sent (and ACKed) before that slot can be
    included in a 0x93 start command.

    delta_peak_mv (-dV, packet byte 22, firmware Chg_Peak), trickle_ma (byte 23,
    sent as mA / 10, firmware Ni_Trickle) and keep_mv (bytes 24-25, firmware
    RST_Volt) are the nickel-charge and keep-voltage settings the front panel
    shows. Their DEFAULTS (6, 0, 0) are what this function always sent before
    they were parameters, so a config that does not name them is byte-for-byte
    what it was. 0 means OFF for trickle and keep voltage, as on the panel. Not
    yet confirmed over BLE: that the panel shows a value sent here. Range policy lives in capture.RunRequest.problem(), not
    here - this function only refuses what cannot be encoded.

    cutoff_timer_min is a SAFETY TIMEOUT enforced by the charger itself:
    the operation stops after this many minutes regardless of what the
    controlling software does. 0 = disabled. Worth setting for any test
    where you are not certain the software can reliably stop the operation.

    *** target_mv SETS the Li-ion charge ceiling over BLE - but ONLY for
    IN-RANGE values (4050-4250 mV). Out-of-range values are silently dropped to
    the per-chemistry default. CLAMP CALLERS TO 4050-4250. ***
    This corrects a long-standing "silently ignored / panel-reserved" verdict
    that turned out to be an out-of-range testing artifact: every earlier "it's
    ignored" test used a value BELOW the panel's 4.05V floor.

    DECISIVE in-range test on hardware: a BLE charge with
    target_mv=4100 (TEST-01, 35E, 1500mA) held flat CC to a 4.100V knee, then
    CV-tapered (1496 -> ~168mA) with voltage PINNED AT 4.100V for the whole ~56min
    CV phase, self-terminating at 555mAh/3833s. The knee landed on the requested
    millivolt - target_mv IS honored over BLE when in range.

    The old out-of-range measurements, now understood as the firmware range-gating
    (out-of-range -> discard, fall back to per-chemistry flash default): target_mv=3800
    sailed past 3.97V at full current, no taper; target_mv=3500
    climbed straight through 3.500V at a flat 996mA, still ACTIVE at
    3.530V. Both below the 4.05V floor -> both dropped to the ~4.2V default.

    WHY, at the code level: the charge executor / state machine (reads
    does NOT read config +0x22 as a raw setpoint (that disasm trace
    was correct, and is why OUT-OF-RANGE values do nothing). The in-range result
    means a normalizer/parser must BRIDGE +0x22 -> the runtime charge-voltage
    setpoint the loop does read, gated on a range check - the SAME runtime setpoint
    the front panel's Target-Voltage input writes (a panel charge set to 4.05V
    regulated to 4.050V exactly). That bridge is inferred from hardware
    behavior; the exact normalizer/runtime variable is not yet pinned in the disasm.
    Corrected offset map, disasm-settled: target_mv = +0x22,
    DChg_Cur = +0x68, End_Cur = +0x7c, End_DCur = +0x90. (The comparator
    reads +0x90 = End_DCur, a CURRENT - the DISCHARGE cutoff-current comparator,
    hardware-confirmed, NOT a charge-voltage CV comparator; an earlier
    note here mislabeled it as config +0x90 = Chg_Volt-10, retracted.)

    The per-chemistry flash default still governs when target_mv is omitted or
    out-of-range (measured on hardware): a charge with NO target_mv kneed at the
    4.200V default and CV-tapered to ~93mA against the 100mA charge_cutoff_current_ma.
    Untested: in-range targets on other chemistries; the exact upper edge (does
    4.25 hold, 4.30 clamp-or-drop?); the normalizer bridge in the disasm.

    charge_cutoff_current_ma (End_Cur, packet bytes 15-16 -> config +0x7c) IS
    honored over BLE - unlike target_mv, the firmware acts on it. Confirmed
    on hardware: the same 30Q, same ~4.08V start and 1000mA, terminated at ~286mA
    / 203mAh with End_Cur=300 vs ~93mA / 383mAh with End_Cur=100 - the CV-taper
    termination current tracks the sent value. It is a real, working per-run
    control. Its discharge twin discharge_cutoff_current_ma (End_DCur, config
    +0x90) is likewise honored (hardware-confirmed: discharge CV-taper
    self-terminated at ~99mA vs ~400mA for sent 100 vs 400).

    NOTE: this is about target_mv on a plain CHARGE. Storage MODE (0x01) is a
    different story - measured on hardware, it DOES drive a cell toward ~3.8 V
    from either side and idles at the target (a full cell discharged down; a
    cell at 3.79 V was left alone; low cells charged up). So "park at storage
    voltage" is viable via Storage mode, unlike target_mv. Still open: whether
    the storage-voltage field (offset 33-34) sets the target or the device
    uses a built-in 3.8 V default - test with a deliberately odd value.
    """
    # TempCut (config +0xe0) is parsed, printed in a trace and DISCARDED by
    # fw 2.02 - never copied into the config struct.
    # The parameter exists only to test that on the bench; 60 keeps every
    # existing config byte-identical. Nothing above 60 is ever sent.
    if not (1 <= temp_cut_c <= 60):
        raise ValueError(f"temp_cut_c {temp_cut_c} outside 1-60 C")
    if not (0 <= delta_peak_mv <= 255):
        raise ValueError(f"delta_peak_mv {delta_peak_mv} does not fit a byte")
    if trickle_ma < 0 or trickle_ma % 10 or trickle_ma // 10 > 255:
        raise ValueError(f"trickle_ma {trickle_ma} must be a multiple of 10 "
                         f"mA that fits a byte after dividing by 10")
    if not (0 <= keep_mv <= 0xFFFF):
        raise ValueError(f"keep_mv {keep_mv} does not fit two bytes")
    defaults = CHEMISTRY_DEFAULTS[chemistry]
    if target_mv is None:
        target_mv = defaults["target_mv"]
    if cutoff_mv is None:
        cutoff_mv = defaults["cutoff_mv"]

    channel = SLOT_BITMASK[slot]

    def be16(v):
        return bytes([(v >> 8) & 0xFF, v & 0xFF])

    data = bytes([channel, mode_byte_for_chemistry(mode, chemistry)])
    data += be16(charge_current_ma)
    data += be16(discharge_current_ma)
    data += be16(capacity_mah)
    data += be16(target_mv)
    data += be16(cutoff_mv)
    data += be16(charge_cutoff_current_ma)
    data += be16(discharge_cutoff_current_ma)
    data += be16(10)  # charge resting time (min) - every worked example in the doc uses 10, regardless of mode/chemistry; mirror it rather than guess
    data += be16(10)  # discharge resting time (min) - same
    data += bytes([1])  # cycle count - default 1 (firmware: Cyc_Count)
    data += bytes([0])  # cycle direction - C->D default (firmware: Cyc_Mode; note the firmware remaps this byte 1<->2 internally, so only 0/3 pass through unchanged)
    data += bytes([delta_peak_mv])  # delta-peak mV - default 6, which every worked example uses even for Li-Ion; likely ignored by firmware for non-nickel chemistries, but mirror the known-good value rather than assume (firmware: Chg_Peak)
    data += bytes([trickle_ma // 10])  # trickle charge (sent as mA / 10) - default 0 = off (firmware: Ni_Trickle)
    data += be16(keep_mv)  # keep voltage - default 0 = off (firmware: RST_Volt, a 2-byte value; window per chemistry in KEEP_VOLTAGE_GATES_MV)
    data += bytes([temp_cut_c])  # firmware: TempCut - a TEMPERATURE cutoff of 0x3C = 60 (degC), not a magic constant. Confirmed by disassembly of the 0x94 parser (fw 2.02). 60C is a sane default; left as-is.
    data += be16(cutoff_timer_min)  # firmware: TimeCut - THE charger-enforced safety timer (min), 0 = no timer
    data += be16(max_time_min)  # NOT READ by the fw-2.02 0x94 config parser (it skips these two bytes). Kept for byte-compat with the reference impl / other firmware, but do not rely on it: cutoff_timer_min (TimeCut) above is the only time backstop the parser ingests.
    data += bytes([0x00])  # chemistry prefix, always 0 (also skipped by the parser)
    data += bytes([chemistry])  # firmware reads this (packet offset 0x23) FIRST, as the per-chemistry selector for every field branch
    # secondary value: storage voltage for lithium-class, else 110% capacity
    # (firmware: Li_Sto_Volt - the lithium storage-mode target voltage field)
    if chemistry in NICKEL_CHEMISTRIES or chemistry == Chemistry.RAM:
        # The firmware gates this field to (0, 0) on nickel, so whatever goes
        # here is discarded; kept as-is for byte-compat with the reference impl.
        data += be16(int(capacity_mah * 1.1))
    elif storage_mv is not None:
        data += be16(storage_mv)
    else:
        data += be16(STORAGE_VOLTAGE_MV.get(chemistry, capacity_mah))
    data += bytes(5)  # padding; note the FIRST pad byte is read by the parser as Ene_lopp_type (eneloop sub-type) - sending 0 selects the default

    return build_packet(0x94, data)


# --- Status response decoding (0x91 response) ---


# --- 0x91 status frame: the byte map, read out of the firmware builder ---
#
# Firmware 2.02 builds the frame. Every byte it writes, in order
# (index == frame offset; the builder starts its running index at 4):
#
#   0      0x0F                      6-7    voltage    (runtime+0x12, BE16)
#   1      length (0x16 = 22)        8      constant 0
#   2      0x91                      9      runtime+0x576 (byte, unidentified)
#   3      1 << slot (bitmask)       10-11  capacity   (runtime+0x1a, BE16)
#   12-15  elapsed (runtime+0x24, BE32)     16-17  resistance (runtime+0x3a, BE16)
#   18     slot run-status enum      19     mode, chemistry-encoded
#   20     alert/error code          21     chemistry (config+1)
#   22     a device-global byte      23     checksum
#
# Voltage is gated in the builder: a raw reading <= 200 mV is reported as 0.
#
# TWO CORRECTIONS THIS YIELDS over the older "offsets 0-17, rest is padding"
# reading:
#   1. elapsed is THIRTY-TWO bits at 12-15, not sixteen at 14-15. Reading only
#      the low half wraps at 65535 s (18h12m) - reachable by a NiMH break-in or
#      a long charge. Bytes 12-13 are 0 in every frame we have, which is exactly
#      what a short run should look like.
#   2. bytes 18-22 are NOT padding. They carry the device's own view of the run.
#
# NOT FOUND, and we went looking: there is no temperature VALUE anywhere in the
# frame. The charger has an internal sensor (the panel raises "Int. Temp.Too
# High!"), but it is only ever surfaced as an alert CODE in byte 20, never as a
# number. Free temperature telemetry over 0x91 does not exist on this firmware.
ALERT_NONE = 0

# Byte 20 is produced by mapping an internal per-slot alert state (runtime+0x3f)
# through a 12-entry jump table:
#
#     internal +0x3f : 0  1  2  3  4  5  6  7  8  9  10 11   >=12
#     wire byte 20   : 0  1  2  9 10  4  5  6  6 11  12 13    0
#
# TWO codes are PROVEN from the firmware, by reading the protection checks that
# raise them - each compares a measured value against a limit derived from the
# config, then calls the per-slot teardown and sets the alert state:
#
#   internal 5 -> wire 4  CAPACITY LIMIT. The protection check compares measured capacity
#       against runtime+0x1c, which the arming routine fills from
#       config+0x02 (Capacity) - x1.2 on one branch, verbatim on the other.
#   internal 6 -> wire 5  TIME LIMIT. The protection check compares elapsed (runtime+0x24)
#       against config+0x108[chem] (TimeCut) x 60, i.e. minutes to seconds.
#       This is the safety timer, in the disassembly - and it is the one code
#       also CONFIRMED ON HARDWARE:
#       a --safety-timer 5 discharge reported byte 20 == 5 -> "time limit" in
#       the very frame the counters reset, and kept reporting it on every
#       latched frame until the explicit stop cleared it.
#
# Those two anchors suggested an alignment to the panel's own message list (a
# newline-separated block, whose ORDER is firmware ground
# truth): wire code N is list entry N-1. THAT ALIGNMENT DOES NOT HOLD PAST 5 -
# see the two hardware sightings below - so no name is taken from it any more.
#
# (An earlier version of this table assumed a 1-based alignment and was off by
# one - it called wire 4 "time limit". The capacity check disproves that.)
#
# Codes 3, 7 and 8 are never produced by the jump table, so those panel
# messages are presumably raised by a path that does not reach the wire.
#
# THIRD PROVEN CODE, AND IT BREAKS THE MODEL ABOVE: wire 13 = FULLY CHARGED,
# seen on hardware: a
# charge to --target-mv 4100 sent to a cell resting at ~4.19 V ended at once,
# byte 20 read 13, and the panel said "Fully Charged!". So "a 12-entry table,
# >=12 = no alert" is incomplete - the table reading, or the path that sets
# byte 20, has more to it than the disassembly showed. 10 used to be named
# "fully charged" from the panel-list ordering; it is now unnamed, since
# that ordering demonstrably does not hold past 9. 10-12 are unknown.
#
# FOURTH PROVEN CODE, AND IT DISPROVES THE LIST ORDER FOR GOOD: wire 9 = CONNECTION
# BREAK, seen on hardware: a cell pulled
# from a running slot made the panel say "CH1 Connection Break!" while byte 20
# read 9, which this table then called "reversed polarity" - a wrong message
# for the commonest thing a user does. The old list order gave "connection
# break" as wire 2 and "reversed polarity" as wire 9, so a name taken from that
# order cannot be trusted anywhere above 5.
#
# So ONLY PROVEN CODES HAVE NAMES: 4, 5, 9, 13. Every other non-zero code is
# reported as "alert code N" rather than as a guess that reads like a fact. The
# panel-order candidates, for whoever proves them (each needs its own bench
# sighting): 1 input voltage, 2 connection break (now known to be 9), 3 battery
# type error, 6 internal temperature too high, 7 battery temperature too high
# (the 70 C ERR_OVER_B_TEM trip), 8 over load, 9 reversed polarity (now known
# to be wrong).
ALERT_NAMES = {
    0: "none",
    4: "capacity limit",                   # PROVEN (capacity check -> teardown)
    5: "time limit",                       # PROVEN in fw AND on hardware
    9: "connection break",                 # PROVEN on hardware
    13: "fully charged",                   # PROVEN on hardware
}


@dataclass
class SlotStatus:
    slot: int
    current_ma: int
    voltage_v: float
    capacity_mah: int
    elapsed_seconds: int
    resistance_mohm: int
    state: str
    checksum_ok: bool
    frame_len: int
    raw: bytes
    # --- the device's own view of the run (frame bytes 18-21) ---
    # All None on a frame too short to carry them. These are REPORTED, not
    # acted on: `state` is still inferred exactly as before, deliberately, because
    # the enum semantics below are not yet confirmed against hardware and the
    # teardown-latch logic keys on `state`.
    device_status: int | None = None   # byte 18: 0 idle/unconfigured, 2/3 running, 4/5/6 other
    mode_code: int | None = None       # byte 19: mode, in the same chemistry-dependent
                                       #          encoding build_config sends
    alert_code: int | None = None      # byte 20: 0 = none; see ALERT_NAMES
    chemistry_code: int | None = None  # byte 21: the slot's chemistry (config+1)

    # NOTE: the alert SURVIVES the per-slot teardown, and that is deliberate in
    # the firmware - the protection paths call the teardown FIRST and set the alert
    # state after, so a torn-down slot keeps reporting why it stopped until an
    # explicit stop clears it. Measured on hardware: the alert appeared in the
    # same frame the counters reset and persisted across every latched frame.
    @property
    def alert(self) -> str | None:
        """The alert name, or None if the frame carried no alert byte."""
        if self.alert_code is None:
            return None
        return ALERT_NAMES.get(self.alert_code, f"alert code {self.alert_code}")

    @property
    def is_active(self) -> bool:
        return self.state in ("charging", "discharging")


def verify_checksum(raw: bytes) -> bool:
    """
    Checksum is SUM (mod 256) of bytes from index 2 up to (not including)
    the final byte. Confirmed against live hardware on 24-byte frames.
    """
    if len(raw) < 4:
        return False
    return sum(raw[2:-1]) % 256 == raw[-1]


def infer_state(voltage_mv: int, current_ma: int, capacity_mah: int, elapsed_s: int, mode_hint: str | None) -> str:
    """
    Work out what a slot is doing from the physical values.

    The protocol doc's "status byte" table is superseded - bytes 4-5 are a
    single big-endian current value, not a status code plus a multiplier
    (confirmed on live hardware: 0x05db = 1499mA for a 1500mA discharge).
    So there is no dedicated status field to read; state has to be inferred.
    """
    if voltage_mv == 0:
        return "empty"
    if current_ma > 0:
        # We can't tell charge from discharge by current alone - the caller
        # supplies mode_hint when it knows (e.g. it issued the config), and
        # otherwise we report the neutral "active".
        if mode_hint == "charge":
            return "charging"
        if mode_hint == "discharge":
            return "discharging"
        return "active"
    if elapsed_s > 0 and capacity_mah > 0:
        return "done"
    return "idle"


def parse_status_response(raw: bytes, mode_hint: str | None = None) -> SlotStatus:
    """
    Parse a 0x91 status response.

    Handles both the 23-byte frames documented in the protocol write-up and
    the 24-byte frames observed on FW 2.02. Offsets 0-17 are identical in
    both. Bytes 18-21 (device status, mode, alert, chemistry) are read only
    from 24-byte frames; a shorter frame leaves them None.
    """
    if len(raw) < 18:
        raise ValueError(f"Response too short to parse: {len(raw)} bytes")

    channel = raw[3]
    slot = {v: k for k, v in SLOT_BITMASK.items()}.get(channel, 0)

    current_ma = (raw[4] << 8) | raw[5]
    voltage_mv = (raw[6] << 8) | raw[7]
    capacity_mah = (raw[10] << 8) | raw[11]
    # 32-bit, big-endian: the builder emits all four bytes (see the byte map
    # above). Reading only 14-15 wraps at 18h12m.
    elapsed_seconds = (raw[12] << 24) | (raw[13] << 16) | (raw[14] << 8) | raw[15]
    resistance_mohm = (raw[16] << 8) | raw[17]

    # Bytes 18-21 exist on the 24-byte frames this firmware sends. On the
    # shorter 23-byte frame the checksum sits at 22 and those bytes are laid
    # out differently, so reading them would invent a status, mode, alert and
    # chemistry: below 24 bytes they stay None.
    has_extras = len(raw) >= 24

    return SlotStatus(
        slot=slot,
        current_ma=current_ma,
        voltage_v=voltage_mv / 1000,
        capacity_mah=capacity_mah,
        elapsed_seconds=elapsed_seconds,
        resistance_mohm=resistance_mohm,
        state=infer_state(voltage_mv, current_ma, capacity_mah, elapsed_seconds, mode_hint),
        checksum_ok=verify_checksum(raw),
        frame_len=len(raw),
        raw=raw,
        device_status=raw[18] if has_extras else None,
        mode_code=raw[19] if has_extras else None,
        alert_code=raw[20] if has_extras else None,
        chemistry_code=raw[21] if has_extras else None,
    )


# --- Init sequence (a stop-everything command on FW 2.02) ---
#
# Transcribed from charger-cli's MC5000Protocol::send_init_sequence.
#
# On FW 2.02 init STOPS EVERY SLOT: its last command is 0xFE with channel 0
# (all slots). It is not needed for anything else - a 0x94 config starts a
# slot without it, and 0x93 does nothing with or without it. So send it only
# when you mean to stop every slot; to stop one, use build_slot_stop.
#
# The reference implementation sleeps 200ms between each command.

def build_version_query() -> bytes:
    """
    0x74 - documented as "version/info request", but NOT the firmware version.
    Measured on hardware on a unit whose panel reads FW 2.02 / HW 2.00: this
    answers 01 00 regardless. Most likely a protocol version. The firmware
    version comes from 0x57 - see build_info_query.
    """
    return build_packet(0x74, bytes(5))


# The 0x57 device-info request, replayed VERBATIM from the worked example in
# skyrc-mc-rs's PROTOCOL.md. The payload carries opaque token bytes
# (ff 34 37 38 c1 a4 ...) whose meaning is unknown; measured on hardware, a
# FW 2.02 unit accepts the 1.82-era example unchanged, so the token is not
# device-specific. Do not "clean up" the zeros or recompute anything - this
# exact frame is the one proven to work.
_INFO_QUERY = bytes.fromhex(
    "0f1357" "00ff343738c1a4" "00000000000000000000" "5e")
assert len(_INFO_QUERY) == _INFO_QUERY[1] + 2
assert sum(_INFO_QUERY[2:-1]) % 256 == _INFO_QUERY[-1]


@dataclass
class DeviceInfo:
    """What 0x57 reveals: the identity the firmware gate keys on."""

    # SkyRC's PRODUCT number, not a unit serial: the MC5000 retails as
    # SK-100214 and every MC5000 answers 100214 (the MC3000 answers 100083 =
    # SK-100083; the TLD001 reads 500043 = SK-500043). So it tells a model from
    # a model and carries nothing per-unit; the real serial is on the back
    # label and is in no frame. Older meta.json files and fingerprints call it
    # `serial`.
    product_code: str
    firmware: str   # e.g. "2.02" - matches the panel's FW Ver display
    hardware: str   # e.g. "2.00"


def build_info_query() -> bytes:
    """0x57 - device-info request: SkyRC's product code (NOT a unit serial -
    see DeviceInfo), plus firmware and hardware versions."""
    return _INFO_QUERY


def parse_info_response(raw: bytes) -> DeviceInfo:
    """
    Decode a 0x57 response.

    Layout, verified against two different units (a FW 1.82 one in the
    skyrc-mc-rs doc, and the FW 2.02 bench unit):

        0f 13 57 00 <ascii product code...> 04 <zeros...> FJ FN HJ HN ?? CK

    The product code is a run of ASCII digits starting at offset 4, terminated
    by a non-digit. The version bytes sit at fixed offsets from the END - its
    length varied between the two units (7 digits vs 6), so end-relative is
    the only anchoring that fits both. FJ.FN is firmware (01 52 -> "1.82",
    02 02 -> "2.02"), HJ.HN is hardware. The byte before the checksum is
    unknown (0x80 on one unit, 0xce on the other).
    """
    if len(raw) < 12 or raw[2] != 0x57:
        raise ValueError(f"not a 0x57 info response: {raw.hex()}")
    i = 4
    digits = []
    while i < len(raw) and 0x30 <= raw[i] <= 0x39:
        digits.append(chr(raw[i]))
        i += 1
    return DeviceInfo(
        product_code="".join(digits),
        firmware=f"{raw[-6]}.{raw[-5]:02d}",
        hardware=f"{raw[-4]}.{raw[-3]:02d}",
    )


def build_settings_query() -> bytes:
    """0x65 - settings query."""
    return build_packet(0x65, bytes(2))


def build_fe_query(channel: int = 0x00) -> bytes:
    """0xFE - slot presence/readiness query. channel 0x00 = general."""
    return build_packet(0xFE, bytes([channel]))


def build_slot_stop(slot: int) -> bytes:
    """
    0xFE addressed to ONE slot's bitmask - the per-slot STOP, on FW 2.02.

    Measured on hardware (the probe_slot_stop probe): with two slots both
    discharging, this took the target slot to 0 mA while the other kept running.
    It needs NO init and disturbs no other slot - the opposite of the init
    sequence, which stops every slot at once.

    The command is the same 0xFE the init sequence ends with; init sends it as
    0xFE(0x00) = all slots, while a single slot's bitmask stops just that slot.
    So `build_slot_stop` is `build_fe_query(SLOT_BITMASK[slot])` with an honest
    name - the byte-identical query is a stop when it carries a slot address.
    (`build_start_stop` / 0x93, the documented stop, does nothing here.)
    """
    return build_fe_query(SLOT_BITMASK[slot])


def init_sequence() -> list[bytes]:
    """
    The three commands, in order, that unlock control commands.
    Send each with ~200ms between them.
    """
    return [build_version_query(), build_settings_query(), build_fe_query()]


# --- 0x55: per-slot TEMPERATURE (confirmed on hardware) ---
#
# *** CONFIRMED ON HARDWARE: byte 14 tracks the cell's temperature. ***
#
# First real frame, slot 1, TEST-01 on a 500 mA discharge at 673 s:
#   0f 22 55 01 03 00 5d 02 a1 0e 75 01 eb 00 22 00 00 00 00 03 00 03 00 9f
#   00 00 00 00 00 00 00 00 00 01 01 91
# 36 bytes, checksum valid, and THREE fields agree to the digit with a 0x91
# polled seconds earlier: capacity 93 mAh, voltage 3.701 V, elapsed 673 s
# against 672 s. The map is right.
#
# Two probes, 52 minutes apart, settle what byte 14 is:
#
#     device elapsed   byte 14   TLD001 on the cell   room (probe B)
#       673 s            34          29.5 C              26.1 C
#      3769 s            37          33.1 C              24.7 C
#
# Byte 14 rose +3 while the probe rose +3.6 - WHILE THE ROOM COOLED 1.4 C. That
# is the discriminator: a field tracking ambient or the chassis would have
# fallen. It tracked the cell.
#
# The firmware truncates (sdiv by 10), so 34 means [34,35) and 37 means [37,38);
# both mid-run points sit 4-5 C above a probe taped to the outside of the cell.
# That offset is NOT constant: measured from the start of a run it opens at about
# 2 C above the cell and ramps over roughly 25 minutes to about 5 C once the run
# is warm, where it plateaus. That is what a thermistor in the bay, pressed
# against the cell inside a chassis that warms as the run goes on, should look
# like.
#
# THAT OFFSET IS DELIBERATELY NOT APPLIED HERE. Same policy as the Govee and the
# TLD001: correct at analysis time, never in the raw record. Neither instrument
# is calibrated, so the offset's sign is established and its attribution is not.
#
# UNEXPLAINED: bytes 11-12 read 491 on the first frame and 506 on the second,
# while 0x91 reported 502 mA both times. The builder computes current+5 there,
# so the raw values would be 486 and 501 - the second is within ripple of 502,
# the first is not. Consistent with current sampled at a different instant, but
# not established. Left deliberately unnamed - see the target_mv saga.
#
# Read out of the firmware 2.02 dispatcher. The 0x91 frame carries
# no temperature value at all (see the byte map above, and it is firmware-
# confirmed), but a SECOND per-slot builder exists, reached from
# the dispatcher when the opcode is 0x55, and it reports the temperature of the
# cell in the bay.
#
# WHY IT IS BELIEVED TO BE BATTERY TEMPERATURE, not a board sensor: Charger_Main
# reads the same array and, above 0x2BC
# (70.0 C), calls the per-slot teardown and emits the firmware's own
# trace string "ERR_OVER_B_TEM %d". B_TEM = battery temperature. The array is
# written only by Sys_Check, from seven ADC channels through an NTC lookup
# whose output is tenths of a degree C; the 0x55 builder divides by 10.
#
# WHY NOBODY ELSE HAS IT: no shipping software appears to send 0x55 to an
# MC5000. Both public reverse-
# engineering efforts (rssdev10/skyrc-mc-rs, which is a capture of the SkyRC
# app, and kolinger/skyrc-mc3000) use only 0x91 and both give up on temperature
# - skyrc-mc-rs ships `temperature: 0, // Temperature not available in this
# protocol response`. kolinger's MC3000 module DOES use 0x55 (BATTERY_INFO, its
# only opcode, with a temperature byte), which corroborates that 0x55 is a READ
# in this product family - but not the MC5000's byte offsets.
#
# Why it is safe to send: the builder's every store lands in the transmit
# buffer, it touches no slot state, and the slot index is bounds-checked. 0x55
# emits the LONGEST frame the firmware produces (36 bytes, against 24 for 0x91),
# but the transmit buffer is 128 bytes with 328 bytes of clearance after it, read
# out of the image's scatter-load table, so it cannot overrun. It has also been
# sent on every verified run with no slot disturbed.
#
# --- 0x55 response frame: the byte map, read out of the builder ---
#
#   0      0x0F                      9-10   voltage   (runtime+0x12, BE16)
#   1      length (0x22 = 34)        11-12  current-ish (see below, BE16)
#   2      0x55                      13     never written by any path read
#   3      1 << slot (bitmask)       14     TEMPERATURE, whole degrees C
#   4      run-status enum           15-16  hardcoded 0
#   5-6    status-dependent, below   17-28  NOT WRITTEN - stale, see below
#   7-8    elapsed (runtime+0x24, BE16)     29-32  zeroed when chemistry < 4
#                                    33     hardcoded 1
#                                    34     mode 0/1/2 from runtime+1
#                                    35     checksum
#
# NOTE elapsed here is SIXTEEN bits, unlike the 32-bit field at 0x91 bytes
# 12-15, so it wraps at 65535 s (18h12m) - reachable by a break-in or a long
# charge. Do not use a 0x55 frame as an elapsed source on a long run.
#
# CORRECTION (from the first real frame): the zeroing loop covers
# bytes 29-32, NOT 17-28 as first written here. The loop runs r5 = 0..7 but
# only stores when r5 >= 6, at offsets 0x11+2*r5 and 0x12+2*r5 - i.e. 29, 30,
# 31, 32. Bytes 17-28 are written by NO path, and the captured frame proves it:
# they came back as 00 00 03 00 03 00 9f 00 00 00 00 00, which is leftover from
# an earlier frame in the shared transmit buffer. That is the uncleared-buffer
# hazard below, observed rather than predicted.
#
# Bytes 5-6 depend on the run status, via a jump table:
#   status 1/2/3 (running) -> capacity (runtime+0x1a, BE16), AND byte 14 is filled
#   status 4/5             -> two table-mapped fault codes; byte 14 NOT filled
#   status 0 or >= 6       -> the payload is skipped entirely
#
# Observed on hardware: a 500 mA discharge reports status 3, a charge reports 2
# (from the run archive), so both modes fill byte 14.
#
# *** THE BUILDER DOES NOT CLEAR THE BUFFER. *** Bytes it does not write on a
# given path keep whatever the PREVIOUS frame left there. So byte 14 on a
# status-4 frame is stale data from an earlier 0x55, not a reading. This is why
# temperature_c is None unless device_status is 1, 2 or 3 - do not relax that.
TEMP_FILLED_STATUSES = (1, 2, 3)


def build_temperature_query(slot: int) -> bytes:
    """
    0x55 - per-slot temperature. Confirmed on hardware; see the notes above.

    Framing is identical to build_status_query with one byte changed, which is
    not an assumption: the dispatcher runs the slot byte of BOTH 0x91 and 0x55
    through the same bitmask->index converter, and kolinger's
    independent MC5000 client builds 0x91 the same way (bitmask, not index).
    """
    return build_packet(0x55, bytes([SLOT_BITMASK[slot]]))


@dataclass
class SlotTemperature:
    """A decoded 0x55 response. Temperature, status, voltage, elapsed and
    capacity are confirmed against hardware; the current-like field (bytes
    11-12) is not decoded here because its meaning is not established."""
    slot: int
    device_status: int
    temperature_c: int | None   # None unless device_status is in TEMP_FILLED_STATUSES
    voltage_v: float
    elapsed_seconds: int
    capacity_mah: int | None    # None unless the running branch filled bytes 5-6
    mode_code: int
    checksum_ok: bool
    frame_len: int
    raw: bytes

    @property
    def trustworthy(self) -> bool:
        """Whether this frame actually carries a reading, as opposed to
        whatever the previous frame left in the buffer."""
        return self.checksum_ok and self.temperature_c is not None


def parse_temperature_response(raw: bytes) -> SlotTemperature:
    """
    Decode a 0x55 response. Raises ValueError if it is not one.

    Deliberately conservative: `temperature_c` is None unless the run status
    says the builder actually took the branch that fills byte 14, because the
    firmware never clears the transmit buffer between frames.
    """
    if len(raw) < 15 or raw[2] != 0x55:
        raise ValueError(f"not a 0x55 temperature response: {raw.hex()}")

    channel = raw[3]
    slot = {v: k for k, v in SLOT_BITMASK.items()}.get(channel, 0)
    device_status = raw[4]
    running = device_status in TEMP_FILLED_STATUSES

    return SlotTemperature(
        slot=slot,
        device_status=device_status,
        temperature_c=raw[14] if running else None,
        voltage_v=((raw[9] << 8) | raw[10]) / 1000,
        elapsed_seconds=(raw[7] << 8) | raw[8],
        capacity_mah=((raw[5] << 8) | raw[6]) if running else None,
        mode_code=raw[34] if len(raw) > 34 else 0,
        checksum_ok=verify_checksum(raw),
        frame_len=len(raw),
        raw=raw,
    )
