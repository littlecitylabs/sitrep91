"""
Validate runfiles.py - the portable folder layout, settings.ini and run-folder
naming - with no charger, no Bluetooth and no Qt.

Standalone like the other suites: prints PASS/FAIL lines and a tally, exits
non-zero on any failure. Never touches the real settings.ini or runs folder:
everything is pointed at a throwaway directory first.
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

import runfiles

results = []


def assert_true(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}")
    if not cond and detail:
        print(f"    {detail}")
    results.append(bool(cond))
    return cond


_TMP = tempfile.TemporaryDirectory()
TMP = Path(_TMP.name)
_REAL_SETTINGS = runfiles.SETTINGS_PATH
runfiles.SETTINGS_PATH = TMP / "settings.ini"
_REAL_HOME = runfiles.HOME_DIR

print("--- the home folder: data\\ beside app\\, or the checkout itself ---")
# This suite also runs inside the release build (build_release.py), where the
# code really is in app\, so check whichever layout it finds itself in.
if runfiles.APP_DIR.name.lower() == "app":
    assert_true("running from a release: home is data\\ beside app\\",
                runfiles.HOME_DIR == runfiles.APP_DIR.parent / "data")
else:
    assert_true("a development checkout is its own home (unchanged behavior)",
                runfiles.HOME_DIR == runfiles.APP_DIR)
_rel = Path("C:/Users/x/SITREP91")
assert_true("the released layout (code in app\\) puts home in data\\ beside it",
            runfiles.home_dir_for(_rel / "app") == _rel / "data")
assert_true("...whatever the case of the app folder's name",
            runfiles.home_dir_for(_rel / "App") == _rel / "data")
assert_true("a folder merely containing 'app' is not the released layout",
            runfiles.home_dir_for(_rel / "myapp") == _rel / "myapp")
assert_true("settings.ini lives in the home folder",
            _REAL_SETTINGS == _REAL_HOME / "settings.ini")

print()
print("--- run folder names ---")
t = datetime(2026, 9, 22, 14, 32, 59)
assert_true("<date>_<time>_S<n>_<cell>_<action>, minute resolution",
            runfiles.run_folder_name(t, 2, "TEST-02", "discharge")
            == "2026-09-22_1432_S2_TEST-02_discharge")
assert_true("no cell -> no cell segment, and no '__'",
            runfiles.run_folder_name(t, 3, None, "external")
            == "2026-09-22_1432_S3_external")
assert_true("a blank cell is no cell",
            runfiles.run_folder_name(t, 1, "   ", "charge")
            == "2026-09-22_1432_S1_charge")
assert_true("name order is time order (timestamp first, zero-padded)",
            runfiles.run_folder_name(datetime(2026, 9, 2, 9, 5), 1, None, "charge")
            < runfiles.run_folder_name(datetime(2026, 10, 1, 0, 0), 1, None,
                                       "charge"))
assert_true("the CLI's --name ends the name, after the action",
            runfiles.run_folder_name(t, 2, "TEST-02", "discharge", "mytest")
            == "2026-09-22_1432_S2_TEST-02_discharge_mytest")
assert_true("...made safe and capped like a cell id",
            runfiles.run_folder_name(t, 1, None, "charge", "pair test/" + "x" * 40)
            == "2026-09-22_1432_S1_charge_pair-test-" + "x" * 10)
assert_true("...and no name means no segment",
            runfiles.run_folder_name(t, 1, None, "charge", None)
            == runfiles.run_folder_name(t, 1, None, "charge"))
try:
    runfiles.run_folder_name(t, 1, None, "breakin")
    assert_true("an action outside the fixed vocabulary is refused", False)
except ValueError:
    assert_true("an action outside the fixed vocabulary is refused", True)
assert_true("the vocabulary is charge/discharge/storage/external",
            runfiles.ACTIONS == ("charge", "discharge", "storage", "external"))

print()
print("--- cell ids made safe for a path ---")
assert_true("unsafe characters become '-'",
            runfiles.cell_segment("ENL A/03:x") == "ENL-A-03-x")
assert_true("runs of '-' collapse and ends are trimmed",
            runfiles.cell_segment("--a  //  b--") == "a-b")
assert_true(f"capped at {runfiles.CELL_SEGMENT_MAX} characters",
            len(runfiles.cell_segment("X" * 40)) == runfiles.CELL_SEGMENT_MAX)
assert_true("case is kept (history matching is case-insensitive instead)",
            runfiles.cell_segment("Test-01") == "Test-01")
assert_true("an id with nothing safe in it is no cell",
            runfiles.cell_segment("///") is None)

print()
print("--- collisions ---")
parent = TMP / "coll"
parent.mkdir()
name = "2026-09-22_1432_S2_discharge"
first = runfiles.unique_run_dir(parent, name)
first.mkdir()
second = runfiles.unique_run_dir(parent, name)
second.mkdir()
third = runfiles.unique_run_dir(parent, name)
assert_true("free name is used as is", first.name == name)
assert_true("a taken name gets -2, then -3",
            second.name == name + "-2" and third.name == name + "-3",
            f"{second.name} {third.name}")

print()
print("--- the one .csv in the folder ---")
new = TMP / "2026-09-22_1432_S2_TEST-02_discharge"
new.mkdir()
(new / f"{new.name}.csv").write_text("a\n", encoding="utf-8")
(new / "meta.json").write_text("{}", encoding="utf-8")
assert_true("the CSV named after its folder",
            runfiles.find_run_csv(new).name == f"{new.name}.csv")
old = TMP / "pair_test_20260816_101010"
old.mkdir()
(old / "samples.csv").write_text("a\n", encoding="utf-8")
assert_true("an older run: samples.csv still found",
            runfiles.find_run_csv(old).name == "samples.csv")
renamed = TMP / "my copy of the run"
renamed.mkdir()
(renamed / "2026-09-22_1432_S2_TEST-02_discharge.csv").write_text(
    "a\n", encoding="utf-8")
assert_true("a renamed folder: its single .csv is still found",
            runfiles.find_run_csv(renamed).name
            == "2026-09-22_1432_S2_TEST-02_discharge.csv")
two = TMP / "two"
two.mkdir()
for n in ("a.csv", "b.csv"):
    (two / n).write_text("a\n", encoding="utf-8")
try:
    runfiles.find_run_csv(two)
    assert_true("two .csv files: refused, never guessed", False)
except ValueError as e:
    assert_true("two .csv files: refused, never guessed", "a.csv" in str(e))
empty = TMP / "empty"
empty.mkdir()
try:
    runfiles.find_run_csv(empty)
    assert_true("no .csv: a clear FileNotFoundError", False)
except FileNotFoundError:
    assert_true("no .csv: a clear FileNotFoundError", True)

print()
print("--- Excel's 218-character path limit ---")
assert_true("a normal logs folder is inside the budget",
            runfiles.path_budget_problem(Path("C:/Users/someone/MC5000/runs"))
            is None)
deep = Path("C:/") / ("very_long_folder_name_" * 8)
problem = runfiles.path_budget_problem(deep)
assert_true("a deep folder is flagged, naming the limit",
            problem is not None and "218" in problem, str(problem))
longest = runfiles.run_folder_name(datetime(2026, 12, 31, 23, 59), 4,
                                   "X" * 40, "discharge", "L" * 40) + "-99"
assert_true("the longest run name, CLI name included, stays within ~70 chars",
            len(longest) <= 70, f"{len(longest)}: {longest}")

print()
print("--- startup guards ---")
assert_true("a folder under %TEMP% (a double-clicked zip) is caught",
            runfiles.inside_temp_dir(Path(tempfile.gettempdir())
                                     / "Temp1_MC5000.zip" / "MC5000"))
assert_true("a path through a .zip is caught",
            runfiles.inside_temp_dir(Path("C:/Users/x/Downloads/MC5000.zip/app")))
assert_true("an ordinary extracted folder is not",
            not runfiles.inside_temp_dir(Path("C:/Users/x/Documents/MC5000")))
assert_true("a writable folder has no problem",
            runfiles.writable_problem(TMP / "w") is None
            and (TMP / "w").is_dir()
            and not any((TMP / "w").iterdir()))
blocker = TMP / "blocker"
blocker.write_text("a file, not a folder", encoding="utf-8")
p = runfiles.writable_problem(blocker / "runs")
assert_true("an unwritable folder is described plainly",
            p is not None and "Cannot write" in p, str(p))
assert_true("the offered alternative is Documents\\SITREP91",
            runfiles.documents_fallback_dir().parts[-3:]
            == ("Documents", "SITREP91", "runs"))
runfiles.HOME_DIR = TMP / "home"
assert_true("no settings.ini yet", not runfiles.SETTINGS_PATH.exists())
runfiles.ensure_home_folders()
assert_true("no profiles\\ folder is created (profiles are not a v1 feature)",
            not (TMP / "home" / "profiles").exists()
            and not hasattr(runfiles, "profiles_dir"))
assert_true("a first launch writes settings.ini with the defaults",
            runfiles.SETTINGS_PATH.exists()
            and runfiles.read_setting("display", "theme") == "system"
            and runfiles.DEFAULT_SETTINGS == {"display": {"theme": "system"}})
runfiles.write_setting("display", {"theme": "dark"})
runfiles.ensure_home_folders()
assert_true("an existing settings.ini is never overwritten",
            runfiles.read_setting("display", "theme") == "dark")
_sp = runfiles.SETTINGS_PATH
runfiles.SETTINGS_PATH = TMP / "damaged.ini"
runfiles.SETTINGS_PATH.write_text("this is [not valid", encoding="utf-8")
runfiles.ensure_home_folders()
assert_true("...not even a damaged one",
            runfiles.SETTINGS_PATH.read_text(encoding="utf-8")
            == "this is [not valid")
runfiles.SETTINGS_PATH = blocker / "settings.ini"
try:
    runfiles.ensure_home_folders()
    assert_true("an unwritable home skips it quietly, never raising", True)
except Exception as e:
    assert_true("an unwritable home skips it quietly, never raising", False,
                repr(e))
runfiles.SETTINGS_PATH = _sp

print()
print("--- the logs folder in settings.ini ---")
assert_true("default: runs\\ in the home folder",
            runfiles.configured_runs_dir() == TMP / "home" / "runs")
runfiles.save_runs_dir(TMP / "home" / "elsewhere")
assert_true("a folder inside home is stored RELATIVE (the app stays portable)",
            runfiles.read_setting("logging", "runs_dir") == "elsewhere",
            str(runfiles.read_setting("logging", "runs_dir")))
assert_true("...and resolves back against home",
            runfiles.configured_runs_dir() == TMP / "home" / "elsewhere")
outside = TMP / "usb" / "MC5000 logs"
runfiles.save_runs_dir(outside)
assert_true("a folder outside home is stored absolute",
            Path(runfiles.read_setting("logging", "runs_dir")) == outside.resolve())
assert_true("...and read back as is",
            runfiles.configured_runs_dir() == outside.resolve())
runfiles.write_setting("logging", {"runs_dir": ""})
assert_true("an empty value means the default",
            runfiles.configured_runs_dir() == TMP / "home" / "runs")
runfiles.HOME_DIR = _REAL_HOME

print()
print("--- settings.ini ---")
runfiles.write_setting("display", {"theme": "dark"})
runfiles.write_setting("display", {"temperature_unit": "F"})
assert_true("writes keep the rest of the section",
            runfiles.read_setting("display", "theme") == "dark"
            and runfiles.read_setting("display", "temperature_unit") == "F")
assert_true("a missing key reads as None",
            runfiles.read_setting("display", "nope") is None)
runfiles.SETTINGS_PATH = blocker / "settings.ini"
msgs = []
assert_true("an unwritable settings.ini is reported, never raised",
            runfiles.write_setting("x", {"a": 1}, msgs.append) is False
            and len(msgs) == 1)
runfiles.SETTINGS_PATH = _sp

# A write that fails part-way must leave the old file whole (rewriting it in
# place would let a crash mid-write truncate it).
runfiles.write_setting("charger AA:BB", {"firmware": "2.02"})
_before = runfiles.SETTINGS_PATH.read_text(encoding="utf-8")
_real_replace = runfiles.os.replace


def _replace_fails(*a, **k):
    raise OSError("disk full (simulated)")


runfiles.os.replace = _replace_fails
try:
    _msgs2 = []
    _ok2 = runfiles.write_setting("charger AA:BB", {"firmware": "9.99"},
                                  _msgs2.append)
finally:
    runfiles.os.replace = _real_replace
assert_true("a write that fails at the last step is reported, not raised",
            _ok2 is False and len(_msgs2) == 1, str(_msgs2))
assert_true("...the old settings.ini is untouched",
            runfiles.SETTINGS_PATH.read_text(encoding="utf-8") == _before
            and runfiles.read_setting("charger AA:BB", "firmware") == "2.02")
assert_true("...and no temp file is left behind",
            not runfiles.SETTINGS_PATH.with_name("settings.ini.tmp").exists())
runfiles.write_setting("charger AA:BB", {"firmware": "2.03"})
assert_true("a normal write goes through and leaves no temp file",
            runfiles.read_setting("charger AA:BB", "firmware") == "2.03"
            and not runfiles.SETTINGS_PATH.with_name(
                "settings.ini.tmp").exists())

print()
print("--- the CLI session log ---")
lp = runfiles.session_log_path(Path("R"), datetime(2026, 9, 28, 9, 5, 7),
                               "pair test/1")
assert_true("runs\\logs\\<date>_<time>_<name>.log, name made safe",
            lp == Path("R") / "logs" / "2026-09-28_090507_pair-test-1.log",
            str(lp))

print()
print("--- the past cell names Configure offers ---")
import json as _json   # noqa: E402
import os as _os       # noqa: E402


def _mk_run(root, folder, meta, age_s):
    """A run folder whose meta.json was last written `age_s` seconds ago."""
    d = Path(root) / folder
    d.mkdir(parents=True)
    f = d / "meta.json"
    f.write_text(meta if isinstance(meta, str) else _json.dumps(meta),
                 encoding="utf-8")
    t = 1_700_000_000 - age_s
    _os.utime(f, (t, t))


_pr = TMP / "pastruns"
_mk_run(_pr, "2026-10-01_0900_S1_TEST-01_discharge",
        {"cell": "TEST-01", "cells": {"1": "TEST-01"}}, 500)
_mk_run(_pr, "2026-10-02_0900_S2_TEST-02_discharge",
        {"cell": "TEST-02", "cells": {"2": "TEST-02"}}, 400)
_mk_run(_pr, "2026-10-03_0900_S1_test-01_charge",
        {"cell": "test-01", "cells": {"1": "test-01"}}, 100)   # newest, same cell
_mk_run(_pr, "old_run_20260818_000000",
        {"slots": [1, 2], "cells": {"1": "ENL-A-01", "2": "ENL-A-02"}}, 900)
_mk_run(_pr, "no_cell_run", {"cell": None, "cells": {"1": None}}, 50)
_mk_run(_pr, "blank_cell_run", {"cell": "  "}, 40)
_mk_run(_pr, "broken_run", "{ not json", 30)
_mk_run(_pr, "list_run", "[1, 2]", 20)
(_pr / "logs").mkdir()
(_pr / "stray_file.txt").write_text("x", encoding="utf-8")
_names = runfiles.past_cell_names(_pr)
assert_true("names come back most recent first, one per cell",
            _names == ["test-01", "TEST-02", "ENL-A-01", "ENL-A-02"],
            str(_names))
assert_true("case is ignored when matching: the most recent spelling wins",
            "test-01" in _names and "TEST-01" not in _names, str(_names))
assert_true("an old-shape run (only a 'cells' mapping) still contributes",
            "ENL-A-01" in _names and "ENL-A-02" in _names, str(_names))
assert_true("no-cell, blank, unreadable and non-object meta.json are skipped",
            all(n.strip() for n in _names) and len(_names) == 4, str(_names))
assert_true("the logs folder and stray files are not mistaken for runs",
            "logs" not in _names and "stray_file.txt" not in _names)
assert_true("a missing runs folder is an empty list, not an error",
            runfiles.past_cell_names(TMP / "nope") == [])
assert_true("the list is capped (limit)",
            runfiles.past_cell_names(_pr, limit=2) == ["test-01", "TEST-02"],
            str(runfiles.past_cell_names(_pr, limit=2)))
assert_true("only the newest `scan` folders are read",
            runfiles.past_cell_names(_pr, scan=2) == [],   # the two newest hold no cell
            str(runfiles.past_cell_names(_pr, scan=2)))
assert_true("it writes nothing: no new file in the runs folder",
            sorted(p.name for p in _pr.iterdir() if p.is_file())
            == ["stray_file.txt"])

print()
print("--- ROADMAP.md rides in the release ---")
# build_release.py and extract_public.py are in the trees that build the zip, not
# in the zip's own tests, so each check runs only where its file exists.
_here = Path(__file__).resolve().parent
_br = _here / "build_release.py"
if _br.exists():
    _top = next((ln for ln in _br.read_text(encoding="utf-8").splitlines()
                 if ln.startswith("TOP_FILES")), "")
    assert_true("build_release.py copies ROADMAP.md to the zip root",
                '"ROADMAP.md"' in _top, _top)
else:
    print("SKIP  build_release.py not beside the code (a release build)")
_ep = _here / "packaging" / "extract_public.py"
if _ep.exists():
    assert_true("extract_public.py carries ROADMAP.md into the release tree",
                '"packaging/public/ROADMAP.md": "ROADMAP.md"'
                in _ep.read_text(encoding="utf-8"))
    assert_true("...and the file it names exists",
                (_here / "packaging" / "public" / "ROADMAP.md").exists())
    _ep_text = _ep.read_text(encoding="utf-8")
    for _doc in ("resting-voltage", "slot-temperature"):
        assert_true(f"extract_public.py carries docs/{_doc}.md",
                    f'"packaging/public/docs/{_doc}.md": "docs/{_doc}.md"'
                    in _ep_text)
        assert_true(f"...and docs/{_doc}.md exists in packaging/public",
                    (_here / "packaging" / "public" / "docs"
                     / f"{_doc}.md").exists())
if _br.exists():
    _br_text = _br.read_text(encoding="utf-8")
    assert_true("build_release.py ships both docs files under docs\\",
                'DOC_FILES = ("docs/resting-voltage.md", '
                '"docs/slot-temperature.md")' in _br_text
                and "for name in DOC_FILES" in _br_text)

# These look at the build scripts and the repo's own notices, which are in the
# trees that build the zip, not in the zip's own test tree.
if (Path(__file__).resolve().parent / "build_release.py").exists():
    print()
    print("--- third-party licenses ship, and the notices are accurate ---")
    _here = Path(__file__).resolve().parent
    _lic = _here / "packaging" / "licenses"
    if not _lic.is_dir():
        _lic = _here / "licenses"             # this repo's layout
    _notices = (_here / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    for _f in ("LGPL-3.0.txt", "GPL-3.0.txt", "winrt-MIT.txt",
               "python-bundled-software.txt",
               "LLVM-Apache-2.0-with-LLVM-exception.txt", "Mesa-MIT.txt"):
        assert_true(f"licenses/{_f} is in the tree and not empty",
                    (_lic / _f).is_file() and (_lic / _f).stat().st_size > 500,
                    str(_lic / _f))
    assert_true("the LGPL-3.0 text is the real license",
                "GNU LESSER GENERAL PUBLIC LICENSE" in
                (_lic / "LGPL-3.0.txt").read_text(encoding="utf-8")
                and "Version 3, 29 June 2007" in
                (_lic / "LGPL-3.0.txt").read_text(encoding="utf-8"))
    _missing = [m for m in __import__("re").findall(r"`licenses\\([^`]+)`", _notices)
                if not (_lic / m.replace("\\", "/")).exists()]
    assert_true("every licenses\\ file the notices name exists",
                not _missing, str(_missing))
    assert_true("the notices have a bundled-components section naming Qt, PySide6, "
                "NumPy, pyqtgraph, bleak, winrt, colorama and typing_extensions, "
                "with LGPL-3.0 and a pointer to the source",
                all(w in _notices for w in ("## Bundled components", "PySide6",
                                            "Shiboken6", "NumPy", "pyqtgraph",
                                            "bleak", "winrt", "colorama",
                                            "typing_extensions", "LGPL-3.0",
                                            "code.qt.io", "unmodified")))
    assert_true("the notices no longer name a test file the zip does not ship",
                "test_protocol.py" not in _notices)
    assert_true("the skyrc-mc-rs copyright line is upstream's, verbatim",
                "Copyright 2026 Roman Samarev\n" in _notices.replace("\r\n", "\n"))
    _br_src = (_here / "build_release.py").read_text(encoding="utf-8")
    assert_true("the build copies licenses\\ into the zip",
                'LICENSES_DIR = "licenses"' in _br_src
                and "shutil.copytree(ROOT / LICENSES_DIR" in _br_src)

    print()
    print("--- double-click launchers for the demo and the diagnose report ---")
    _pk = Path(__file__).resolve().parent / "packaging"
    for _name, _arg in (("SITREP91_Demo.bat", "--demo"),
                        ("SITREP91_Diagnose.bat", "--diagnose")):
        _bat = _pk / _name
        _bt = _bat.read_text(encoding="utf-8") if _bat.exists() else ""
        assert_true(f"{_name} exists and calls SITREP91.bat {_arg}, from its own "
                    f"folder (nobody has to type an argument)",
                    f'call "%~dp0SITREP91.bat" {_arg}' in _bt, _bt)
        assert_true(f"...is built into the zip", f'"{_name}"' in _br_text)
        if _ep.exists():
            assert_true(f"...and extract_public.py carries it into the release tree",
                        f'"packaging/{_name}"' in _ep.read_text(encoding="utf-8"))
    if not _ep.exists():
        print("SKIP  launchers in extract_public.py: not in this repo")
else:
    print("SKIP  licenses / launchers: not beside the build scripts")

print()
print("=" * 55)
print(f"{sum(results)}/{len(results)} checks passed")
print("=" * 55)
sys.exit(0 if all(results) else 1)
