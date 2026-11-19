"""
SITREP91 diagnostics: a scan-only health check, for troubleshooting and for
anyone reporting a problem. Run it with `SITREP91.bat --diagnose`.

It NEVER connects to the charger. It only listens for Bluetooth adverts for
about ten seconds - connecting would use up the couple of minutes the charger
needs to advertise again after a clean disconnect, and could collide with a
running SITREP91. So it is safe to run at any time, even mid-run.

It reports: the app and Python versions, whether the data folder can be
written, whether a Bluetooth adapter is present, whether the charger is seen
advertising, and the firmware version remembered for it in settings.ini. The
same text goes to the screen and to <data>\\logs\\diagnose_<date>_<time>.log.

"No Bluetooth adapter" is a normal answer, reported plainly, never a crash.
"""

from __future__ import annotations

import asyncio
import configparser
import platform
import struct
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import runfiles

SCAN_S = 10.0
CHARGER_PREFIX = "charger "     # settings.ini sections: [charger AA:BB:CC:DD:EE:FF]

NO_ADAPTER = ("No Bluetooth adapter was found, or Bluetooth is switched off. "
              "SITREP91 needs a Bluetooth LE adapter (built in or a USB "
              "dongle) that is on. A desktop PC without one cannot connect to "
              "the charger.")


async def default_scan(timeout: float) -> list:
    """Listen for adverts; return objects with .name, .address and .rssi."""
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return [SimpleNamespace(name=(adv.local_name or dev.name or ""),
                            address=dev.address, rssi=adv.rssi)
            for dev, adv in found.values()]


def remembered_chargers(path: str | Path | None = None) -> dict[str, tuple]:
    """{address: (firmware, read_at)} from settings.ini. A missing or damaged
    file is simply 'nothing remembered'."""
    cp = configparser.ConfigParser()
    try:
        cp.read(path or runfiles.SETTINGS_PATH, encoding="utf-8")
    except (OSError, configparser.Error):
        return {}
    out = {}
    for section in cp.sections():
        if section.lower().startswith(CHARGER_PREFIX):
            out[section[len(CHARGER_PREFIX):].strip().upper()] = (
                cp.get(section, "firmware", fallback=None),
                cp.get(section, "read_at", fallback=None))
    return out


def scan_problem(exc: BaseException) -> str:
    """A plain sentence for a scan that could not run."""
    try:
        from bleak.exc import BleakBluetoothNotAvailableError
        if isinstance(exc, BleakBluetoothNotAvailableError):
            return NO_ADAPTER
    except Exception:
        pass
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if any(w in low for w in ("no bluetooth", "adapter", "not available",
                              "powered_off", "radio", "not ready")):
        return NO_ADAPTER + f" (Windows said: {text})"
    return f"The scan could not run: {type(exc).__name__}: {text}"


def run(scan=default_scan, out=print, log_dir: str | Path | None = None,
        timeout: float = SCAN_S) -> int:
    """Run the checks. `scan(timeout)` and `out(line)` are replaceable for
    tests. Returns 0 if the scan ran (whatever it found), 1 if it could not."""
    lines: list[str] = []

    def say(text: str = "") -> None:
        lines.append(text)
        out(text)

    now = datetime.now()
    say(f"SITREP91 diagnostics - {now:%Y-%m-%d %H:%M:%S}")
    say("(scan only: this never connects to the charger)")
    say()
    say(f"App version  : {runfiles.APP_VERSION}")
    say(f"Python       : {platform.python_version()} "
        f"({struct.calcsize('P') * 8}-bit) on {platform.platform()}")
    say(f"App folder   : {runfiles.APP_DIR}")
    say(f"Data folder  : {runfiles.HOME_DIR}")
    problem = runfiles.writable_problem(runfiles.HOME_DIR)
    say("Data folder can be written : " + ("yes" if problem is None
                                           else f"NO - {problem}"))
    say()

    # NAME_HINTS lives in capture.py (the one place that decides what counts
    # as the charger); it needs bleak, so a broken install shows up here.
    try:
        from capture import NAME_HINTS
    except Exception as e:
        say(f"Bluetooth    : the Bluetooth support could not be loaded "
            f"({type(e).__name__}: {e}).")
        say("               The program files look incomplete: extract the "
            "whole zip again.")
        return _finish(lines, out, log_dir, now, 1)

    say(f"Scanning for {timeout:.0f} seconds ...")
    code = 0
    seen = []
    try:
        seen = asyncio.run(scan(timeout))
    except Exception as e:
        code = 1
        say(f"Bluetooth    : {scan_problem(e)}")
    else:
        say(f"Bluetooth    : an adapter is present and scanning works "
            f"({len(seen)} Bluetooth device(s) heard)")
        chargers = [d for d in seen
                    if any(h in (d.name or "").lower() for h in NAME_HINTS)]
        if chargers:
            for d in chargers:
                rssi = getattr(d, "rssi", None)
                say(f"Charger      : seen advertising as '{d.name}' "
                    f"[{d.address}]"
                    + (f", signal {rssi} dBm" if rssi is not None else ""))
        else:
            say("Charger      : NOT seen. Check that it is switched on and "
                "within a few meters.")
            say("               If SITREP91 was closed in the last couple of "
                "minutes, wait: after a clean")
            say("               disconnect the charger can take about two "
                "minutes to advertise again.")
            say("               If it still does not appear, power-cycle the "
                "charger (this ends any run on it).")
            chargers = []

    remembered = remembered_chargers()
    say()
    if remembered:
        for addr, (fw, read_at) in remembered.items():
            here = any(d.address.upper() == addr for d in seen)
            say(f"Firmware     : {fw or 'unknown'} remembered for {addr}"
                + (f" (read {read_at})" if read_at else "")
                + ("  <- the charger seen above" if here else ""))
    else:
        say("Firmware     : none remembered yet. It is read the first time "
            "SITREP91 connects to an idle charger.")
    return _finish(lines, out, log_dir, now, code)


def _finish(lines, out, log_dir, now, code) -> int:
    """Save the report beside the crash logs; say where, or why not."""
    folder = Path(log_dir) if log_dir else runfiles.crash_log_dir()
    path = folder / f"diagnose_{now:%Y-%m-%d_%H%M%S}.log"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out("")
        out(f"This report was saved to: {path}")
    except OSError as e:
        out("")
        out(f"(This report could not be saved to {path}: {e.strerror or e})")
    return code


if __name__ == "__main__":
    sys.exit(run())
