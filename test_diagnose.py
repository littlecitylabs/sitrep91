"""
Tests for diagnose.py: the scan-only health check. The Bluetooth scan is
replaced by a stand-in, so nothing here touches a radio; nothing is written
outside a temp folder.

    python test_diagnose.py
"""

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import runfiles
import diagnose

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name}  {detail}")


TMP = Path(tempfile.mkdtemp(prefix="diagnose_test_"))
ADDR = "AA:BB:CC:00:11:22"
(TMP / "settings.ini").write_text(
    f"[charger {ADDR}]\nfirmware = 2.02\nread_at = 2026-10-29T12:24:12\n",
    encoding="utf-8")
runfiles.SETTINGS_PATH = TMP / "settings.ini"
runfiles.HOME_DIR = TMP / "home"


def go(scan, **kw):
    lines = []
    logs = TMP / f"logs{len(list(TMP.glob('logs*')))}"
    code = diagnose.run(scan=scan, out=lines.append, log_dir=logs, **kw)
    return code, "\n".join(lines), logs


def charger_scan(_):
    async def inner():
        return [SimpleNamespace(name="Some Speaker", address="11:11:11:11:11:11",
                                rssi=-70),
                SimpleNamespace(name="MC5000 Charger", address=ADDR, rssi=-61)]
    return inner()


code, text, logs = go(charger_scan)
check("charger present: exit 0", code == 0, str(code))
check("...reports the adapter, the charger by name and address, its signal",
      "adapter is present" in text and "MC5000 Charger" in text
      and ADDR in text and "-61 dBm" in text, text)
check("...and the remembered firmware, tied to the charger seen",
      "2.02 remembered for " + ADDR in text and "the charger seen above" in text,
      text)
check("...with the app and Python versions",
      runfiles.APP_VERSION in text and "Python" in text)
saved = list(logs.glob("diagnose_*.log"))
check("...and the same report saved in the log folder",
      len(saved) == 1 and "Charger      : seen advertising"
      in saved[0].read_text(encoding="utf-8"), str(saved))
check("...saying where", "saved to" in text and str(logs) in text)


def empty_scan(_):
    async def inner():
        return [SimpleNamespace(name="Some Speaker", address="11:11:11:11:11:11",
                                rssi=-70)]
    return inner()


code, text, _ = go(empty_scan)
check("charger absent: still exit 0, says NOT seen and why it may take a while",
      code == 0 and "NOT seen" in text and "two minutes" in text
      and "power-cycle" in text, text)

# No adapter: reported cleanly, never a crash.
from bleak.exc import BleakBluetoothNotAvailableError, BleakError  # noqa: E402


def no_adapter_scan(_):
    async def inner():
        raise BleakBluetoothNotAvailableError("no adapter", None)
    return inner()


code, text, logs = go(no_adapter_scan)
check("no adapter: exit 1, a plain sentence, no traceback",
      code == 1 and "No Bluetooth adapter" in text
      and "Traceback" not in text, text)
check("...still saved, and firmware still listed",
      len(list(logs.glob("diagnose_*.log"))) == 1
      and "remembered for " + ADDR in text)


def windows_error_scan(_):
    async def inner():
        raise OSError("The device is not ready")
    return inner()


code, text, _ = go(windows_error_scan)
check("a Windows 'not ready' error reads as no adapter / Bluetooth off",
      code == 1 and "No Bluetooth adapter" in text and "not ready" in text,
      text)


def odd_scan(_):
    async def inner():
        raise ValueError("something unexpected")
    return inner()


code, text, _ = go(odd_scan)
check("an unexpected scan error is named, not raised",
      code == 1 and "ValueError: something unexpected" in text, text)

# Nothing remembered, damaged settings file.
runfiles.SETTINGS_PATH = TMP / "missing.ini"
code, text, _ = go(charger_scan)
check("no settings.ini: says nothing remembered yet",
      "none remembered" in text, text)
(TMP / "bad.ini").write_text("this is [not valid\n=== ini", encoding="utf-8")
runfiles.SETTINGS_PATH = TMP / "bad.ini"
code, text, _ = go(charger_scan)
check("a damaged settings.ini is not a crash", code == 0
      and "none remembered" in text, text)

# Unwritable log folder: the report still appears.
blocker = TMP / "blocker"
blocker.write_text("a file", encoding="utf-8")
lines = []
diagnose.run(scan=charger_scan, out=lines.append, log_dir=blocker / "logs")
check("an unwritable log folder: report on screen, and says it was not saved",
      any("could not be saved" in l for l in lines)
      and any("Charger      :" in l for l in lines), "\n".join(lines))

# The scan never connects: diagnose.py must not even reference a client.
src = Path(diagnose.__file__).read_text(encoding="utf-8")
check("diagnose.py never uses BleakClient (scan only)",
      "BleakClient" not in src.replace("NEVER connects", "")
      and ".connect(" not in src)

print(f"\n{passed}/{passed + failed} passed")
sys.exit(1 if failed else 0)
