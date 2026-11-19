"""
Prove the drift detector catches the real failures we hit today,
plus synthetic versions of the ones we got lucky on.
"""
from drift import Fingerprint, FrameValidator

def banner(t): print(f"\n--- {t} ---")

# Real frames captured from the FW 2.02 charger today
GOOD = [
    bytes.fromhex("0f169101 05db 1009 0000 0025 0000 005f 000d 03020000 0021".replace(" ","")),
    bytes.fromhex("0f169102 0000 1065 0000 0000 0000 0000 0000 00000000 0008".replace(" ","")),
    bytes.fromhex("0f169104 0000 0000 0000 0000 0000 0000 0000 00000000 0095".replace(" ","")),
]

banner("1. FIRST RUN: no baseline yet - validator accumulates one")
v = FrameValidator(baseline=None)
v.current.set_device_info(firmware="2.02", product_code="TESTCODE")
for f in GOOD:
    r = v.check(f)
    print(f"  {r.severity:5s}  {len(f)}B  {'; '.join(r.messages) or 'clean'}")
# what you would persist at the end of a first clean run:
fp = v.current
fp.captured_at = "2026-08-02T16:00:00"
print(f"  baseline established: 0x91 frames are {fp.frame_lengths['0x91']} bytes")

banner("2. FW 1.82-style 23-byte frame arrives (simulates a firmware rollback/change)")
body = bytes.fromhex("9101" + "00c6" + "059d" + "0000" + "005c" + "0000" + "0761" + "02b0" + "02" + "0000" + "03")
old = bytes([0x0F, len(body) + 1]) + body
old = old + bytes([sum(old[2:]) % 256])
assert len(old) == 23, f"wanted 23 bytes, got {len(old)}"
r = FrameValidator(fp).check(old)
print(f"  severity={r.severity}")
for m in r.messages: print(f"    {m}")

banner("3. corrupted frame (single bit flipped)")
bad = bytearray(GOOD[0]); bad[7] ^= 0x40
r = FrameValidator(fp).check(bytes(bad))
print(f"  severity={r.severity}")
for m in r.messages: print(f"    {m}")

banner("4. THE REAL BUG: current decoded with the old status-byte method")
print("  simulating a frame where current reads 876mA instead of 1499mA")
print("  by keeping capacity/elapsed truthful but current wrong.")
# 876mA = 0x036C ; keep capacity 37mAh, elapsed 95s -> physically inconsistent
frame = bytearray(GOOD[0])
frame[4], frame[5] = 0x03, 0x6C          # current -> 876mA
frame[14], frame[15] = 0x00, 0x5F        # elapsed 95s
frame[10], frame[11] = 0x00, 0x25        # capacity 37mAh
frame[-1] = sum(frame[2:-1]) % 256       # fix checksum so ONLY physics catches it
r = FrameValidator(fp).check(bytes(frame))
print(f"  severity={r.severity}   (checksum is VALID - only the physics check can catch this)")
for m in r.messages: print(f"    {m}")

banner("5. impossible value (voltage decoded from wrong offset)")
frame2 = bytearray(GOOD[0]); frame2[6], frame2[7] = 0xFF, 0xFF   # 65.535 V
frame2[-1] = sum(frame2[2:-1]) % 256
r = FrameValidator(fp).check(bytes(frame2))
print(f"  severity={r.severity}")
for m in r.messages: print(f"    {m}")

banner("6. fingerprint diff: baseline vs a changed device")
newfp = Fingerprint()
newfp.set_device_info(firmware="2.10", product_code="TESTCODE")
for f in GOOD:
    newfp.observe(f)
newfp.observe(old)
for line in fp.diff(newfp):
    print(f"  {line}")

banner("8. a break-in past 24 h is NOT a drift error")
#
# Measured on hardware: a front-panel Break-In counts elapsed CUMULATIVELY over
# all its phases, so a 37 h cycle crosses 86400 s. The old 24 h range bound -
# unreachable back when elapsed was read as 16 bits - then flagged every
# subsequent frame, 34833 of them, all checksum-valid with sane values.
def frame_at_elapsed(seconds):
    """A GOOD frame with its 32-bit elapsed field (bytes 12-15) set."""
    f = bytearray(GOOD[0])
    f[12:16] = seconds.to_bytes(4, "big")
    f[-1] = sum(f[2:-1]) % 256
    return bytes(f)

for label, secs in (("18h12m, the old 16-bit wrap", 65535),
                    ("24 h exactly, the old bound", 86400),
                    ("24 h + 2 s, where the run broke", 86402),
                    ("37 h, a full break-in", 133200),
                    ("6 days, a long multi-cycle run", 518400)):
    r = FrameValidator(fp).check(frame_at_elapsed(secs))
    print(f"  {secs:>9}s  severity={r.severity:5s}  {label}")
    assert r.severity != "error", (
        f"elapsed {secs}s wrongly flagged an error: {r.messages}")

# ...but a genuine misparse still is one. A 32-bit field read at the wrong
# offset lands in the hundreds of millions, far outside any real run.
huge = FrameValidator(fp).check(frame_at_elapsed(0x7FFFFFFF))
print(f"  {0x7FFFFFFF:>9}s  severity={huge.severity:5s}  a 32-bit misparse")
assert huge.severity == "error", "an impossible elapsed must still be caught"
print("  PASS: real long runs accepted, impossible values still rejected")

banner("7. full report over a clean run")
v2 = FrameValidator(fp)
for _ in range(10):
    for f in GOOD:
        v2.check(f)
print(v2.report())

banner("9. an older saved fingerprint still loads")
#
# Its product code was stored under the old key "serial". It must come back as
# product_code, and a fingerprint saved now must use the new key.
import json as _json
import os as _os
import tempfile as _tf
_d = _tf.mkdtemp()
_old_path = _os.path.join(_d, "old.json")
with open(_old_path, "w", encoding="utf-8") as _fh:
    _json.dump({"firmware": "2.02", "hardware": "2.00", "serial": "100214"}, _fh)
_old = Fingerprint.load(_old_path)
assert _old.product_code == "100214", _old.product_code
_new_path = _os.path.join(_d, "new.json")
_old.save(_new_path)
with open(_new_path, encoding="utf-8") as _fh:
    _saved = _json.load(_fh)
assert _saved.get("product_code") == "100214" and "serial" not in _saved, _saved
assert Fingerprint.builtin("2.02").product_code == "100214"
print("  PASS: old 'serial' key loads as product_code; saves use product_code")
