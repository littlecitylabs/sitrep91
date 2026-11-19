"""
Validate protocol.py.

Packet ENCODING is checked against the doc's worked examples (those are
byte-exact captures and remain trustworthy).

Packet DECODING is checked against frames captured live from the actual
charger, NOT the doc's status examples - the doc contains a superseded
"status byte + multiplier" interpretation of bytes 4-5 that live hardware
disproves.
"""

from protocol import (
    build_status_query, build_start_stop, build_config,
    parse_status_response, verify_checksum, Chemistry, Mode,
    build_temperature_query, parse_temperature_response, SLOT_BITMASK,
)


def hexstr(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


results = []


def check(label, got: bytes, expected_hex: str):
    expected = bytes.fromhex(expected_hex.replace(" ", ""))
    ok = got == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"    expected: {hexstr(expected)}")
        print(f"    got:      {hexstr(got)}")
    results.append(ok)
    return ok


def assert_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"    {detail}")
    results.append(bool(cond))
    return cond


print("--- ENCODING (vs doc's byte-exact captures) ---")
check("status query slot 1", build_status_query(1), "0f 03 91 01 92")
check("status query slot 2", build_status_query(2), "0f 03 91 02 93")
check("status query slot 3", build_status_query(3), "0f 03 91 04 95")
check("status query slot 4", build_status_query(4), "0f 03 91 08 99")

check("stop all", build_start_stop([]), "0f 03 93 00 93")
check("start slot 1 only", build_start_stop([1]), "0f 03 93 01 94")
check("start slot 2 only", build_start_stop([2]), "0f 03 93 02 95")
check("start slots 1+2 combined", build_start_stop([1, 2]), "0f 03 93 03 96")

cfg = build_config(
    slot=3, mode=Mode.CHARGE, chemistry=Chemistry.LIION,
    charge_current_ma=3000, discharge_current_ma=2000, capacity_mah=5000,
)
check(
    "config example 1 (Li-Ion slot3 3A/2000mA/5000mAh)",
    cfg,
    "0f 2a 94 04 00 0b b8 07 d0 13 88 10 68 0c 80 00 64 00 64 00 0a 00 0a "
    "01 00 06 00 00 00 3c 00 00 01 2c 00 00 0e d8 00 00 00 00 00 03",
)

print()
print("--- DECODING (vs frames captured live from the real charger) ---")

# Captured on hardware while slot 1 ran a 1500mA Li-Ion discharge started
# from the charger's front-panel menu, slot 2 held a topped-off cell,
# slots 3 and 4 were empty.
LIVE_S1_ACTIVE = bytes.fromhex("0f16910105db10090000002500 00005f000d0302000000 21".replace(" ", ""))
LIVE_S2_IDLE = bytes.fromhex("0f169102000010650000000000 000000000000000000 08".replace(" ", ""))
LIVE_S3_EMPTY = bytes.fromhex("0f169104000000000000000000 000000000000000000 95".replace(" ", ""))

assert_true("live frame is 24 bytes", len(LIVE_S1_ACTIVE) == 24, f"got {len(LIVE_S1_ACTIVE)}")
assert_true("checksum validates on active frame", verify_checksum(LIVE_S1_ACTIVE))
assert_true("checksum validates on idle frame", verify_checksum(LIVE_S2_IDLE))
assert_true("checksum validates on empty frame", verify_checksum(LIVE_S3_EMPTY))

s1 = parse_status_response(LIVE_S1_ACTIVE, mode_hint="discharge")
print(f"    S1 decoded: {s1.current_ma}mA {s1.voltage_v}V {s1.capacity_mah}mAh "
      f"{s1.elapsed_seconds}s {s1.resistance_mohm}mOhm state={s1.state}")

assert_true("current ~= configured 1500mA", abs(s1.current_ma - 1500) <= 5,
            f"got {s1.current_ma}mA - if this is 876 or 2190, the old status-byte "
            f"interpretation has crept back in")
assert_true("voltage plausible for 30Q mid-discharge", 3.0 < s1.voltage_v < 4.2,
            f"got {s1.voltage_v}V")
assert_true("slot number decoded", s1.slot == 1, f"got {s1.slot}")
assert_true("state inferred as discharging", s1.state == "discharging", f"got {s1.state}")

expected_mah = s1.current_ma * (s1.elapsed_seconds / 3600)
err_pct = abs(expected_mah - s1.capacity_mah) / expected_mah * 100
print(f"    physical check: {s1.current_ma}mA over {s1.elapsed_seconds}s "
      f"=> {expected_mah:.1f}mAh expected, device reports {s1.capacity_mah}mAh "
      f"({err_pct:.1f}% off)")
assert_true("capacity consistent with current x elapsed (within 15%)", err_pct < 15,
            f"{err_pct:.1f}% discrepancy suggests one of these three fields "
            f"is decoded at the wrong offset")

s2 = parse_status_response(LIVE_S2_IDLE)
assert_true("idle slot: zero current", s2.current_ma == 0, f"got {s2.current_ma}")
assert_true("idle slot: voltage present", abs(s2.voltage_v - 4.197) < 0.001, f"got {s2.voltage_v}")
assert_true("idle slot: state == idle", s2.state == "idle", f"got {s2.state}")

s3 = parse_status_response(LIVE_S3_EMPTY)
assert_true("empty slot: state == empty", s3.state == "empty", f"got {s3.state}")

# --- frame bytes 12-15 and 18-21, from the fw-2.02 builder ---
#
# The builder emits elapsed as a 32-bit big-endian value across bytes 12-15,
# and bytes 18-21 carry the device's own view of the run. Both were previously
# read as "low 16 bits" and "padding" respectively.
assert_true("elapsed is unchanged on real frames (high bytes are 0)",
            s1.elapsed_seconds == 95, f"got {s1.elapsed_seconds}")

# a synthetic frame with the high half set - what a >18h run looks like
_long = bytearray(LIVE_S1_ACTIVE)
_long[12], _long[13], _long[14], _long[15] = 0x00, 0x01, 0x00, 0x2F  # 65583 s
_long[-1] = sum(_long[2:-1]) % 256
_long = bytes(_long)
assert_true("elapsed past the 16-bit wrap decodes as 32-bit",
            parse_status_response(_long).elapsed_seconds == 65583,
            f"got {parse_status_response(_long).elapsed_seconds} - if this is 47, "
            f"only the low half is being read and an 18h+ run wraps")

assert_true("byte 18 decodes as the device's run-status enum", s1.device_status == 3,
            f"got {s1.device_status}")
assert_true("byte 19 is the mode, lithium discharge == 2", s1.mode_code == 2,
            f"got {s1.mode_code}")
assert_true("byte 20 reports no alert on a healthy run",
            s1.alert_code == 0 and s1.alert == "none", f"got {s1.alert_code}/{s1.alert}")
assert_true("byte 21 is the chemistry, Li-ion == 0", s1.chemistry_code == 0,
            f"got {s1.chemistry_code}")
assert_true("mode and chemistry agree with the known capture (Li-ion discharge)",
            s1.chemistry_code == Chemistry.LIION and s1.mode_code == 2)

# an alert frame: the panel would be showing the matching message
_alert = bytearray(LIVE_S1_ACTIVE)
_alert[20] = 5
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("alert 5 is the safety timer firing (proven: 0x6A78 vs TimeCut x 60)",
            parse_status_response(bytes(_alert)).alert == "time limit")
_alert[20] = 4
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("alert 4 is the capacity ceiling (proven: 0x6AE4 vs runtime+0x1c)",
            parse_status_response(bytes(_alert)).alert == "capacity limit")
_alert[20] = 13
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("alert 13 is fully charged (seen on hardware)",
            parse_status_response(bytes(_alert)).alert == "fully charged")
_alert[20] = 9
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("alert 9 is a connection break (seen on hardware: a pulled cell, "
            "panel 'Connection Break!')",
            parse_status_response(bytes(_alert)).alert == "connection break")
_alert[20] = 10
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("alert 10 is unnamed (its old 'fully charged' name was disproven)",
            parse_status_response(bytes(_alert)).alert == "alert code 10")
for _code in (1, 2, 3, 6, 7, 8):
    _alert[20] = _code
    _alert[-1] = sum(_alert[2:-1]) % 256
    assert_true(f"alert {_code} has no proven name, so it is shown as its code",
                parse_status_response(bytes(_alert)).alert == f"alert code {_code}",
                parse_status_response(bytes(_alert)).alert)
_alert[20] = 200
_alert[-1] = sum(_alert[2:-1]) % 256
assert_true("an unrecognized alert code is reported, not swallowed",
            "alert code 200" in parse_status_response(bytes(_alert)).alert)
from protocol import ALERT_NAMES as _AN
assert_true("only proven codes carry a name: 4, 5, 9, 13 (and 0 = none)",
            sorted(_AN) == [0, 4, 5, 9, 13], str(sorted(_AN)))

# the shorter documented frame puts its checksum at 22, so the extras must not
# be read off the end of it
_short = LIVE_S1_ACTIVE[:18] + bytes([sum(LIVE_S1_ACTIVE[2:18]) % 256])
_s = parse_status_response(_short)
assert_true("a frame too short for the extras reports them as None",
            _s.device_status is None and _s.alert_code is None and _s.alert is None)
assert_true("...but still decodes everything at offsets 0-17",
            _s.current_ma == 1499 and _s.resistance_mohm == 13)

# The 23-byte frame (the documented, firmware-1.82-style length; the same worked
# example test_drift.py uses). Its checksum is at byte 22, so bytes 18-21 are NOT
# the 2.02 status/mode/alert/chemistry bytes - reading them anyway turned "02 00
# 00 03" into a made-up device status, mode, alert and chemistry. Shorter than 24
# bytes means those four stay blank.
_body23 = bytes.fromhex("9101" + "00c6" + "059d" + "0000" + "005c" + "0000"
                        + "0761" + "02b0" + "02" + "0000" + "03")
_f23 = bytes([0x0F, len(_body23) + 1]) + _body23
_f23 = _f23 + bytes([sum(_f23[2:]) % 256])
assert_true("the worked 23-byte frame really is 23 bytes with a good checksum",
            len(_f23) == 23 and verify_checksum(_f23))
_s23 = parse_status_response(_f23)
assert_true("a 23-byte frame leaves device_status, mode, alert and chemistry blank",
            _s23.device_status is None and _s23.mode_code is None
            and _s23.alert_code is None and _s23.chemistry_code is None
            and _s23.alert is None,
            f"got {_s23.device_status}/{_s23.mode_code}/{_s23.alert_code}/"
            f"{_s23.chemistry_code}")
assert_true("...and still decodes offsets 0-17 (current, voltage, capacity, IR)",
            _s23.current_ma == 0x00C6 and _s23.voltage_v == 1.437
            and _s23.capacity_mah == 0x5C and _s23.resistance_mohm == 0x02B0,
            f"got {_s23.current_ma}/{_s23.voltage_v}/{_s23.capacity_mah}/"
            f"{_s23.resistance_mohm}")
assert_true("a 24-byte frame still carries all four (the 2.02 shape)",
            LIVE_S1_ACTIVE.__len__() == 24
            and parse_status_response(LIVE_S1_ACTIVE).device_status == 3)

print()
print("--- 0x57 DEVICE INFO (vs two real units) ---")

from protocol import build_info_query, parse_info_response  # noqa: E402

q = build_info_query()
assert_true("info query frame is 21 bytes with a valid length byte",
            len(q) == 21 and q[1] + 2 == len(q))
assert_true("info query checksum valid", sum(q[2:-1]) % 256 == q[-1])

# Captured from the bench unit; panel reads FW 2.02, and the app's
# settings screen agrees.
OURS = bytes.fromhex("0f135700313030323134040000000002020200ce57")
info = parse_info_response(OURS)
assert_true("bench unit: product code decoded", info.product_code == "100214",
            info.product_code)
assert_true("bench unit: firmware 2.02 (matches the panel)",
            info.firmware == "2.02", info.firmware)
assert_true("bench unit: hardware 2.00", info.hardware == "2.00", info.hardware)

# The worked example in skyrc-mc-rs's PROTOCOL.md, from a DIFFERENT unit on
# FW 1.82 with a 7-digit product code - which is why the version bytes are anchored
# to the END of the frame rather than a fixed offset from the start.
DOC_182 = bytes.fromhex("0f1357003130303231333404000000000152010080 57".replace(" ", ""))
info182 = parse_info_response(DOC_182)
assert_true("doc unit: product code decoded despite different length",
            info182.product_code == "1002134", info182.product_code)
assert_true("doc unit: firmware 1.82", info182.firmware == "1.82", info182.firmware)
assert_true("doc unit: hardware 1.00", info182.hardware == "1.00", info182.hardware)


# ---------------------------------------------------------------------------
# 0x55 - per-slot battery temperature.
#
# *** THESE FRAMES ARE SYNTHETIC. *** They are built from the firmware byte map
# (builder) to pin the parser's behavior on branches the real
# frames further down do not reach, and they are NOT evidence that the map is
# right - the real frames are.
# ---------------------------------------------------------------------------

def temp_frame(slot=1, status=2, capacity=1234, elapsed=567, voltage=3700,
               current=500, temp=27, stale13=0xAB, mode=2):
    """Synthesize a 0x55 response per the firmware map, with a valid checksum."""
    f = bytearray(36)
    f[0], f[1], f[2], f[3] = 0x0F, 0x22, 0x55, SLOT_BITMASK[slot]
    f[4] = status
    f[5], f[6] = capacity >> 8, capacity & 0xFF
    f[7], f[8] = elapsed >> 8, elapsed & 0xFF
    f[9], f[10] = voltage >> 8, voltage & 0xFF
    f[11], f[12] = current >> 8, current & 0xFF
    f[13] = stale13
    f[14] = temp
    f[33], f[34] = 1, mode
    f[35] = sum(f[2:35]) % 256
    return bytes(f)


# --- the request ---
check("0x55 temperature query, slot 1", build_temperature_query(1), "0f 03 55 01 56")
check("0x55 temperature query, slot 2", build_temperature_query(2), "0f 03 55 02 57")
check("0x55 temperature query, slot 3", build_temperature_query(3), "0f 03 55 04 59")
check("0x55 temperature query, slot 4", build_temperature_query(4), "0f 03 55 08 5d")

for s in (1, 2, 3, 4):
    q = build_temperature_query(s)
    assert_true(f"0x55 query slot {s}: checksum valid", verify_checksum(q))

# The whole safety argument rests on this being a status query with one byte
# changed, so assert exactly that rather than trusting it.
for s in (1, 2, 3, 4):
    t, st = build_temperature_query(s), build_status_query(s)
    diff = [i for i in range(len(t)) if t[i] != st[i]]
    assert_true(f"0x55 vs 0x91 slot {s}: same length", len(t) == len(st))
    assert_true(f"0x55 vs 0x91 slot {s}: differs only at the opcode and checksum",
                diff == [2, 4], str(diff))

# --- the response, running: temperature is reported ---
for status in (1, 2, 3):
    r = parse_temperature_response(temp_frame(slot=3, status=status, temp=31))
    assert_true(f"status {status}: temperature reported", r.temperature_c == 31,
                str(r.temperature_c))
    assert_true(f"status {status}: marked trustworthy", r.trustworthy)

r = parse_temperature_response(temp_frame(slot=3, status=2, temp=27))
assert_true("slot decoded from the bitmask", r.slot == 3, str(r.slot))
assert_true("checksum verifies on a 36-byte frame", r.checksum_ok)
assert_true("frame length recorded", r.frame_len == 36, str(r.frame_len))
assert_true("voltage decoded", abs(r.voltage_v - 3.700) < 1e-9, str(r.voltage_v))
assert_true("elapsed decoded", r.elapsed_seconds == 567, str(r.elapsed_seconds))
assert_true("capacity decoded on the running branch", r.capacity_mah == 1234,
            str(r.capacity_mah))

# --- the response, NOT running: byte 14 is stale, not a reading ---
# The firmware never clears the transmit buffer, so on a status-4 frame byte 14
# still holds whatever the previous 0x55 left there. Reporting it would be
# inventing a temperature, which is the single worst thing this parser could do.
for status in (0, 4, 5, 6, 7):
    r = parse_temperature_response(temp_frame(status=status, temp=99))
    assert_true(f"status {status}: temperature withheld as stale",
                r.temperature_c is None, str(r.temperature_c))
    assert_true(f"status {status}: NOT marked trustworthy", not r.trustworthy)

r = parse_temperature_response(temp_frame(status=4))
assert_true("status 4: capacity withheld too (bytes 5-6 are fault codes there)",
            r.capacity_mah is None, str(r.capacity_mah))
assert_true("status 4: raw frame still handed back for inspection",
            r.raw[14] == 27, str(r.raw[14]))

# --- THE REAL FRAME ---------------------------------------------------------
# Captured on hardware, the first 0x55 ever sent to this charger. Slot 1,
# TEST-01 (35E) on a 500 mA discharge, 673 s in. This supersedes the synthetic
# frames above as the authority on the layout; they remain only to exercise the
# branches this one does not reach.
REAL_0X55 = bytes.fromhex(
    "0f22550103005d02a10e7501eb0022000000000300030"
    "09f0000000000000000000101" "91")

r = parse_temperature_response(REAL_0X55)
assert_true("real frame: 36 bytes", len(REAL_0X55) == 36, str(len(REAL_0X55)))
assert_true("real frame: length byte describes the frame",
            REAL_0X55[1] + 2 == len(REAL_0X55), str(REAL_0X55[1]))
assert_true("real frame: checksum verifies", r.checksum_ok)
assert_true("real frame: slot 1", r.slot == 1, str(r.slot))
assert_true("real frame: device_status 3 (a discharge)", r.device_status == 3,
            str(r.device_status))
assert_true("real frame: temperature reported as 34 C", r.temperature_c == 34,
            str(r.temperature_c))
assert_true("real frame: marked trustworthy", r.trustworthy)

# These three agreed to the digit with a 0x91 polled seconds earlier - 93 mAh,
# 3.701 V, and 672 s against this frame's 673. That agreement is what confirms
# the byte map, so pin it.
assert_true("real frame: capacity 93 mAh (0x91 agreed)", r.capacity_mah == 93,
            str(r.capacity_mah))
assert_true("real frame: voltage 3.701 V (0x91 agreed)",
            abs(r.voltage_v - 3.701) < 1e-9, str(r.voltage_v))
assert_true("real frame: elapsed 673 s (0x91 said 672 a second earlier)",
            r.elapsed_seconds == 673, str(r.elapsed_seconds))

# The zeroing loop covers 29-32, NOT 17-28 as first documented. Bytes 17-28 are
# written by no path and came back holding leftovers from an earlier frame in
# the shared transmit buffer - the uncleared-buffer hazard, observed.
assert_true("real frame: bytes 29-32 are the zeroed range",
            set(REAL_0X55[29:33]) == {0}, REAL_0X55[29:33].hex())
assert_true("real frame: bytes 17-28 are NOT zeroed (stale buffer, not a field)",
            any(REAL_0X55[17:29]), REAL_0X55[17:29].hex())
assert_true("real frame: byte 13 unwritten", REAL_0X55[13] == 0)
assert_true("real frame: bytes 15-16 hardcoded 0",
            REAL_0X55[15] == 0 and REAL_0X55[16] == 0)
assert_true("real frame: byte 33 hardcoded 1", REAL_0X55[33] == 1)

# The SECOND real frame, 52 minutes later in the same discharge. This is the
# one that turned "a plausible number" into a verified field: byte 14 moved
# 34 -> 37 while a TLD001 on the same cell moved 29.5 -> 33.1, AND the room
# cooled 26.1 -> 24.7. Tracking the cell, not the room.
REAL_0X55_LATER = bytes.fromhex(
    "0f225501030 20b0eb90e0301fa00250000000003000300"
    "9f0000000000000000000101".replace(" ", "") + "05")

r2 = parse_temperature_response(REAL_0X55_LATER)
assert_true("later frame: 36 bytes", len(REAL_0X55_LATER) == 36)
assert_true("later frame: checksum verifies", r2.checksum_ok)
assert_true("later frame: temperature 37 C", r2.temperature_c == 37,
            str(r2.temperature_c))
assert_true("later frame: capacity 523 mAh (0x91 agreed)", r2.capacity_mah == 523,
            str(r2.capacity_mah))
assert_true("later frame: voltage 3.587 V (0x91 agreed)",
            abs(r2.voltage_v - 3.587) < 1e-9, str(r2.voltage_v))
assert_true("later frame: elapsed 3769 s (0x91 said 3768)",
            r2.elapsed_seconds == 3769, str(r2.elapsed_seconds))

# The pair is the actual result, so assert the pair.
assert_true("the pair: temperature ROSE between the two frames",
            r2.temperature_c > r.temperature_c,
            f"{r.temperature_c} -> {r2.temperature_c}")
assert_true("the pair: rise is +3 C, matching the probe's +3.6",
            r2.temperature_c - r.temperature_c == 3,
            str(r2.temperature_c - r.temperature_c))
assert_true("the pair: the run advanced between them",
            r2.elapsed_seconds > r.elapsed_seconds)
assert_true("the pair: both trustworthy", r.trustworthy and r2.trustworthy)

# --- rejection ---
try:
    parse_temperature_response(LIVE_S1_ACTIVE)
except ValueError:
    assert_true("a 0x91 frame is rejected, not silently misparsed", True)
else:
    assert_true("a 0x91 frame is rejected, not silently misparsed", False)

try:
    parse_temperature_response(bytes.fromhex("0f035501"))
except ValueError:
    assert_true("a truncated frame is rejected", True)
else:
    assert_true("a truncated frame is rejected", False)

bad = bytearray(temp_frame())
bad[35] ^= 0xFF
r = parse_temperature_response(bytes(bad))
assert_true("a bad checksum is reported, not raised", not r.checksum_ok)
assert_true("a bad checksum makes the frame untrustworthy", not r.trustworthy)

# --- TempCut (research parameter; fw 2.02 discards the byte) ---
print()
print("TempCut parameter")
_tc_args = dict(slot=1, mode=Mode.DISCHARGE, chemistry=Chemistry.LIION,
                charge_current_ma=0, discharge_current_ma=500,
                capacity_mah=3400, cutoff_mv=3000, cutoff_timer_min=45)
_tc_default = build_config(**_tc_args)
_tc_35 = build_config(**_tc_args, temp_cut_c=35)
_tc_diff = [i for i in range(len(_tc_default)) if _tc_default[i] != _tc_35[i]]
assert_true("TempCut default is 60 (0x3C), so existing configs are unchanged",
            build_config(**_tc_args, temp_cut_c=60) == _tc_default
            and 0x3C in _tc_default)
assert_true("temp_cut_c=35 changes only the TempCut byte and the checksum",
            len(_tc_diff) == 2 and _tc_35[_tc_diff[0]] == 35
            and _tc_diff[1] == len(_tc_35) - 1, str(_tc_diff))
try:
    build_config(**_tc_args, temp_cut_c=61)
except ValueError:
    assert_true("temp_cut_c above 60 is refused", True)
else:
    assert_true("temp_cut_c above 60 is refused", False)

# The 0x55 opcode is confirmed on hardware and is polled on every verified run,
# so nothing in its section may still call it unverified (checked below).
# --- -dV, trickle and keep voltage (the Advanced section) ---
print()
print("-dV / trickle / keep voltage parameters")
from protocol import KEEP_VOLTAGE_GATES_MV, VOLTAGE_GATES                 # noqa: E402
_adv_args = dict(slot=1, mode=Mode.CHARGE, chemistry=Chemistry.NIMH,
                 charge_current_ma=1000, discharge_current_ma=0,
                 capacity_mah=3000, cutoff_timer_min=450)
_adv_default = build_config(**_adv_args)
# data[n] is packet[n + 3]: -dV is data 22, trickle 23, keep voltage 24-25.
assert_true("naming the three at their defaults (6, 0, 0) changes nothing",
            build_config(**_adv_args, delta_peak_mv=6, trickle_ma=0,
                         keep_mv=0) == _adv_default)
assert_true("the default packet holds 6, 0 and 0 there, as it always did",
            (_adv_default[25], _adv_default[26], _adv_default[27:29])
            == (6, 0, bytes([0, 0])), _adv_default.hex())
_adv_set = build_config(**_adv_args, delta_peak_mv=9, trickle_ma=120,
                        keep_mv=1400)
_adv_diff = [i for i in range(len(_adv_set)) if _adv_set[i] != _adv_default[i]]
assert_true("-dV 9, trickle 120 mA (sent as 12) and keep 1400 mV land in bytes "
            "25, 26 and 27-28",
            (_adv_set[25], _adv_set[26], _adv_set[27:29])
            == (9, 12, bytes([1400 >> 8, 1400 & 0xFF])), _adv_set.hex())
assert_true("...and nothing else moves except the checksum",
            set(_adv_diff) <= {25, 26, 27, 28, len(_adv_set) - 1},
            str(_adv_diff))
for _kw, _why in (({"trickle_ma": 55}, "not a multiple of 10"),
                  ({"trickle_ma": 2600}, "too big for the byte"),
                  ({"trickle_ma": -10}, "negative"),
                  ({"keep_mv": 70000}, "too big for two bytes"),
                  ({"delta_peak_mv": 256}, "too big for a byte")):
    try:
        build_config(**_adv_args, **_kw)
    except ValueError:
        assert_true(f"build_config refuses what cannot be encoded ({_why})", True)
    else:
        assert_true(f"build_config refuses what cannot be encoded ({_why})",
                    False)
assert_true("keep-voltage windows exist for every chemistry the gates cover",
            set(KEEP_VOLTAGE_GATES_MV) == set(VOLTAGE_GATES))
assert_true("Li-ion's is the firmware normalizer's own 3980-4180, nickel's the "
            "manual's 1300-1450",
            KEEP_VOLTAGE_GATES_MV[Chemistry.LIION] == (3980, 4180)
            and KEEP_VOLTAGE_GATES_MV[Chemistry.NIMH] == (1300, 1450)
            and KEEP_VOLTAGE_GATES_MV[Chemistry.ENELOOP] == (1300, 1450))
assert_true("every keep-voltage window is ordered (low < high)",
            all(lo < hi for lo, hi in KEEP_VOLTAGE_GATES_MV.values()))

print()
print("0x55 comments")
import inspect as _inspect                                    # noqa: E402
import protocol as _protocol                                   # noqa: E402
_psrc = _inspect.getsource(_protocol)
_start = _psrc.index("# --- 0x55:")
_end = _psrc.index("TEMP_FILLED_STATUSES = ")
assert_true("the 0x55 section no longer calls itself unverified",
            "UNVERIFIED" not in _psrc[_start:_end])
assert_true("build_temperature_query's docstring does not say unverified",
            "UNVERIFIED" not in (_protocol.build_temperature_query.__doc__ or ""))
assert_true("SlotTemperature's docstring does not say unverified",
            "UNVERIFIED" not in (_protocol.SlotTemperature.__doc__ or ""))
assert_true("the transmit-buffer hazard is not described as still open",
            "cannot be recovered" not in _psrc[_start:_end])

print()
print("=" * 55)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 55)
