"""
Validate capture.py's frame handling - no charger, no Bluetooth.

WHAT THIS COVERS
----------------
Capture.on_notify is the single point every parsed frame passes through, and
it does four jobs at once: drift accounting, transition-artifact tagging, CSV
writing, and finished-slot detection. Those interact, and none of them are
exercised by test_protocol.py or test_drift.py, which stop at the parse.

So: hand-build a fixed sequence of FW-2.02 status frames, push them straight
into on_notify, and compare the resulting samples.csv against a known-good
copy recorded from the pre-subscriber implementation.

The frame script deliberately includes the awkward cases - a slot that was
never requested, a corrupt frame, the stop-time transition artifact, and an
idle streak long enough to mark a slot finished.

DETERMINISM
-----------
capture.datetime is replaced with a fake clock advancing exactly one second
per call, so wall_time and elapsed_s are reproducible.

Note this couples the expected timestamps to the NUMBER of datetime.now()
calls, which includes the ones inside Log. If you add or remove a log line in
capture.py, the timestamp column shifts and the strict check below fails while
the data check still passes. That combination means "logging changed", not
"data broke" - update EXPECTED_CSV and move on.
"""

import csv
import io
import json
import sys
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

try:
    import capture
    from protocol import Chemistry
except ModuleNotFoundError as e:
    print(f"cannot import capture.py: {e}")
    print("capture.py needs bleak even for offline tests: pip install bleak")
    sys.exit(1)

# The per-charger firmware record lives in settings.ini in the app folder
# (runfiles.py). No test may touch the real one, so point it at a scratch file
# for the whole run.
import runfiles                                              # noqa: E402
_SETTINGS_DIR = tempfile.TemporaryDirectory()
runfiles.SETTINGS_PATH = Path(_SETTINGS_DIR.name) / "settings.ini"


results = []


def assert_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"    {detail}")
    results.append(bool(cond))
    return cond


# A capture writes one folder per slot per run (runfiles.py),
# not one samples.csv. These read a capture's record back.

def csv_text(cap):
    """Every run's rows merged back into arrival order (wall_time, which the
    fake clock makes unique), as one CSV text in CSV_COLUMNS order. elapsed_s
    is each row's OWN run's elapsed time."""
    rows = []
    for run in cap.runs:
        with open(run.csv_path, encoding="utf-8", newline="") as fh:
            rows.extend(csv.DictReader(fh))
    rows.sort(key=lambda r: r["wall_time"])
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=capture.CSV_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def meta_of(cap, slot=None):
    """The meta.json of the slot's latest run - the lowest slot's when none is
    named (for fields every run records alike, such as chemistry)."""
    if slot is None:
        slot = min(r.slot for r in cap.runs)
    runs = [r for r in cap.runs if r.slot == slot]
    return json.loads(Path(runs[-1].meta_path).read_text(encoding="utf-8"))


# --- known-good output, recorded before the subscriber refactor ---
# Timing columns regenerated for one folder per run: each run now has its
# own file whose elapsed_s starts at 0, and opening a run logs a line (which
# consumes fake-clock ticks). Every data column is unchanged from the
# original recording - checked before regenerating.

EXPECTED_CSV = """\
wall_time,elapsed_s,slot,voltage_v,current_ma,device_capacity_mah,device_elapsed_s,resistance_mohm,state,checksum_ok,drift_severity,note,device_status,device_mode,device_alert,slot_temp_c
2026-08-15T12:00:03,0.0,1,4.05,1500,25,60,45,active,True,note,,0,0,none,
2026-08-15T12:00:06,0.0,2,4.005,1500,25,60,52,active,True,ok,,0,0,none,
2026-08-15T12:00:07,4.0,1,4.0,1500,50,120,45,active,True,ok,,0,0,none,
2026-08-15T12:00:08,2.0,2,3.96,1500,50,120,52,active,True,ok,,0,0,none,
2026-08-15T12:00:09,6.0,1,3.95,1500,75,180,45,active,True,ok,,0,0,none,
2026-08-15T12:00:10,4.0,2,3.915,1500,75,180,52,active,True,ok,,0,0,none,
2026-08-15T12:00:11,8.0,1,3.9,1500,100,240,45,active,True,ok,,0,0,none,
2026-08-15T12:00:12,6.0,2,3.87,1500,100,240,52,active,True,ok,,0,0,none,
2026-08-15T12:00:13,10.0,1,3.85,1500,125,300,45,active,True,ok,,0,0,none,
2026-08-15T12:00:14,8.0,2,3.825,1500,125,300,52,active,True,ok,,0,0,none,
2026-08-15T12:00:15,12.0,1,3.8,1500,150,360,45,active,True,ok,,0,0,none,
2026-08-15T12:00:16,10.0,2,3.78,1500,150,360,52,active,True,ok,,0,0,none,
2026-08-15T12:00:17,14.0,1,3.75,1500,175,420,45,active,True,ok,,0,0,none,
2026-08-15T12:00:18,12.0,2,3.735,1500,175,420,52,active,True,ok,,0,0,none,
2026-08-15T12:00:19,16.0,1,3.7,1500,200,480,45,active,True,ok,,0,0,none,
2026-08-15T12:00:20,14.0,2,3.69,1500,200,480,52,active,True,ok,,0,0,none,
2026-08-15T12:00:21,18.0,1,3.65,1500,225,540,45,active,True,ok,,0,0,none,
2026-08-15T12:00:22,16.0,2,3.645,1500,225,540,52,active,True,ok,,0,0,none,
2026-08-15T12:00:23,20.0,1,3.6,1500,250,600,45,active,True,ok,,0,0,none,
2026-08-15T12:00:24,18.0,2,3.6,1500,250,600,52,active,True,ok,,0,0,none,
2026-08-15T12:00:26,23.0,1,3.55,1500,250,660,45,active,False,error,,0,0,none,
2026-08-15T12:00:27,24.0,1,3.5,1500,0,0,45,active,True,note,transition_artifact,0,0,none,
2026-08-15T12:00:28,25.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:29,23.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
2026-08-15T12:00:30,27.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:31,25.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
2026-08-15T12:00:32,29.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:33,27.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
2026-08-15T12:00:34,31.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:36,30.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
2026-08-15T12:00:37,34.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:38,32.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
2026-08-15T12:00:40,37.0,1,3.6,0,250,600,45,done,True,ok,,0,0,none,
2026-08-15T12:00:41,35.0,2,3.62,0,250,600,52,done,True,ok,,0,0,none,
"""


class FakeDateTime(datetime):
    """datetime subclass whose now() returns a fixed, advancing sequence."""

    _tick = 0
    _base = datetime(2026, 8, 15, 12, 0, 0)

    @classmethod
    def now(cls, tz=None):
        t = cls._base + timedelta(seconds=cls._tick)
        cls._tick += 1
        return t


def build_status_frame(slot, current_ma, voltage_mv, capacity_mah,
                       elapsed_s, resistance_mohm, break_checksum=False,
                       alert=0, mode=0):
    """
    Build a 24-byte 0x91 status frame in the FW 2.02 layout.

      [0]=0x0F  [1]=length byte  [2]=0x91  [3]=channel bitmask
      [4:6] current mA   [6:8] voltage mV   [10:12] capacity mAh
      [14:16] elapsed s  [16:18] resistance mOhm
      [-1] checksum = sum(raw[2:-1]) % 256

    The length byte is 22 because drift.py checks raw[1] + 2 == len(raw).
    """
    channel = {1: 0x01, 2: 0x02, 3: 0x04, 4: 0x08}[slot]

    def be16(v):
        return [(v >> 8) & 0xFF, v & 0xFF]

    raw = [0x00] * 24
    raw[0] = 0x0F
    raw[1] = 22
    raw[2] = 0x91
    raw[3] = channel
    raw[4:6] = be16(current_ma)
    raw[6:8] = be16(voltage_mv)
    raw[10:12] = be16(capacity_mah)
    raw[14:16] = be16(elapsed_s)
    raw[16:18] = be16(resistance_mohm)
    raw[19] = mode           # byte 19: the device's own mode
    raw[20] = alert          # byte 20: the charger's own termination reason

    chk = sum(raw[2:-1]) % 256
    raw[-1] = (chk + 1) % 256 if break_checksum else chk
    return bytes(raw)


def frame_script():
    """The fixed replay sequence. Every branch in on_notify gets hit."""
    frames = []

    # steady 1500mA discharge on slots 1 and 2. Capacity is kept physically
    # consistent with current x time so drift.py's integral check stays quiet
    # and does not add noise to the expected output.
    for t in range(60, 601, 60):
        cap_mah = round(1500 * t / 3600)
        frames.append(build_status_frame(1, 1500, 4100 - (t // 60) * 50, cap_mah, t, 45))
        frames.append(build_status_frame(2, 1500, 4050 - (t // 60) * 45, cap_mah, t, 52))

    # slot 3 was never requested: must be dropped, not written
    frames.append(build_status_frame(3, 1000, 3900, 100, 300, 40))

    # deliberately corrupt checksum -> drift error, but still recorded
    frames.append(build_status_frame(1, 1500, 3550, 250, 660, 45, break_checksum=True))

    # the stop-time transition artifact: current flowing, counters zeroed
    frames.append(build_status_frame(1, 1500, 3500, 0, 0, 45))

    # idle streak past --done-after 5, so both slots get marked finished
    for _ in range(6):
        frames.append(build_status_frame(1, 0, 3600, 250, 600, 45))
        frames.append(build_status_frame(2, 0, 3620, 250, 600, 52))

    return frames


def replay(outdir, subscribers=(), cells=None, chemistry="liion"):
    """Push the frame script through a real Capture. Returns (cap, csv_text)."""
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0

    args = Namespace(
        name="replay", slots="1,2", chemistry=chemistry, outdir=outdir,
        baseline="__no_such_baseline__.json",  # nonexistent -> no baseline loaded
        lenient=False, done_after=5, cells=cells,
        # only read by save_meta, present so Capture stays fully constructible
        start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
    )

    # Log output is suppressed for tidiness, but the log calls themselves must
    # still happen - they consume ticks from the fake clock.
    with redirect_stdout(io.StringIO()):
        # Built through from_args, because that is the path the command line
        # takes. The keyword constructor is exercised separately below.
        cap = capture.Capture.from_args(args, capture.Log())
        for fn in subscribers:
            cap.subscribe(fn)
        for raw in frame_script():
            cap.on_notify(None, bytearray(raw))
        cap.close()

    return cap, csv_text(cap)


def rows_of(text):
    return list(csv.DictReader(io.StringIO(text)))


def strip_timing(rows):
    """Every column except the two derived from wall-clock time."""
    return [{k: v for k, v in r.items() if k not in ("wall_time", "elapsed_s")}
            for r in rows]


print("--- CSV output vs known-good recording ---")

with tempfile.TemporaryDirectory() as tmp:
    cap, got_csv = replay(tmp)

got = rows_of(got_csv)
want = rows_of(EXPECTED_CSV)

assert_true("row count matches", len(got) == len(want),
            f"expected {len(want)} rows, got {len(got)}")

# The check that matters: the data itself, independent of log-call counts.
data_ok = strip_timing(got) == strip_timing(want)
assert_true("all data columns identical to known-good output", data_ok)
if not data_ok:
    for i, (g, w) in enumerate(zip(strip_timing(got), strip_timing(want)), 1):
        if g != w:
            print(f"    first difference at row {i}:")
            print(f"      expected: {w}")
            print(f"      got:      {g}")
            break

# Stricter, and more brittle by design - see the DETERMINISM note above.
assert_true("timestamps identical too (fails harmlessly if logging changed)",
            got_csv == EXPECTED_CSV)

assert_true("column order unchanged",
            got_csv.splitlines()[0] == ",".join(capture.CSV_COLUMNS))

print()
print("--- on_notify bookkeeping ---")

assert_true("frames for unrequested slots are dropped",
            all(int(r["slot"]) in (1, 2) for r in got),
            "a slot 3 frame reached the CSV")
assert_true("corrupt frame counted as a drift error", cap.drift_errors == 1,
            f"got {cap.drift_errors}")
assert_true("corrupt frame still recorded, flagged not silently dropped",
            any(r["checksum_ok"] == "False" and r["drift_severity"] == "error"
                for r in got))
assert_true("transition artifact tagged exactly once",
            sum(1 for r in got if r["note"] == "transition_artifact") == 1)
assert_true("transition artifact did not reset the idle streak",
            cap.finished == {1, 2},
            f"got {cap.finished} - a phantom sample may have restarted the count")
assert_true("both slots seen active", cap.ever_active == {1, 2},
            f"got {cap.ever_active}")
assert_true("all_done() true once every active slot finished", cap.all_done())

# elapsed_s counts from each run's own first row, so every file starts at 0.
for slot in (1, 2):
    elapsed = [float(r["elapsed_s"]) for r in got if int(r["slot"]) == slot]
    assert_true(f"slot {slot}: elapsed_s starts at zero",
                elapsed[0] == 0.0, f"got {elapsed[0]}")
    assert_true(f"slot {slot}: elapsed_s never goes backward",
                all(b >= a for a, b in zip(elapsed, elapsed[1:])))

print()
print("--- safety-timer termination (latched teardown) ---")

# Measured on hardware: when the charger's TimeCut fires, the per-slot
# teardown zeroes capacity/elapsed/resistance but voltage, current and `state`
# keep echoing their last pre-teardown values, frame after frame, until an
# explicit stop clears it. The run is over; nothing in the frame says so except
# the zeroed counters. This replay is that shape exactly.


def timer_frames():
    frames = []
    # a normal discharge, counters advancing
    for t in range(60, 601, 60):
        frames.append(build_status_frame(1, 1500, 4100 - (t // 60) * 50,
                                         round(1500 * t / 3600), t, 45))
    # TimeCut fires: counters zeroed, everything else latched at its last value
    for _ in range(8):
        frames.append(build_status_frame(1, 1500, 3600, 0, 0, 0))
    return frames


with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args = Namespace(
        name="timer", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=5, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()) as timer_log:
        cap3 = capture.Capture.from_args(args, capture.Log())
        for raw in timer_frames():
            cap3.on_notify(None, bytearray(raw))
        cap3.close()
    timer_rows = rows_of(csv_text(cap3))

assert_true("slot marked FINISHED after a timer termination",
            cap3.finished == {1},
            f"got {cap3.finished} - latched current kept the streak at zero")
assert_true("all_done() true, so the capture would exit", cap3.all_done())
assert_true("teardown latched, not tagged once",
            sum(1 for r in timer_rows if r["note"] == "transition_artifact") == 8,
            "every post-teardown frame must be tagged, not just the first")
assert_true("no phantom row counts as a live sample",
            all(r["note"] == "transition_artifact"
                for r in timer_rows
                if int(r["device_elapsed_s"]) == 0 and int(r["current_ma"]) > 0))
assert_true("finished report uses the last real reading, not the zeroed frame",
            "250mAh over 600s" in timer_log.getvalue(),
            f"log said: {timer_log.getvalue().strip().splitlines()[-1:]}")

# An explicit stop clears the latch, and the capture must notice. The stop
# produces the same teardown signature, so torn_down is set - and if it only
# ever cleared on counters advancing, a stopped slot stayed marked torn-down
# forever, reading "latched" at 0mA long after its stop.
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args = Namespace(
        name="cleared", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=5, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        cap5 = capture.Capture.from_args(args, capture.Log())
        for raw in timer_frames():
            cap5.on_notify(None, bytearray(raw))
        assert cap5.torn_down == {1}, "setup: the latch should be on"
        # the stop lands: current goes to zero, counters stay zeroed
        for _ in range(3):
            cap5.on_notify(None, bytearray(
                build_status_frame(1, 0, 3620, 0, 0, 0)))
        cap5.close()

assert_true("an explicit stop clears the teardown latch",
            cap5.torn_down == set(), str(cap5.torn_down))
assert_true("and the slot stays finished afterward", cap5.finished == {1})

# The counters advancing again means a NEW run on that slot, not a phantom.
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args = Namespace(
        name="restart", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        cap4 = capture.Capture.from_args(args, capture.Log())
        for raw in timer_frames()[:11]:      # advance, then one teardown frame
            cap4.on_notify(None, bytearray(raw))
        for t in range(60, 301, 60):         # a fresh run starts on that slot
            cap4.on_notify(None, bytearray(build_status_frame(
                1, 1500, 4100 - (t // 60) * 50, round(1500 * t / 3600), t, 45)))
        cap4.close()

assert_true("counters advancing again clears the teardown latch",
            cap4.torn_down == set() and cap4.finished == set(),
            f"torn_down={cap4.torn_down} finished={cap4.finished}")

print()
print("--- pre-flight IR sampling ---")
#
# The charger measures IR ONCE per arm and holds it for the whole run, so the
# run's single figure has no corroboration. Re-arming is the only way to make it
# measure again (Start_Measure_Res has one caller, inside the per-slot state
# machine tick), so the sampler arms and stops the slot a few times first.
#
# The property that matters most: those arms are THROWAWAY. If their frames
# reached the CSV they would put phantom rows in the run's own record and feed
# the teardown detector a counter reset per sample.

with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args = Namespace(
        name="irsuspend", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=5, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        capir = capture.Capture.from_args(args, capture.Log())
        capir.measuring_ir.add(1)
        for t in (60, 120, 180):                    # frames during sampling
            capir.on_notify(None, bytearray(build_status_frame(
                1, 1500, 4000, 25, t, 45)))
        suspended_rows = capir.rows
        seen_while_suspended = capir.latest_any.get(1)
        capir.measuring_ir.discard(1)
        capir.on_notify(None, bytearray(build_status_frame(
            1, 1500, 4000, 25, 240, 45)))           # the real run's first frame
        capir.close()

assert_true("frames from a slot being sampled write no CSV rows",
            suspended_rows == 0, str(suspended_rows))
assert_true("...and do not reach the run's own bookkeeping",
            1 not in capir.ever_active or capir.rows == 1,
            f"rows={capir.rows} ever_active={capir.ever_active}")
assert_true("but latest_any IS updated - it is what the sampler reads",
            seen_while_suspended is not None
            and seen_while_suspended.resistance_mohm == 45)
assert_true("recording resumes the moment sampling ends",
            capir.rows == 1, str(capir.rows))

# the sampler itself, over a fake BLE client
class _IRClient:
    """Answers status queries with a frame carrying `ir`, after `delay` polls."""

    def __init__(self, cap, ir_values, delay=1):
        self.cap, self.ir_values, self.delay = cap, list(ir_values), delay
        self.written = []
        self.polls = 0

    async def write_gatt_char(self, _char, data, response=False):
        raw = bytes(data)
        self.written.append(raw)
        if raw[2] == 0x91:                      # a status query
            self.polls += 1
            if self.polls >= self.delay and self.ir_values:
                self.cap.latest_any[1] = parse_status_response(
                    build_status_frame(1, 100, 4000, 0, 1, self.ir_values[0]))
        elif raw[2] == 0xFE:                    # per-slot stop: IR goes away
            self.cap.latest_any.pop(1, None)
            if self.ir_values:
                self.ir_values.pop(0)

import asyncio as _asyncio                                        # noqa: E402
from protocol import (Mode, Chemistry, build_slot_stop,             # noqa: E402
                      parse_status_response)
from capture import build_start_packets                              # noqa: E402

capture.IR_PREFLIGHT_POLL_S = 0.0
capture.IR_PREFLIGHT_TIMEOUT_S = 1.0
capture.IR_PREFLIGHT_SETTLE_S = 0.0

def _run_sampler(ir_values, samples=3):
    with tempfile.TemporaryDirectory() as tmp:
        args = Namespace(
            name="ir", slots="1", chemistry="liion", outdir=tmp,
            baseline="__no_such_baseline__.json", lenient=False, done_after=5,
            cells=None, start=None, current=500, capacity=3400,
            safety_timer=240, cutoff_mv=None, target_mv=None)
        with redirect_stdout(io.StringIO()):
            c = capture.Capture.from_args(args, capture.Log())
            try:
                req = capture.RunRequest(
                    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                    current_ma=500, capacity_mah=3400, safety_timer_min=240)
                cl = _IRClient(c, ir_values)
                got = _asyncio.run(capture.measure_ir_preflight(
                    cl, None, c, req, [1], capture.Log(), samples=samples))
            finally:
                c.close()          # or the temp dir cannot be removed
        return got, cl, c

ir_got, ir_cl, ir_cap = _run_sampler([15, 16, 40])
assert_true("the sampler returns the readings it saw",
            ir_got.get(1) == [15, 16], str(ir_got))
assert_true("...and stops early once two agree (third arm never sent)",
            len(ir_got[1]) == 2, str(ir_got))
assert_true("every arm is followed by a per-slot stop",
            sum(1 for w in ir_cl.written if w[2] == 0x94)
            == sum(1 for w in ir_cl.written if w[2] == 0xFE),
            str([hex(w[2]) for w in ir_cl.written]))
assert_true("the stop is addressed to that slot alone",
            all(w == build_slot_stop(1)
                for w in ir_cl.written if w[2] == 0xFE))
assert_true("the slot is released when sampling ends",
            ir_cap.measuring_ir == set(), str(ir_cap.measuring_ir))
assert_true("ownership is taken, so stop-on-exit covers a crash mid-sample",
            ir_cap.started_by_us)
assert_true("the samples are kept for meta.json", ir_cap.ir_samples == ir_got)

# A pre-flight arm must be the RUN'S OWN config, byte for byte. An earlier
# version sent a 1 mAh capacity as a tight backstop; the real config that
# followed did not take, the slot kept the 1 mAh ceiling, and the actual run was
# torn down after 15 s at 1 mAh with alert 'capacity limit'.
# A config value can outlive the stop that should have ended its run, so the
# sampler must write nothing the real run could inherit.
_arm = next(w for w in ir_cl.written if w[2] == 0x94)
_real = build_start_packets([1], capture.RunRequest(
    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
    capacity_mah=3400, safety_timer_min=240))[0][1]
assert_true("a pre-flight arm is byte-identical to the real run's config",
            _arm == _real, _arm.hex())
assert_true("in particular it does NOT send a throwaway capacity",
            int.from_bytes(_arm[9:11], "big") == 3400,
            str(int.from_bytes(_arm[9:11], "big")))
assert_true("...nor a throwaway safety timer",
            int.from_bytes(_arm[30:32], "big") == 240,
            str(int.from_bytes(_arm[30:32], "big")))

# disagreement keeps sampling, and a silent charger is survivable
ir_spread, _, _ = _run_sampler([15, 40, 80])
assert_true("samples that disagree are all kept, not truncated",
            len(ir_spread.get(1, [])) == 3, str(ir_spread))
ir_none, _, ir_cap2 = _run_sampler([], samples=2)
assert_true("a slot that never reports a resistance yields nothing, not a crash",
            ir_none == {}, str(ir_none))
assert_true("...and the slot is still released and stopped",
            ir_cap2.measuring_ir == set())

print()
print("--- the charger's own termination reason (status byte 20) ---")
#
# Confirmed on hardware: a --safety-timer
# 5 discharge reported byte 20 == 5 -> "time limit" in the VERY frame the
# counters reset, and kept reporting it on every latched frame until an explicit
# stop cleared it. The alert survives the teardown because the firmware's
# protection paths call the teardown first and set the alert after.
#
# The alert is an ADDITIONAL, more authoritative trigger - never a weaker one. A
# normal termination (voltage cutoff, End_Cur taper) may raise no alert at all,
# so its ABSENCE must never be read as "not finished".

def timer_frames_with_alert(alert=5):
    frames = []
    for t in range(60, 601, 60):
        frames.append(build_status_frame(1, 1500, 4100 - (t // 60) * 50,
                                         round(1500 * t / 3600), t, 45))
    for _ in range(8):
        frames.append(build_status_frame(1, 1500, 3600, 0, 0, 0, alert=alert))
    return frames


def _replay(frames, done_after=5, name="alert"):
    with tempfile.TemporaryDirectory() as tmp:
        capture.datetime = FakeDateTime
        FakeDateTime._tick = 0
        args = Namespace(
            name=name, slots="1", chemistry="liion", outdir=tmp,
            baseline="__no_such_baseline__.json", lenient=False,
            done_after=done_after, cells=None, start=None, current=1500,
            capacity=3400, safety_timer=5, cutoff_mv=None, target_mv=None,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            cap = capture.Capture.from_args(args, capture.Log())
            for raw in frames:
                cap.on_notify(None, bytearray(raw))
            cap.close()
        return cap, buf.getvalue()

capA, logA = _replay(timer_frames_with_alert())
assert_true("a timer termination is still detected, and still latches",
            capA.torn_down == {1} and capA.finished == {1},
            f"torn_down={capA.torn_down} finished={capA.finished}")
assert_true("the charger's reason is recorded per slot",
            capA.alert_by_slot.get(1) == "time limit", str(capA.alert_by_slot))
assert_true("...and the FINISHED line quotes it instead of guessing",
            "charger reports 'time limit'" in logA, logA[-400:])
assert_true("the run's real totals are still reported, not the zeroed counters",
            "1500mAh over 600s" in logA or "over 600s" in logA, logA[-400:])

# The capability this unlocks: an ATTACH that connects AFTER the teardown. It
# never saw the counters advance, so it used to abstain - it could not tell a
# phantom from a live run. A standing alert removes the ambiguity.
attach_only = timer_frames_with_alert()[10:]      # teardown frames alone
capB, logB = _replay(attach_only, name="attach_alert")
assert_true("an attach that sees only latched frames WITH an alert now detects it",
            capB.torn_down == {1}, f"torn_down={capB.torn_down}")
assert_true("...and names the reason",
            capB.alert_by_slot.get(1) == "time limit", str(capB.alert_by_slot))

# ...but the abstention itself must survive: no alert, no guess. This is the
# documented behavior and the alert is the ONLY thing that may lift it.
capC, _ = _replay([f for f in timer_frames_with_alert(alert=0)][10:],
                  name="attach_noalert")
assert_true("an attach seeing latched frames with NO alert still does not guess",
            capC.torn_down == set(), f"torn_down={capC.torn_down}")

# A slot that is counting again has no standing alert.
resumed = timer_frames_with_alert() + [
    build_status_frame(1, 1500, 4100 - (t // 60) * 50, round(1500 * t / 3600), t, 45)
    for t in range(60, 301, 60)]
capD, _ = _replay(resumed, name="alert_cleared")
assert_true("a fresh run on the slot clears the stale alert",
            capD.alert_by_slot.get(1) is None and capD.torn_down == set(),
            f"alerts={capD.alert_by_slot} torn_down={capD.torn_down}")

print()
print("--- subscriber contract ---")

seen_a, seen_b = [], []


def exploding(_s):
    raise RuntimeError("simulated consumer bug")


with tempfile.TemporaryDirectory() as tmp:
    cap2, csv2 = replay(tmp, subscribers=(exploding, seen_a.append, seen_b.append))

assert_true("one sample delivered per CSV row",
            len(seen_a) == cap2.rows == len(want),
            f"{len(seen_a)} samples vs {cap2.rows} rows")
# Data only, not timestamps: each swallowed exception is logged, and log calls
# consume ticks from the fake clock, so wall_time legitimately slides here.
# What must not change is a single value the charger reported.
assert_true("a subscriber raising on every sample does not corrupt the CSV",
            strip_timing(rows_of(csv2)) == strip_timing(want),
            "CSV data diverged once a subscriber started throwing")
assert_true("every subscriber failure counted",
            cap2.subscriber_errors == len(want), f"got {cap2.subscriber_errors}")
assert_true("subscribers after a broken one still receive everything",
            len(seen_b) == len(want), f"got {len(seen_b)}")
assert_true("all subscribers receive the identical Sample object",
            all(a is b for a, b in zip(seen_a, seen_b)))
assert_true("delivery order matches CSV row order",
            [s.slot for s in seen_a] == [int(r["slot"]) for r in got])

# The no-duplicate-parsing invariant: the Sample carries the SlotStatus that
# drift.py already produced. If someone re-parses the raw bytes in on_notify
# this stays true, so also check the row was rendered from that same object.
assert_true("Sample.status is a parsed SlotStatus",
            all(isinstance(s.status, capture.SlotStatus) for s in seen_a))
assert_true("sample_to_row() reproduces the CSV row from the Sample alone",
            all(str(capture.sample_to_row(s)["voltage_v"]) == r["voltage_v"]
                and str(capture.sample_to_row(s)["device_capacity_mah"]) == r["device_capacity_mah"]
                for s, r in zip(seen_a, got)))

assert_true("subscribers observe fully updated capture state",
            cap2.finished == {1, 2})

print()
print("--- cell identity ---")

# Parsing. Cell identity is per slot: a two-slot run holds two cells.
parsed = capture.parse_cells("1=ENL-A-01,2=ENL-A-02", [1, 2])
assert_true("SLOT=CELL_ID pairs parsed",
            parsed == {1: "ENL-A-01", 2: "ENL-A-02"}, str(parsed))
assert_true("whitespace tolerated",
            capture.parse_cells(" 1 = ENL-A-01 , 2=ENL-A-02 ", [1, 2])
            == {1: "ENL-A-01", 2: "ENL-A-02"})
assert_true("unlabeled slots recorded as None, not omitted",
            capture.parse_cells("1=ENL-A-01", [1, 2])
            == {1: "ENL-A-01", 2: None})
assert_true("no --cells still covers every slot",
            capture.parse_cells(None, [1, 2]) == {1: None, 2: None})


def rejects(label, spec, slots):
    try:
        capture.parse_cells(spec, slots)
        assert_true(label, False, "no ValueError raised")
    except ValueError:
        assert_true(label, True)


rejects("rejects a bare id with no slot", "ENL-A-01", [1, 2])
rejects("rejects a non-numeric slot", "one=ENL-A-01", [1, 2])
rejects("rejects a slot not being captured", "3=ENL-A-03", [1, 2])
rejects("rejects an empty cell id", "1=", [1, 2])

# Recording. This is the whole point: a run captured without it can never be
# linked back to the physical cell afterward.
with tempfile.TemporaryDirectory() as tmp:
    cap3, _ = replay(tmp, cells="1=ENL-A-01,2=ENL-A-02")
    cap3.save_meta({"end_reason": "done"})
    meta = meta_of(cap3, 1)
    meta2 = meta_of(cap3, 2)
    names3 = [r.dir.name for r in cap3.runs]

# One folder per slot per run: each run's meta.json carries ITS slot's cell.
assert_true("meta.json records the slot-to-cell mapping, per run",
            meta.get("cells") == {"1": "ENL-A-01"}
            and meta2.get("cells") == {"2": "ENL-A-02"},
            f"{meta.get('cells')} / {meta2.get('cells')}")
assert_true("mapping keys are strings, as JSON requires",
            all(isinstance(k, str) for k in meta["cells"]))
assert_true("existing meta fields still present",
            {"name", "slots", "samples", "drift_errors"} <= set(meta))
assert_true("each run folder is named for its cell - and, from the CLI, "
            "ends with --name",
            names3[0].endswith("_S1_ENL-A-01_external_replay")
            and names3[1].endswith("_S2_ENL-A-02_external_replay"), str(names3))

with tempfile.TemporaryDirectory() as tmp:
    cap4, _ = replay(tmp)
    cap4.save_meta()
    meta4 = meta_of(cap4, 2)
    name4 = cap4.runs[-1].dir.name

assert_true("a run with no --cells still records its slot as unlabeled",
            meta4.get("cells") == {"2": None} and meta4.get("cell") is None,
            str(meta4.get("cells")))
assert_true("...and its folder name simply has no cell segment (no '__')",
            name4.endswith("_S2_external_replay") and "__" not in name4, name4)

print()
print("--- meta.json shape ---")
#
# meta.json is an on-disk format: past runs already carry it, so its keys and
# value types cannot be changed freely. These checks pin the whole shape so a
# refactor of how Capture is constructed cannot quietly alter what gets written.

with tempfile.TemporaryDirectory() as tmp:
    cap5, _ = replay(tmp, cells="1=ENL-A-01")
    cap5.save_meta({"end_reason": "done"})
    m = meta_of(cap5, 1)
    m5_slot2 = meta_of(cap5, 2)

EXPECTED_META_KEYS = {
    "name", "app_version", "created", "slots", "cells", "chemistry", "mode", "current_ma",
    "capacity_mah", "safety_timer_min", "cutoff_mv", "target_mv", "storage_mv",
    "ir_samples",
    "charge_cutoff_current_ma", "discharge_cutoff_current_ma", "samples",
    "gaps", "drift_errors", "protocol_fingerprint", "end_reason", "device",
    "temp_interval_s", "reconnect_attempts", "firmware_verified",
    # Peak slot temperature and the app-side cutoff.
    "peak_slot_temp_c", "temp_cutoff_c", "temp_cutoff_trips",
    # One folder per slot per run: the run's identity,
    # its full config snapshot, how it ended, and the session it came from.
    "folder", "slot", "cell", "action", "group", "config",
    "finished", "alert", "final", "relocated", "session",
    # The Advanced section: -dV, trickle and keep voltage. Additive -
    # null when the charger's default was used, absent from older runs.
    "delta_v_mv", "trickle_ma", "keep_mv",
}
assert_true("meta.json has exactly the expected keys",
            set(m) == EXPECTED_META_KEYS,
            f"missing {EXPECTED_META_KEYS - set(m)}, extra {set(m) - EXPECTED_META_KEYS}")

assert_true("name recorded", m["name"] == "replay", str(m["name"]))
assert_true("slots recorded as a list of ints - this run's one slot",
            m["slots"] == [1] and m["slot"] == 1, str(m["slots"]))
assert_true("cells recorded with string keys",
            m["cells"] == {"1": "ENL-A-01"} and m["cell"] == "ENL-A-01",
            str(m["cells"]))
assert_true("action is 'external' for a run this capture did not start",
            m["action"] == "external", str(m["action"]))
assert_true("no config snapshot for a run this capture did not start",
            m["config"] is None, str(m["config"]))
assert_true("mode is 'attach' when no run was started",
            m["mode"] == "attach", str(m["mode"]))
assert_true("current_ma recorded", m["current_ma"] == 1500, str(m["current_ma"]))
assert_true("capacity_mah recorded", m["capacity_mah"] == 3400, str(m["capacity_mah"]))
assert_true("safety_timer_min recorded",
            m["safety_timer_min"] == 240, str(m["safety_timer_min"]))
assert_true("unset cutoff stays null", m["cutoff_mv"] is None, str(m["cutoff_mv"]))
assert_true("unset target stays null", m["target_mv"] is None, str(m["target_mv"]))
assert_true("samples counts THIS run's rows only",
            m["samples"] == 18 and cap5.rows == 34, str(m["samples"]))
assert_true("drift_errors counts THIS run's (slot 1 holds the corrupt frame)",
            m["drift_errors"] == 1 and m5_slot2["drift_errors"] == 0,
            str(m["drift_errors"]))
assert_true("the session's own drift total is kept alongside",
            m["session"]["drift_errors"] == 1, str(m["session"]))
assert_true("gaps recorded as a list", m["gaps"] == [], str(m["gaps"]))
assert_true("extra fields merge in", m["end_reason"] == "done", str(m.get("end_reason")))
assert_true("device is null when the run never asked (no 0x57 sent)",
            m["device"] is None, str(m["device"]))
assert_true("protocol fingerprint embedded as an object",
            isinstance(m["protocol_fingerprint"], dict)
            and "frame_lengths" in m["protocol_fingerprint"])

# Chemistry is stored as the name the user typed, NOT the enum member name.
# "liionhv" and Chemistry.LIION_HV.name.lower() ("liion_hv") differ - deriving
# the string from the enum would silently change the format for past runs.
assert_true("chemistry stored as the typed name (liion)",
            m["chemistry"] == "liion", str(m["chemistry"]))

with tempfile.TemporaryDirectory() as tmp:
    cap6, _ = replay(tmp, chemistry="liionhv")
    cap6.save_meta()
    m6 = meta_of(cap6)

assert_true("chemistry name survives round-trip where it differs from the enum",
            m6["chemistry"] == "liionhv",
            f"got {m6['chemistry']!r} - deriving this from Chemistry.LIION_HV "
            f"would give 'liion_hv' and change the on-disk format")

print()
print("--- the init sequence is never sent on the start path ---")
#
# Safety-critical. Init STOPS whatever is running (measured on FW 2.02 with
# the probe_init probe: 1747mA at 494s went to 0mA/0mAh/0s within two
# seconds), and it stops EVERY slot. Starting used to send it anyway, for a
# 0x93 stop that does not work on this firmware, which meant starting one slot
# could kill a run on another. Nothing on the start path may send it now.
#
# session() needs a live BLE client, so what is pinned here is the surrounding
# contract: the helper that used to authorize the send is gone, and a starting
# Capture declares that init responses will not arrive.

assert_true("the should_send_init authorisation helper is gone entirely",
            not hasattr(capture, "should_send_init"),
            "a start path that can send init is the bug this removed")
assert_true("init responses are the trio, kept separate from 0x94/0x57",
            capture.INIT_RESPONSE_COMMANDS == ("0x74", "0x65", "0xfe"),
            str(capture.INIT_RESPONSE_COMMANDS))
assert_true("a controlling session may still trigger 0x94 and 0x57",
            set(capture.CONTROL_RESPONSE_COMMANDS)
            - set(capture.INIT_RESPONSE_COMMANDS) == {"0x94", "0x57"},
            str(capture.CONTROL_RESPONSE_COMMANDS))

with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        starter = capture.Capture(name="s", slots=[1], outdir=tmp,
                                  baseline=None, sends_control=True)
        watcher = capture.Capture(name="w", slots=[1], outdir=tmp,
                                  baseline=None, sends_control=False)
    starter.close()
    watcher.close()

assert_true("a starting run expects no init responses, so their absence is "
            "not reported as drift",
            starter.validator.expected_absent == capture.INIT_RESPONSE_COMMANDS,
            str(starter.validator.expected_absent))
assert_true("an attach expects no control responses at all",
            watcher.validator.expected_absent
            == capture.CONTROL_RESPONSE_COMMANDS,
            str(watcher.validator.expected_absent))

print("--- stopping reaches only the slots we started ---")
#
# Automatic teardown used to be the all-slot init sequence, which halts every
# slot, so it could only ever decline: if someone else's run was going, ours was
# left to the safety timer. The per-slot stop
# (the probe_slot_stop probe: 0xFE 0x01 took slot 1 to 0mA while slot 2 kept
# running at 500mA) is surgical, so it never has to decline. The guarded init
# stays as the fallback for when 0xFE does not take.

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import protocol  # noqa: E402


class _NoSleep:
    """capture's asyncio, with sleep neutered so the stop paths run instantly."""

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, _seconds):
        return None


class FakeClient:
    """Records every packet written. Whether a slot obeys is decided by the
    latest_any the fake cap carries, not by the client."""

    def __init__(self):
        self.written = []

    async def write_gatt_char(self, _char, data, response=False):
        self.written.append(bytes(data))


def fake_cap(our_slots, running):
    """A Capture stand-in: only .slots, .latest_any and .run_config are read."""
    return SimpleNamespace(
        slots=list(our_slots),
        latest_any={s: SimpleNamespace(current_ma=ma) for s, ma in running.items()},
        run_config=capture.RunConfig(safety_timer_min=240),
    )


def run_stop(fn, cap, client):
    real = capture.asyncio
    capture.asyncio = _NoSleep()
    try:
        with redirect_stdout(io.StringIO()) as out:
            ok = asyncio.run(fn(client, "char", cap, capture.Log()))
        return ok, out.getvalue()
    finally:
        capture.asyncio = real


init_packets = set(capture.init_sequence())

# --- the pure guard -------------------------------------------------------
def seen(**by_slot):
    """{slot: current_ma} shaped like Capture.latest_any."""
    return {int(s): SimpleNamespace(current_ma=ma) for s, ma in by_slot.items()}


busy_others = seen(**{"1": 500, "2": 0, "3": 502, "4": 0})
assert_true("a running slot we did not start is reported",
            capture.foreign_busy([3], busy_others) == [1],
            str(capture.foreign_busy([3], busy_others)))
assert_true("our own running slot is not foreign",
            capture.foreign_busy([1, 3], busy_others) == [])
assert_true("an idle foreign slot is not an obstacle",
            capture.foreign_busy([1], seen(**{"1": 500, "2": 0})) == [])
assert_true("a slot that never answered is not assumed busy",
            capture.foreign_busy([1], seen(**{"1": 500})) == [])

# --- 0x57 is only ever aimed at a wholly idle charger ---------------------
# Verified harmless on IDLE slots and nowhere else. Before init was dropped the
# start path could not meet a busy foreign slot - init had already killed it -
# so this question only became reachable with that fix.
assert_true("a wholly idle charger passes the 0x57 gate",
            not capture.any_slot_busy(seen(**{"1": 0, "2": 0, "3": 0, "4": 0})))
assert_true("any slot drawing current fails it, ours or not",
            capture.any_slot_busy(busy_others))
assert_true("a charger nothing has answered for is treated as idle",
            not capture.any_slot_busy({}),
            "nothing has been observed, so there is nothing to avoid")

# The MID-SESSION read alone also lets through a slot the charger has ended and
# is only echoing: status 0 with a standing alert.
from types import SimpleNamespace as _NS
_latched = _NS(current_ma=502, device_status=0, alert_code=5)
_running = _NS(current_ma=502, device_status=3, alert_code=0)
_stat0_no_alert = _NS(current_ma=502, device_status=0, alert_code=0)
_no_status = _NS(current_ma=502, device_status=None, alert_code=None)
assert_true("a latched slot (status 0 + alert) does not block the firmware read",
            not capture.busy_beyond_latch({1: _latched, 2: _NS(
                current_ma=0, device_status=0, alert_code=0)}))
assert_true("...a running slot still does",
            capture.busy_beyond_latch({1: _latched, 2: _running}))
assert_true("...and so does current with NO alert, or with no status byte",
            capture.busy_beyond_latch({1: _stat0_no_alert})
            and capture.busy_beyond_latch({1: _no_status}))
assert_true("...while the START path's gate still counts the latched slot busy",
            capture.any_slot_busy({1: _latched}))

# --- the happy path: our slot stops, nobody else is touched ---------------
# Slot 1 is someone else's live discharge; we started slot 3 and it goes idle.
cap_ok = fake_cap([3], {1: 500, 2: 0, 3: 0, 4: 0})
client_ok = FakeClient()
ok, log_ok = run_stop(capture.stop_our_slots, cap_ok, client_ok)

stops = [w for w in client_ok.written if w == protocol.build_slot_stop(3)]
assert_true("the per-slot stop is sent, addressed to our slot alone",
            len(stops) == 1, f"got {len(stops)}")
assert_true("no stop is addressed to a slot we did not start",
            not any(w == protocol.build_slot_stop(s)
                    for s in (1, 2, 4) for w in client_ok.written))
assert_true("no init packet goes out on the normal teardown",
            not (init_packets & set(client_ok.written)),
            "init stops every slot - it must not be the routine path")
assert_true("teardown reports success", ok is True)
assert_true("and says the others were left alone",
            "no other slot was touched" in log_ok, log_ok.strip())

# --- the fallback: 0xFE does not take, and the blunt tool must decline -----
# Our slot 3 is still drawing current afterward, and slot 1 is not ours.
cap_stuck = fake_cap([3], {1: 500, 2: 0, 3: 502, 4: 0})
client_stuck = FakeClient()
stuck_ok, log_stuck = run_stop(capture.stop_our_slots, cap_stuck, client_stuck)

assert_true("a slot still drawing current escalates to the all-slot teardown",
            "falling back" in log_stuck, log_stuck.strip())
assert_true("which declines, because a slot we did not start is running",
            "NOT stopping" in log_stuck and "[1]" in log_stuck,
            log_stuck.strip())
assert_true("so no init goes out even in the fallback",
            not (init_packets & set(client_stuck.written)))
assert_true("and the failure is reported, not swallowed", stuck_ok is False)

# --- the fallback when nobody else is running: init is allowed to fire -----
cap_free = fake_cap([3], {1: 0, 2: 0, 3: 502, 4: 0})
client_free = FakeClient()
free_ok, log_free = run_stop(capture.stop_our_slots, cap_free, client_free)

assert_true("with no foreign run, the fallback does send init",
            bool(init_packets & set(client_free.written)),
            "the guarded teardown must still work when it is safe")

# --- the GUI case: MONITORED is not STARTED --------------------------------
# A GUI watching all four slots started a run on slot 3 only; slot 1 is a
# front-panel run. Closing used to send 0xFE to all four - stopping slot 1.


def gui_cap(started, latest):
    """latest: {slot: (current_ma, device_status)}"""
    return SimpleNamespace(
        slots=[1, 2, 3, 4], started_slots=set(started),
        latest_any={s: SimpleNamespace(current_ma=ma, device_status=ds)
                    for s, (ma, ds) in latest.items()},
        run_config=capture.RunConfig(safety_timer_min=240))


class _ObeyingClient(FakeClient):
    """Applies each 0xFE to the fake cap, as the charger would."""

    def __init__(self, cap):
        super().__init__()
        self.cap = cap

    async def write_gatt_char(self, _char, data, response=False):
        await super().write_gatt_char(_char, data, response)
        raw = bytes(data)
        if raw[2] == 0xFE:
            for s, bit in protocol.SLOT_BITMASK.items():
                if raw[3] == bit and s in self.cap.latest_any:
                    self.cap.latest_any[s] = SimpleNamespace(
                        current_ma=0, device_status=0)


cap_mon = gui_cap({3}, {1: (500, 3), 2: (0, 0), 3: (502, 3), 4: (0, 0)})
client_mon = _ObeyingClient(cap_mon)
mon_ok, log_mon = run_stop(capture.stop_our_slots, cap_mon, client_mon)
fe_to = sorted(s for s, b in protocol.SLOT_BITMASK.items()
               if protocol.build_slot_stop(s) in client_mon.written)
assert_true("GUI exit: 0xFE goes ONLY to the slot the app started",
            fe_to == [3], str(fe_to))
assert_true("...the front-panel run on a monitored slot is untouched",
            cap_mon.latest_any[1].current_ma == 500 and mon_ok is True)

# A started slot the charger reports idle (status 0, 0 mA) has nothing to
# stop; a latched one (current echoing) and a just-started one (0 mA while
# its IR is measured, status 3) are both stopped; one never heard from too.
cap_mix = gui_cap({1, 2, 3, 4}, {1: (0, 0), 2: (499, 0), 3: (0, 3)})
client_mix = _ObeyingClient(cap_mix)
mix_ok, log_mix = run_stop(capture.stop_our_slots, cap_mix, client_mix)
fe_mix = sorted(s for s in (1, 2, 3, 4)
                if protocol.build_slot_stop(s) in client_mix.written)
assert_true("an idle started slot (status 0, 0 mA) gets no stop",
            1 not in fe_mix and "already idle" in log_mix, str(fe_mix))
assert_true("a latched slot (current echoing) IS stopped", 2 in fe_mix)
assert_true("a just-started slot at 0 mA but status 3 IS stopped - 0 mA "
            "alone is not idle", 3 in fe_mix)
assert_true("a started slot never heard from IS stopped", 4 in fe_mix)

cap_none = gui_cap({1}, {1: (0, 0), 2: (500, 3)})
client_none = _ObeyingClient(cap_none)
none_ok, _l = run_stop(capture.stop_our_slots, cap_none, client_none)
assert_true("nothing of ours to stop: no packets at all, reported as success",
            client_none.written == [] and none_ok is True,
            str([w.hex() for w in client_none.written]))

# The fallback must count a monitored-but-unstarted slot as FOREIGN, or its
# init would kill the panel run. Our slot 3 refuses 0xFE; slot 1 is a panel run.
cap_fb = gui_cap({3}, {1: (500, 3), 2: (0, 0), 3: (502, 3), 4: (0, 0)})
client_fb = FakeClient()                         # ignores 0xFE: stop "fails"
fb_ok, log_fb = run_stop(capture.stop_our_slots, cap_fb, client_fb)
assert_true("fallback: a monitored panel run blocks init (it is not ours)",
            not (init_packets & set(client_fb.written))
            and "NOT stopping" in log_fb and fb_ok is False, log_fb.strip())

print()
print("--- refusing to start on a slot we must not touch ---")
#
# These checks only mean anything because they run BEFORE init, which stops
# whatever is running. Checking afterward reports a run it has already ended.


def status_for(slot, current_ma, voltage_mv, capacity_mah=0, elapsed_s=0):
    from protocol import parse_status_response
    return parse_status_response(build_status_frame(
        slot, current_ma, voltage_mv, capacity_mah, elapsed_s, 40))


busy_slot = {1: status_for(1, 1500, 3700, 250, 500)}
idle_slot = {1: status_for(1, 0, 4100)}
empty_slot = {1: status_for(1, 0, 0)}

assert_true("an idle slot holding a cell may be started",
            capture.refuse_reason([1], idle_slot) is None,
            str(capture.refuse_reason([1], idle_slot)))
assert_true("a slot already running is refused",
            "already running" in (capture.refuse_reason([1], busy_slot) or ""),
            str(capture.refuse_reason([1], busy_slot)))
assert_true("the refusal points at --attach",
            "--attach" in (capture.refuse_reason([1], busy_slot) or ""))
assert_true("for the GUI the busy-slot refusal has no command-line flag in it",
            "--attach" not in (capture.refuse_reason(
                [1], busy_slot, for_gui=True) or "x --attach")
            and "already running something" in (capture.refuse_reason(
                [1], busy_slot, for_gui=True) or ""),
            str(capture.refuse_reason([1], busy_slot, for_gui=True)))
assert_true("...and it is still a refusal for the same slots as the CLI's",
            (capture.refuse_reason([1], idle_slot, for_gui=True) is None)
            and capture.refuse_reason([1], empty_slot, for_gui=True)
            == capture.refuse_reason([1], empty_slot)
            and (capture.refuse_reason([1], {}, for_gui=True)
                 == capture.refuse_reason([1], {})))
assert_true("an empty slot is refused",
            "no cell" in (capture.refuse_reason([1], empty_slot) or ""),
            str(capture.refuse_reason([1], empty_slot)))
assert_true("a slot that never reported is refused",
            "did not report" in (capture.refuse_reason([1, 2], idle_slot) or ""),
            str(capture.refuse_reason([1, 2], idle_slot)))
assert_true("one bad slot refuses the whole run",
            capture.refuse_reason([1, 2], {1: idle_slot[1], 2: busy_slot[1]}) is not None)
assert_true("silence is checked before anything is dereferenced",
            capture.refuse_reason([1], {}) is not None,
            "must not raise KeyError on a slot that never answered")

print()
print("--- what a run may be asked to do ---")
#
# Config packets are built in exactly one place, so the CLI and any future
# controller cannot drift apart. On this firmware a config IS the start command,
# so everything below happens before anything is sent.

from protocol import Mode  # noqa: E402

good = capture.RunRequest(
    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
    capacity_mah=2000, safety_timer_min=240)
assert_true("a sane discharge is accepted", good.problem() is None,
            str(good.problem()))

packets = capture.build_start_packets([1, 2], good)
assert_true("one config packet per slot", len(packets) == 2)
assert_true("packets are addressed to the right slots",
            [s for s, _ in packets] == [1, 2])
assert_true("a config packet is what gets built",
            all(pkt[2] == 0x94 for _, pkt in packets),
            "0x94 is the config command, and IS the start on this firmware")

# The safety timer is the only protection that survives a crash.
no_timer = capture.RunRequest(
    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
    capacity_mah=2000, safety_timer_min=0)
assert_true("a zero safety timer is refused by default",
            "safety timer" in (no_timer.problem() or ""),
            str(no_timer.problem()))
assert_true("the refusal names no opt-out - the released tools have none",
            "--no-safety-timer" not in (no_timer.problem() or ""))
import inspect as _insp9
_ap_src = _insp9.getsource(capture.main)
assert_true("the research overrides are not command-line options of "
            "capture.py any more",
            "--allow-unvalidated-firmware" not in _ap_src
            and "--allow-unverified-chemistry" not in _ap_src
            and "--no-safety-timer" not in _ap_src and "set_defaults" in _ap_src,
            "an override flag is still in main()")
assert_true("there is no way to switch it off: the request has no such field "
            "and a snapshot carries no such key",
            not hasattr(no_timer, "allow_no_safety_timer")
            and "allow_no_safety_timer" not in capture.request_snapshot(no_timer))


def _problem_if_verified(req):
    """req.problem() as if its chemistry and mode were in VERIFIED_CONTROL. The
    checks behind the whitelist are only reachable for a combination that is
    not in it, and the product has no override; so a test patches the table for
    one call and puts it back."""
    old = capture.VERIFIED_CONTROL.get(req.chemistry)
    capture.VERIFIED_CONTROL[req.chemistry] = tuple(old or ()) + (req.mode,)
    try:
        return req.problem()
    finally:
        if old is None:
            del capture.VERIFIED_CONTROL[req.chemistry]
        else:
            capture.VERIFIED_CONTROL[req.chemistry] = old


# "True by construction": no override survives anywhere in the shipped code,
# so only a tested combination can start. Source-level, so a field or a flag coming
# back under the same name fails here.
import inspect as _insp9
_src_cap = _insp9.getsource(capture)
_src_gui = (Path(capture.__file__).resolve().parent / "gui.py").read_text(
    encoding="utf-8")
_gone = [w for w in ("allow_unverified", "allow_no_safety_timer",
                     "no_safety_timer", "allow-unverified-chemistry")
         if w in _src_cap or w in _src_gui]
assert_true("the unverified-chemistry and no-safety-timer overrides exist "
            "nowhere in capture.py or gui.py", not _gone, str(_gone))

# The modes whose current mapping was never established. The old inline code
# sent 0mA in BOTH directions for these.
for name in ("cycle", "refresh", "break_in"):
    req = capture.RunRequest(
        mode=capture.MODE_BY_NAME[name], chemistry=Chemistry.LIION,
        current_ma=500, capacity_mah=2000, safety_timer_min=240)
    assert_true(f"{name} is refused rather than configured with 0mA",
                "not supported" in (req.problem() or ""), str(req.problem()))

# storage joined the verified set; it must configure with BOTH currents set,
# not zero (probe_storage confirmed it charges or discharges to the target).
from protocol import Mode as _M
storage = capture.RunRequest(mode=_M.STORAGE, chemistry=Chemistry.LIION,
                             current_ma=500, capacity_mah=2000,
                             safety_timer_min=30)
assert_true("storage is an accepted action", storage.problem() is None,
            str(storage.problem()))
_s, spkt = capture.build_start_packets([1], storage)[0]
# config bytes: charge current at offset 5-6, discharge at 7-8 (frame offsets)
scharge = (spkt[5] << 8) | spkt[6]
sdischarge = (spkt[7] << 8) | spkt[8]
assert_true("storage sets both charge and discharge current",
            scharge == 500 and sdischarge == 500,
            f"charge {scharge}, discharge {sdischarge}")

for bad, label in (
    (capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                        current_ma=0, capacity_mah=2000, safety_timer_min=240),
     "zero current"),
    (capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                        current_ma=500, capacity_mah=0, safety_timer_min=240),
     "zero rated capacity"),
    (capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                        current_ma=500, capacity_mah=2000,
                        safety_timer_min=-5),
     "a negative safety timer"),
):
    assert_true(f"{label} is refused", bad.problem() is not None)
    try:
        capture.build_start_packets([1], bad)
        assert_true(f"{label} cannot be built into packets", False,
                    "build_start_packets did not raise")
    except ValueError:
        assert_true(f"{label} cannot be built into packets", True)

# Chemistry-dependent mode byte: sending the lithium value to a nickel cell
# silently starts Break-In instead of a discharge.
li = capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                        current_ma=500, capacity_mah=2000, safety_timer_min=60)
ni = capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.NIMH,
                        current_ma=500, capacity_mah=2000, safety_timer_min=60)
assert_true("lithium discharge is mode byte 0x02", li.mode_byte() == 0x02,
            hex(li.mode_byte()))
assert_true("nickel discharge is mode byte 0x03", ni.mode_byte() == 0x03,
            f"{hex(ni.mode_byte())} - 0x02 would silently start Break-In")
assert_true("chemistry defaults supply the cutoff",
            li.effective_cutoff_mv() == 3200 and ni.effective_cutoff_mv() == 900,
            f"{li.effective_cutoff_mv()} / {ni.effective_cutoff_mv()}")
assert_true("an explicit cutoff wins over the default",
            capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.NIMH,
                               current_ma=500, capacity_mah=2000,
                               safety_timer_min=60,
                               cutoff_mv=1000).effective_cutoff_mv() == 1000)

# target_mv (charge ceiling): honored in-range over BLE for Li-ion (confirmed
# on hardware), but out-of-range values are SILENTLY dropped to the default by the
# firmware - so problem() refuses them loudly rather than charge to a voltage the
# user did not ask for. Verified only for Li-ion, and only meaningful on charge.
def _charge_req(chemistry=Chemistry.LIION, target_mv=None):
    return capture.RunRequest(
        mode=Mode.CHARGE, chemistry=chemistry, current_ma=1500,
        capacity_mah=3500, safety_timer_min=240, target_mv=target_mv)

assert_true("no target voltage is fine (unchanged behavior)",
            _charge_req(target_mv=None).problem() is None)
assert_true("an in-range Li-ion target is accepted (4100)",
            _charge_req(target_mv=4100).problem() is None)
assert_true("both range edges are accepted (4050 and 4250)",
            _charge_req(target_mv=capture.TARGET_MV_MIN).problem() is None
            and _charge_req(target_mv=capture.TARGET_MV_MAX).problem() is None)
assert_true("just below the floor is refused (4049)",
            _charge_req(target_mv=4049).problem() is not None)
assert_true("just above the ceiling is refused (4251)",
            _charge_req(target_mv=4251).problem() is not None)
low = _charge_req(target_mv=3800).problem()
assert_true("the old out-of-range value (3800) is now refused, loudly",
            low is not None and "silently" in low.lower()
            and "4050-4250" in low, str(low))
assert_true("target voltage on a discharge is refused (charge-only)",
            "charge ceiling" in (capture.RunRequest(
                mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
                capacity_mah=2000, safety_timer_min=60,
                target_mv=4100).problem() or ""))
# LIION_HV is monitor-only, so it never reaches the target_mv check in the
# product; the table is patched for this one call (see _problem_if_verified).
_hv = _charge_req(chemistry=Chemistry.LIION_HV, target_mv=4100)
_hv_msg = _problem_if_verified(_hv) or ""
assert_true("a target outside ITS OWN chemistry's window is refused naming it "
            "(Li-ion's 4100 is below Li-ion HV's 4250-4400)",
            "4250-4400" in _hv_msg and "SILENTLY" in _hv_msg, _hv_msg)

# cutoff_mv (discharge floor) is range-gated the same way - firmware 2.02
# the normalizer swaps an out-of-range value for the chemistry default with
# no error, so an unchecked request would end the run at a voltage nobody asked
# for. Windows come from protocol.VOLTAGE_GATES (Li-ion 2500-3300, NiMH 600-1000).
def _dis_req(chemistry=Chemistry.LIION, cutoff_mv=None):
    return capture.RunRequest(
        mode=Mode.DISCHARGE, chemistry=chemistry, current_ma=500,
        capacity_mah=3400, safety_timer_min=240, cutoff_mv=cutoff_mv)

assert_true("no cutoff is fine - the firmware default applies",
            _dis_req(cutoff_mv=None).problem() is None)
assert_true("the house standard 3000mV is in range for Li-ion",
            _dis_req(cutoff_mv=3000).problem() is None)
assert_true("both Li-ion cutoff edges are accepted (2500 and 3300)",
            _dis_req(cutoff_mv=2500).problem() is None
            and _dis_req(cutoff_mv=3300).problem() is None)
assert_true("just outside either Li-ion edge is refused (2499 / 3301)",
            _dis_req(cutoff_mv=2499).problem() is not None
            and _dis_req(cutoff_mv=3301).problem() is not None)
deep = _dis_req(cutoff_mv=2000).problem()
assert_true("a deep-discharge request is refused, loudly, naming the swap",
            deep is not None and "silently" in deep.lower()
            and "2500-3300" in deep and "3200" in deep, str(deep))
# storage_mv (Li_Sto_Volt, config+0xa4) is gated per chemistry, and the nickel
# window is (0, 0) - the chemistry has no storage target at all. The default
# 3800 sits mid-window for Li-ion, which is exactly why the earlier
# probe_storage run could not tell "honored" from "ignored".
def _sto_req(chemistry=Chemistry.LIION, storage_mv=None, mode=Mode.STORAGE):
    return capture.RunRequest(
        mode=mode, chemistry=chemistry, current_ma=500, capacity_mah=3500,
        safety_timer_min=330, storage_mv=storage_mv)

assert_true("a storage run with no explicit target is fine",
            _sto_req().problem() is None)
assert_true("an in-range Li-ion storage target is accepted (3750)",
            _sto_req(storage_mv=3750).problem() is None)
assert_true("both Li-ion storage edges are accepted (3700 and 3850)",
            _sto_req(storage_mv=3700).problem() is None
            and _sto_req(storage_mv=3850).problem() is None)
assert_true("just outside either edge is refused (3699 / 3851)",
            _sto_req(storage_mv=3699).problem() is not None
            and _sto_req(storage_mv=3851).problem() is not None)
assert_true("storage voltage on a discharge is refused (storage-only)",
            "only applies to a storage run" in (
                _sto_req(storage_mv=3750, mode=Mode.DISCHARGE).problem() or ""))
# Nickel storage is monitor-only, so this check is unreachable in the product;
# the table is patched for this one call.
_nsto = _sto_req(chemistry=Chemistry.NIMH, storage_mv=3750)
assert_true("nickel is told it has no storage target at all",
            "no storage-voltage target" in (_problem_if_verified(_nsto) or ""))
assert_true("...and without the override it is monitor-only before that",
            "monitor-only" in (
                _sto_req(chemistry=Chemistry.NIMH, storage_mv=3750).problem() or ""))

# and the value actually reaches the packet, at offsets 36-37
from protocol import build_config as _bc  # noqa: E402
_dflt = _bc(slot=1, mode=Mode.STORAGE, chemistry=Chemistry.LIION,
            charge_current_ma=500, discharge_current_ma=500, capacity_mah=3500)
_odd = _bc(slot=1, mode=Mode.STORAGE, chemistry=Chemistry.LIION,
           charge_current_ma=500, discharge_current_ma=500, capacity_mah=3500,
           storage_mv=3750)
assert_true("omitting storage_mv still sends the 3800 chemistry default",
            int.from_bytes(_dflt[36:38], "big") == 3800)
assert_true("storage_mv lands at packet offsets 36-37",
            int.from_bytes(_odd[36:38], "big") == 3750)
assert_true("and changes nothing else but the checksum",
            [i for i in range(len(_dflt)) if _dflt[i] != _odd[i]] == [37, len(_dflt) - 1])

# capacity_mah is gated too (<=10000), and it is NOT just metadata: the charger
# derives a ceiling from it (runtime+0x1c) and tears the slot down when
# delivered capacity passes it.
assert_true("a normal rated capacity is accepted",
            capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                               current_ma=500, capacity_mah=3400,
                               safety_timer_min=240).problem() is None)
_big = capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                          current_ma=500, capacity_mah=20000,
                          safety_timer_min=240).problem()
assert_true("a capacity above the firmware ceiling is refused, loudly",
            _big is not None and "silently" in _big.lower() and "10000" in _big,
            str(_big))
assert_true("the ceiling itself is accepted",
            capture.RunRequest(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                               current_ma=500, capacity_mah=10000,
                               safety_timer_min=240).problem() is None)

assert_true("the gate is per-chemistry: 3000mV is refused for NiMH",
            _dis_req(chemistry=Chemistry.NIMH, cutoff_mv=3000).problem() is not None)
assert_true("...and 900mV, nonsense for Li-ion, is right for NiMH",
            _dis_req(chemistry=Chemistry.NIMH, cutoff_mv=900).problem() is None
            and _dis_req(cutoff_mv=900).problem() is not None)

print()
print("--- per-slot start injection over the live session ---")
#
# probe_independent.py established that a 0x94 config starts an
# idle slot WITHOUT init and leaves other slots undisturbed.
# drain_start_injections is the live-session seam that does it: guarded by
# refuse_reason at the instant of the write, gated on firmware, and taking
# ownership of the slot so stop-on-exit teardown applies. Its stop counterpart,
# drain_stop_injections, is tested below.
import asyncio as _asyncio          # noqa: E402
import queue as _queue              # noqa: E402
import types as _types              # noqa: E402


class _RecordingClient:
    is_connected = True

    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char, data, response=False):
        self.writes.append(bytes(data))


_inj_req = capture.RunRequest(
    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
    capacity_mah=2000, safety_timer_min=20)
_inj = capture.StartInjection.build(1, _inj_req)
assert_true("StartInjection.build carries the slot's config packet",
            _inj.slot == 1 and _inj.config[2] == 0x94
            and _inj.config == capture.build_start_packets([1], _inj_req)[0][1])

# a) idle slot with a cell -> the config is written and the slot becomes ours.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        icap = capture.Capture(name="inj", slots=[1, 2], outdir=tmp,
                               baseline=None)
        icap.on_notify(None, bytearray(          # idle slot 1, a cell present
            build_status_frame(1, 0, 3850, 0, 0, 40)))
        icap.device_info = _types.SimpleNamespace(firmware="2.02",
                                                  product_code="100214")
        q = _queue.Queue()
        q.put(capture.StartInjection.build(1, _inj_req))
        client = _RecordingClient()
        _asyncio.run(capture.drain_start_injections(
            q, client, "char", icap, lambda *a: None))
        icap.close()
assert_true("an injected start writes the config to an idle slot",
            client.writes == [_inj.config], f"{len(client.writes)} writes")
assert_true("an injected start takes ownership (stop-guard now applies)",
            icap.started_by_us is True)

# b) busy slot -> nothing is written and the slot is NOT taken over. The guard
# fires at the instant of the write, not when the slot was armed.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        bcap = capture.Capture(name="injb", slots=[1], outdir=tmp,
                               baseline=None)
        bcap.on_notify(None, bytearray(          # slot 1 already drawing 700 mA
            build_status_frame(1, 700, 3700, 10, 30, 40)))
        q = _queue.Queue()
        q.put(capture.StartInjection.build(1, _inj_req))
        client = _RecordingClient()
        _asyncio.run(capture.drain_start_injections(
            q, client, "char", bcap, lambda *a: None))
        bcap.close()
assert_true("an injected start is refused on a busy slot (nothing written)",
            client.writes == [] and bcap.started_by_us is False)

# c) an unsupported version refuses even an idle slot - the same gate a CLI
# start faces, and the GUI has no override.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        fcap = capture.Capture(name="injf", slots=[1], outdir=tmp,
                               baseline=None)
        fcap.on_notify(None, bytearray(
            build_status_frame(1, 0, 3850, 0, 0, 40)))
        fcap.device_info = _types.SimpleNamespace(firmware="2.99")
        q = _queue.Queue()
        q.put(capture.StartInjection.build(1, _inj_req))
        client = _RecordingClient()
        _asyncio.run(capture.drain_start_injections(
            q, client, "char", fcap, lambda *a: None))
        fcap.close()
assert_true("an injected start is refused on unvalidated firmware",
            client.writes == [] and fcap.started_by_us is False)

# d) version UNREAD, charger wholly idle: the injection reads 0x57 itself
# before deciding. Here it goes unanswered and nothing is remembered, so the
# start is REFUSED - unread used to mean "start anyway".
_real_aio = capture.asyncio
capture.asyncio = _NoSleep()
try:
    with tempfile.TemporaryDirectory() as tmp:
        with redirect_stdout(io.StringIO()):
            ucap = capture.Capture(name="inju", slots=[1], outdir=tmp,
                                   baseline=None)
            ucap.address = "11:22:33:44:55:66"
            ucap.on_notify(None, bytearray(
                build_status_frame(1, 0, 3850, 0, 0, 40)))
            q = _queue.Queue()
            q.put(capture.StartInjection.build(1, _inj_req))
            client = _RecordingClient()
            logged = []
            _asyncio.run(capture.drain_start_injections(
                q, client, "char", ucap, logged.append))
            ucap.close()
    assert_true("unread firmware on an idle charger: 0x57 is asked, no config",
                client.writes and all(w[2] == 0x57 for w in client.writes)
                and ucap.started_by_us is False,
                f"{[w.hex() for w in client.writes]}")
    assert_true("...and the refusal says what to do",
                any("Couldn't read" in m for m in logged), str(logged))

    # e) same, but this charger was last read as 2.02: the record decides.
    capture.remember_firmware("11:22:33:44:55:66", "2.02",
                              product_code="100214")
    with tempfile.TemporaryDirectory() as tmp:
        with redirect_stdout(io.StringIO()):
            rcap = capture.Capture(name="injr", slots=[1], outdir=tmp,
                                   baseline=None)
            rcap.address = "11:22:33:44:55:66"
            rcap.on_notify(None, bytearray(          # slot 2 busy: no 0x57
                build_status_frame(2, 700, 3700, 10, 30, 40)))
            rcap.on_notify(None, bytearray(
                build_status_frame(1, 0, 3850, 0, 0, 40)))
            q = _queue.Queue()
            q.put(capture.StartInjection.build(1, _inj_req))
            client = _RecordingClient()
            _asyncio.run(capture.drain_start_injections(
                q, client, "char", rcap, lambda *a: None))
            rcap.close()
    assert_true("a busy neighbour blocks 0x57, but a remembered 2.02 starts",
                client.writes == [_inj.config] and rcap.started_by_us,
                f"{[w.hex() for w in client.writes]}")
finally:
    capture.asyncio = _real_aio

# Per-slot STOP: 0xFE addressed to one slot's bitmask (probe_slot_stop).
# The counterpart to start injection - drain_stop_injections
# sends it for each queued slot, halting that slot alone with no init.
from protocol import (build_slot_stop as _bss, build_fe_query as _bfe,   # noqa
                      SLOT_BITMASK as _SB)
assert_true("build_slot_stop is 0xFE addressed to the slot's bitmask",
            _bss(1) == _bfe(_SB[1]) and _bss(1)[2] == 0xFE and _bss(1)[3] == 0x01,
            _bss(1).hex())
assert_true("each slot maps to its own stop packet",
            _bss(2)[3] == 0x02 and _bss(3)[3] == 0x04 and _bss(4)[3] == 0x08)

with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        scap = capture.Capture(name="stopinj", slots=[1, 2], outdir=tmp,
                               baseline=None)
        q = _queue.Queue()
        q.put(1)
        client = _RecordingClient()
        _asyncio.run(capture.drain_stop_injections(
            q, client, "char", scap, lambda *a: None))
        scap.close()
assert_true("a queued stop sends 0xFE to that slot's bitmask, nothing else",
            client.writes == [_bss(1)],
            f"{[w.hex() for w in client.writes]}")

print()
print("--- session() reads nothing SessionOptions does not provide ---")
#
# session() is driven by parsed CLI args OR a SessionOptions, duck-typed. The
# CLI args carry extra attributes (chemistry, name, slots...) that
# SessionOptions deliberately does not, so any session() code reading one of
# those crashes only on the embedded path - which no test executes end to end,
# because session() needs hardware. Guards against a GUI-started run that
# died on a LOG LINE reading args.chemistry, after the guardrails and before
# the config, leaving the charger untouched but the run unstarted.
#
# This audit closes the class: every args.<attr> in session()/run_capture must
# be a SessionOptions field.

import dataclasses
import inspect
import re

session_src = inspect.getsource(capture.session) + inspect.getsource(capture.run_capture)
attrs_read = set(re.findall(r"\bargs\.([a-z_]+)\b", session_src))
option_fields = {f.name for f in dataclasses.fields(capture.SessionOptions)}
unknown = attrs_read - option_fields
assert_true("every attribute session() reads exists on SessionOptions",
            unknown == set(),
            f"session() reads args.{'/args.'.join(sorted(unknown))} which "
            f"SessionOptions does not provide - crashes on the embedded path")
assert_true("the audit actually saw session() reading options",
            len(attrs_read) >= 10, str(sorted(attrs_read)))

print()
print("--- transport errors must not kill a long capture ---")
#
# Seen 9.5 hours into a 37-hour break-in attach: a WinRT write
# failed with OSError("Operation aborted") and the exception propagated out of
# session(), past run_capture's reconnect loop - which only ever inspected a
# RETURNED reason - and killed the process. 67429 samples survived; the run did
# not. The reconnect machinery was already there and simply unreachable for a
# raised error.

class _Up:
    """A session that CONNECTED (notifications flowing) and then ended with
    `outcome` - a reason string, or an exception raised mid-session."""

    def __init__(self, outcome):
        self.outcome = outcome


def _run_capture_with(session_results, cap=None, watch=None):
    """Drive run_capture with a fake session() that yields each item in turn -
    an exception instance is raised, anything else is returned. Wrap an item in
    _Up to mean the session got a live link first. `watch(cap)` runs after each
    session, to observe state as it evolves."""
    calls = []

    async def fake_session(cap, opts, address, **kw):
        if calls and watch:
            watch(cap)
        item = session_results[len(calls)]
        calls.append(item)
        if isinstance(item, _Up):
            cap.link_up = True
            item = item.outcome
        if isinstance(item, BaseException):
            raise item
        return item

    real_session, real_asyncio = capture.session, capture.asyncio
    capture.session, capture.asyncio = fake_session, _NoSleep()
    try:
        cap = cap or SimpleNamespace(gaps=[], log=capture.Log(),
                                     reconnect_attempts=0, link_up=False,
                                     stale_link_warning=None, address=None)
        with redirect_stdout(io.StringIO()) as out:
            reason = asyncio.run(capture.run_capture(cap, None, address="AA:BB"))
        return reason, len(calls), cap.gaps, out.getvalue()
    finally:
        capture.session, capture.asyncio = real_session, real_asyncio


# --- keep-awake: held only while a slot is busy --------------------------------
# It used to be held for as long as the window was open, which kept an idle PC
# awake overnight. A stand-in for the Windows call is installed for every
# run_capture test below so none of them really holds this machine awake.
from unittest import mock  # noqa: E402

_ES_CALLS = []
_ES_RESULT = [1]            # what the stand-in returns; 0 = Windows refused
_ES_RAISE = [None]


def _fake_set_execution_state(flags):
    _ES_CALLS.append(flags)
    if _ES_RAISE[0] is not None:
        raise _ES_RAISE[0]
    return _ES_RESULT[0]


capture._set_execution_state = _fake_set_execution_state
_HOLD = capture.ES_CONTINUOUS | capture.ES_SYSTEM_REQUIRED
_CLEAR = capture.ES_CONTINUOUS


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _guard(platform="win32", result=1, raises=None):
    _ES_CALLS.clear()
    _ES_RESULT[0], _ES_RAISE[0] = result, raises
    lines = []
    clock = _Clock()
    g = capture.AwakeGuard(lines.append, clock=clock)
    return g, clock, lines


with mock.patch.object(capture.sys, "platform", "win32"):
    g, clock, lines = _guard()
    for _ in range(5):
        g.update(False)
        clock.now += 30
    assert_true("an idle app with the window open holds NOTHING (it used to "
                "hold for as long as it was open)",
                _ES_CALLS == [] and not g.held, str(_ES_CALLS))

    g.update(True)
    assert_true("a slot going busy takes the hold - system-required only, "
                "never the display",
                _ES_CALLS == [_HOLD] and g.held
                and not (_ES_CALLS[0] & 0x00000002), str(_ES_CALLS))
    assert_true("...and says so in the log",
                any("asking Windows not to sleep" in m for m in lines),
                str(lines))
    for _ in range(10):
        clock.now += 30
        g.update(True)
    assert_true("busy for a long time: one request, not one per poll",
                _ES_CALLS == [_HOLD], str(_ES_CALLS))
    clock.now += capture.AWAKE_GRACE_S - 1
    g.update(False)
    assert_true("just idle: the hold stays for the grace period (a rest phase "
                "or the gap between two runs must not let the PC sleep)",
                g.held and _ES_CALLS == [_HOLD], str(_ES_CALLS))
    g.update(True)
    clock.now += capture.AWAKE_GRACE_S - 1
    g.update(False)
    assert_true("...and any busy moment restarts the grace period",
                g.held)
    clock.now += 2
    g.update(False)
    assert_true("idle past the grace period: the hold is RELEASED",
                _ES_CALLS == [_HOLD, _CLEAR] and not g.held, str(_ES_CALLS))
    assert_true("...and the log says it let go",
                any("no longer asking Windows" in m for m in lines),
                str(lines))
    g.update(True)
    assert_true("busy again: taken again",
                _ES_CALLS == [_HOLD, _CLEAR, _HOLD], str(_ES_CALLS))
    g.release()
    g.release()
    assert_true("release() at the end clears it once, however often it is "
                "called",
                _ES_CALLS == [_HOLD, _CLEAR, _HOLD, _CLEAR], str(_ES_CALLS))

    g, clock, lines = _guard(result=0)
    g.update(True)
    g.update(True)
    g.release()
    assert_true("a refused request is logged and nothing is cleared that was "
                "never set",
                _ES_CALLS == [_HOLD] and any("refused the request" in m
                                             for m in lines), str(_ES_CALLS))

    g, clock, lines = _guard(raises=OSError("no kernel32"))
    g.update(True)
    g.update(True)
    assert_true("a failing keep-awake call never raises into the capture, and "
                "is not retried every poll",
                any("could not ask Windows" in m for m in lines)
                and len(_ES_CALLS) == 1, f"{lines} {_ES_CALLS}")

with mock.patch.object(capture.sys, "platform", "linux"):
    g, clock, lines = _guard()
    g.update(True)
    assert_true("not Windows: no power request is made at all",
                _ES_CALLS == [] and not g.held, str(_ES_CALLS))
_ES_RESULT[0], _ES_RAISE[0] = 1, None

# What counts as busy.
_idle_st = SimpleNamespace(current_ma=0)
_run_st = SimpleNamespace(current_ma=500)
_cap_b = SimpleNamespace(start_pending={}, open_runs={}, torn_down=set(),
                         latest_any={1: _idle_st})
assert_true("nothing running, nothing open: not busy",
            capture.anything_busy(_cap_b) is False)
_cap_b.latest_any = {1: _run_st}
assert_true("current flowing: busy", capture.anything_busy(_cap_b) is True)
_cap_b.torn_down = {1}
assert_true("...but a torn-down slot's phantom current is not",
            capture.anything_busy(_cap_b) is False)
_cap_b.latest_any = {1: _idle_st}
_cap_b.open_runs = {1: SimpleNamespace(finished=False)}
assert_true("a run open and unfinished is busy even at 0 mA (a rest phase)",
            capture.anything_busy(_cap_b) is True)
_cap_b.open_runs = {1: SimpleNamespace(finished=True)}
assert_true("a finished run is not", capture.anything_busy(_cap_b) is False)
_cap_b.start_pending = {2: {}}
assert_true("a start still being confirmed is busy",
            capture.anything_busy(_cap_b) is True)


def _awake_run(script, platform="win32"):
    """run_capture over a fake session that reports each item of `script` to
    cap.awake (True = something busy) and then ends. Returns the flags sent to
    Windows."""
    _ES_CALLS.clear()

    async def fake_session(cap, opts, address, **kw):
        for busy in script:
            cap.awake.update(busy)
        return "done"

    real_session, real_asyncio = capture.session, capture.asyncio
    capture.session, capture.asyncio = fake_session, _NoSleep()
    try:
        cap = SimpleNamespace(gaps=[], log=capture.Log(), reconnect_attempts=0,
                              link_up=False, stale_link_warning=None,
                              address=None)
        with mock.patch.object(capture.sys, "platform", platform), \
                redirect_stdout(io.StringIO()):
            reason = asyncio.run(capture.run_capture(cap, None, address="AA:BB"))
        return list(_ES_CALLS), reason
    finally:
        capture.session, capture.asyncio = real_session, real_asyncio


calls, reason = _awake_run([False, False])
assert_true("run_capture on an idle charger holds nothing at all",
            calls == [] and reason == "done", f"{calls} {reason}")
calls, reason = _awake_run([False, True])
assert_true("run_capture releases a hold it took, however the capture ends",
            calls == [_HOLD, _CLEAR] and reason == "done", f"{calls} {reason}")


async def _no_device(log, **kw):
    return None


_ES_CALLS.clear()
_real_find = capture.find_device
capture.find_device = _no_device
try:
    with mock.patch.object(capture.sys, "platform", "win32"), \
            redirect_stdout(io.StringIO()):
        _nd_reason = asyncio.run(capture.run_capture(
            SimpleNamespace(log=capture.Log()), None))
finally:
    capture.find_device = _real_find
assert_true("the early no-device return holds nothing",
            _nd_reason == "no-device" and _ES_CALLS == [],
            f"{_nd_reason!r} {_ES_CALLS}")

reason, n, gaps, log_text = _run_capture_with(
    [_Up(OSError("[WinError -2147467260] Operation aborted")), "done"])
assert_true("an OSError out of session() does not escape run_capture",
            reason == "done", f"got {reason!r}")
assert_true("...and the session is retried rather than abandoned", n == 2, str(n))
assert_true("...and the drop is recorded as a reconnect gap",
            len(gaps) == 1, str(gaps))
assert_true("...and the error is reported, not swallowed silently",
            "OSError" in log_text and "Operation aborted" in log_text)

reason, n, _, _wording = _run_capture_with(["disconnected", "no-device", "done"])
assert_true("while it retries the log says the connection was lost, never that "
            "the session 'ended' (it is still reconnecting)",
            "connection lost; retrying in 3s" in _wording
            and "charger not found; retrying in 3s" in _wording
            and "session ended" not in _wording and n == 3, _wording)

reason, n, _, _ = _run_capture_with([capture.BleakError("disconnected"), "done"])
assert_true("a BleakError is absorbed the same way",
            reason == "done" and n == 2, f"{reason!r} after {n}")

reason, n, _, _ = _run_capture_with(["done"])
assert_true("a clean session still returns immediately",
            reason == "done" and n == 1, f"{reason!r} after {n}")

# The guard belongs in BOTH layers: session() turns a failed status query into
# a returned "disconnected" so the normal path handles it, and run_capture
# backstops anything else that raises. Losing either one reopens the hole.
poll_src = inspect.getsource(capture.session)
assert_true("session() guards the status-query write",
            "except (BleakError, OSError)" in poll_src)
assert_true("run_capture backstops a raising session()",
            "except (BleakError, OSError)" in inspect.getsource(capture.run_capture))

print()
print("--- reconnect gaps count LINK LOSSES, not retries ---")
#
# Replays a real run in which two USB-dongle replugs produced
# "reconnect gaps : 53", because a gap was logged per retry and retries fire
# every 3 s while the adapter is away. A gap is now a connected session that
# dropped; attempts are counted separately. Zero must still mean zero.
from bleak.exc import (BleakBluetoothNotAvailableError as _NoAdapter,  # noqa
                       BleakBluetoothNotAvailableReason as _NoAdapterWhy,
                       BleakDeviceNotFoundError as _NotFound)


def _no_adapter():
    return _NoAdapter("No Bluetooth adapter found", _NoAdapterWhy.NO_BLUETOOTH)


def _not_found():
    return _NotFound("AA:BB:CC:00:00:01", "Device with address AA:BB:CC:00:00:01 "
                     "was not found.")


def _fresh_cap():
    return SimpleNamespace(gaps=[], log=capture.Log(), reconnect_attempts=0,
                           link_up=False, stale_link_warning=None, address=None)


reason, n, gaps, _ = _run_capture_with([_Up("done")])
assert_true("a clean capture: zero gaps", gaps == [] and n == 1, str(gaps))

# reconnect_check2's shape: live, dropped (OSError), 25 retries with the
# dongle out, one not-found as it came back, live, dropped again, 24 more
# retries, live to the end.
_dongle = ([_Up(OSError("Operation aborted"))] + [_no_adapter()] * 25
           + [_not_found(), _Up("disconnected")] + [_no_adapter()] * 24
           + [_Up("done")])
_warned = []
cap_d = _fresh_cap()
reason, n, gaps, log_d = _run_capture_with(
    _dongle, cap=cap_d,
    watch=lambda c: _warned.append(bool(c.stale_link_warning)))
assert_true("two dongle replugs are TWO gaps, not 53",
            len(gaps) == 2, f"{len(gaps)} gaps")
assert_true("...while every retry is still counted as an attempt",
            cap_d.reconnect_attempts == len(_dongle) - 1,
            str(cap_d.reconnect_attempts))
assert_true("...and the run completes", reason == "done", str(reason))

reason, n, gaps, _ = _run_capture_with([_not_found(), _Up("done")])
assert_true("a retry that never connected is not a gap", gaps == [], str(gaps))

print()
print("--- the radio-toggle lockup is named, with its cure ---")
#
# Replays a real run: after a Windows radio toggle the
# charger held the severed link and stopped advertising - 15 not-found in a
# row, 9+ minutes, cured only by a power cycle. A dongle replug (above)
# produced ONE not-found and recovered. The warning must fire for the first
# and never for the second.
assert_true("...the dongle replug never raised the stale-link warning",
            not any(_warned), str(_warned))
assert_true("...and said nothing about power-cycling",
            "POWER-CYCLE" not in log_d)

_stuck = ([_Up(OSError("Operation aborted"))] + [_no_adapter()] * 5
          + [_not_found()] * 15 + [_Up("done")])
_seen = []
cap_s = _fresh_cap()
reason, n, gaps, log_s = _run_capture_with(
    _stuck, cap=cap_s, watch=lambda c: _seen.append(c.stale_link_warning))
_first = next((i for i, w in enumerate(_seen) if w), None)
# _seen[i] is the state after session i: 0 is the live one, 1-5 adapter
# errors, 6.. the not-founds. The third not-found is session 8.
assert_true(f"the warning fires on consecutive not-found number "
            f"{capture.STALE_LINK_AFTER}",
            _first == 5 + capture.STALE_LINK_AFTER, str(_first))
_msg = next((w for w in _seen if w), "") or ""
assert_true("it names the POWER CYCLE", "POWER-CYCLE THE CHARGER" in _msg, _msg)
assert_true("it says waiting will not help", "NOT come back on its own" in _msg)
assert_true("it says --attach cannot help", "--attach will not help" in _msg)
assert_true("it says the run carries on unrecorded",
            "nothing is being recorded" in _msg)
assert_true("it reaches the log (the CLI's surface)",
            "POWER-CYCLE THE CHARGER" in log_s)
assert_true("the warning stands while the charger stays missing",
            all(_seen[5 + capture.STALE_LINK_AFTER:]), str(_seen))
assert_true("...and clears once the link is back",
            cap_s.stale_link_warning is None and reason == "done")
assert_true("the stuck episode is ONE link loss", len(gaps) == 1, str(gaps))

_brief = [_Up(OSError("aborted"))] + [_not_found()] * (capture.STALE_LINK_AFTER - 1) \
    + [_Up("done")]
_bseen = []
_run_capture_with(_brief, watch=lambda c: _bseen.append(c.stale_link_warning))
assert_true("a short not-found run below the threshold does not warn",
            not any(_bseen), str(_bseen))

_mixed = ([_Up(OSError("aborted"))]
          + [_not_found(), _not_found(), _no_adapter()] * 4 + [_Up("done")])
_mseen = []
_run_capture_with(_mixed, watch=lambda c: _mseen.append(c.stale_link_warning))
assert_true("not-founds interrupted by adapter errors do not add up",
            not any(_mseen), str(_mseen))

print()
import inspect  # noqa: E402
import math  # noqa: E402

print()
print("--- the ~2 minute re-advertise delay: waiting, not warning ---")
#
# Measured over two sessions: after a CLEAN disconnect
# the charger takes ~2 minutes to advertise again - about four reconnect
# attempts of ~33 s. A drop that behaves the same must not be told to
# power-cycle; the radio-toggle lockup (15 in a row, above) still must.
assert_true("the stale-link threshold clears ~2 min with margin (>= 6 attempts)",
            capture.STALE_LINK_AFTER >= 6, str(capture.STALE_LINK_AFTER))
assert_true("...and stays well inside the 15 of the genuinely stuck case",
            capture.STALE_LINK_AFTER <= 8, str(capture.STALE_LINK_AFTER))
_readv = [_Up(OSError("aborted"))] + [_not_found()] * 4 + [_Up("done")]
_rseen = []
reason, _, _, log_r = _run_capture_with(
    _readv, watch=lambda c: _rseen.append(c.stale_link_warning))
assert_true("four not-founds (the ~2 min re-advertise) raise NO warning",
            not any(_rseen) and "POWER-CYCLE" not in log_r, str(_rseen))
assert_true("...and the capture carries on to the end", reason == "done")
assert_true("the warning itself says a fresh drop normally reappears in ~2 min",
            "within about 2 minutes" in capture.stale_link_message(6))


class _FakeScanner:
    """Stands in for BleakScanner: `misses` empty scans, then the charger."""

    def __init__(self, misses):
        self.misses = misses
        self.timeouts = []

    async def discover(self, timeout):
        self.timeouts.append(timeout)
        others = [SimpleNamespace(name="Govee_H5074_A1B2", address="BB")]
        if len(self.timeouts) > self.misses:
            return others + [SimpleNamespace(name="MC5000 Charger",
                                             address="AA:BB")]
        return others


def _find_with(misses, **kw):
    real = capture.BleakScanner
    capture.BleakScanner = scanner = _FakeScanner(misses)
    try:
        msgs = []
        dev = asyncio.run(capture.find_device(msgs.append, **kw))
        return dev, scanner, msgs
    finally:
        capture.BleakScanner = real


print()
print("--- the first connect keeps looking for ~2 minutes ---")
_scans = math.ceil(capture.CONNECT_WAIT_S / capture.SCAN_TIMEOUT_S)
assert_true("the connect wait covers the ~2 min delay with margin",
            capture.CONNECT_WAIT_S >= 140, str(capture.CONNECT_WAIT_S))

dev, sc, msgs = _find_with(11, wait_s=capture.CONNECT_WAIT_S)
assert_true("a charger that reappears after ~2 min (12th scan) is found",
            dev is not None and dev.address == "AA:BB"
            and len(sc.timeouts) == 12, f"{len(sc.timeouts)} scans")
assert_true("...each scan the standard length",
            set(sc.timeouts) == {capture.SCAN_TIMEOUT_S})
assert_true("...and the user is told it can take a couple of minutes",
            any("can take a couple of minutes to reappear" in m for m in msgs),
            str(msgs[:3]))
assert_true("the explanation is given once, not every scan",
            sum("couple of minutes" in m for m in msgs) == 1)

dev, sc, msgs = _find_with(10**6, wait_s=capture.CONNECT_WAIT_S)
assert_true("a charger that never appears is given up on after the wait",
            dev is None and len(sc.timeouts) == _scans,
            f"{len(sc.timeouts)} scans, expected {_scans}")

dev, sc, msgs = _find_with(0, wait_s=capture.CONNECT_WAIT_S)
assert_true("a charger already advertising is found on the first scan, "
            "with no waiting message",
            dev is not None and len(sc.timeouts) == 1
            and not any("couple of minutes" in m for m in msgs))

_stops = iter([False, False, True])
dev, sc, msgs = _find_with(10**6, wait_s=capture.CONNECT_WAIT_S,
                           stop_requested=lambda: next(_stops, True))
assert_true("closing the window abandons the wait between scans",
            dev is None and len(sc.timeouts) == 2, f"{len(sc.timeouts)} scans")

dev, sc, msgs = _find_with(10**6, attempts=3)
assert_true("the All Stop path keeps its own 3 attempts, unchanged",
            dev is None and len(sc.timeouts) == 3
            and any("attempt 2 of 3" in m for m in msgs))

# run_capture - what the GUI runs - waits the same way when it has to scan.
_real_scanner = capture.BleakScanner
capture.BleakScanner = _sc_rc = _FakeScanner(10**6)
try:
    _rc_cap = SimpleNamespace(gaps=[], log=capture.Log(), reconnect_attempts=0,
                              link_up=False, stale_link_warning=None,
                              address=None)
    with redirect_stdout(io.StringIO()) as _rc_out:
        _rc_reason = asyncio.run(capture.run_capture(_rc_cap, None))
finally:
    capture.BleakScanner = _real_scanner
assert_true("run_capture (the GUI's path) waits the full connect wait",
            _rc_reason == "no-device" and len(_sc_rc.timeouts) == _scans,
            f"{_rc_reason}, {len(_sc_rc.timeouts)} scans")
assert_true("...saying so in the status line the GUI mirrors",
            "couple of minutes" in _rc_out.getvalue())
assert_true("the CLI's first scan waits too",
            "wait_s=CONNECT_WAIT_S" in inspect.getsource(capture.main))
print("--- the firmware gate ---")
#
# Never START on firmware
# that is not in SUPPORTED_FIRMWARE - an exact allowlist held in code, not a
# bench file. firmware_refusal(observed_now, remembered_for_charger, override).

assert_true("the allowlist is exactly 2.02, and immutable",
            capture.SUPPORTED_FIRMWARE == frozenset({"2.02"})
            and isinstance(capture.SUPPORTED_FIRMWARE, frozenset))
assert_true("every supported version has an embedded fingerprint",
            capture.SUPPORTED_FIRMWARE <= set(capture.FIRMWARE_FINGERPRINTS))
assert_true("supported firmware read now may start",
            capture.firmware_refusal("2.02", None, False) is None)
newer = capture.firmware_refusal("2.03", None, False)
assert_true("a NEWER version refuses - an allowlist, not a minimum",
            newer is not None)
assert_true("the refusal is the friendly text from the spec",
            (newer or "").startswith(
                "Firmware 2.03 found; this version supports 2.02. "
                "Watching is available; starting runs is not."), str(newer))
assert_true("...and points at reporting the version",
            "report" in (newer or "").lower(), str(newer))
assert_true("...and does not advertise the developer override",
            "--allow-unvalidated-firmware" not in (newer or ""))
assert_true("an older version refuses too",
            capture.firmware_refusal("1.82", None, False) is not None)
assert_true("the explicit override starts anyway",
            capture.firmware_refusal("2.03", None, True) is None)
unread = capture.firmware_refusal(None, None, False)
assert_true("an UNREAD version with nothing remembered now REFUSES",
            unread is not None and "Watching is available" in unread,
            str(unread))
assert_true("an unread version remembered as 2.02 may start",
            capture.firmware_refusal(None, "2.02", False) is None)
assert_true("an unread version remembered as unsupported refuses",
            "2.03" in (capture.firmware_refusal(None, "2.03", False) or ""))
assert_true("a version read NOW beats the remembered one (charger updated)",
            capture.firmware_refusal("2.03", "2.02", False) is not None)

# The per-charger record in settings.ini, keyed by BLE address.
assert_true("nothing is remembered for a charger never read",
            capture.remembered_firmware("DE:AD:BE:EF:00:01") is None)
assert_true("...or for no address at all",
            capture.remembered_firmware(None) is None)
capture.remember_firmware("de:ad:be:ef:00:01", "2.02")
capture.remember_firmware("DE:AD:BE:EF:00:02", "2.03")
assert_true("a read version is remembered per charger",
            capture.remembered_firmware("DE:AD:BE:EF:00:01") == "2.02"
            and capture.remembered_firmware("DE:AD:BE:EF:00:02") == "2.03")
assert_true("the address key ignores case",
            capture.remembered_firmware("de:ad:be:ef:00:02") == "2.03")
capture.remember_firmware("DE:AD:BE:EF:00:02", "2.02")
assert_true("a later read replaces the record",
            capture.remembered_firmware("DE:AD:BE:EF:00:02") == "2.02")
_real_sp = runfiles.SETTINGS_PATH
try:
    runfiles.SETTINGS_PATH = Path(_SETTINGS_DIR.name) / "corrupt.ini"
    runfiles.SETTINGS_PATH.write_text("this is [not an ini\n= file",
                                     encoding="utf-8")
    assert_true("a damaged settings.ini reads as nothing remembered",
                capture.remembered_firmware("DE:AD:BE:EF:00:01") is None)
    runfiles.SETTINGS_PATH = Path(_SETTINGS_DIR.name) / "no_such_dir" / "s.ini"
    _msgs = []
    capture.remember_firmware("DE:AD:BE:EF:00:01", "2.02", _msgs.append)
    assert_true("an unwritable settings.ini is logged, not raised",
                len(_msgs) == 1, str(_msgs))
finally:
    runfiles.SETTINGS_PATH = _real_sp
assert_true("app files resolve from the program's folder, not the CWD",
            capture.APP_DIR == Path(capture.__file__).resolve().parent
            and runfiles.SETTINGS_PATH.name == "settings.ini")

# The embedded reference replaces baseline.json, which is developer-only now.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        _refcap = capture.Capture(name="ref", slots=[1], outdir=tmp)
        _refcap.close()
_ref = _refcap.validator.baseline
assert_true("by default a capture validates against the embedded 2.02 reference",
            _ref is not None and _ref.firmware == "2.02"
            and _ref.frame_lengths["0x91"] == [24]
            and _ref.frame_lengths["0x55"] == [36], str(_ref))
_fp_a = capture.Fingerprint.builtin("2.02")
_fp_a.frame_lengths["0x91"].append(99)
assert_true("the embedded constant cannot be mutated through a copy",
            capture.Fingerprint.builtin("2.02").frame_lengths["0x91"] == [24])
_bl_path = Path(capture.APP_DIR) / "baseline.json"
if _bl_path.exists():
    _shape = ("frame_lengths", "length_bytes", "commands_seen")
    _bench = json.loads(_bl_path.read_text(encoding="utf-8"))
    assert_true("the embedded 2.02 reference matches the bench baseline.json",
                {k: _bench[k] for k in _shape}
                == {k: capture.FIRMWARE_FINGERPRINTS["2.02"][k] for k in _shape})
_main_src = inspect.getsource(capture.main)
assert_true("the CLI never writes a baseline any more",
            ".save(" not in _main_src)
assert_true("--baseline is read only when passed (no default file)",
            'add_argument("--baseline", default=None' in _main_src)

# The 0x57 response flowing through on_notify: device identity lands on the
# Capture, in the drift fingerprint, and in meta.json.
INFO_FRAME = bytes.fromhex("0f135700313030323134040000000002020200ce57")
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    with redirect_stdout(io.StringIO()):
        devcap = capture.Capture(name="dev", slots=[1], outdir=tmp,
                                 baseline=None, record_idle=True)
        devcap.on_notify(None, bytearray(INFO_FRAME))
        devcap.on_notify(None, bytearray(INFO_FRAME))  # second is ignored
        rows_after_info = devcap.rows
        # One status frame, so there is a run (and a meta.json) to record it in.
        devcap.on_notify(None, bytearray(build_status_frame(1, 0, 3850, 0, 0, 40)))
        devcap.close()
        devcap.save_meta()
    dm2 = meta_of(devcap)

assert_true("0x57 through on_notify yields the device identity",
            devcap.device_info is not None
            and devcap.device_info.firmware == "2.02"
            and devcap.device_info.product_code == "100214",
            str(devcap.device_info))
assert_true("the drift fingerprint carries the firmware at last",
            devcap.validator.current.firmware == "2.02",
            str(devcap.validator.current.firmware))
assert_true("meta.json records which device produced the run",
            dm2["device"] == {"product_code": "100214", "firmware": "2.02",
                              "hardware": "2.00"},
            str(dm2["device"]))
assert_true("...and the fingerprint calls it product_code, not serial",
            dm2["protocol_fingerprint"].get("product_code") == "100214"
            and "serial" not in dm2["protocol_fingerprint"],
            str(dm2["protocol_fingerprint"].get("product_code")))
assert_true("meta.json records which version of the app recorded the run",
            dm2.get("app_version") == capture.APP_VERSION == "1.0.0",
            str(dm2.get("app_version")))
assert_true("an info frame is not a sample",
            rows_after_info == 0, f"{rows_after_info} rows")

print()
print("--- the run's result survives the stop that zeroes the counters ---")
#
# Stopping sends the init sequence, which zeroes elapsed and capacity, and the
# confirmation polls afterward arrive through on_notify like any other frame.
# Reporting cap.latest at that point claims 0mAh for a run that really
# delivered - a 20mAh run would be summarized as "0mAh over 0s". final_by_slot keeps the last reading that still had counters.

with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    with redirect_stdout(io.StringIO()):
        stopped = capture.Capture(name="stopped", slots=[1], outdir=tmp,
                                  baseline=None)
        # a real run, then the zeroed frames a stop produces
        for t in (60, 120, 180):
            stopped.on_notify(None, bytearray(
                build_status_frame(1, 500, 3600, round(500 * t / 3600), t, 40)))
        for _ in range(3):
            stopped.on_notify(None, bytearray(
                build_status_frame(1, 0, 3700, 0, 0, 0)))
        stopped.close()

assert_true("latest holds the post-stop reading",
            stopped.latest[1].capacity_mah == 0
            and stopped.latest[1].elapsed_seconds == 0)
assert_true("final_by_slot holds the run's actual result",
            stopped.final_by_slot[1].capacity_mah == 25
            and stopped.final_by_slot[1].elapsed_seconds == 180,
            f"got {stopped.final_by_slot[1].capacity_mah}mAh over "
            f"{stopped.final_by_slot[1].elapsed_seconds}s")
assert_true("the zeroed frames are still recorded as samples",
            stopped.rows == 6, f"{stopped.rows} rows")

print()
print("--- slots we are not capturing are still remembered ---")
#
# on_notify drops frames for unrequested slots from the CSV, but must still
# record their state: stopping is done with the init sequence, which stops
# EVERY slot, so the stop path has to be able to see whether a slot we did not
# start is busy.

with tempfile.TemporaryDirectory() as tmp:
    cap7, _ = replay(tmp)

assert_true("an unrequested slot never reaches the captured set",
            3 not in cap7.latest, str(sorted(cap7.latest)))
assert_true("but it is remembered for the stop guard",
            3 in cap7.latest_any, str(sorted(cap7.latest_any)))
assert_true("captured slots appear in both",
            {1, 2} <= set(cap7.latest) and {1, 2} <= set(cap7.latest_any))
assert_true("the remembered reading is the parsed status",
            cap7.latest_any[3].current_ma == 1000,
            f"got {cap7.latest_any[3].current_ma}")

print()
print("--- the drift report does not cry wolf over unsent init ---")
#
# baseline.json was recorded on a run that sent init, so it contains the init
# responses. An attach never sends init, so those replies never arrive - which
# says nothing about the firmware. Reporting it on every attach would train the
# reader to ignore the one report that matters.

from drift import Fingerprint  # noqa: E402

base = Fingerprint()
for cmd, frame in (("0x91", b"\x0f\x16\x91\x01"), ("0x65", b"\x0f\x04\x65\x00"),
                   ("0xfe", b"\x0f\x03\xfe\x00")):
    base.observe(frame)

attached = Fingerprint()
attached.observe(b"\x0f\x16\x91\x01")

noisy = base.diff(attached)
assert_true("absent commands are reported by default",
            any("0x65" in d for d in noisy), str(noisy))
quiet = base.diff(attached, ignore_absent=capture.CONTROL_RESPONSE_COMMANDS)
assert_true("init responses are not reported when init was never sent",
            quiet == [], str(quiet))

lost = Fingerprint()
lost.observe(b"\x0f\x04\x65\x00")
lost.observe(b"\x0f\x03\xfe\x00")
still_flagged = base.diff(lost, ignore_absent=capture.CONTROL_RESPONSE_COMMANDS)
assert_true("a genuinely missing status command is still reported",
            any("0x91" in d for d in still_flagged), str(still_flagged))

print()
print("--- constructing a Capture without command-line arguments ---")
#
# The whole point of the keyword constructor: a GUI, a test, or anything else
# embedding this must not have to fabricate an argparse Namespace.

with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    with redirect_stdout(io.StringIO()):
        direct = capture.Capture(
            name="direct",
            slots=[1, 2],
            chemistry=Chemistry.NIMH,
            outdir=tmp,
            baseline="__no_such_baseline__.json",
            cells={1: "ENL-A-01", 2: None},
            run_config=capture.RunConfig(
                mode="discharge", current_ma=500, capacity_mah=2000,
                safety_timer_min=180,
            ),
        )
        for raw in frame_script():
            direct.on_notify(None, bytearray(raw))
        direct.close()
        direct.save_meta()

    dm = meta_of(direct)
    dm_2 = meta_of(direct, 2)

assert_true("constructs with no Namespace anywhere", direct.rows == 34,
            f"{direct.rows} rows")
assert_true("slots taken as a list of ints", direct.slots == [1, 2])
assert_true("chemistry taken as the enum", direct.chemistry == Chemistry.NIMH)
assert_true("chemistry still written under its typed name",
            dm["chemistry"] == "nimh", str(dm["chemistry"]))
assert_true("cells taken as a dict (each run records its own slot's)",
            dm["cells"] == {"1": "ENL-A-01"}
            and dm_2["cells"] == {"2": None}, str(dm["cells"]))
assert_true("run config recorded verbatim",
            (dm["mode"], dm["current_ma"], dm["capacity_mah"],
             dm["safety_timer_min"]) == ("discharge", 500, 2000, 180),
            str({k: dm[k] for k in ("mode", "current_ma", "capacity_mah",
                                    "safety_timer_min")}))
assert_true("subscriber machinery is initialised", direct.subscriber_errors == 0)
assert_true("slot tracking is initialised", direct.finished == {1, 2},
            f"got {direct.finished} - state set up in __init__ must actually run")

try:
    capture.Capture("positional", [1])
    assert_true("positional arguments are refused", False, "no TypeError")
except TypeError:
    assert_true("positional arguments are refused", True)

with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        minimal = capture.Capture(name="minimal", slots=[1], outdir=tmp,
                                  baseline=None)
        minimal.close()
assert_true("defaults cover everything except name and slots",
            minimal.chemistry == Chemistry.LIION
            and minimal.done_after == 5
            and minimal.cells == {1: None}
            and minimal.run_config.mode == "attach")


print()
print("--- 0x55 per-cell temperature in the CSV ---")
#
# The column is SPARSE by design: a reading lands on the next status row for
# that slot and nowhere else. Carrying it forward would invent a measurement
# for timestamps where nothing was measured, and make staleness invisible.
#
# These frames are built from the firmware byte map, the same one the two real
# captured frames in test_protocol.py pin. Only the fields capture.py reads
# are filled - status, slot, and byte 14.


from protocol import SLOT_BITMASK  # noqa: E402


def temp_frame(slot=1, status=3, temp=34):
    """A 0x55 response, per the builder."""
    f = bytearray(36)
    f[0], f[1], f[2], f[3] = 0x0F, 0x22, 0x55, SLOT_BITMASK[slot]
    f[4] = status
    f[14] = temp
    f[33] = 1
    f[35] = sum(f[2:35]) % 256
    return bytes(f)


def temp_replay(outdir, frames):
    """Replay an explicit frame list through a real Capture."""
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args = Namespace(
        name="temps", slots="1,2", chemistry="liion", outdir=outdir,
        baseline="__no_such_baseline__.json",
        lenient=False, done_after=5, cells=None,
        start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        cap = capture.Capture.from_args(args, capture.Log())
        for raw in frames:
            cap.on_notify(None, bytearray(raw))
        cap.close()
    return cap, rows_of(csv_text(cap))


def st_frame(slot, cap_mah, elapsed):
    return build_status_frame(slot, 1500, 4000, cap_mah, elapsed, 45)


with tempfile.TemporaryDirectory() as tmp:
    # temperature arrives, THEN two status rows for that slot
    cap_t, rows_t = temp_replay(tmp, [
        st_frame(1, 25, 60),
        temp_frame(slot=1, temp=34),
        st_frame(1, 50, 120),
        st_frame(1, 75, 180),
    ])

s1 = [r for r in rows_t if r["slot"] == "1"]
assert_true("a 0x55 frame writes no CSV row of its own", len(s1) == 3,
            str(len(s1)))
assert_true("the reading lands on the NEXT status row after it",
            s1[1]["slot_temp_c"] == "34", s1[1]["slot_temp_c"])
assert_true("the row BEFORE the reading is blank", s1[0]["slot_temp_c"] == "")
assert_true("the row AFTER is blank too - sparse, not carried forward",
            s1[2]["slot_temp_c"] == "", s1[2]["slot_temp_c"])
assert_true("the latest reading is kept for consumers that want one",
            cap_t.temp_by_slot == {1: 34}, str(cap_t.temp_by_slot))

with tempfile.TemporaryDirectory() as tmp:
    # a slot that is NOT running: byte 14 is stale buffer content, not a reading
    cap_s, rows_s = temp_replay(tmp, [
        st_frame(1, 25, 60),
        temp_frame(slot=1, status=4, temp=99),
        st_frame(1, 50, 120),
    ])

assert_true("a non-running slot's byte 14 is REFUSED, not written",
            all(r["slot_temp_c"] == "" for r in rows_s),
            str([r["slot_temp_c"] for r in rows_s]))
assert_true("and it does not become the latest reading either",
            cap_s.temp_by_slot == {}, str(cap_s.temp_by_slot))

with tempfile.TemporaryDirectory() as tmp:
    # two slots must not cross-contaminate
    cap_x, rows_x = temp_replay(tmp, [
        temp_frame(slot=1, temp=31),
        temp_frame(slot=2, temp=37),
        st_frame(1, 25, 60),
        st_frame(2, 25, 60),
    ])

got = {r["slot"]: r["slot_temp_c"] for r in rows_x}
assert_true("each slot gets its own reading", got == {"1": "31", "2": "37"},
            str(got))
assert_true("both are remembered separately",
            cap_x.temp_by_slot == {1: 31, 2: 37}, str(cap_x.temp_by_slot))

with tempfile.TemporaryDirectory() as tmp:
    # a truncated or corrupt 0x55 must not crash the capture
    cap_b, rows_b = temp_replay(tmp, [
        bytes.fromhex("0f035501"),
        st_frame(1, 25, 60),
    ])
assert_true("a malformed 0x55 is dropped without killing the run",
            len(rows_b) == 1 and rows_b[0]["slot_temp_c"] == "")

# The reconnect guard: a reading measured before a dropped link must not be
# written onto a row after it. session() clears pending_temp on every connect;
# this pins the mechanism that makes that possible.
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    args_r = Namespace(
        name="recon", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        cap_r = capture.Capture.from_args(args_r, capture.Log())
        cap_r.on_notify(None, bytearray(temp_frame(slot=1, temp=34)))
        stranded = dict(cap_r.pending_temp)
        cap_r.pending_temp.clear()          # what session() does on reconnect
        cap_r.on_notify(None, bytearray(st_frame(1, 25, 60)))
        cap_r.close()
    rows_r = rows_of(csv_text(cap_r))

assert_true("a reading waits in pending_temp until a row consumes it",
            stranded == {1: 34}, str(stranded))
assert_true("clearing it strands nothing onto a later row",
            rows_r[0]["slot_temp_c"] == "", rows_r[0]["slot_temp_c"])
assert_true("but the last-known value survives for display",
            cap_r.temp_by_slot == {1: 34}, str(cap_r.temp_by_slot))

# --- the flag plumbing ---
# outdir goes to a temp dir: constructing a Capture CREATES its run directory,
# so "." would leave stray n_<timestamp>/ folders in the repo root every run.
with tempfile.TemporaryDirectory() as tmp:
    _a_on = Namespace(name="n", slots="1", chemistry="liion", outdir=tmp,
                      baseline=None, lenient=False, done_after=5, cells=None,
                      start=None, current=1, capacity=1, safety_timer=1,
                      cutoff_mv=None, target_mv=None,
                      temps=True, temp_interval=60.0)
    _a_off = Namespace(**{**vars(_a_on), "temps": False})
    with redirect_stdout(io.StringIO()):
        _c_on = capture.Capture.from_args(_a_on, capture.Log())
        _c_off = capture.Capture.from_args(_a_off, capture.Log())
        _c_on.close()
        _c_off.close()
    assert_true("--temps sets the poll interval", _c_on.temp_interval_s == 60.0)
    assert_true("--no-temps disables polling entirely",
                _c_off.temp_interval_s is None)


print()
print("--- session(): the poll loop, driven by a fake BLE client ---")
#
# WHY THIS EXISTS. Everything above replays frames straight into
# Capture.on_notify, and the run_capture tests stub session() out entirely - so
# the poll loop itself had NO offline coverage at all. That gap once
# let an UnboundLocalError reach hardware: the temperature-poll state was
# inserted into stop_all_mode() instead of session() by a replace that matched
# the wrong function, and every capture died on its first poll iteration.
# Nothing here would have needed a charger to catch it.
#
# This is a SMOKE test, not a protocol test: it asserts the loop runs, polls,
# writes rows, sends 0x55, records temperature and terminates. The frame
# decoding is covered thoroughly above and in test_protocol.py.

import asyncio as _aio                                          # noqa: E402
from protocol import build_temperature_query                    # noqa: E402


class _FakeChar:
    uuid = capture.CHAR_UUID


class _FakeService:
    characteristics = [_FakeChar()]


class _FakeClient:
    """Answers 0x91 with a scripted status frame and 0x55 with a temperature.

    Responses are delivered by calling the notify callback inline, which is
    exactly how bleak behaves - subscribers run on its event loop.
    """

    instances = []

    def __init__(self, address, **kwargs):
        self.address = address
        self.is_connected = True
        self.services = [_FakeService()]
        self._cb = None
        self.writes = []          # every packet the session sent
        self.polls = 0            # how many 0x91 rounds have been answered
        _FakeClient.instances.append(self)

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    async def start_notify(self, char, cb):
        self._cb = cb

    async def write_gatt_char(self, char, data, response=False):
        raw = bytes(data)
        self.writes.append(raw)
        if len(raw) < 4 or self._cb is None:
            return
        cmd, chan = raw[2], raw[3]
        slot = {v: k for k, v in SLOT_BITMASK.items()}.get(chan)
        if slot is None:
            return
        if cmd == 0x91:
            self.polls += 1
            # Six active readings, then idle ones so finished-detection fires.
            active = self.polls <= 6
            self._cb(None, bytearray(build_status_frame(
                slot,
                1500 if active else 0,
                3900 if active else 4050,
                25 * self.polls if active else 25 * 6,
                60 * self.polls if active else 60 * 6,
                45)))
        elif cmd == 0x55:
            self._cb(None, bytearray(temp_frame(slot=slot, status=3, temp=33)))


def _session_args(**over):
    a = Namespace(
        fresh=True, start=None, interval=0.01, max_hours=0.05,
        stop_on_drift=False, stop_on_exit=False, report_every=1e9,
        allow_unvalidated_firmware=True, capacity=3400, current=1500,
        cutoff_mv=None, target_mv=None, mode=None,
        safety_timer=240,
    )
    for k, v in over.items():
        setattr(a, k, v)
    return a


def _run_session(tmp, temp_interval=0.0, address="AA:BB:CC:DD:EE:FF",
                 override=False):
    """Drive session() to completion against the fake client."""
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    _FakeClient.instances.clear()

    cap_args = Namespace(
        name="smoke", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
        temps=True, temp_interval=temp_interval,
    )
    real_client, real_sleep = capture.BleakClient, _aio.sleep

    async def _fast_sleep(_d, *a, **k):
        return await real_sleep(0)

    capture.BleakClient = _FakeClient
    _aio.sleep = _fast_sleep
    try:
        with redirect_stdout(io.StringIO()):
            cap = capture.Capture.from_args(cap_args, capture.Log())
            cap.address = address           # run_capture's job, bypassed here
            reason = _aio.run(capture.session(
                cap, _session_args(allow_unvalidated_firmware=override),
                address, first=True))
            cap.close()
    finally:
        capture.BleakClient = real_client
        _aio.sleep = real_sleep
    return cap, reason, _FakeClient.instances[0]


# An attach sends no 0x57, so verification comes from this charger's record.
capture.remember_firmware("AA:BB:CC:DD:EE:FF", "2.02")
with tempfile.TemporaryDirectory() as tmp:
    cap_sm, reason_sm, client_sm = _run_session(tmp, temp_interval=0.0)
    rows_sm = rows_of(csv_text(cap_sm))

# The regression that motivated all of this: the loop must simply RUN.
assert_true("session() completes instead of raising", reason_sm in ("done", "duration"),
            str(reason_sm))
assert_true("it polled the charger", client_sm.polls > 0, str(client_sm.polls))
assert_true("and wrote rows", len(rows_sm) > 0, str(len(rows_sm)))
assert_true("a CLI capture ends when its slot finishes", reason_sm == "done",
            str(reason_sm))
assert_true("the link is closed on the way out", client_sm.is_connected is False)

sent = {w[2] for w in client_sm.writes if len(w) > 2}
assert_true("0x91 status queries went out", 0x91 in sent, str(sorted(sent)))
assert_true("0x55 temperature queries went out too", 0x55 in sent, str(sorted(sent)))
assert_true("the 0x55 packet is exactly what the builder produces",
            build_temperature_query(1) in client_sm.writes)
assert_true("temperature reached the CSV",
            any(r["slot_temp_c"] == "33" for r in rows_sm),
            str([r["slot_temp_c"] for r in rows_sm]))
assert_true("and the latest value is exposed for the GUI",
            cap_sm.temp_by_slot == {1: 33}, str(cap_sm.temp_by_slot))
assert_true("no drift errors on a clean scripted run", cap_sm.drift_errors == 0,
            str(cap_sm.drift_errors))
assert_true("a charger remembered as 2.02 is verified",
            cap_sm.firmware_verified is True)
assert_true("a session that got data marks the link up (a drop = a gap)",
            cap_sm.link_up is True)

# Attach on UNVERIFIED firmware: allowed, but watch-only - no 0x55, labeled.
for _label, _addr, _fw in (("never read", "AA:BB:CC:00:00:01", None),
                           ("remembered as 2.03", "AA:BB:CC:00:00:02", "2.03")):
    if _fw:
        capture.remember_firmware(_addr, _fw)
    with tempfile.TemporaryDirectory() as tmp:
        cap_u, reason_u, client_u = _run_session(tmp, temp_interval=0.0,
                                                 address=_addr)
        rows_u = rows_of(csv_text(cap_u))
        with redirect_stdout(io.StringIO()):
            cap_u.save_meta()
        meta_u = meta_of(cap_u)
    sent_u = {w[2] for w in client_u.writes if len(w) > 2}
    assert_true(f"unverified ({_label}): attaching still works",
                reason_u == "done" and len(rows_u) > 0, str(reason_u))
    assert_true(f"unverified ({_label}): NO 0x55 is sent",
                0x55 not in sent_u, str(sorted(sent_u)))
    assert_true(f"unverified ({_label}): labeled in the capture and meta.json",
                cap_u.firmware_verified is False
                and meta_u["firmware_verified"] is False
                and meta_u["temp_interval_s"] is None, str(meta_u["firmware_verified"]))
    assert_true(f"unverified ({_label}): the missing 0x55 is not called drift",
                "0x55" in cap_u.validator.expected_absent)

# ...but the allow_unvalidated_firmware parameter (code only, not a
# command-line option) keeps 0x55 on, still labeled unverified.
with tempfile.TemporaryDirectory() as tmp:
    cap_o, reason_o, client_o = _run_session(tmp, temp_interval=0.0,
                                             address="AA:BB:CC:00:00:02",
                                             override=True)
    rows_o = rows_of(csv_text(cap_o))
sent_o = {w[2] for w in client_o.writes if len(w) > 2}
assert_true("override on unverified firmware: 0x55 IS polled",
            0x55 in sent_o, str(sorted(sent_o)))
assert_true("...and temperature reaches the CSV",
            any(r["slot_temp_c"] == "33" for r in rows_o))
assert_true("...while the run is still labeled unverified",
            cap_o.firmware_verified is False)
assert_true("...and 0x55 is expected, not excused, by the drift report",
            "0x55" not in cap_o.validator.expected_absent)

# --no-temps must send nothing at all, and still run.
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    _FakeClient.instances.clear()
    cap_args_off = Namespace(
        name="smoke_off", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=1500, capacity=3400,
        safety_timer=240, cutoff_mv=None, target_mv=None,
        temps=False, temp_interval=0.0,
    )
    _rc, _rs = capture.BleakClient, _aio.sleep

    async def _fs(_d, *a, **k):
        return await _rs(0)

    capture.BleakClient = _FakeClient
    _aio.sleep = _fs
    try:
        with redirect_stdout(io.StringIO()):
            cap_off = capture.Capture.from_args(cap_args_off, capture.Log())
            reason_off = _aio.run(capture.session(
                cap_off, _session_args(), "AA:BB:CC:DD:EE:FF", first=True))
            cap_off.close()
    finally:
        capture.BleakClient = _rc
        _aio.sleep = _rs
    client_off = _FakeClient.instances[0]
    rows_off = rows_of(csv_text(cap_off))

sent_off = {w[2] for w in client_off.writes if len(w) > 2}
assert_true("--no-temps sends no 0x55 at all", 0x55 not in sent_off,
            str(sorted(sent_off)))
assert_true("--no-temps still polls status", 0x91 in sent_off)
assert_true("--no-temps still completes the run", reason_off == "done",
            str(reason_off))
assert_true("and leaves every slot_temp_c blank",
            all(r["slot_temp_c"] == "" for r in rows_off))

assert_true("the default temperature poll is 30 s",
            capture.TEMP_INTERVAL_S == 30.0)


print()
print("--- the app-side temperature cutoff ---")
#
# APP-SIDE because the firmware parses TempCut and discards it.
# So the cutoff is a Capture behavior: trip on the
# first reading at or above the line, stop only a run WE started (per-slot
# 0xFE), warn on anything else, and say when it has gone blind.

from capture import RunRequest as _RR, temp_cutoff_problem    # noqa: E402


def _req_tc(tc):
    return _RR(mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
               capacity_mah=3000, safety_timer_min=240, cutoff_mv=3000,
               temp_cutoff_c=tc)


assert_true("no cutoff is fine", _req_tc(None).problem() is None)
assert_true("30 and 65 C are the accepted edges",
            _req_tc(30).problem() is None and _req_tc(65).problem() is None)
assert_true("29 C is refused (a warm idle bay would trip it at once)",
            "30-65" in (_req_tc(29).problem() or ""), str(_req_tc(29).problem()))
assert_true("66 C is refused (too close to the charger's own 70)",
            _req_tc(66).problem() is not None)
assert_true("temp_cutoff_problem is the same rule, for the CLI's early check",
            temp_cutoff_problem(29) and temp_cutoff_problem(45) is None)
assert_true("the cutoff is NOT sent to the charger: the config is byte-identical",
            capture.build_start_packets([1], _req_tc(45))
            == capture.build_start_packets([1], _req_tc(None)))


def _cut_cap(tmp, slots="1,2"):
    args = Namespace(
        name="cut", slots=slots, chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=5,
        cells=None, start=None, current=500, capacity=3000,
        safety_timer=240, cutoff_mv=None, target_mv=None,
    )
    with redirect_stdout(io.StringIO()):
        return capture.Capture.from_args(args, capture.Log())


with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    cc = _cut_cap(tmp)
    cc.begin_slot_run(1, temp_cutoff_c=40)         # ours
    cc.set_temp_cutoff(2, 40)          # not ours (an attached, panel run)
    cc.on_notify(None, bytearray(st_frame(2, 25, 60)))   # ...seen running
    for raw in (temp_frame(1, temp=38), temp_frame(2, temp=38)):
        cc.on_notify(None, bytearray(raw))
    below = (dict(cc.cutoff_trips), set(cc.pending_cutoff_stops))
    for raw in (temp_frame(1, temp=40), temp_frame(2, temp=41),
                temp_frame(1, temp=42)):
        cc.on_notify(None, bytearray(raw))
    cc.save_meta()
    meta_cc = meta_of(cc, 1)
    meta_cc2 = meta_of(cc, 2)
    run1_first = cc.open_runs[1]
    trips1 = dict(cc.cutoff_trips)
    stops1 = set(cc.pending_cutoff_stops)
    peaks1 = dict(cc.peak_temp_by_slot)

    # PER-RUN EXACTNESS: a second run on slot 1 gets its own
    # folder and its own figures, and the first run's record keeps its own.
    with redirect_stdout(io.StringIO()):
        cc.begin_slot_run(1)
    reset_state = (1 not in cc.cutoff_trips and 1 not in cc.peak_temp_by_slot
                   and 1 not in cc.temp_by_slot
                   and 1 not in cc.temp_cutoff_by_slot)
    run1_second = cc.open_runs[1]
    cc.on_notify(None, bytearray(temp_frame(1, temp=33)))
    cc.save_meta()
    first_after = json.loads(run1_first.meta_path.read_text(encoding="utf-8"))
    second = meta_of(cc, 1)
    cc.close()

assert_true("below the line nothing trips", below == ({}, set()), str(below))
assert_true("the FIRST reading at the line trips it (>=, not >)",
            trips1.get(1) == 40, str(trips1))
assert_true("our slot is queued for a per-slot stop",
            stops1 == {1}, str(stops1))
assert_true("a slot we did not start is WARNED about, never queued",
            trips1.get(2) == 41 and 2 not in stops1)
assert_true("one trip per run - a later, hotter reading does not re-trip",
            trips1[1] == 40)
assert_true("peak follows the hottest reading of the run",
            peaks1 == {1: 42, 2: 41}, str(peaks1))
assert_true("each run's meta.json records ITS peak, cutoff and trip",
            meta_cc["peak_slot_temp_c"] == {"1": 42}
            and meta_cc["temp_cutoff_c"] == {"1": 40}
            and meta_cc["temp_cutoff_trips"] == {"1": {"temp_c": 40,
                                                       "action": "warned"}}
            and meta_cc2["peak_slot_temp_c"] == {"2": 41}
            and meta_cc2["temp_cutoff_c"] == {"2": 40}
            and meta_cc2["temp_cutoff_trips"] == {"2": {"temp_c": 41,
                                                        "action": "warned"}},
            f"{meta_cc['temp_cutoff_trips']} / {meta_cc2['temp_cutoff_trips']}")
assert_true("starting a new run resets the slot's trip, peak and reading",
            reset_state)
assert_true("a second run on a slot gets its own folder",
            run1_second.dir != run1_first.dir, str(run1_second.dir))
assert_true("...and its meta.json has only its own peak, no inherited trip",
            second["peak_slot_temp_c"] == {"1": 33}
            and second["temp_cutoff_trips"] == {}
            and second["temp_cutoff_c"] == {}, str(second["peak_slot_temp_c"]))
assert_true("...while the first run's record keeps its peak and trip",
            first_after["peak_slot_temp_c"] == {"1": 42}
            and first_after["temp_cutoff_trips"]["1"]["temp_c"] == 40
            and first_after["end_reason"] == "replaced",
            f"{first_after['peak_slot_temp_c']} {first_after['end_reason']}")

# The blind warning, on a controlled clock.
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    cb = _cut_cap(tmp, slots="1")
    cb.begin_slot_run(1, temp_cutoff_c=45)
    armed = cb._cutoff_since[1]
    cb.update_cutoff_blind(armed + 400)
    idle_blind = set(cb.cutoff_blind)
    cb.on_notify(None, bytearray(st_frame(1, 25, 60)))        # now running
    cb.update_cutoff_blind(armed + 200)
    early = set(cb.cutoff_blind)
    cb.update_cutoff_blind(armed + capture.CUTOFF_BLIND_AFTER_S + 1)
    blind = set(cb.cutoff_blind)
    cb.on_notify(None, bytearray(temp_frame(1, temp=33)))
    got_at = cb.temp_at[1]
    cb.update_cutoff_blind(got_at + 1)
    back = set(cb.cutoff_blind)
    # a single dropped reply at the 30 s cadence is a 60 s gap - never blind
    cb.update_cutoff_blind(got_at + 2 * capture.TEMP_INTERVAL_S)
    one_drop = set(cb.cutoff_blind)
    cb.close()

assert_true("an idle slot is never 'blind' - it reports no temperature anyway",
            idle_blind == set(), str(idle_blind))
assert_true("a running slot is not blind before 5 minutes", early == set())
assert_true("5 minutes with no reading on a running slot: blind",
            blind == {1}, str(blind))
assert_true("a reading clears it", back == set(), str(back))
assert_true("a single dropped reply (the ~3 % case) does not raise it",
            one_drop == set(), str(one_drop))


print()
print("--- an empty bay drops the slot's temperature ---")
# Guards against a finished slot that kept its last run's 36 C after the cell was
# pulled, while every other reading cleared. 0 V is the empty bay.
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    ce = _cut_cap(tmp)
    ce.begin_slot_run(1)
    ce.on_notify(None, bytearray(st_frame(1, 25, 60)))
    ce.on_notify(None, bytearray(temp_frame(1, temp=36)))
    ce.on_notify(None, bytearray(st_frame(1, 26, 62)))
    had = (ce.temp_by_slot.get(1), ce.peak_temp_by_slot.get(1))
    run_e = ce.open_runs[1]
    ce.on_notify(None, bytearray(build_status_frame(1, 0, 0, 0, 0, 0)))
    gone = (1 in ce.temp_by_slot, 1 in ce.temp_at, 1 in ce.peak_temp_by_slot)
    ce.close()
assert_true("a reading was held while the cell was in", had == (36, 36), str(had))
assert_true("...and the empty bay (0 V) clears it, peak included",
            gone == (False, False, False), str(gone))
assert_true("...while the run's own recorded peak is kept",
            run_e.peak_temp_c == 36, str(run_e.peak_temp_c))

print()
print("--- a slot re-run in the same session is finished AGAIN ---")
#
# Seen on the bench: slot 1 ran, finished, then ran again from the
# GUI - and its second end was never reported, because `finished` was set once
# and never cleared. A live frame on a finished slot is a new run.


def _st(cur, cap_mah, el):
    return bytearray(build_status_frame(1, cur, 4000, cap_mah, el, 45))


_rerun_log = io.StringIO()
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(_rerun_log):
    rr = _cut_cap(tmp, slots="1")
    for el in (60, 120, 180):                         # run 1
        rr.on_notify(None, _st(500, el // 12, el))
    for _ in range(5):                                # normal end, counters intact
        rr.on_notify(None, _st(0, 15, 180))
    after_first = (set(rr.finished), rr.all_done())
    rr.on_notify(None, _st(500, 0, 0))                # teardown phantom
    after_phantom = set(rr.finished)
    rr.on_notify(None, _st(0, 0, 0))                  # stop clears the latch
    for el in (30, 60):                               # run 2 begins
        rr.on_notify(None, _st(500, el // 12, el))
    during_second = (set(rr.finished), rr.all_done())
    for el in (90, 120):
        rr.on_notify(None, _st(500, el // 12, el))
    for _ in range(5):                                # run 2 ends
        rr.on_notify(None, _st(0, 10, 120))
    after_second = (set(rr.finished), rr.all_done())
    rr.close()

_announced = _rerun_log.getvalue().count("slot 1 appears FINISHED")
assert_true("the first run finishes", after_first == ({1}, True), str(after_first))
assert_true("a teardown phantom is NOT a new run", after_phantom == {1},
            str(after_phantom))
assert_true("a genuine second run clears 'finished' (and all_done)",
            during_second == (set(), False), str(during_second))
assert_true("...and its end is detected again", after_second == ({1}, True),
            str(after_second))
assert_true("both ends are announced, not just the first", _announced == 2,
            f"{_announced} announcements")
assert_true("the second FINISHED line reports the SECOND run's figures",
            "10mAh over 120s" in _rerun_log.getvalue().split(
                "slot 1 appears FINISHED")[-1].splitlines()[0],
            _rerun_log.getvalue())


# A run started AT THE PANEL on a slot this capture ran earlier must get its own
# folder. A panel start sends an elapsed-1,
# zero-current frame while the charger measures resistance; that frame used to
# overwrite the finished run's "final" elapsed with 1, so the first live frame
# (elapsed 3) no longer looked like "the counter went backward" and the new
# run's rows were appended to the OLD run's folder.
def _panel_run_after(tmp, how_it_ended):
    pc = _cut_cap(tmp, slots="3")
    with redirect_stdout(io.StringIO()):
        pc.begin_slot_run(3)

    def feed(raw):
        with redirect_stdout(io.StringIO()):
            pc.on_notify(None, bytearray(raw))

    for t in range(3, 120, 2):
        feed(build_status_frame(3, 500, 3700, round(500 * t / 3600), t, 13))
    if how_it_ended == "time limit":
        for _ in range(8):                        # teardown: counters 0, echo
            feed(build_status_frame(3, 500, 3668, 0, 0, 0, alert=5))
        for _ in range(8):                        # then a stop
            feed(build_status_frame(3, 0, 3683, 0, 0, 0))
    else:
        for _ in range(8):                        # normal end: counters intact
            feed(build_status_frame(3, 0, 3683, 16, 119, 13))
    before = len([r for r in pc.runs if r.slot == 3])
    feed(build_status_frame(3, 0, 3683, 0, 1, 12))    # the panel run's IR phase
    for t in range(3, 40, 2):
        feed(build_status_frame(3, 500, 3668, round(500 * t / 3600), t, 12))
    runs = [r for r in pc.runs if r.slot == 3]
    with redirect_stdout(io.StringIO()):
        pc.close()
    return before, runs


for _how in ("time limit", "normal end"):
    with tempfile.TemporaryDirectory() as tmp:
        _before, _runs = _panel_run_after(tmp, _how)
        assert_true(f"a panel run after a run that ended on a {_how} "
                    f"opens its own folder", _before == 1 and len(_runs) == 2,
                    f"{_before} -> {len(_runs)} runs")
        if len(_runs) == 2:
            assert_true("...recorded as an outside run, in a different folder",
                        _runs[1].action == "external"
                        and _runs[1].dir != _runs[0].dir,
                        str([r.dir for r in _runs]))


class _BenchClient(_FakeClient):
    """
    A charger with state: each slot idle until a 0x94 config lands on it,
    running until 0xFE stops it. `foreign` slots start out busy (someone
    else's panel run) and go idle after `foreign_polls` status answers.
    Answers 0x57 with a 2.02 info frame, and records whether ANY slot was
    busy at the moment each 0x57 arrived - the gate that must never fail.
    """

    temp = 33
    foreign = {}           # slot -> polls answered busy before going idle
    info = True
    drop_configs = {}      # slot -> how many 0x94 configs to lose (lost writes)
    mute_after_config = set()   # slots whose 0x91 replies stop after a 0x94
    # slot -> alert code: the charger hears the config, never starts, and every
    # status reply carries that alert (a charge sent to a cell that is full).
    end_at_once = {}

    def __init__(self, address, **kwargs):
        super().__init__(address, **kwargs)
        self.alerted = {}
        self.end_at_once = dict(type(self).end_at_once)
        self.running = {s: False for s in (1, 2, 3, 4)}
        self.el = {s: 0 for s in (1, 2, 3, 4)}
        self.left = dict(type(self).foreign)
        for s in self.left:
            self.running[s] = True
        self.info_while_busy = []
        self.drop = dict(type(self).drop_configs)
        self.muted = set()

    async def write_gatt_char(self, char, data, response=False):
        raw = bytes(data)
        self.writes.append(raw)
        if len(raw) < 4 or self._cb is None:
            return
        cmd, chan = raw[2], raw[3]
        if cmd == 0x57:
            self.info_while_busy.append(any(self.running.values()))
            if self.info:
                self._cb(None, bytearray(INFO_FRAME))
            return
        slot = {v: k for k, v in SLOT_BITMASK.items()}.get(chan)
        if slot is None:
            return
        if cmd == 0x94:
            if slot in type(self).mute_after_config:
                self.muted.add(slot)
            if self.drop.get(slot, 0) > 0:
                self.drop[slot] -= 1           # lost in the air
                return
            if slot in self.end_at_once:
                self.alerted[slot] = self.end_at_once[slot]
                return                         # heard, and never started
            self.running[slot] = True
        elif cmd == 0xFE:
            self.running[slot] = False
        elif cmd == 0x91:
            self.polls += 1
            if slot in self.muted:
                return
            if slot in self.alerted:
                self._cb(None, bytearray(build_status_frame(
                    slot, 0, 4200, 0, 0, 0, alert=self.alerted[slot])))
                return
            if slot in self.left:
                self.left[slot] -= 1
                if self.left[slot] <= 0:
                    del self.left[slot]
                    self.running[slot] = False
            if self.running[slot]:
                self.el[slot] += 60
            self._cb(None, bytearray(build_status_frame(
                slot, 500 if self.running[slot] else 0,
                3900 if self.running[slot] else 4050,
                max(1, self.el[slot] // 10) if self.el[slot] else 0,
                # A never-run idle slot reports 0 mOhm, as the charger does
                # (probed: R=0, el=0, status 0).
                self.el[slot], 45 if self.el[slot] else 0)))
        elif cmd == 0x55:
            self._cb(None, bytearray(temp_frame(
                slot=slot, status=3 if self.running[slot] else 0,
                temp=self.temp)))


def _bench_session(tmp, address, start=None, temp_cutoff=None, slots="1",
                   stop_after_polls=None, fw_retry=None,
                   run_until_stopped=False, chemistry="liion",
                   cutoff_mv=3000,
                   stop_on_exit=False, client_cls=None,
                   inject_queue=None, stop_queue=None, all_stop_event=None):
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    _FakeClient.instances.clear()
    cap_args = Namespace(
        name="bench", slots=slots, chemistry=chemistry, outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=3,
        cells=None, start=start, current=500, capacity=3000,
        safety_timer=240, cutoff_mv=3000, target_mv=None,
        temps=True, temp_interval=0.0,
    )
    real_client, real_sleep = capture.BleakClient, _aio.sleep
    real_retry = capture.FIRMWARE_RETRY_S

    async def _fast_sleep(_d, *a, **k):
        return await real_sleep(0)

    capture.BleakClient = client_cls or _BenchClient
    _aio.sleep = _fast_sleep
    if fw_retry is not None:
        capture.FIRMWARE_RETRY_S = fw_retry

    def _stop():
        inst = _FakeClient.instances
        return bool(stop_after_polls and inst
                    and inst[0].polls >= stop_after_polls)
    try:
        with redirect_stdout(io.StringIO()) as _buf:
            cap = capture.Capture.from_args(cap_args, capture.Log())
            cap.address = address
            reason = _aio.run(capture.session(
                cap, _session_args(start=start, temp_cutoff=temp_cutoff,
                                   allow_unvalidated_firmware=False,
                                   cutoff_mv=cutoff_mv, ir_samples=0,
                                   stop_on_exit=stop_on_exit),
                address, first=True, stop_requested=_stop,
                inject_queue=inject_queue, stop_queue=stop_queue,
                all_stop_event=all_stop_event,
                run_until_stopped=run_until_stopped))
            cap.save_meta({"end_reason": reason})
            cap.close()
        # A refused start never opens a run, so there is no meta.json to read.
        cap.meta = meta_of(cap) if cap.runs else {}
        cap.logtext = _buf.getvalue()
    finally:
        capture.BleakClient = real_client
        _aio.sleep = real_sleep
        capture.FIRMWARE_RETRY_S = real_retry
    return cap, reason, _FakeClient.instances[0]


# --- the start goes out once per capture, in the first session that connects --
# Seen twice on the bench: the first connection attempt failed (not found, then a
# timeout) and the next one succeeded - and no run was ever started, because only
# attempt number 1 was allowed to start one. The capture sat there watching an
# idle slot, saying nothing. The gate is now "has a start been SENT".
from bleak.exc import BleakDeviceNotFoundError as _NotFoundErr  # noqa: E402


class _FlakyBench(_BenchClient):
    """_BenchClient whose first `fail_connects` connections never happen, and
    whose first connection that does happen can lose its link after
    `drop_after` polls."""

    fail_connects = 0
    drop_after = None

    async def connect(self):
        if len(_FakeClient.instances) <= type(self).fail_connects:
            raise _NotFoundErr(self.address, "Device was not found.")
        await super().connect()

    async def write_gatt_char(self, char, data, response=False):
        await super().write_gatt_char(char, data, response)
        n = type(self).drop_after
        if (n is not None and self.polls >= n
                and _FakeClient.instances.index(self)
                == type(self).fail_connects):
            self.is_connected = False


def _flaky_run(tmp, start, fail_connects=0, drop_after=None, stop_at=8):
    """run_capture end to end (retries and all) against _FlakyBench; stops once
    `stop_at` status polls have been answered in total."""
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    _FakeClient.instances.clear()
    _FlakyBench.fail_connects, _FlakyBench.drop_after = fail_connects, drop_after
    cap_args = Namespace(
        name="flaky", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=3,
        cells=None, start=start, current=500, capacity=3000,
        safety_timer=240, cutoff_mv=3000, target_mv=None,
        temps=True, temp_interval=0.0,
    )
    real_client, real_sleep = capture.BleakClient, _aio.sleep

    async def _fast_sleep(_d, *a, **k):
        return await real_sleep(0)

    capture.BleakClient = _FlakyBench
    _aio.sleep = _fast_sleep
    try:
        with redirect_stdout(io.StringIO()) as _buf:
            cap = capture.Capture.from_args(cap_args, capture.Log())
            reason = _aio.run(capture.run_capture(
                cap, _session_args(start=start, allow_unvalidated_firmware=False,
                                   ir_samples=0, stop_on_exit=False,
                                   cutoff_mv=3000),
                "AA:BB:CC:00:F1:01",
                stop_requested=lambda: sum(
                    c.polls for c in _FakeClient.instances) >= stop_at))
            cap.close()
        cap.logtext = _buf.getvalue()
    finally:
        capture.BleakClient = real_client
        _aio.sleep = real_sleep
        _FlakyBench.fail_connects, _FlakyBench.drop_after = 0, None
    return cap, reason, list(_FakeClient.instances)


def _configs(instances):
    return [w for c in instances for w in c.writes if len(w) > 2 and w[2] == 0x94]


capture.remember_firmware("AA:BB:CC:00:F1:01", "2.02")
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True

with tempfile.TemporaryDirectory() as tmp:
    cap_f0, reason_f0, inst_f0 = _flaky_run(tmp, "discharge")
assert_true("baseline: a clean first connect sends the start config once",
            len(_configs(inst_f0)) == 1, str(len(_configs(inst_f0))))

with tempfile.TemporaryDirectory() as tmp:
    cap_f1, reason_f1, inst_f1 = _flaky_run(tmp, "discharge", fail_connects=2)
assert_true("two failed connects, then one that works: the run IS started",
            len(inst_f1) == 3 and len(_configs(inst_f1)) == 1,
            f"{len(inst_f1)} connections, {len(_configs(inst_f1))} configs")
assert_true("...and started by us, so it is recorded and stoppable",
            cap_f1.started_by_us and cap_f1.started_slots == {1}
            and len(cap_f1.runs) == 1,
            f"{cap_f1.started_by_us} {cap_f1.started_slots} {len(cap_f1.runs)}")
assert_true("...and the log says the start was late, not silently skipped",
            "starting now" in cap_f1.logtext, cap_f1.logtext)
assert_true("...and the once-per-capture setup ran on that session too",
            cap_f1.setup_done and cap_f1.start_attempted)

with tempfile.TemporaryDirectory() as tmp:
    cap_f2, reason_f2, inst_f2 = _flaky_run(tmp, "discharge", drop_after=3,
                                            stop_at=10)
assert_true("a link lost AFTER the start went out reconnects as a watcher",
            len(inst_f2) == 2, str(len(inst_f2)))
assert_true("...and the start is never sent a second time",
            len(_configs(inst_f2)) == 1, str(len(_configs(inst_f2))))
assert_true("...and the log says so",
            "already sent and is not sent again" in cap_f2.logtext,
            cap_f2.logtext)

with tempfile.TemporaryDirectory() as tmp:
    cap_f3, reason_f3, inst_f3 = _flaky_run(tmp, None, fail_connects=1)
assert_true("an attach whose first connect failed still gets its setup",
            cap_f3.setup_done and "attach: sending no init" in cap_f3.logtext
            and not _configs(inst_f3),
            f"{cap_f3.setup_done} {len(_configs(inst_f3))}")

# A started run that crosses its cutoff is stopped by 0xFE to its slot alone.
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 41, {}, True
with tempfile.TemporaryDirectory() as tmp:
    cap_ct, reason_ct, client_ct = _bench_session(
        tmp, "AA:BB:CC:00:10:01", start="discharge", temp_cutoff=40)
stops_ct = [w for w in client_ct.writes if len(w) > 3 and w[2] == 0xFE]
assert_true("cutoff on a started run: the session sends 0xFE to that slot",
            stops_ct and all(w == capture.build_slot_stop(1) for w in stops_ct),
            str([w.hex() for w in stops_ct]))
assert_true("...never the init sequence (it would stop every slot)",
            not any(w[2] in (0x74, 0x65) for w in client_ct.writes if len(w) > 2))
assert_true("...the slot is recorded as stopped by the cutoff",
            cap_ct.cutoff_stopped == {1} and cap_ct.cutoff_trips == {1: 41},
            str(cap_ct.cutoff_trips))
assert_true("...and the run then ends like any finished run",
            reason_ct == "done", str(reason_ct))
assert_true("...with meta.json saying the cutoff stopped it",
            cap_ct.meta["temp_cutoff_trips"]
            == {"1": {"temp_c": 41, "action": "stopped"}})

# Below the line, the same run is left alone.
_BenchClient.temp = 39
with tempfile.TemporaryDirectory() as tmp:
    cap_nt, _r, client_nt = _bench_session(
        tmp, "AA:BB:CC:00:10:01", start="discharge", temp_cutoff=40,
        stop_after_polls=40)
assert_true("below its cutoff a started run is never stopped",
            not any(len(w) > 2 and w[2] == 0xFE for w in client_nt.writes)
            and cap_nt.cutoff_trips == {})


print()
print("--- the session-length cap never stops a slot ---")
#
# Seen on the bench: a GUI left open for 8 hours ended its capture
# ("duration") and grayed every card - the app's own cap, not a lost link. The
# cap counts from CONNECT, not from the run's start, and the exit path then
# sent 0xFE to every slot the app had started, so a long run could be stopped
# part-way by a clock that had nothing to do with it. So the GUI has
# no cap by default, and everywhere the cap only ENDS THE RECORDING - the run
# carries on under its own safety timer and is recorded as such.
#
# The clock is faked: every loop.time() call moves it 30 s on, so an 8-hour cap
# is reached in a few hundred polls and the test needs no real waiting.


def _capped_session(tmp, address, max_hours, run_until_stopped=False,
                    stop_after_polls=None, stop_on_exit=True):
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    _FakeClient.instances.clear()
    cap_args = Namespace(
        name="capped", slots="1", chemistry="liion", outdir=tmp,
        baseline="__no_such_baseline__.json", lenient=False, done_after=3,
        cells=None, start="discharge", current=500, capacity=3000,
        safety_timer=240, cutoff_mv=3000, target_mv=None,
        temps=True, temp_interval=0.0,
    )
    real_client, real_sleep = capture.BleakClient, _aio.sleep

    async def _fast_sleep(_d, *a, **k):
        return await real_sleep(0)

    loop = _aio.new_event_loop()
    real_time, ticks = loop.time, [0]

    def _fake_time():
        ticks[0] += 1
        return real_time() + ticks[0] * 30.0
    loop.time = _fake_time

    def _stop():
        inst = _FakeClient.instances
        return bool(stop_after_polls and inst
                    and inst[0].polls >= stop_after_polls)
    capture.BleakClient = _BenchClient
    _aio.sleep = _fast_sleep
    try:
        with redirect_stdout(io.StringIO()) as _buf:
            cap = capture.Capture.from_args(cap_args, capture.Log())
            cap.address = address
            reason = loop.run_until_complete(capture.session(
                cap, _session_args(start="discharge", max_hours=max_hours,
                                   allow_unvalidated_firmware=False,
                                   cutoff_mv=3000, ir_samples=0,
                                   stop_on_exit=stop_on_exit),
                address, first=True, stop_requested=_stop,
                run_until_stopped=run_until_stopped))
            cap.save_meta({"end_reason": reason})
            cap.close()
        cap.meta = meta_of(cap) if cap.runs else {}
        cap.logtext = _buf.getvalue()
    finally:
        capture.BleakClient = real_client
        _aio.sleep = real_sleep
        loop.close()
    return cap, reason, _FakeClient.instances[0], ticks[0] * 30.0


capture.remember_firmware("AA:BB:CC:00:10:02", "2.02")
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True

# CLI path: --max-hours reached with a run the app started and still going.
with tempfile.TemporaryDirectory() as tmp:
    cap_cp, reason_cp, client_cp, _t = _capped_session(
        tmp, "AA:BB:CC:00:10:02", max_hours=8)
assert_true("the cap ends the session as 'duration'", reason_cp == "duration",
            str(reason_cp))
assert_true("the run was started here and is still going on the charger",
            cap_cp.started_slots == {1} and client_cp.running[1] is True,
            f"{cap_cp.started_slots} {client_cp.running}")
assert_true("the cap sends NO stop to any slot (no 0xFE)",
            not any(len(w) > 2 and w[2] == 0xFE for w in client_cp.writes),
            str([w.hex() for w in client_cp.writes if len(w) > 2
                 and w[2] == 0xFE]))
assert_true("...and never the init sequence either",
            not any(len(w) > 2 and w[2] in (0x74, 0x65)
                    for w in client_cp.writes))
assert_true("the link is still closed on the way out",
            client_cp.is_connected is False)
assert_true("meta.json says only the RECORDING ended",
            cap_cp.meta.get("end_reason") == "recording_ended",
            str(cap_cp.meta.get("end_reason")))
assert_true("...which readers treat as incomplete, never as a full measurement",
            cap_cp.meta.get("end_reason") not in ("done", "duration"))
assert_true("the log says the run carries on and nothing was stopped",
            "carries on" in cap_cp.logtext and "slot 1" in cap_cp.logtext,
            cap_cp.logtext[-600:])

# GUI path: no cap by default - well past 8 hours of (fake) loop time the
# session is still polling, and only the stop request ends it.
with tempfile.TemporaryDirectory() as tmp:
    cap_gp, reason_gp, client_gp, t_gp = _capped_session(
        tmp, "AA:BB:CC:00:10:02", max_hours=None, run_until_stopped=True,
        stop_after_polls=1500, stop_on_exit=False)
assert_true("with no cap the session runs on until it is told to stop",
            reason_gp == "stopped", str(reason_gp))
assert_true("...past 8 hours of loop time (so a cap would have fired)",
            t_gp > 8 * 3600 and client_gp.polls >= 1500,
            f"{client_gp.polls} polls, {t_gp:.0f} s")
assert_true("...and left the run alone (no stop sent, still running)",
            client_gp.running[1] is True
            and not any(len(w) > 2 and w[2] == 0xFE for w in client_gp.writes))
assert_true("the CLI keeps its own default cap",
            capture.SessionOptions().max_hours == 6.0)


print()
print("--- the exit stops our slots ONLY on a deliberate end ---")
#
# The `finally` in session() used to stop every slot the app started whenever
# the client still reported connected - whatever the reason for leaving. A
# WinRT write that fails ("Operation aborted", seen on hardware) while the link
# still looks up therefore stopped a multi-hour run, and stop_our_slots never
# said it had, so the run was recorded as a normal "done".


class _WriteFailsStillConnected(_BenchClient):
    """After `fail_after` status polls every write raises OSError, but the
    client keeps reporting itself connected - the case that matters."""

    fail_after = 6

    async def write_gatt_char(self, char, data, response=False):
        # Only the polling writes fail, so a stop that wrongly fired at exit
        # would get through and be seen.
        if (self.polls >= type(self).fail_after and len(data) > 2
                and bytes(data)[2] in (0x91, 0x55)):
            raise OSError("Operation aborted (simulated WinRT write failure)")
        await super().write_gatt_char(char, data, response)


capture.remember_firmware("AA:BB:CC:00:50:01", "2.02")
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True

with tempfile.TemporaryDirectory() as tmp:
    cap_x1, reason_x1, client_x1 = _bench_session(
        tmp, "AA:BB:CC:00:50:01", start="discharge", stop_on_exit=True,
        client_cls=_WriteFailsStillConnected)
stops_x1 = [w for w in _FakeClient.instances[0].writes
            if len(w) > 2 and w[2] in (0xFE, 0x74, 0x65)]
assert_true("a failed write ends the session as 'disconnected' (reconnect)",
            reason_x1 == "disconnected", str(reason_x1))
assert_true("...and the exit sends NO stop to the run it started",
            not stops_x1, str([w.hex() for w in stops_x1]))
assert_true("...the run is still going on the charger",
            client_x1.running[1] is True, str(client_x1.running))

with tempfile.TemporaryDirectory() as tmp:
    cap_x2, reason_x2, client_x2 = _bench_session(
        tmp, "AA:BB:CC:00:50:01", start="discharge", stop_on_exit=True,
        stop_after_polls=20)
stops_x2 = [w for w in client_x2.writes if len(w) > 2 and w[2] == 0xFE]
assert_true("a DELIBERATE stop still stops the slot it started",
            reason_x2 == "stopped" and stops_x2
            and all(w == capture.build_slot_stop(1) for w in stops_x2)
            and client_x2.running[1] is False,
            f"{reason_x2} {[w.hex() for w in stops_x2]}")
assert_true("...and the run is recorded as STOPPED, never 'done' (analysis "
            "reads 'done' as a full measurement)",
            cap_x2.meta.get("end_reason") == "stopped",
            str(cap_x2.meta.get("end_reason")))

# A stop-on-exit that reaches the slot marks the run, whatever the session says.
cap_x3 = capture.Capture.from_args(Namespace(
    name="x3", slots="1", chemistry="liion", outdir=tempfile.mkdtemp(),
    baseline="__no_such_baseline__.json", lenient=False, done_after=3,
    cells=None, start="discharge", current=500, capacity=3000,
    safety_timer=240, cutoff_mv=3000, target_mv=None, temps=True,
    temp_interval=0.0), capture.Log())
cap_x3.started_slots = {1}
cap_x3.latest_any[1] = parse_status_response(
    build_status_frame(1, 500, 3900, 10, 60, 45))
cap_x3.begin_slot_run(1, capture.RunRequest(
    mode=capture.Mode.DISCHARGE, chemistry=capture.Chemistry.LIION,
    current_ma=500, capacity_mah=3000, safety_timer_min=240))


class _Quiet:
    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char, data, response=False):
        self.writes.append(bytes(data))


with redirect_stdout(io.StringIO()):
    _aio.run(capture.stop_our_slots(_Quiet(), object(), cap_x3, lambda m: None))
assert_true("stop_our_slots records each stop it sends on the run",
            cap_x3.open_runs[1].stop_sent is True
            and 1 in cap_x3.user_stopped,
            str(cap_x3.open_runs[1].stop_sent))


print()
print("--- Configure's starting currents are derived from the capacity "
      "---")
#
# The panel's own currents (Li-ion 3 A charge / 2 A discharge, 1 A nickel) suit
# a 5000 mAh cell; an 800 mAh Li-ion cell opened at 3 A. Charge is 0.5C and
# discharge 0.2C for Li-ion, NiMH and Eneloop alike, capped at the panel's own
# default and rounded to 10 mA.
from capture import derive_current_ma as _dcm                         # noqa: E402
from protocol import Chemistry as _Cd, Mode as _Md                    # noqa: E402

_CASES = [
    # chemistry, mode, capacity, expected
    (_Cd.LIION, _Md.CHARGE, 800, 400),            # was 3000: 3.75C on this cell
    (_Cd.LIION, _Md.CHARGE, 3400, 1700),
    (_Cd.LIION, _Md.CHARGE, 5000, 2500),
    (_Cd.LIION, _Md.CHARGE, 9000, 3000),          # 0.5C = 4500, held at the panel's
    (_Cd.LIION, _Md.DISCHARGE, 800, 160),
    (_Cd.LIION, _Md.DISCHARGE, 3400, 680),        # was 2000
    (_Cd.LIION, _Md.DISCHARGE, 9000, 1800),
    (_Cd.LIION, _Md.DISCHARGE, 10000, 2000),      # 0.2C = 2000, exactly the cap
    (_Cd.NIMH, _Md.CHARGE, 2000, 1000),           # 0.5C = 1000 = the panel's
    (_Cd.NIMH, _Md.CHARGE, 2600, 1000),           # 1300, held at 1000
    (_Cd.NIMH, _Md.DISCHARGE, 2500, 500),         # the house standard rate
    (_Cd.NIMH, _Md.DISCHARGE, 3000, 600),         # was 1000
    (_Cd.ENELOOP, _Md.CHARGE, 800, 400),
    (_Cd.ENELOOP, _Md.DISCHARGE, 2000, 400),      # was 1000
    (_Cd.LIION, _Md.DISCHARGE, 2300, 460),
    (_Cd.LIION, _Md.CHARGE, 2300, 1150),          # rounded to the 10 mA step
    (_Cd.LIION, _Md.CHARGE, 2310, 1160),          # 1155 rounds up
    (_Cd.LIION, _Md.CHARGE, 2308, 1150),          # 1154 rounds down
    (_Cd.LIION, _Md.DISCHARGE, 100, 50),          # 0.2C = 20, held at the panel's lowest
    (_Cd.LIION, _Md.CHARGE, 100, 50),             # 0.5C = 50, exactly the lowest
    (_Cd.NIMH, _Md.DISCHARGE, 100, 50),
]
_bad = [(c.name, m.name, cap, want, _dcm(c, m, cap))
        for c, m, cap, want in _CASES if _dcm(c, m, cap) != want]
assert_true("every chemistry and action derives 0.5C charge / 0.2C discharge, "
            "capped at the panel's default and rounded to 10 mA",
            not _bad, str(_bad))
assert_true("none of the derived values is the old fixed default for a small "
            "cell (the old code gave 3000 / 2000 / 1000 whatever the cell)",
            _dcm(_Cd.LIION, _Md.CHARGE, 800) != 3000
            and _dcm(_Cd.LIION, _Md.DISCHARGE, 800) != 2000
            and _dcm(_Cd.NIMH, _Md.DISCHARGE, 2500) != 1000)
assert_true("nothing is derived without a capacity",
            _dcm(_Cd.LIION, _Md.CHARGE, None) is None
            and _dcm(_Cd.LIION, _Md.CHARGE, 0) is None
            and _dcm(_Cd.LIION, _Md.CHARGE, -5) is None)
assert_true("...nor for an action this does not cover (Cycle has none)",
            _dcm(_Cd.LIION, _Md.CYCLE, 3000) is None)

# Storage: each leg is the same operation as the plain run.
_dsd = capture.derive_storage_discharge_ma
_STO = [(800, 400, 160), (3400, 1700, 680), (5000, 2500, 1000),
        (9000, 3000, 1800), (10000, 3000, 2000), (12000, 3000, 2000),
        (2300, 1150, 460), (100, 50, 50)]
_bad = [(cap, _dcm(_Cd.LIION, _Md.STORAGE, cap), _dsd(_Cd.LIION, cap), c, d)
        for cap, c, d in _STO
        if (_dcm(_Cd.LIION, _Md.STORAGE, cap), _dsd(_Cd.LIION, cap)) != (c, d)]
assert_true("Li-ion Storage derives 0.5C charge and 0.2C discharge, each "
            "capped at the panel's 3000 / 2000 and rounded to 10 mA",
            not _bad, str(_bad))
assert_true("...and neither is the old fixed 3000 / 2000 for a small cell",
            _dcm(_Cd.LIION, _Md.STORAGE, 800) != 3000
            and _dsd(_Cd.LIION, 800) != 2000)
assert_true("the storage discharge leg is exactly what a Discharge run gets "
            "(same operation), up to the panel's cap",
            all(_dsd(_Cd.LIION, cap) == min(
                _dcm(_Cd.LIION, _Md.DISCHARGE, cap), 2000)
                for cap in (100, 800, 2300, 3400, 5000, 10000, 12000)))
assert_true("no storage discharge without a capacity, or for a chemistry "
            "with no storage run",
            _dsd(_Cd.LIION, None) is None and _dsd(_Cd.LIION, 0) is None
            and _dsd(_Cd.NIMH, 2000) is None
            and _dcm(_Cd.NIMH, _Md.STORAGE, 2000) is None)
# "Cover the worse case plus margin": a full cell discharged to the storage
# voltage or an empty one charged to it. Neither moves more than the whole
# capacity, so the timer must outlast the WHOLE capacity at the slower leg.
_short = []
for cap in (800, 2300, 3400, 5000, 9000, 12000):
    c, d = _dcm(_Cd.LIION, _Md.STORAGE, cap), _dsd(_Cd.LIION, cap)
    mins, _capped = capture.suggest_safety_timer_min(
        _Cd.LIION, _Md.STORAGE, cap, min(c, d))
    worst = max(cap / c, cap / d) * 60          # whole capacity, slower leg
    if not (mins >= worst * 1.5 or mins == capture.SAFETY_TIMER_MIN_MAX):
        _short.append((cap, c, d, mins, worst))
assert_true("the storage timer covers the whole capacity at the slower leg "
            "plus the 1.5 margin (or is held at the charger's 24 h limit)",
            not _short, str(_short))
assert_true("a derived storage pair is always acceptable to "
            "RunRequest.problem()",
            all(capture.RunRequest(
                mode=_Md.STORAGE, chemistry=_Cd.LIION,
                current_ma=_dcm(_Cd.LIION, _Md.STORAGE, cap),
                discharge_current_ma=_dsd(_Cd.LIION, cap),
                capacity_mah=cap, safety_timer_min=480).problem() is None
                for cap in (100, 800, 3400, 10000)))
assert_true("...nor for a monitor-only chemistry",
            _dcm(_Cd.NICD, _Md.CHARGE, 2000) is None
            and _dcm(_Cd.LIFEPO4, _Md.CHARGE, 2000) is None)
assert_true("a derived current is always acceptable to RunRequest.problem()",
            all(capture.RunRequest(
                mode=m, chemistry=c, current_ma=_dcm(c, m, cap),
                capacity_mah=cap, safety_timer_min=240).problem() is None
                for c, m, cap, _w in _CASES
                if not capture.control_verified(c, m) is False))

print()
print("--- a start needs the MC5000's product code ---")
#
# Any device whose name contains "charger" and whose 0x57 reply decoded to a
# supported firmware could be started. The product code (100214 = SK-100214,
# the MC5000; the MC3000 answers 100083) is parsed already and tells the models
# apart.
_ok = capture.firmware_refusal("2.02", None, False, product="100214")
assert_true("the MC5000 (100214) on supported firmware may start",
            _ok is None, str(_ok))
_other = capture.firmware_refusal("2.02", None, False, product="100083")
assert_true("another SkyRC model on the same firmware number is refused, "
            "naming both codes",
            _other and "100083" in _other and "100214" in _other
            and "MC5000" in _other, str(_other))
_unk = capture.firmware_refusal(None, "2.02", False, product=None)
assert_true("a remembered firmware with NO known model is refused, saying why "
            "and what to do",
            _unk and "model" in _unk and "idle" in _unk, str(_unk))
assert_true("...but the existing three-argument call is unchanged",
            capture.firmware_refusal("2.02", None, False) is None)
assert_true("an unsupported firmware is still refused as before, whatever the "
            "model",
            "Firmware 2.50" in (capture.firmware_refusal(
                "2.50", None, False, product="100214") or ""))
assert_true("remember_firmware stores the product code beside the version",
            (capture.remember_firmware("AA:BB:CC:00:80:01", "2.02",
                                       product_code="100214"),
             capture.remembered_product("AA:BB:CC:00:80:01"))[1] == "100214")

# End to end: a charger that answers 0x57 with ANOTHER product code is refused,
# and nothing is sent.
_OTHER_INFO = bytearray(bytes.fromhex(
    "0f135700313030323134040000000002020200ce57"))
_OTHER_INFO[4:10] = b"100083"
_OTHER_INFO[-1] = sum(_OTHER_INFO[2:-1]) % 256


class _OtherModelBench(_BenchClient):
    async def write_gatt_char(self, char, data, response=False):
        raw = bytes(data)
        if len(raw) > 2 and raw[2] == 0x57:
            self.writes.append(raw)
            self._cb(None, bytearray(_OTHER_INFO))
            return
        await super().write_gatt_char(char, data, response)


capture.remember_firmware("AA:BB:CC:00:80:02", "2.02", product_code="100214")
with tempfile.TemporaryDirectory() as tmp:
    cap_pc, reason_pc, client_pc = _bench_session(
        tmp, "AA:BB:CC:00:80:02", start="discharge",
        client_cls=_OtherModelBench)
assert_true("a different model answering 0x57: the start is REFUSED",
            reason_pc == "refused" and not cap_pc.started_by_us
            and "product code 100083" in cap_pc.logtext, cap_pc.logtext[-400:])
assert_true("...and no config reached it",
            not any(len(w) > 2 and w[2] == 0x94 for w in client_pc.writes))

# meta.json's protocol_fingerprint names a firmware only once it was READ.
with tempfile.TemporaryDirectory() as tmp:
    capture.datetime = FakeDateTime
    FakeDateTime._tick = 0
    with redirect_stdout(io.StringIO()):
        _fpcap = capture.Capture(name="fp", slots=[1], outdir=tmp,
                                 baseline=None, record_idle=True)
        _before = (_fpcap.validator.current.firmware,
                   _fpcap.validator.current.product_code)
        _fpcap.on_notify(None, bytearray(INFO_FRAME))
        _after = (_fpcap.validator.current.firmware,
                  _fpcap.validator.current.product_code)
        _fpcap.close()
assert_true("the observed fingerprint is EMPTY of identity until 0x57 is read "
            "(it was pre-filled with the baseline's 2.02)",
            _before == (None, None), str(_before))
assert_true("...and holds what the charger said once it has been",
            _after == ("2.02", "100214"), str(_after))

print()
print("--- no command outlives its link ---")
#
# A Start pressed while the link was down was queued with no timestamp and
# written whenever a session next connected - after a sleep, a power cycle,
# hours later, possibly onto a different cell. Everything waiting is now
# dropped when a session ends and again as the next one comes up.
import queue as _q3                                                 # noqa: E402
import threading as _t3                                             # noqa: E402

capture.remember_firmware("AA:BB:CC:00:60:01", "2.02")
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True
_req3 = capture.RunRequest(
    mode=capture.Mode.DISCHARGE, chemistry=capture.Chemistry.LIION,
    current_ma=500, capacity_mah=3000, safety_timer_min=240)
_inj, _stp, _alls = _q3.Queue(), _q3.Queue(), _t3.Event()
_inj.put(capture.StartInjection.build(1, _req3))       # queued during an outage
_stp.put(1)
_alls.set()
with tempfile.TemporaryDirectory() as tmp:
    cap_l1, reason_l1, client_l1 = _bench_session(
        tmp, "AA:BB:CC:00:60:01", stop_after_polls=8, run_until_stopped=True,
        inject_queue=_inj, stop_queue=_stp, all_stop_event=_alls)
_sent3 = [w[2] for w in client_l1.writes if len(w) > 2]
assert_true("a start queued before the link came up is NEVER sent",
            0x94 not in _sent3, str(sorted(set(_sent3))))
assert_true("...nor a queued per-slot stop, nor a pending ALL STOP",
            0xFE not in _sent3 and 0x74 not in _sent3 and 0x65 not in _sent3,
            str(sorted(set(_sent3))))
assert_true("...the queues and the event are empty, and the log says what "
            "was dropped",
            _inj.empty() and _stp.empty() and not _alls.is_set()
            and "dropped 3 command(s)" in cap_l1.logtext
            and "Nothing is sent late" in cap_l1.logtext,
            cap_l1.logtext[-500:])
assert_true("no run was started by it",
            not cap_l1.started_by_us and cap_l1.started_slots == set())

# The link is over the moment a session ends, whatever was still waiting.
cap_l2 = capture.Capture.from_args(Namespace(
    name="l2", slots="1", chemistry="liion", outdir=tempfile.mkdtemp(),
    baseline="__no_such_baseline__.json", lenient=False, done_after=3,
    cells=None, start=None, current=500, capacity=3000, safety_timer=240,
    cutoff_mv=3000, target_mv=None, temps=True, temp_interval=0.0),
    capture.Log())
cap_l2.start_pending[1] = {"config": b"x", "sent_at": 0.0, "tries": 1,
                           "first_at": 0.0}
_inj.put(capture.StartInjection.build(1, _req3))
_msgs = []
n_l2 = capture.flush_pending_commands(cap_l2, _inj, _stp, _alls, _msgs.append)
assert_true("flushing also forgets a start being watched for a re-send (it "
            "would RE-SEND the config after a reconnect)",
            n_l2 == 2 and cap_l2.start_pending == {} and _inj.empty(),
            f"{n_l2} {cap_l2.start_pending}")
assert_true("...and an empty flush says nothing",
            capture.flush_pending_commands(cap_l2, _inj, _stp, _alls,
                                           _msgs.append) == 0
            and len(_msgs) == 1)
assert_true("link_ok is down once the session has ended",
            cap_l1.link_ok is False)

print()
print("--- a frame whose handling raises is logged, never silent ---")
#
# bleak runs the notification callback as an asyncio callback, where an
# exception is dropped without a trace: no crash log, no message, the cards
# just stop updating. The capture now guards it, writes the error up once per
# distinct error, and carries on.
import os as _os5                                                   # noqa: E402
import crashlog as _cl5                                             # noqa: E402

_cl5_dir = tempfile.mkdtemp()
_os5.environ["SITREP91_LOG_DIR"] = _cl5_dir
_cl5.reset_for_tests()
_real_on_notify = capture.Capture.on_notify


def _on_notify_breaks(self, sender, data):
    if len(data) > 2 and bytes(data)[2] == 0x91:
        raise OSError("disk full (simulated)")
    return _real_on_notify(self, sender, data)


capture.remember_firmware("AA:BB:CC:00:70:01", "2.02")
capture.Capture.on_notify = _on_notify_breaks
try:
    with tempfile.TemporaryDirectory() as tmp:
        cap_e1, reason_e1, client_e1 = _bench_session(
            tmp, "AA:BB:CC:00:70:01", stop_after_polls=10,
            run_until_stopped=True)
finally:
    capture.Capture.on_notify = _real_on_notify
_crash_files = sorted(Path(_cl5_dir).glob("crash_*.log"))
assert_true("the session survives a handler that raises on every frame",
            reason_e1 == "stopped", str(reason_e1))
assert_true("...every failed frame is counted",
            cap_e1.frame_errors >= 10, str(cap_e1.frame_errors))
assert_true("...and ONE distinct error is written up once, not per frame",
            cap_e1.logtext.count("handling a charger frame failed") == 1,
            cap_e1.logtext[-600:])
assert_true("...with a crash log holding the traceback (nothing was silent)",
            len(_crash_files) == 1
            and "disk full (simulated)" in _crash_files[0].read_text(
                encoding="utf-8")
            and "Traceback" in _crash_files[0].read_text(encoding="utf-8"),
            str(_crash_files))
del _os5.environ["SITREP91_LOG_DIR"]
_cl5.reset_for_tests()

print()
print("--- a start is CONFIRMED, re-sent only on evidence, and failure is said ---")
#
# Seen on the bench: slot 4's start was logged as sent and never
# took - most likely a lost write-without-response. The capture now watches
# every start until the charger shows it alive.

_real_start = (capture.START_CONFIRM_S, capture.START_FRESH_S,
               capture.START_GIVEUP_S)
capture.START_CONFIRM_S = capture.START_FRESH_S = 0.0
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True


def _configs(client):
    return [w for w in client.writes if len(w) > 2 and w[2] == 0x94]


# A normal start: confirmed, one config, nothing re-sent.
with tempfile.TemporaryDirectory() as tmp:
    cap_s1, reason_s1, client_s1 = _bench_session(
        tmp, "AA:BB:CC:00:40:01", start="discharge", stop_after_polls=60)
assert_true("a start that takes is confirmed with ONE config",
            1 in cap_s1.ever_active and len(_configs(client_s1)) == 1
            and cap_s1.start_pending == {} and cap_s1.start_failed == set(),
            f"{reason_s1} {len(_configs(client_s1))}")

# The first config is lost: the charger keeps reporting the slot idle, so the
# identical config goes again, the run happens, and the log says so.
_BenchClient.drop_configs = {1: 1}
with tempfile.TemporaryDirectory() as tmp:
    cap_s2, reason_s2, client_s2 = _bench_session(
        tmp, "AA:BB:CC:00:40:01", start="discharge", stop_after_polls=60)
cf2 = _configs(client_s2)
assert_true("a lost config is re-sent - the IDENTICAL packet",
            len(cf2) == 2 and cf2[0] == cf2[1], str([c.hex() for c in cf2]))
assert_true("...and the run then goes ahead",
            1 in cap_s2.ever_active and cap_s2.start_failed == set(),
            str(reason_s2))
assert_true("...with the log saying it took on the second try",
            "did not start" in cap_s2.logtext
            and "start confirmed on try 2" in cap_s2.logtext)

# Every config lost: two tries, then a plain failure - and a CLI capture ends
# instead of polling an idle charger for hours.
_BenchClient.drop_configs = {1: 10}
with tempfile.TemporaryDirectory() as tmp:
    cap_s3, reason_s3, client_s3 = _bench_session(
        tmp, "AA:BB:CC:00:40:01", start="discharge", stop_after_polls=200)
assert_true("a start that never takes: exactly START_TRIES configs, no more",
            len(_configs(client_s3)) == capture.START_TRIES,
            str(len(_configs(client_s3))))
assert_true("...is declared failed, loudly",
            cap_s3.start_failed == {1}
            and "START DID NOT TAKE on slot 1" in cap_s3.logtext)
assert_true("...and the CLI capture ends 'start_failed', not at --max-hours",
            reason_s3 == "start_failed", str(reason_s3))
_BenchClient.drop_configs = {}

# Replies lost after the config (the slot may well be running): NOTHING is
# re-sent on silence - a second config to a running slot is untested ground.
_BenchClient.mute_after_config = {1}
capture.START_GIVEUP_S = 0.0
with tempfile.TemporaryDirectory() as tmp:
    cap_s4, reason_s4, client_s4 = _bench_session(
        tmp, "AA:BB:CC:00:40:01", start="discharge", stop_after_polls=200)
assert_true("no reply after a start: the config is NEVER re-sent on silence",
            len(_configs(client_s4)) == 1, str(len(_configs(client_s4))))
assert_true("...and the start is reported unconfirmed, not assumed",
            cap_s4.start_failed == {1}
            and "has not confirmed it" in cap_s4.logtext)
_BenchClient.mute_after_config = set()
(capture.START_CONFIRM_S, capture.START_FRESH_S,
 capture.START_GIVEUP_S) = _real_start

# The pure pieces.
_idle = SimpleNamespace(current_ma=0, resistance_mohm=0, elapsed_seconds=0,
                        device_status=0)
assert_true("an all-zero, status-0 frame is not a started slot",
            not capture._shows_started(_idle))
for _field, _val in (("device_status", 3), ("resistance_mohm", 18),
                     ("elapsed_seconds", 1), ("current_ma", 145)):
    assert_true(f"{_field}={_val} alone counts as started",
                capture._shows_started(SimpleNamespace(**{**vars(_idle),
                                                         _field: _val})))


print()
print("--- temperature on ATTACH: the firmware is learned without the 0x57 gate "
      "ever opening on a busy charger ---")

# Idle at connect, nothing remembered: the attach now reads 0x57 itself (it
# used to only when a GUI start queue existed), and temperature comes on.
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True
with tempfile.TemporaryDirectory() as tmp:
    cap_ai, _r, client_ai = _bench_session(tmp, "AA:BB:CC:00:20:01",
                                           stop_after_polls=30)
assert_true("an attach to an idle charger reads the firmware",
            cap_ai.device_info is not None
            and cap_ai.device_info.firmware == "2.02")
assert_true("...remembers it for this charger",
            capture.remembered_firmware("AA:BB:CC:00:20:01") == "2.02")
assert_true("...and polls temperature",
            cap_ai.firmware_verified is True
            and any(w[2] == 0x55 for w in client_ai.writes if len(w) > 2))

# Busy at connect (a panel run on slot 2), nothing remembered: no 0x57, no
# temperature, and an honest line saying why. Then slot 2 finishes, the
# charger goes idle, and the next retry reads it and switches temperature on.
_BenchClient.foreign = {2: 12}
with tempfile.TemporaryDirectory() as tmp:
    cap_ab, _r, client_ab = _bench_session(
        tmp, "AA:BB:CC:00:20:02", slots="1,2", stop_after_polls=80, fw_retry=0)
w_ab = [w for w in client_ab.writes if len(w) > 2]
first_57 = next((i for i, w in enumerate(w_ab) if w[2] == 0x57), None)
first_55 = next((i for i, w in enumerate(w_ab) if w[2] == 0x55), None)
assert_true("0x57 is NEVER sent while any slot is busy",
            client_ab.info_while_busy and not any(client_ab.info_while_busy),
            str(client_ab.info_while_busy))
assert_true("...but IS sent once the charger goes idle between runs",
            first_57 is not None)
assert_true("no temperature poll until the firmware was verified",
            first_55 is not None and first_55 > first_57,
            f"55 at {first_55}, 57 at {first_57}")
assert_true("...after which the attach is verified and polling temperature",
            cap_ab.firmware_verified is True and cap_ab.temp_interval_s == 0.0)

# Busy the whole time: the honest line, never a silent blank.
_BenchClient.foreign = {2: 10_000}
with tempfile.TemporaryDirectory() as tmp:
    cap_nb, _r, client_nb = _bench_session(
        tmp, "AA:BB:CC:00:20:03", slots="1,2", stop_after_polls=60, fw_retry=0)
assert_true("busy throughout: no 0x57 and no 0x55 ever sent",
            not any(w[2] in (0x57, 0x55) for w in client_nb.writes if len(w) > 2))
_note = cap_nb.temperature_note() or ""
assert_true("...and the capture SAYS why there is no temperature",
            "once the firmware is checked" in _note, _note)

# A version that cannot be read (no 0x57 answer) stays unverified; a
# supported one read now is what verifies, never a guess.
_BenchClient.foreign, _BenchClient.info = {}, False
with tempfile.TemporaryDirectory() as tmp:
    cap_nr, _r, client_nr = _bench_session(
        tmp, "AA:BB:CC:00:20:04", stop_after_polls=30, fw_retry=0)
assert_true("an unanswered 0x57 leaves the attach unverified, with no 0x55",
            cap_nr.firmware_verified is False
            and not any(w[2] == 0x55 for w in client_nr.writes if len(w) > 2))
_BenchClient.info = True

assert_true("temperature_note is None while temperature is being read",
            cap_ai.temperature_note() is None)

# The CLI attach watching a panel run used to END a few seconds after that run
# did - well inside FIRMWARE_RETRY_S - so the version was never read, never
# remembered, and the next attach had no temperature either. The retry timer
# is set out of reach here, so only the new triggers can read it.
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {1: 8}, True
with tempfile.TemporaryDirectory() as tmp:
    cap_ex, reason_ex, client_ex = _bench_session(
        tmp, "AA:BB:CC:00:30:01", fw_retry=1e9)
assert_true("CLI attach: the watched panel run ends and the capture is done",
            reason_ex == "done", str(reason_ex))
assert_true("...having read the firmware on the way out",
            cap_ex.device_info is not None
            and cap_ex.device_info.firmware == "2.02")
assert_true("...and remembered it, so the NEXT attach gets temperature",
            capture.remembered_firmware("AA:BB:CC:00:30:01") == "2.02")
assert_true("...with 0x57 still never sent while a slot was busy",
            client_ex.info_while_busy and not any(client_ex.info_while_busy),
            str(client_ex.info_while_busy))

# Another slot still busy when ours ends: the gate holds, nothing is read.
_BenchClient.foreign = {1: 8, 2: 10_000}
with tempfile.TemporaryDirectory() as tmp:
    cap_eb, reason_eb, client_eb = _bench_session(
        tmp, "AA:BB:CC:00:30:02", fw_retry=1e9)
assert_true("an unwatched slot still running: done, but NO 0x57 on the way out",
            reason_eb == "done" and cap_eb.device_info is None
            and not any(w[2] == 0x57 for w in client_eb.writes if len(w) > 2))

# A session that stays up (the GUI): a slot finishing triggers the read at
# once, and temperature comes on for the rest of the session.
_BenchClient.foreign = {1: 8}
with tempfile.TemporaryDirectory() as tmp:
    cap_gf, _r, client_gf = _bench_session(
        tmp, "AA:BB:CC:00:30:03", fw_retry=1e9, stop_after_polls=60,
        run_until_stopped=True)
w_gf = [w for w in client_gf.writes if len(w) > 2]
i57 = next((i for i, w in enumerate(w_gf) if w[2] == 0x57), None)
assert_true("a slot finishing triggers the read without waiting for the timer",
            i57 is not None and cap_gf.firmware_verified is True)
assert_true("...and temperature is polled after it",
            any(w[2] == 0x55 for w in w_gf[i57 or 0:]))
_BenchClient.foreign = {}

print()
print("--- one folder per slot per run ---")
#
# runs\<date>_<time>_S<n>_<cell>_<action>\ holding
# <same name>.csv and meta.json. A run opens when this program starts a slot,
# or when a slot is seen running something it did not start ("external").
import dataclasses as _dc   # noqa: E402

_lay_req = capture.RunRequest(
    mode=Mode.DISCHARGE, chemistry=Chemistry.LIION, current_ma=500,
    capacity_mah=3000, safety_timer_min=240, cutoff_mv=3000, temp_cutoff_c=45)


def _live(slot, t, ma=500):
    return build_status_frame(slot, ma, 4000 - t // 10, round(ma * t / 3600),
                              t, 20)


def _idle(slot, t=0, cap_mah=0):
    return build_status_frame(slot, 0, 4050, cap_mah, t, 20)


capture.datetime = FakeDateTime
FakeDateTime._tick = 0
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        lc = capture.Capture(name="lay", slots=[1, 2], outdir=tmp,
                             baseline=None, cells={1: "TEST 02/x", 2: None})
        r1 = lc.begin_slot_run(1, _lay_req)
        for t in (60, 120):
            lc.on_notify(None, bytearray(_live(1, t)))
        r1b = lc.begin_slot_run(1, _lay_req)     # restarted within the minute
        lc.on_notify(None, bytearray(_live(1, 5)))
        lc.save_meta({"end_reason": "stopped"})
        lc.close()
    m_r1 = json.loads(r1.meta_path.read_text(encoding="utf-8"))
    m_r1b = json.loads(r1b.meta_path.read_text(encoding="utf-8"))
    listing = sorted(p.name for p in r1.dir.iterdir())
    csv_rows = rows_of(r1.csv_path.read_text(encoding="utf-8"))

assert_true("the folder is <date>_<time>_S<n>_<cell>_<action>",
            r1.dir.name == "2026-08-15_1200_S1_TEST-02-x_discharge",
            r1.dir.name)
assert_true("a capture built without a label (the GUI) adds no name segment",
            lc.folder_label is None and r1.dir.name.endswith("_discharge"))
assert_true("the cell id is sanitised for the path (never in meta.json)",
            m_r1["cell"] == "TEST 02/x" and "/" not in r1.dir.name)
assert_true("the folder holds the CSV named after it, and meta.json",
            listing == [f"{r1.dir.name}.csv", "meta.json"], str(listing))
assert_true("a restart within the same minute gets the -2 suffix",
            r1b.dir.name == r1.dir.name + "-2", r1b.dir.name)
assert_true("each run's file holds only its own rows",
            len(csv_rows) == 2 and r1b.rows == 1, f"{len(csv_rows)}, {r1b.rows}")
assert_true("the action is the mode word, the same one the card shows",
            m_r1["action"] == "discharge" and m_r1["mode"] == "discharge")
_req_fields = {f.name for f in _dc.fields(capture.RunRequest)}
assert_true("meta.json holds the FULL config snapshot - every RunRequest field",
            set(m_r1["config"]) == _req_fields,
            f"missing {_req_fields - set(m_r1['config'])}")
assert_true("...as plain JSON values (mode and chemistry as their names)",
            m_r1["config"]["mode"] == "discharge"
            and m_r1["config"]["chemistry"] == "liion"
            and m_r1["config"]["cutoff_mv"] == 3000
            and m_r1["config"]["temp_cutoff_c"] == 45, str(m_r1["config"]))
assert_true("...and the run's own numbers, not the capture's defaults",
            m_r1["current_ma"] == 500 and m_r1["capacity_mah"] == 3000)
assert_true("a replaced run says so; the one still open takes the session's end",
            m_r1["end_reason"] == "replaced"
            and m_r1b["end_reason"] == "stopped",
            f"{m_r1['end_reason']} / {m_r1b['end_reason']}")
# Deliberate limit: v1 has no profiles, so meta.json carries no "profile" key.
# Adding one in 1.1 is non-breaking; removing one after release would not be.
# Runs recorded before this still hold "profile": null - readers must cope.
assert_true("meta.json has no 'profile' key in v1",
            "profile" not in m_r1 and "profile" not in m, str(sorted(m_r1)))

# Groups: the members' folders share a timestamp; meta.json names the group.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        gcap = capture.Capture(name="grp", slots=[1, 2], outdir=tmp,
                               baseline=None)
        g = capture.RunGroup(id="2026-09-22_143201-A", slots=(1, 2),
                             started=datetime(2026, 9, 22, 14, 32, 1))
        ga = gcap.begin_slot_run(1, _lay_req, cell_id="TEST-01", group=g)
        gb = gcap.begin_slot_run(2, _lay_req, cell_id="TEST-02", group=g)
        gcap.close()
    ma = json.loads(ga.meta_path.read_text(encoding="utf-8"))
assert_true("group members' folders share one timestamp",
            ga.dir.name == "2026-09-22_1432_S1_TEST-01_discharge"
            and gb.dir.name == "2026-09-22_1432_S2_TEST-02_discharge",
            f"{ga.dir.name} {gb.dir.name}")
assert_true("the group id and members are in each meta.json",
            ma["group"] == {"id": "2026-09-22_143201-A", "slots": [1, 2]},
            str(ma["group"]))
assert_true("a cell given with the start is the run's cell",
            ma["cell"] == "TEST-01" and ma["cells"] == {"1": "TEST-01"})

print()
print("--- when a run opens: the GUI watches, and records what runs ---")
FakeDateTime._tick = 0
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        wc = capture.Capture(name="watch", slots=[1], outdir=tmp,
                             baseline=None)          # the GUI's settings
        for _ in range(3):
            wc.on_notify(None, bytearray(_idle(1)))
        idle_runs = len(wc.runs)
        for t in (60, 120, 180):
            wc.on_notify(None, bytearray(_live(1, t)))
        first_panel = wc.open_runs.get(1)
        for _ in range(6):                           # a normal end
            wc.on_notify(None, bytearray(_idle(1, 180, 25)))
        finished_first = first_panel.finished
        for t in (30, 90):                           # a NEW panel run
            wc.on_notify(None, bytearray(_live(1, t)))
        second_panel = wc.open_runs.get(1)
        wc.close()
    m_fp = json.loads(first_panel.meta_path.read_text(encoding="utf-8"))
assert_true("an idle slot with nothing on it gets no folder",
            idle_runs == 0, str(idle_runs))
assert_true("a slot seen running opens an 'external' run",
            first_panel is not None and first_panel.action == "external"
            and first_panel.dir.name.endswith("_S1_external"))
assert_true("the finished run keeps the rested rows after it (the rebound)",
            first_panel.rows == 3 + 6, str(first_panel.rows))
assert_true("a new run after a finish gets its own folder",
            finished_first and second_panel is not first_panel
            and second_panel.rows == 2, str(second_panel and second_panel.rows))
assert_true("the first run's meta.json says it finished, and how",
            m_fp["finished"] is True and m_fp["end_reason"] == "done"
            and m_fp["final"]["capacity_mah"] == 25, str(m_fp["final"]))

# A multi-phase panel operation (break-in: charge, rest, discharge) carries its
# elapsed counter ON across the rest; it must stay in one folder.
FakeDateTime._tick = 0
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        bc = capture.Capture(name="bi", slots=[1], outdir=tmp, baseline=None)
        for t in (60, 120):
            bc.on_notify(None, bytearray(_live(1, t, ma=200)))
        for _ in range(6):                           # the rest phase
            bc.on_notify(None, bytearray(_idle(1, 120, 7)))
        for t in (800, 860):                         # discharge: elapsed carries on
            bc.on_notify(None, bytearray(_live(1, t, ma=400)))
        bc.close()
assert_true("a break-in's next phase stays in the same folder",
            len(bc.runs) == 1 and bc.runs[0].rows == 10, str(len(bc.runs)))

# A panel run on a slot this capture started earlier is NOT ours: the cutoff
# must warn, not stop it, and stop-on-exit must leave it alone.
FakeDateTime._tick = 0
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        oc = capture.Capture(name="own", slots=[1], outdir=tmp, baseline=None)
        oc.begin_slot_run(1, _dc.replace(_lay_req, temp_cutoff_c=40))
        for t in (60, 120):
            oc.on_notify(None, bytearray(_live(1, t)))
        for _ in range(6):
            oc.on_notify(None, bytearray(_idle(1, 120, 17)))
        oc.on_notify(None, bytearray(_live(1, 20)))  # someone's panel run
        oc.on_notify(None, bytearray(temp_frame(1, temp=41)))
        oc.close()
assert_true("a panel run after ours is not ours to stop",
            1 not in oc.started_slots and oc.cutoff_trips.get(1) == 41
            and not oc.pending_cutoff_stops,
            f"started {oc.started_slots}, stops {oc.pending_cutoff_stops}")

print()
print("--- how each run ended ---")
_er = capture.Capture.__new__(capture.Capture)
_mk = lambda **kw: capture.SlotRun(slot=1, action="discharge", cell_id=None,  # noqa: E731
                                   started=datetime(2026, 1, 1), name="x", **kw)
assert_true("a run the charger ended is 'done' whatever the session did",
            _er._end_reason(_mk(finished=True), "stopped") == "done")
assert_true("a run we stopped is 'stopped', even once it reads finished",
            _er._end_reason(_mk(finished=True, stop_sent=True), "done")
            == "stopped")
assert_true("a run the temperature cutoff stopped says so",
            _er._end_reason(_mk(cutoff_stopped=True, stop_sent=True), "done")
            == "temp_cutoff")
assert_true("an unfinished run of OURS takes the session's reason",
            _er._end_reason(_mk(ours=True), "stopped") == "stopped")
# This used to say "duration", and a run the session-length cap cut
# off read as a COMPLETE capture. Only the recording ended, so it says that.
assert_true("...except the cap: an unfinished run is 'recording_ended', "
            "never 'duration' (which readers take as complete)",
            _er._end_reason(_mk(ours=True), "duration") == "recording_ended"
            and _er._end_reason(_mk(), "duration") == "recording_ended")
assert_true("...and a FINISHED run keeps its own ending when the cap fires",
            _er._end_reason(_mk(ours=True, finished=True), "duration")
            == "done")
assert_true("an unfinished EXTERNAL run whose recording ended says so, not "
            "'stopped'",
            [_er._end_reason(_mk(), r) for r in
             ("stopped", "duration", "disconnected", "done")]
            == ["recording_ended"] * 4)
assert_true("...but 'replaced' and 'drift' still pass through for it",
            [_er._end_reason(_mk(), r) for r in ("replaced", "drift")]
            == ["replaced", "drift"])
assert_true("an external run the charger finished is still 'done'",
            _er._end_reason(_mk(finished=True), "stopped") == "done")
assert_true("a run first seen ended after a reconnect is not 'done'",
            _er._end_reason(_mk(finished=True, ended_unseen=True), "done")
            == "ended_while_disconnected")

# The same, end to end through on_notify (guards against a power cut mid-run
# being recorded 'done').
def _unseen_case(alert):
    _buf = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, redirect_stdout(_buf):
        cu = capture.Capture(name="ls", slots=[1], outdir=tmp, baseline=None)
        cu.begin_slot_run(1, _lay_req)
        for t in (60, 62, 64):
            cu.on_notify(None, bytearray(_live(1, t)))
        cu.link_restored()
        for _ in range(cu.done_after):
            cu.on_notify(None, bytearray(
                build_status_frame(1, 0, 4087, 0, 0, 0, alert=alert)))
        cu.save_meta({"end_reason": "done"})
        m = meta_of(cu, 1)
        cu.close()
    txt = _buf.getvalue()
    return m, txt
_mu, _lu = _unseen_case(0)
assert_true("a run first seen ended after a link loss: ended_while_disconnected",
            _mu.get("end_reason") == "ended_while_disconnected",
            str(_mu.get("end_reason")))
assert_true("...and the FINISHED line says why is unknown",
            "first seen ended after a link loss" in _lu, _lu)
_ma, _la = _unseen_case(5)
# This used to pin 'done' - a reason from the charger
# was taken to make an unwatched end trustworthy. It does not: a run that timed
# out during a PC sleep was recorded 'done' with the last reading BEFORE the
# drop (9 mAh / 70 s of a 180 s run). end_reason says how the run ended from OUR
# side; alert says why the CHARGER ended it. An unwatched end is the former.
assert_true("with the charger's own reason ('time limit') an unwatched end is "
            "STILL ended_while_disconnected",
            _ma.get("end_reason") == "ended_while_disconnected",
            str(_ma.get("end_reason")))
assert_true("...and the charger's reason is kept in alert",
            _ma.get("alert") == "time limit", str(_ma.get("alert")))
assert_true("...and the FINISHED line says the figures are the last received",
            "last reading received" in _la
            and "first seen ended after a link loss" in _la
            and "why it ended is unknown" not in _la, _la)


def _watched_case(alert):
    """Same run, same alert, but the end is SEEN: no reconnect in between."""
    with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
        cw = capture.Capture(name="lw", slots=[1], outdir=tmp, baseline=None)
        cw.begin_slot_run(1, _lay_req)
        for t in (60, 62, 64):
            cw.on_notify(None, bytearray(_live(1, t)))
        for _ in range(cw.done_after):
            cw.on_notify(None, bytearray(
                build_status_frame(1, 0, 4087, 25, 180, 22, alert=alert)))
        cw.save_meta({"end_reason": "done"})
        m = meta_of(cw, 1)
        unwatched = cw.unwatched_end(1)
        cw.close()
    return m, unwatched


_mw, _uw = _watched_case(5)
assert_true("the final summary's peak is 'highest seen' after an unwatched end",
            capture.peak_temp_text(38, True)
            == ", highest slot temp seen 38 C (may have gone higher)",
            capture.peak_temp_text(38, True))
assert_true("...and plain 'peak' after a watched one",
            capture.peak_temp_text(38, False) == ", peak slot temp 38 C",
            capture.peak_temp_text(38, False))
assert_true("...and nothing at all with no reading",
            capture.peak_temp_text(None, True) == ""
            and capture.peak_temp_text(None, False) == "")
assert_true("the final summary goes through peak_temp_text",
            "peak_temp_text(peak, cap.unwatched_end(s))"
            in inspect.getsource(capture))
assert_true("a WATCHED end with the charger's reason stays 'done' plus alert",
            _mw.get("end_reason") == "done" and _mw.get("alert") == "time limit"
            and not _uw, str({k: _mw.get(k) for k in ("end_reason", "alert")}))
assert_true("...and Capture.unwatched_end is False for it", _uw is False)
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    cx = capture.Capture(name="lx", slots=[1], outdir=tmp, baseline=None)
    cx.begin_slot_run(1, _lay_req)
    for t in (60, 62, 64):
        cx.on_notify(None, bytearray(_live(1, t)))
    cx.link_restored()
    for _ in range(cx.done_after):
        cx.on_notify(None, bytearray(
            build_status_frame(1, 0, 4087, 0, 0, 0, alert=5)))
    _ux = cx.unwatched_end(1)
    cx.close()
    _ux_after = cx.unwatched_end(1)
assert_true("Capture.unwatched_end is True for an end first seen after a "
            "reconnect, even with the charger's reason - open or closed run",
            _ux is True and _ux_after is True, f"{_ux} {_ux_after}")
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    cn = capture.Capture(name="ln", slots=[1, 2], outdir=tmp, baseline=None)
    _un_none = cn.unwatched_end(2)
    cn.close()
assert_true("...and False for a slot with no run at all", _un_none is False,
            str(_un_none))
with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    cs = capture.Capture(name="ls2", slots=[1], outdir=tmp, baseline=None)
    cs.begin_slot_run(1, _lay_req)
    cs.on_notify(None, bytearray(_live(1, 60)))
    cs.link_restored()
    cs.on_notify(None, bytearray(_live(1, 66)))          # seen live again
    for _ in range(cs.done_after):
        cs.on_notify(None, bytearray(build_status_frame(1, 0, 4087, 30, 70, 20)))
    cs.save_meta({"end_reason": "done"})
    _ms = meta_of(cs, 1)
    cs.close()
assert_true("a run seen live again after the reconnect ends 'done' as usual",
            _ms.get("end_reason") == "done", str(_ms.get("end_reason")))
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        sc_ = capture.Capture(name="st", slots=[1], outdir=tmp, baseline=None)
        sc_.begin_slot_run(1, _lay_req)
        sc_.on_notify(None, bytearray(_live(1, 60)))
        sc_.note_stop_sent(1)
        sc_.save_meta({"end_reason": "done"})
        sc_.close()
        m_st = meta_of(sc_)
assert_true("note_stop_sent reaches the run's meta.json",
            m_st["end_reason"] == "stopped", str(m_st["end_reason"]))

print()
print("--- a write failure moves the run and carries on (public app) ---")


class _BrokenWriter:
    def writerow(self, row):
        raise OSError(5, "The device is not ready")


FakeDateTime._tick = 0
with tempfile.TemporaryDirectory() as tmp:
    usb, local = Path(tmp) / "usb", Path(tmp) / "local"
    with redirect_stdout(io.StringIO()) as fb_log:
        fc = capture.Capture(name="fb", slots=[1], outdir=usb,
                             fallback_dir=local, baseline=None)
        fr = fc.begin_slot_run(1, _lay_req)
        for t in (60, 120):
            fc.on_notify(None, bytearray(_live(1, t)))
        first_dir = fr.dir
        fr.writer = _BrokenWriter()                  # the drive is pulled
        for t in (180, 240):
            fc.on_notify(None, bytearray(_live(1, t)))
        fc.save_meta({"end_reason": "stopped"})
        fc.close()
    m_fb = json.loads(fr.meta_path.read_text(encoding="utf-8"))
    old_rows = rows_of((first_dir / f"{first_dir.name}.csv")
                       .read_text(encoding="utf-8"))
    new_rows = rows_of(fr.csv_path.read_text(encoding="utf-8"))
    warn = fc.warning_for(1) or ""
assert_true("the run carries on in the local default folder",
            fr.dir.parent == local and fr.dir.name == first_dir.name,
            str(fr.dir))
assert_true("...losing no row: those before stay put, the rest follow",
            len(old_rows) == 2 and len(new_rows) == 2,
            f"{len(old_rows)} + {len(new_rows)}")
assert_true("the card gets a plain warning naming both folders",
            str(usb) in warn and str(fr.dir) in warn and "not ready" in warn,
            warn)
assert_true("...and the log says it loudly", "!!!" in fb_log.getvalue())
assert_true("meta.json records the move, the error and when",
            m_fb["relocated"] and m_fb["relocated"][0]["from"] == str(first_dir)
            and "not ready" in m_fb["relocated"][0]["error"],
            str(m_fb["relocated"]))

# The CLI has no fallback: its rule is unchanged, a CSV failure is a crash.
with tempfile.TemporaryDirectory() as tmp:
    with redirect_stdout(io.StringIO()):
        nc = capture.Capture(name="nofb", slots=[1], outdir=tmp, baseline=None)
        nr = nc.begin_slot_run(1, _lay_req)
        nr.writer = _BrokenWriter()
        try:
            nc.on_notify(None, bytearray(_live(1, 60)))
            raised = False
        except OSError:
            raised = True
        nc.close()
assert_true("without a fallback folder, a write failure still raises",
            raised)

# A folder that fails when the run OPENS (checked at run start) falls back too.
with tempfile.TemporaryDirectory() as tmp:
    not_a_dir = Path(tmp) / "share"
    not_a_dir.write_text("a file where the folder should be", encoding="utf-8")
    local = Path(tmp) / "local"
    with redirect_stdout(io.StringIO()):
        oc2 = capture.Capture(name="open", slots=[1], outdir=not_a_dir,
                              fallback_dir=local, baseline=None)
        orr = oc2.begin_slot_run(1, _lay_req)
        oc2.close()
    opened_in = orr.dir.parent
    m_open = json.loads(orr.meta_path.read_text(encoding="utf-8"))
assert_true("an unusable folder at run start: the run opens in the fallback",
            opened_in == local and orr.warning
            and m_open["relocated"][0]["from"] == str(not_a_dir),
            str(opened_in))

# The fallback failing as well is not absorbed.
with tempfile.TemporaryDirectory() as tmp:
    usb = Path(tmp) / "usb"
    bad_local = Path(tmp) / "local"
    bad_local.write_text("not a folder either", encoding="utf-8")
    with redirect_stdout(io.StringIO()):
        bc2 = capture.Capture(name="both", slots=[1], outdir=usb,
                              fallback_dir=bad_local, baseline=None)
        br = bc2.begin_slot_run(1, _lay_req)
        br.writer = _BrokenWriter()
        try:
            bc2.on_notify(None, bytearray(_live(1, 60)))
            raised2 = False
        except OSError:
            raised2 = True
        bc2.close()
assert_true("if the fallback fails too, that is raised, not swallowed", raised2)

# The logs folder is read per run: a change applies from the NEXT run.
with tempfile.TemporaryDirectory() as tmp:
    a_dir, b_dir = Path(tmp) / "a", Path(tmp) / "b"
    with redirect_stdout(io.StringIO()):
        cc2 = capture.Capture(name="mv", slots=[1, 2], outdir=a_dir,
                              baseline=None)
        ra = cc2.begin_slot_run(1, _lay_req)
        cc2.outdir = b_dir                          # Settings changed
        cc2.on_notify(None, bytearray(_live(1, 60)))
        rb = cc2.begin_slot_run(2, _lay_req)
        cc2.close()
assert_true("a changed logs folder applies from the next run, not the current",
            ra.dir.parent == a_dir and rb.dir.parent == b_dir and ra.rows == 1)

print()
print("--- the CLI session log ---")
assert_true("the CLI keeps its log in runs/logs/, not inside a run folder",
            "session_log_path(cap.outdir" in inspect.getsource(capture.main))

print()
print("--- chemistry whitelist: control only for what has been run ---")

_ALLOWED = {
    Chemistry.LIION: {Mode.CHARGE, Mode.DISCHARGE, Mode.STORAGE},
    Chemistry.NIMH: {Mode.CHARGE, Mode.DISCHARGE},
    Chemistry.ENELOOP: {Mode.CHARGE, Mode.DISCHARGE},
}
assert_true("the table holds exactly the verified chemistries",
            set(capture.VERIFIED_CONTROL) == set(_ALLOWED),
            str(set(capture.VERIFIED_CONTROL)))
assert_true("...and exactly their verified modes",
            all(set(capture.VERIFIED_CONTROL[c]) == m
                for c, m in _ALLOWED.items()))


def _wl_req(chemistry, mode, **kw):
    return capture.RunRequest(mode=mode, chemistry=chemistry, current_ma=500,
                              capacity_mah=2000, safety_timer_min=60, **kw)


assert_true("every verified combination is accepted",
            all(_wl_req(c, m).problem() is None
                for c, ms in _ALLOWED.items() for m in ms))
assert_true("control_verified agrees with the table",
            capture.control_verified(Chemistry.LIION, Mode.STORAGE)
            and not capture.control_verified(Chemistry.NIMH, Mode.STORAGE)
            and not capture.control_verified(Chemistry.LIFEPO4, Mode.DISCHARGE))

for _c in (Chemistry.LIION_HV, Chemistry.LIFEPO4, Chemistry.NICD,
           Chemistry.NIZN, Chemistry.RAM, Chemistry.LTO, Chemistry.NAION):
    _p = _wl_req(_c, Mode.DISCHARGE).problem()
    assert_true(f"{_c.name.lower()} is monitor-only, in plain words",
                _p is not None and "monitor-only" in _p
                and "front panel" in _p, str(_p))
_p = _wl_req(Chemistry.LIFEPO4, Mode.CHARGE).problem()
assert_true("the refusal lists what IS allowed, from the table",
            "liion (charge, discharge, storage)" in _p
            and "nimh (charge, discharge)" in _p
            and "eneloop (charge, discharge)" in _p, str(_p))
assert_true("nickel storage is not a verified combination",
            "monitor-only" in (_wl_req(Chemistry.NIMH, Mode.STORAGE).problem() or ""))
assert_true("cycle is still refused outright (its own, older reason)",
            _wl_req(Chemistry.LIION, Mode.CYCLE).problem() is not None)
assert_true("the monitor-only message names the chemistry and mode",
            capture.monitor_only_message(Chemistry.LIFEPO4, Mode.CHARGE)
            .startswith("lifepo4 charge is monitor-only"))
assert_true("...and does not point at ROADMAP.md (the CLI and logs use it too)",
            "ROADMAP" not in capture.monitor_only_message(Chemistry.LIFEPO4))

# ROADMAP.md's table is the user-facing copy of VERIFIED_CONTROL, so it must not
# drift from it. The file is looked for at the repo root and in
# packaging/public/; a release build's tests have neither.
_RM_HERE = Path(__file__).resolve().parent
_RM = next((p for p in (_RM_HERE / "ROADMAP.md",
                        _RM_HERE / "packaging" / "public" / "ROADMAP.md")
            if p.exists()), None)
if _RM is None:
    print("SKIP  ROADMAP.md not beside the code (a release build)")
else:
    _RM_CHEM = {"Li-ion": Chemistry.LIION, "Li-ion HV": Chemistry.LIION_HV,
                "LiFePO4": Chemistry.LIFEPO4, "LTO": Chemistry.LTO,
                "Na-ion": Chemistry.NAION, "NiMH": Chemistry.NIMH,
                "Eneloop": Chemistry.ENELOOP, "NiCd": Chemistry.NICD,
                "NiZn": Chemistry.NIZN, "RAM": Chemistry.RAM}
    _RM_MODES = [Mode.CHARGE, Mode.STORAGE, Mode.DISCHARGE, Mode.CYCLE,
                 Mode.REFRESH, Mode.BREAK_IN]
    _rm_rows = {}
    for _ln in _RM.read_text(encoding="utf-8").splitlines():
        _cells = [c.strip() for c in _ln.strip().strip("|").split("|")]
        if _ln.lstrip().startswith("|") and _cells[0] in _RM_CHEM:
            _rm_rows[_cells[0]] = _cells[1:]
    assert_true("ROADMAP.md's table has a row for every chemistry",
                set(_rm_rows) == set(_RM_CHEM), str(sorted(_rm_rows)))
    assert_true("...with a cell for each of the six modes",
                all(len(v) == 6 for v in _rm_rows.values()))
    _rm_bad = [f"{n} {m.name}: says {v[i]!r}"
               for n, v in _rm_rows.items() for i, m in enumerate(_RM_MODES)
               if (v[i] == "verified")
               != capture.control_verified(_RM_CHEM[n], m)]
    assert_true("ROADMAP.md says 'verified' exactly where VERIFIED_CONTROL allows",
                not _rm_bad, "; ".join(_rm_bad))

_lf = _wl_req(Chemistry.LIFEPO4, Mode.DISCHARGE)
assert_true("an unverified combination is refused as monitor-only",
            "monitor-only" in (_lf.problem() or ""), str(_lf.problem()))
try:
    _wl_req(Chemistry.LIFEPO4, Mode.DISCHARGE, allow_unverified=True)
    _took = True
except TypeError:
    _took = False
assert_true("...and there is no argument that changes that", not _took)

# The same gate through a real session: nothing may reach the charger.
_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True
with tempfile.TemporaryDirectory() as tmp:
    cap_wr, reason_wr, client_wr = _bench_session(
        tmp, "AA:BB:CC:00:60:01", start="discharge", chemistry="lifepo4",
        cutoff_mv=None)
assert_true("starting a monitor-only chemistry is refused by the session",
            reason_wr == "refused", str(reason_wr))
assert_true("...with the plain message in the log",
            "monitor-only" in cap_wr.logtext
            and "nothing was sent to the charger" in cap_wr.logtext)
assert_true("...and no config (0x94) was written",
            not any(len(w) > 2 and w[2] == 0x94 for w in client_wr.writes))


print()
print("--- a run the charger ends at once: done, or failed, never re-sent ---")
#
# A charge sent to a cell that is already full: the charger hears the config
# and answers with the reason it ended, with no sign the run ever began. That
# used to read as "START DID NOT TAKE" and the config was sent a second time.
# "fully charged" is wire code 13, seen on hardware;
# the behavior is still pinned by NAME, through ENDED_AT_ONCE_NORMAL.

_BenchClient.temp, _BenchClient.foreign, _BenchClient.info = 33, {}, True
_BenchClient.end_at_once = {1: 13}                  # "fully charged"
with tempfile.TemporaryDirectory() as tmp:
    cap_fc, reason_fc, client_fc = _bench_session(
        tmp, "AA:BB:CC:00:70:01", start="charge", stop_after_polls=300)
    # What the CLI does on the way out: hand save_meta the session's end reason.
    with redirect_stdout(io.StringIO()):
        cap_fc.save_meta({"end_reason": reason_fc})
    meta_fc = meta_of(cap_fc)
assert_true("a charge ended at once as 'fully charged' is DONE, not failed",
            cap_fc.ended_at_once == {1} and cap_fc.start_failed == set(),
            f"{cap_fc.ended_at_once} {cap_fc.start_failed}")
assert_true("...the config is sent exactly once (never re-sent)",
            len([w for w in client_fc.writes if len(w) > 2 and w[2] == 0x94]) == 1)
assert_true("...the log says it is a normal end",
            "normal end, not a failed start" in cap_fc.logtext
            and "START DID NOT TAKE" not in cap_fc.logtext, cap_fc.logtext)
assert_true("...the slot reads finished, so the card shows the charger's reason",
            1 in cap_fc.finished and cap_fc.alert_by_slot.get(1) == "fully charged")
assert_true("...the CLI capture ends 'done', not at --max-hours",
            reason_fc == "done", str(reason_fc))
assert_true("...and the run's meta.json says done, with the reason",
            meta_fc.get("end_reason") == "done"
            and meta_fc.get("alert") == "fully charged",
            str({k: meta_fc.get(k) for k in ("end_reason", "alert")}))

_BenchClient.end_at_once = {1: 9}                   # "connection break"
with tempfile.TemporaryDirectory() as tmp:
    cap_rp, reason_rp, client_rp = _bench_session(
        tmp, "AA:BB:CC:00:70:02", start="charge", stop_after_polls=300)
assert_true("any other reason is a failed start, saying why",
            cap_rp.start_failed == {1} and cap_rp.ended_at_once == set()
            and "START ENDED AT ONCE on slot 1" in cap_rp.logtext
            and "connection break" in cap_rp.logtext, cap_rp.logtext)
assert_true("...and it is not re-sent either - the charger did hear it",
            len([w for w in client_rp.writes if len(w) > 2 and w[2] == 0x94]) == 1)
assert_true("...and the CLI capture ends 'start_failed'",
            reason_rp == "start_failed", str(reason_rp))
_BenchClient.end_at_once = {}

# An alert that was ALREADY standing when the start was sent is not the
# charger's answer to it: the old re-send behavior is untouched.
import time as _time
from protocol import parse_status_response as _psr


class _Wire:
    def __init__(self):
        self.sent = []

    async def write_gatt_char(self, char, data, response=False):
        self.sent.append(bytes(data))


def _confirm(alert_before, alert_now):
    with tempfile.TemporaryDirectory() as tmp:
        with redirect_stdout(io.StringIO()) as buf:
            c = capture.Capture(name="ao", slots=[1], outdir=tmp, baseline=None)
            if alert_before:
                c.alert_by_slot[1] = alert_before
            c.expect_start(1, b"CFG")
            sent_at = c.start_pending[1]["sent_at"]
            c.latest_any[1] = _psr(build_status_frame(1, 0, 4200, 0, 0, 0,
                                                       alert=alert_now))
            c.answered_at[1] = sent_at + 10
            wire = _Wire()
            _aio.run(capture.confirm_starts(wire, None, c, c.log,
                                            now=sent_at + 20))
            c.close()
    return c, wire, buf.getvalue()


c_new, w_new, _ = _confirm(None, 13)
assert_true("a NEW alert after the send ends the start at once, no resend",
            c_new.ended_at_once == {1} and w_new.sent == [])
c_old, w_old, log_old = _confirm("time limit", 5)
assert_true("the SAME alert as before the send is a leftover, not an answer",
            c_old.ended_at_once == set() and c_old.start_failed == set())
assert_true("...so the lost-write retry still happens",
            w_old.sent == [b"CFG"] and "did not start" in log_old, log_old)

print()
print("--- Configure's starting values (the charger's own) and the limits ---")

_LI, _NI, _EN = Chemistry.LIION, Chemistry.NIMH, Chemistry.ENELOOP
_cd = capture.configure_defaults

assert_true("Li-ion starts as the panel does: 5000 mAh, 3.00 A charge, "
            "2.00 A discharge, 3200 mV",
            _cd(_LI, Mode.CHARGE) == dict(current_ma=3000, capacity_mah=5000,
                                          cutoff_mv=3200)
            and _cd(_LI, Mode.DISCHARGE) == dict(current_ma=2000,
                                                 capacity_mah=5000,
                                                 cutoff_mv=3200),
            str(_cd(_LI, Mode.CHARGE)))
assert_true("Li-ion storage is the panel's 3.00 A charge here; its 2.00 A "
            "discharge is the first Advanced default",
            _cd(_LI, Mode.STORAGE)["current_ma"] == 3000
            and capture.advanced_defaults(_LI, Mode.STORAGE)
            ["discharge_current_ma"] == 2000)
assert_true("NiMH starts as the panel does: 3000 mAh, 1.00 A both ways, 900 mV",
            all(_cd(_NI, m) == dict(current_ma=1000, capacity_mah=3000,
                                    cutoff_mv=900)
                for m in (Mode.CHARGE, Mode.DISCHARGE)))
assert_true("Eneloop is Std AA: 2000 mAh, 1.00 A both ways, 900 mV",
            all(_cd(_EN, m) == dict(current_ma=1000, capacity_mah=2000,
                                    cutoff_mv=900)
                for m in (Mode.CHARGE, Mode.DISCHARGE)))
assert_true("no values where none were read off the charger",
            _cd(Chemistry.LIION_HV, Mode.CHARGE) is None
            and _cd(_LI, Mode.CYCLE) is None
            and _cd(_NI, Mode.STORAGE) is None)
assert_true("the cutoffs are the firmware's own defaults, not copies",
            all(_cd(c, Mode.DISCHARGE)["cutoff_mv"]
                == capture.CHEMISTRY_DEFAULTS[c]["cutoff_mv"]
                for c in (_LI, _NI, _EN)))
assert_true("every starting value is a verified combination",
            all(capture.control_verified(c, m)
                for c, row in capture.CONFIGURE_DEFAULTS.items()
                for m in row["current_ma"]))

_sug = capture.suggest_safety_timer_min
assert_true("the timer rule for each default (nickel charge x2.5, the rest x1.5)",
            [_sug(_LI, Mode.DISCHARGE, 5000, 2000),
             _sug(_LI, Mode.CHARGE, 5000, 3000),
             _sug(_NI, Mode.CHARGE, 3000, 1000),
             _sug(_EN, Mode.CHARGE, 2000, 1000),
             _sug(_NI, Mode.DISCHARGE, 3000, 1000),
             _sug(_EN, Mode.DISCHARGE, 2000, 1000)]
            == [(230, False), (150, False), (450, False), (300, False),
                (270, False), (180, False)],
            str([_sug(_LI, Mode.DISCHARGE, 5000, 2000),
                 _sug(_NI, Mode.CHARGE, 3000, 1000)]))
assert_true("an Eneloop charge comes to 300 min - the figure the panel's own "
            "hand-set timer showed",
            _sug(_EN, Mode.CHARGE, 2000, 1000)[0] == 300)
assert_true("the old fixed 240 min would cut a 3000 mAh discharge at 500 mA "
            "(6 h); the rule allows it",
            _sug(_LI, Mode.DISCHARGE, 3000, 500)[0] > 360,
            str(_sug(_LI, Mode.DISCHARGE, 3000, 500)))
assert_true("the same numbers get a longer timer on a nickel charge than a "
            "lithium charge",
            _sug(_NI, Mode.CHARGE, 2000, 1000)[0]
            > _sug(_LI, Mode.CHARGE, 2000, 1000)[0])
assert_true("it rounds UP to the next 10 min (225 -> 230), never down",
            _sug(_LI, Mode.DISCHARGE, 5000, 2000)[0] == 230)
assert_true("it is held to the charger's 24 h limit and says so",
            _sug(_LI, Mode.DISCHARGE, 5000, 100) == (1440, True),
            str(_sug(_LI, Mode.DISCHARGE, 5000, 100)))
assert_true("...and never below 30 min, so it is never off",
            _sug(_LI, Mode.DISCHARGE, 100, 2000) == (30, False),
            str(_sug(_LI, Mode.DISCHARGE, 100, 2000)))
assert_true("no timer is suggested for a capacity or current that is not positive",
            _sug(_LI, Mode.DISCHARGE, 0, 500) is None
            and _sug(_LI, Mode.DISCHARGE, 3000, 0) is None)
assert_true("every starting value, with its suggested timer, passes problem()",
            all(capture.RunRequest(
                mode=m, chemistry=c, current_ma=d["current_ma"],
                capacity_mah=d["capacity_mah"], cutoff_mv=d["cutoff_mv"],
                # Storage has two currents; its discharge one is an Advanced field.
                discharge_current_ma=(
                    capture.advanced_defaults(c, m)["discharge_current_ma"]
                    if m is Mode.STORAGE else None),
                safety_timer_min=_sug(c, m, d["capacity_mah"],
                                      d["current_ma"])[0]).problem() is None
                for c, row in capture.CONFIGURE_DEFAULTS.items()
                for m in row["current_ma"]
                for d in (_cd(c, m),)))

_cw = capture.c_rate_warning
assert_true("exactly 1C is not warned about",
            _cw(Mode.CHARGE, 3000, 3000) is None)
assert_true("above 1C is, naming both figures and the datasheet",
            "3001" in (_cw(Mode.CHARGE, 3001, 3000) or "")
            and "3000" in (_cw(Mode.CHARGE, 3001, 3000) or "")
            and "datasheet" in (_cw(Mode.CHARGE, 3001, 3000) or ""),
            str(_cw(Mode.CHARGE, 3001, 3000)))
assert_true("it names the action",
            (_cw(Mode.DISCHARGE, 2000, 1000) or "").startswith("Discharge"))
assert_true("nothing to say about a zero",
            _cw(Mode.CHARGE, 0, 3000) is None
            and _cw(Mode.CHARGE, 500, 0) is None)

# Fixed bounds the firmware silently substitutes outside (config +0x54 / +0x68
# / TimeCut): refuse them by name rather than run at a current nobody chose.
def _lim(mode, current, timer=60, chem=_LI):
    return capture.RunRequest(mode=mode, chemistry=chem, current_ma=current,
                              capacity_mah=3000, safety_timer_min=timer)

assert_true("charge at 5000 mA is accepted, 5001 is refused naming the limit",
            _lim(Mode.CHARGE, 5000).problem() is None
            and "5000" in (_lim(Mode.CHARGE, 5001).problem() or "")
            and "SILENTLY" in (_lim(Mode.CHARGE, 5001).problem() or ""),
            str(_lim(Mode.CHARGE, 5001).problem()))
assert_true("discharge at 2000 mA is accepted, 2001 is refused",
            _lim(Mode.DISCHARGE, 2000).problem() is None
            and "2000" in (_lim(Mode.DISCHARGE, 2001).problem() or ""))
assert_true("storage is held to the discharge limit, and says why",
            _lim(Mode.STORAGE, 2000).problem() is None
            and "both" in (_lim(Mode.STORAGE, 2001).problem() or ""),
            str(_lim(Mode.STORAGE, 2001).problem()))
assert_true("a safety timer of 1440 min is accepted, 1441 is refused",
            _lim(Mode.DISCHARGE, 500, timer=1440).problem() is None
            and "1440" in (_lim(Mode.DISCHARGE, 500, timer=1441).problem() or ""))
assert_true("a 5-minute research timer is still allowed (no new floor)",
            _lim(Mode.DISCHARGE, 500, timer=5).problem() is None)

print()
print("--- the Advanced settings: matrix, defaults, ranges, bytes ---")

from protocol import build_config as _bcfg                          # noqa: E402

_CH, _DI, _ST = Mode.CHARGE, Mode.DISCHARGE, Mode.STORAGE
_adv_fields = capture.advanced_fields
_adv_def = capture.advanced_defaults


def _areq(mode, chem=_LI, current=1000, **kw):
    return capture.RunRequest(mode=mode, chemistry=chem, current_ma=current,
                              capacity_mah=3000, safety_timer_min=240, **kw)


def _apkt(req):
    return capture.build_start_packets([1], req)[0][1]


# Packet offsets: [0x0F, len, 0x94, data...], so data[n] is packet[n + 3].
_P_DELTA, _P_TRICKLE, _P_KEEP = 22 + 3, 23 + 3, 24 + 3
_P_CHG, _P_DCHG = 2 + 3, 4 + 3

assert_true("the matrix is exactly what the panel pages showed, in panel order",
            _adv_fields(_LI, _CH) == ("target_mv", "charge_cutoff_current_ma",
                                      "keep_mv")
            and _adv_fields(_LI, _DI) == ("discharge_cutoff_current_ma",)
            and _adv_fields(_LI, _ST) == ("storage_mv", "discharge_current_ma",
                                          "charge_cutoff_current_ma",
                                          "discharge_cutoff_current_ma")
            and all(_adv_fields(c, _CH) == ("delta_v_mv", "target_mv",
                                            "trickle_ma")
                    and _adv_fields(c, _DI) == ("discharge_cutoff_current_ma",)
                    for c in (_NI, _EN)),
            str(_adv_fields(_NI, _CH)))
assert_true("nickel charge has no charge cut-off current; only nickel charge "
            "has -dV and trickle; only Li-ion storage has a second current",
            "charge_cutoff_current_ma" not in _adv_fields(_NI, _CH)
            and not any(f in _adv_fields(_LI, m) for m in (_CH, _DI, _ST)
                        for f in ("delta_v_mv", "trickle_ma"))
            and "discharge_current_ma" not in _adv_fields(_NI, _DI)
            and "discharge_current_ma" not in _adv_fields(_LI, _DI))
assert_true("nothing is offered where control is not verified (monitor-only "
            "chemistry, Cycle, nickel storage)",
            _adv_fields(Chemistry.LIION_HV, _CH) == ()
            and _adv_fields(_LI, Mode.CYCLE) == ()
            and _adv_fields(_NI, _ST) == ()
            and _adv_def(Chemistry.NICD, _CH) == {})
assert_true("every field the matrix names is a RunRequest field",
            all(f in capture.RunRequest.__dataclass_fields__
                for c, row in capture.VERIFIED_CONTROL.items()
                for m in row for f in _adv_fields(c, m)))

assert_true("the panel's Advanced defaults, every chemistry and action",
            _adv_def(_LI, _CH) == dict(target_mv=4200,
                                       charge_cutoff_current_ma=100, keep_mv=0)
            and _adv_def(_LI, _DI) == dict(discharge_cutoff_current_ma=100)
            and _adv_def(_LI, _ST) == dict(storage_mv=3800,
                                           discharge_current_ma=2000,
                                           charge_cutoff_current_ma=100,
                                           discharge_cutoff_current_ma=100)
            and all(_adv_def(c, _CH) == dict(delta_v_mv=6, target_mv=1650,
                                             trickle_ma=0)
                    and _adv_def(c, _DI) == dict(discharge_cutoff_current_ma=100)
                    for c in (_NI, _EN)),
            str(_adv_def(_NI, _CH)))

# --- byte identity: the whole point of leaving every new field None ---------
_old_style = _bcfg(slot=1, mode=_CH, chemistry=_LI, charge_current_ma=1000,
                   discharge_current_ma=0, capacity_mah=3000,
                   cutoff_timer_min=240)
_explicit = _bcfg(slot=1, mode=_CH, chemistry=_LI, charge_current_ma=1000,
                  discharge_current_ma=0, capacity_mah=3000,
                  cutoff_timer_min=240, delta_peak_mv=6, trickle_ma=0, keep_mv=0)
assert_true("build_config with the new parameters at their defaults is "
            "byte-identical to a call that never names them",
            _old_style == _explicit)
assert_true("...and those three fields hold the values it always sent: 6, 0, 0",
            (_old_style[_P_DELTA], _old_style[_P_TRICKLE],
             _old_style[_P_KEEP:_P_KEEP + 2]) == (6, 0, bytes([0, 0])),
            _old_style.hex())
assert_true("a request naming none of the new fields builds the packet it "
            "always did, for every verified chemistry and action",
            all(_apkt(_areq(m, c)) == _bcfg(
                slot=1, mode=m, chemistry=c,
                charge_current_ma=1000 if m in (_CH, _ST) else 0,
                discharge_current_ma=1000 if m in (_DI, _ST) else 0,
                capacity_mah=3000, cutoff_timer_min=240)
                for c, row in capture.VERIFIED_CONTROL.items() for m in row))


def _explicit_defaults_req(chem, mode, **extra):
    """A request with EVERY Advanced field set to the panel default - what the
    dialog would hold if the user opened Advanced and changed nothing."""
    fields = dict(_adv_def(chem, mode))
    fields.update(extra)
    return _areq(mode, chem, **fields)


assert_true("pre-filled panel defaults, sent explicitly, are byte-identical to "
            "sending nothing - so showing them in the dialog changes no packet",
            all(_apkt(_explicit_defaults_req(c, m)) == _apkt(
                _areq(m, c, **({"discharge_current_ma": 2000}
                               if m is _ST else {})))
                for c, row in capture.VERIFIED_CONTROL.items() for m in row))
assert_true("every chemistry and action's explicit defaults pass problem()",
            all(_explicit_defaults_req(c, m).problem() is None
                for c, row in capture.VERIFIED_CONTROL.items() for m in row),
            str([(c.name, m.name, _explicit_defaults_req(c, m).problem())
                 for c, row in capture.VERIFIED_CONTROL.items() for m in row
                 if _explicit_defaults_req(c, m).problem()]))

# --- the new fields land at their offsets, and nowhere else ------------------
_nick = _apkt(_areq(_CH, _NI, delta_v_mv=9, trickle_ma=120))
_nick_base = _apkt(_areq(_CH, _NI))
assert_true("-dV 9 and trickle 120 mA (sent as 12) land at bytes 22 and 23 of "
            "the config, and the keep-voltage bytes stay 0 (off)",
            (_nick[_P_DELTA], _nick[_P_TRICKLE], _nick[_P_KEEP:_P_KEEP + 2])
            == (9, 12, bytes([0, 0])), _nick.hex())
_diff = [i for i in range(len(_nick)) if _nick[i] != _nick_base[i]]
assert_true("...and only those bytes (plus the checksum) differ from the "
            "default packet",
            set(_diff) <= {_P_DELTA, _P_TRICKLE, len(_nick) - 1}, str(_diff))
assert_true("a Li-ion charge's keep voltage lands in the same two bytes",
            _apkt(_areq(_CH, _LI, keep_mv=4100))[_P_KEEP:_P_KEEP + 2]
            == bytes([4100 >> 8, 4100 & 0xFF]))
assert_true("a nickel target voltage reaches the target field",
            _apkt(_areq(_CH, _NI, target_mv=1700))[8 + 3:10 + 3]
            == bytes([1700 >> 8, 1700 & 0xFF]))
for _bad_kw, _why in (({"trickle_ma": 55}, "not a multiple of 10"),
                      ({"trickle_ma": 2600}, "does not fit a byte"),
                      ({"keep_mv": 70000}, "does not fit two bytes"),
                      ({"delta_peak_mv": 300}, "does not fit a byte")):
    try:
        _bcfg(slot=1, mode=_CH, chemistry=_NI, charge_current_ma=1000,
              discharge_current_ma=0, capacity_mah=3000, **_bad_kw)
        _raised = False
    except ValueError:
        _raised = True
    assert_true(f"build_config refuses what cannot be encoded ({_why})", _raised)

# --- Storage's two currents --------------------------------------------------
_sp = _apkt(_areq(_ST, _LI, current=3000, discharge_current_ma=2000))
assert_true("storage with two currents sends the charge one and the discharge "
            "one in their own fields",
            (int.from_bytes(_sp[_P_CHG:_P_CHG + 2], "big"),
             int.from_bytes(_sp[_P_DCHG:_P_DCHG + 2], "big")) == (3000, 2000),
            _sp.hex())
_su = _apkt(_areq(_ST, _LI, current=1500))
assert_true("storage naming no discharge current still sends its one current "
            "both ways, as before",
            (int.from_bytes(_su[_P_CHG:_P_CHG + 2], "big"),
             int.from_bytes(_su[_P_DCHG:_P_DCHG + 2], "big")) == (1500, 1500))
assert_true("storage: charge may be 5000 mA with its own discharge current; "
            "5001 is refused",
            _areq(_ST, _LI, current=5000, discharge_current_ma=2000).problem()
            is None
            and "5000" in (_areq(_ST, _LI, current=5001,
                                 discharge_current_ma=2000).problem() or ""))
assert_true("storage discharge current: 2000 accepted, 2001 and 0 refused",
            _areq(_ST, _LI, current=3000, discharge_current_ma=2000).problem()
            is None
            and "2000" in (_areq(_ST, _LI, current=3000,
                                 discharge_current_ma=2001).problem() or "")
            and _areq(_ST, _LI, current=3000,
                      discharge_current_ma=0).problem() is not None)
assert_true("a separate discharge current on a charge or discharge is refused",
            "storage" in (_areq(_CH, _LI, discharge_current_ma=500).problem()
                          or "")
            and "storage" in (_areq(_DI, _LI,
                                    discharge_current_ma=500).problem() or ""))
assert_true("storage with ONE current is still held to 2000, as before",
            _areq(_ST, _LI, current=2001).problem() is not None
            and "both" in (_areq(_ST, _LI, current=2001).problem() or ""))

# --- ranges: every edge accepted, one outside refused, naming the window ----
def _range_case(label, make, ok_values, bad_values, window):
    good = all(make(v).problem() is None for v in ok_values)
    bad_msgs = [make(v).problem() or "" for v in bad_values]
    assert_true(f"{label}: {ok_values} accepted",
                good, str([make(v).problem() for v in ok_values]))
    assert_true(f"{label}: {bad_values} refused, naming {window}",
                all(window in m and "SILENTLY" in m for m in bad_msgs),
                str(bad_msgs))


_range_case("-dV on a nickel charge",
            lambda v: _areq(_CH, _NI, delta_v_mv=v), (3, 6, 12), (2, 13), "3-12mV")
_range_case("trickle on a nickel charge",
            lambda v: _areq(_CH, _EN, trickle_ma=v), (0, 50, 120, 300),
            (10, 40, 310, 55), "50-300mA")
_range_case("Li-ion keep voltage",
            lambda v: _areq(_CH, _LI, keep_mv=v), (0, 3980, 4180),
            (3979, 4181), "3980-4180mV")
_range_case("nickel target voltage",
            lambda v: _areq(_CH, _NI, target_mv=v), (1470, 1650, 1800),
            (1469, 1801), "1470-1800mV")
_range_case("Li-ion charge cut-off current",
            lambda v: _areq(_CH, _LI, charge_cutoff_current_ma=v),
            (10, 100, 990), (9, 991, 0), "10-990mA")
_range_case("discharge cut-off current",
            lambda v: _areq(_DI, _NI, discharge_cutoff_current_ma=v),
            (10, 100, 990), (9, 991, 0), "10-990mA")
_range_case("storage voltage",
            lambda v: _areq(_ST, _LI, current=2000, storage_mv=v),
            (3700, 3800, 3850), (3699, 3851), "3700-3850mV")

# --- keep voltage: the charger discards it over Bluetooth for nickel ---------
# Firmware 2.02's 0x94 parser copies RST_Volt into the config only for the
# lithium-class chemistries; the panel showed OFF after 1400 mV was sent on
# NiMH and Eneloop, from this program and from the vendor app.
_NICKEL_NO_KEEP = (Chemistry.NIMH, Chemistry.NICD, Chemistry.ENELOOP,
                   Chemistry.NIZN, Chemistry.RAM)
for _c in _NICKEL_NO_KEEP:
    _m = _areq(_CH, _c, keep_mv=1400)._advanced_problem() or ""
    assert_true(f"{_c.name}: a keep voltage is refused, saying the charger "
                f"ignores it and the panel can set it",
                "ignores a keep voltage" in _m and "panel" in _m, _m)
    assert_true(f"{_c.name}: keep voltage 0 (off) is still fine",
                _areq(_CH, _c, keep_mv=0)._advanced_problem() is None,
                str(_areq(_CH, _c, keep_mv=0)._advanced_problem()))
for _c in (Chemistry.LIION, Chemistry.LIION_HV, Chemistry.LIFEPO4,
           Chemistry.LTO, Chemistry.NAION):
    _lo, _hi = capture.KEEP_VOLTAGE_GATES_MV[_c]
    assert_true(f"{_c.name}: a keep voltage inside its window is accepted",
                _areq(_CH, _c, keep_mv=_lo)._advanced_problem() is None
                and _areq(_CH, _c, keep_mv=_hi)._advanced_problem() is None)
assert_true("the keep-voltage chemistries are exactly the lithium class the "
            "firmware copies (0, 1, 2, 8, 9)",
            capture.KEEP_VOLTAGE_OVER_BLE == {
                Chemistry.LIION, Chemistry.LIION_HV, Chemistry.LIFEPO4,
                Chemistry.LTO, Chemistry.NAION})
assert_true("no nickel chemistry offers a keep voltage in the Advanced section",
            all("keep_mv" not in _adv_fields(c, m)
                for c in _NICKEL_NO_KEEP
                for m in (_CH, _DI, _ST)))
assert_true("and a nickel config never carries one into the packet",
            all(_apkt(_areq(_CH, c))[_P_KEEP:_P_KEEP + 2] == bytes([0, 0])
                for c in (_NI, _EN)))

# --- where each applies ------------------------------------------------------
for _label, _req in (
    ("-dV on a Li-ion charge", _areq(_CH, _LI, delta_v_mv=6)),
    ("-dV on a nickel discharge", _areq(_DI, _NI, delta_v_mv=6)),
    ("trickle on a Li-ion charge", _areq(_CH, _LI, trickle_ma=100)),
    ("trickle on a nickel discharge", _areq(_DI, _NI, trickle_ma=100)),
    ("keep voltage on a discharge", _areq(_DI, _LI, keep_mv=4100)),
    ("a charge cut-off current on a nickel charge",
     _areq(_CH, _NI, charge_cutoff_current_ma=100)),
    ("a charge cut-off current on a discharge",
     _areq(_DI, _LI, charge_cutoff_current_ma=100)),
    ("a discharge cut-off current on a charge",
     _areq(_CH, _LI, discharge_cutoff_current_ma=100)),
):
    _msg = _req.problem() or ""
    assert_true(f"{_label} is refused as not applicable",
                "only applies" in _msg and "Drop it" in _msg, _msg)
assert_true("a discharge cut-off current IS fine on a storage run, and so is a "
            "charge one",
            _areq(_ST, _LI, current=2000, discharge_cutoff_current_ma=200,
                  charge_cutoff_current_ma=150).problem() is None)

# --- meta.json ---------------------------------------------------------------
_snap = capture.request_snapshot(_areq(_CH, _NI, delta_v_mv=9, trickle_ma=120))
_snap_li = capture.request_snapshot(_areq(_CH, _LI, keep_mv=4000))
assert_true("the config snapshot records the new fields with their values",
            (_snap["delta_v_mv"], _snap["trickle_ma"], _snap_li["keep_mv"])
            == (9, 120, 4000)
            and "discharge_current_ma" in _snap, str(_snap))
_snap0 = capture.request_snapshot(_areq(_CH, _NI))
assert_true("...and null when the charger's own default was used",
            (_snap0["delta_v_mv"], _snap0["trickle_ma"], _snap0["keep_mv"])
            == (None, None, None))

print()
print("--- panel minimums, keep-awake, Retry ---")


def _rq(**kw):
    base = dict(mode=_Md.DISCHARGE, chemistry=_Cd.LIION, current_ma=500,
                capacity_mah=3000, safety_timer_min=480)
    return capture.RunRequest(**{**base, **kw})


# Item 6: nothing below what the charger's own panel offers (50 mA, 100 mAh).
assert_true("the panel's lowest values are the limits (50 mA, 100 mAh)",
            (capture.CURRENT_MA_MIN, capture.CAPACITY_MAH_MIN) == (50, 100))
assert_true("50 mA and 100 mAh, the panel's own lowest, are accepted",
            _rq(current_ma=50, capacity_mah=100).problem() is None)
_low_i = _rq(current_ma=49).problem()
assert_true("49 mA is refused, naming the 50 mA the panel allows",
            _low_i is not None and "50mA" in _low_i and "panel" in _low_i,
            str(_low_i))
_low_c = _rq(capacity_mah=99).problem()
assert_true("99 mAh is refused, naming the 100 mAh the panel allows",
            _low_c is not None and "100mAh" in _low_c and "panel" in _low_c,
            str(_low_c))
assert_true("1 mA and 1 mAh (the old floor) are refused too",
            _rq(current_ma=1).problem() and _rq(capacity_mah=1).problem())
assert_true("a charge below the minimum is refused as well",
            _rq(mode=_Md.CHARGE, current_ma=20).problem() is not None)
assert_true("Storage's second current is held to the minimum",
            _rq(mode=_Md.STORAGE, current_ma=500,
                discharge_current_ma=49).problem() is not None
            and _rq(mode=_Md.STORAGE, current_ma=500,
                    discharge_current_ma=50).problem() is None)
assert_true("derived currents are raised to the minimum, never below it",
            all(_dcm(c, m, 100) >= 50 for c, m in
                ((_Cd.LIION, _Md.DISCHARGE), (_Cd.LIION, _Md.CHARGE),
                 (_Cd.NIMH, _Md.DISCHARGE), (_Cd.ENELOOP, _Md.DISCHARGE)))
            and capture.derive_storage_discharge_ma(_Cd.LIION, 100) == 50)

# A start that did not take must not hold the PC awake.
_cap_f = SimpleNamespace(start_pending={}, torn_down=set(),
                         latest_any={1: SimpleNamespace(current_ma=0)},
                         open_runs={1: SimpleNamespace(finished=False, slot=1)},
                         start_failed=set())
assert_true("an open, unfinished run is busy", capture.anything_busy(_cap_f))
_cap_f.start_failed = {1}
assert_true("...but not when its start was declared failed",
            capture.anything_busy(_cap_f) is False)
_cap_f.open_runs[2] = SimpleNamespace(finished=False, slot=2)
assert_true("...while another slot's genuinely open run still is",
            capture.anything_busy(_cap_f) is True)

# Retry carries the runs the old session
# started, and a run is claimed only if its elapsed matches where the old run
# would be by now (last elapsed + wall-clock since last seen, within
# CARRY_MARGIN_S) AND the device reports the same mode.
_CLK = [10000.0]


def _at(t):
    _CLK[0] = float(t)


def _clock():
    return mock.patch.object(capture.time, "time", lambda: _CLK[0])


assert_true("the claim margin is 30 s", capture.CARRY_MARGIN_S == 30)
_REQ = capture.RunRequest(mode=_Md.DISCHARGE, chemistry=_Cd.LIION,
                          current_ma=500, capacity_mah=3000,
                          safety_timer_min=480, temp_cutoff_c=40)


def _old_session(tmp):
    """A session that started slot 1 (its config in hand) and saw it at
    elapsed 60 at wall time 10000, then lost the charger."""
    old = _cut_cap(tmp, slots="1,3")
    old.begin_slot_run(1, _REQ)
    old.begin_slot_run(3, temp_cutoff_c=45)
    with _clock(), redirect_stdout(io.StringIO()):
        _at(9970)
        old.on_notify(None, bytearray(build_status_frame(1, 500, 3700, 4, 30, 13)))
        _at(10000)
        old.on_notify(None, bytearray(build_status_frame(1, 500, 3700, 5, 60, 13)))
    return old


with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    old = _old_session(tmp)
    carried = old.carry_forward()
    old.close()
assert_true("carry_forward names the run still going - elapsed, when it was "
            "seen, device mode, config and cutoff - and not the slot that "
            "never ran",
            list(carried) == [1] and carried[1]["elapsed"] == 60
            and carried[1]["seen_wall"] == 10000.0
            and carried[1]["mode_code"] == 0
            and carried[1]["request"] == _REQ
            and carried[1]["temp_cutoff_c"] == 40, str(carried))


def _new_session(tmp, carry=None):
    n = _cut_cap(tmp, slots="1,3")
    n.inherit_runs(carry if carry is not None else carried)
    return n


def _feed_new(n, raw, at):
    with _clock(), redirect_stdout(io.StringIO()):
        _at(at)
        n.on_notify(None, bytearray(raw))


# A genuine continuation IS claimed: Retry two minutes later, elapsed 60 + 120.
with tempfile.TemporaryDirectory() as tmp:
    n = _new_session(tmp)
    _feed_new(n, build_status_frame(1, 500, 3700, 15, 182, 13), 10120)
    assert_true("a genuine continuation is claimed: owned, cutoff armed",
                1 in n.started_slots and n.temp_cutoff_by_slot.get(1) == 40
                and n.open_runs[1].ours, f"{n.started_slots}")
    _feed_new(n, temp_frame(1, temp=41), 10122)
    assert_true("...and the cutoff trips and stops it",
                1 in n.cutoff_trips and 1 in n.pending_cutoff_stops,
                f"{n.cutoff_trips} {n.pending_cutoff_stops}")
    with redirect_stdout(io.StringIO()):
        n.save_meta()
    _meta = json.loads(n.open_runs[1].meta_path.read_text(encoding="utf-8"))
    assert_true("the carried run's meta.json copies the old run's configuration "
                "(not 'attach' with nothing)",
                _meta["mode"] == "discharge" and _meta["current_ma"] == 500
                and _meta["capacity_mah"] == 3000
                and _meta["safety_timer_min"] == 480
                and _meta["config"]["temp_cutoff_c"] == 40, str(_meta)[:300])
    with redirect_stdout(io.StringIO()):
        n.close()

# The margin's edges (expected 60 + 120 = 180).
for off, claimed in ((30, True), (-30, True), (31, False), (-31, False)):
    with tempfile.TemporaryDirectory() as tmp:
        n = _new_session(tmp)
        _feed_new(n, build_status_frame(1, 500, 3700, 15, 180 + off, 13), 10120)
        assert_true(f"elapsed {off:+d} s from expected: "
                    f"{'claimed' if claimed else 'not claimed'}",
                    (1 in n.started_slots) is claimed, f"{n.started_slots}")
        with redirect_stdout(io.StringIO()):
            n.close()

# A panel run started LATER is NOT claimed - whatever its elapsed.
for label, el, at in (("one that has run longer than the old run did", 1500, 10600),
                      ("a short one that began after the old run", 100, 10600),
                      ("one whose counter is 10 minutes past the last reading",
                       660, 10120)):
    with tempfile.TemporaryDirectory() as tmp:
        n = _new_session(tmp)
        _feed_new(n, build_status_frame(1, 1000, 3700, 15, el, 13), at)
        assert_true(f"a panel run ({label}) is not claimed: no ownership, no "
                    f"cutoff, labeled external",
                    1 not in n.started_slots and not n.open_runs[1].ours
                    and n.open_runs[1].action == "external"
                    and n.temp_cutoff_by_slot.get(1) is None,
                    f"{n.started_slots}")
        with redirect_stdout(io.StringIO()):
            n.close()

# ...and a matching elapsed with a DIFFERENT device mode is not claimed either.
with tempfile.TemporaryDirectory() as tmp:
    n = _new_session(tmp)
    _feed_new(n, build_status_frame(1, 500, 3700, 15, 180, 13, mode=1), 10120)
    assert_true("the same elapsed but another device mode is not the same run",
                1 not in n.started_slots, f"{n.started_slots}")
    with redirect_stdout(io.StringIO()):
        n.close()

# A slot seen idle first has lost its claim.
with tempfile.TemporaryDirectory() as tmp:
    n = _new_session(tmp)
    _feed_new(n, build_status_frame(1, 0, 3900, 0, 0, 0), 10100)
    _feed_new(n, build_status_frame(1, 500, 3700, 9, 180, 13), 10120)
    assert_true("a slot seen idle first has lost its claim even if a later "
                "run's counters happen to fit",
                1 not in n.started_slots and not n.open_runs[1].ours,
                f"{n.started_slots}")
    with redirect_stdout(io.StringIO()):
        n.close()

# A SECOND Retry (the charger was not found the first time) keeps the run: the
# inherited, never-claimed entry is carried on with its original last-seen time.
with tempfile.TemporaryDirectory() as tmp:
    second = _new_session(tmp)              # session 2: finds nothing
    carried2 = second.carry_forward()
    with redirect_stdout(io.StringIO()):
        second.close()
    assert_true("an inherited run that was never seen again is carried on "
                "unchanged", carried2 == carried, str(carried2))
    third = _new_session(tmp, carried2)
    _feed_new(third, build_status_frame(1, 500, 3700, 30, 260, 13), 10200)
    assert_true("...and a third session claims it, measured from the ORIGINAL "
                "last-seen time (60 + 200 = 260)",
                1 in third.started_slots and third.open_runs[1].ours
                and third.open_runs[1].request == _REQ, f"{third.started_slots}")
    carried3 = third.carry_forward()
    with redirect_stdout(io.StringIO()):
        third.close()
    assert_true("a claimed run carries its config on again",
                carried3[1]["request"] == _REQ, str(carried3))

with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
    idle_cap = _cut_cap(tmp, slots="1")
    idle_cap.begin_slot_run(1, temp_cutoff_c=40)
    for _ in range(3):
        idle_cap.on_notify(None, bytearray(build_status_frame(1, 0, 3900, 0, 0, 0)))
    assert_true("a slot last seen idle carries nothing", idle_cap.carry_forward() == {})
    idle_cap.close()

print()
print("--- capture.py --start needs --capacity and derives the rest "
      "---")


def _sa(**kw):
    base = dict(start="discharge", chemistry="liion", mode=None,
                capacity=None, current=None, safety_timer=None)
    base.update(kw)
    return Namespace(**base)


_a = _sa()
with redirect_stdout(io.StringIO()):
    _why = capture.resolve_start_args(_a)
assert_true("--start without --capacity is refused, saying why",
            _why is not None and "--capacity" in _why, str(_why))
_a = _sa(start=None)
assert_true("an attach is left alone (nothing derived, nothing required)",
            capture.resolve_start_args(_a) is None and _a.current is None)
for _kw, _cur, _tim in (
        (dict(capacity=3400), 680, 450),                    # Li-ion discharge 0.2C
        (dict(capacity=3400, start="charge"), 1700, 180),   # 0.5C
        (dict(capacity=3400, start="storage"), 680, 450),   # one current: 0.2C
        (dict(capacity=100), 50, 180),                      # the 50 mA floor
        (dict(capacity=2000, chemistry="nimh", start="charge"), 1000, 300)):
    _a = _sa(**_kw)
    with redirect_stdout(io.StringIO()):
        _why = capture.resolve_start_args(_a)
    assert_true(f"{_kw}: current {_cur} mA, timer {_tim} min, the same as "
                f"Configure", _why is None and (_a.current, _a.safety_timer)
                == (_cur, _tim), f"{_why} {_a.current} {_a.safety_timer}")
_a = _sa(capacity=3400, current=900, safety_timer=77)
with redirect_stdout(io.StringIO()):
    capture.resolve_start_args(_a)
assert_true("values given on the command line are kept, never overwritten",
            (_a.current, _a.safety_timer) == (900, 77))
_a = _sa(capacity=3400, chemistry="nicd")
with redirect_stdout(io.StringIO()):
    _why = capture.resolve_start_args(_a)
assert_true("a chemistry with no derivation asks for --current instead of "
            "guessing", _why is not None and "--current" in _why, str(_why))
import subprocess                                          # noqa: E402
_here18 = Path(capture.__file__).resolve()
_p = subprocess.run([sys.executable, str(_here18), "--start", "discharge",
                     "--name", "needs_capacity"], capture_output=True,
                    text=True, timeout=60)
assert_true("the command itself exits 2 naming --capacity, before any scan",
            _p.returncode == 2 and "--capacity" in _p.stdout,
            f"{_p.returncode} {_p.stdout[-200:]} {_p.stderr[-200:]}")

print()
print("=" * 55)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 55)

sys.exit(0 if all(results) else 1)
