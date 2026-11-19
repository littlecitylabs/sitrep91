"""
Build the SITREP91 Windows release zip from this source tree.

    python build_release.py        # -> dist\\SITREP91-<version>-win64.zip

Run it on Windows from a clone of this repo, with any Python that has pip and
internet access to python.org and PyPI. It produces:

    SITREP91\\
      SITREP91.bat
      python\\        <- embeddable CPython, with the pinned packages in
                        python\\Lib\\site-packages
      app\\           <- the shipped modules, an explicit allowlist
      README.md, ROADMAP.md, LICENSE, THIRD_PARTY_NOTICES.md
      docs\\, images\\   <- what the README links to and shows

data\\ is not in the zip: the app creates it on first launch (runfiles.py).

The steps, in order, and any failure stops the build before a zip is written:
  1. fetch the embeddable Python zip, checked against the SHA-256 python.org
     publishes (cached in build\\cache\\ once it has passed);
  2. install requirements.txt into it - binary wheels only, no dependency
     resolution, so requirements.txt is the whole list;
  3. prune Qt to the modules the app and pyqtgraph import;
  4. copy the allowlisted app files, the top-level docs and the launcher;
  5. precompile: the embedded interpreter never writes bytecode (its ._pth
     makes it isolated), so without this it would recompile everything on
     every launch. app\\ gets source-checked pycs, so an edit to the shipped
     source still takes effect; site-packages gets unchecked ones;
  6. run the offline suites under the embedded interpreter, against a copy of
     the tree so nothing they write can reach the zip;
  7. zip.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
CACHE = BUILD / "cache"
STAGE = BUILD / "SITREP91"
TEST_TREE = BUILD / "test_tree"
DIST = ROOT / "dist"

PY_VERSION = "3.13.14"
EMBED_NAME = f"python-{PY_VERSION}-embed-amd64.zip"
EMBED_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{EMBED_NAME}"
# From python.org's release page, and matched by a download.
EMBED_SHA256 = "90b4e5b9898b72d744650524bff92377c367f44bd5fbd09e3148656c080ad907"

# What gui.py imports, directly or through capture.py - and nothing else.
APP_FILES = ("gui.py", "gui_theme.py", "capture.py", "protocol.py",
             "drift.py", "runfiles.py", "crashlog.py", "diagnose.py")
TOP_FILES = ("README.md", "ROADMAP.md", "LICENSE", "THIRD_PARTY_NOTICES.md")
# The README links to these, so they ride in the zip under docs\.
DOC_FILES = ("docs/resting-voltage.md", "docs/slot-temperature.md")
# ... and shows these two screenshots (light and dark), so they ride under
# images\ and the local README displays them instead of broken links.
IMAGE_FILES = ("images/sitrep91-light.png", "images/sitrep91-dark.png")
# The license texts of everything bundled that THIRD_PARTY_NOTICES.md points at
# (LGPL-3.0 and GPL-3.0 for Qt and PySide6, the winrt packages' MIT text, the
# Qt-bundled third-party texts, ...). Copied whole, under licenses\.
LICENSES_DIR = "licenses"
LAUNCHER = ROOT / "packaging" / "SITREP91.bat"
# Double-click launchers for the two options a hobbyist is told to use: they
# call SITREP91.bat with the argument, so nobody has to type one.
EXTRA_LAUNCHERS = (ROOT / "packaging" / "SITREP91_Demo.bat",
                   ROOT / "packaging" / "SITREP91_Diagnose.bat")

# The suites that exercise only shipped modules. Run, never shipped.
TESTS = ("test_protocol.py", "test_drift.py", "test_capture.py",
         "test_gui.py", "test_runfiles.py", "test_crashlog.py",
         "test_diagnose.py")

# Qt modules kept: what `import gui` actually loads (gui and pyqtgraph,
# measured). Every other Qt6*.dll / Qt*.pyd goes.
QT_KEEP = {"Core", "Gui", "Widgets", "OpenGL", "OpenGLWidgets", "Svg", "Test"}
# Folders inside PySide6 that only tools or QML use.
QT_DROP_DIRS = ("qml", "translations", "include", "typesystems", "glue",
                "scripts", "metatypes", "resources", "doc")
QT_KEEP_PLUGINS = ("platforms", "styles", "imageformats", "iconengines")


def step(text: str) -> None:
    print(f"\n=== {text}")


def fail(text: str) -> None:
    print(f"\nBUILD FAILED: {text}")
    sys.exit(1)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def rmtree_inside(path: Path, parent: Path) -> None:
    """Delete `path`, but only if it lies inside `parent` - a guard against a
    mistyped constant turning this into a delete of something that matters."""
    if parent.resolve() not in path.resolve().parents:
        fail(f"refusing to delete {path}: not inside {parent}")
    if path.exists():
        shutil.rmtree(path)


def run(args, **kw) -> subprocess.CompletedProcess:
    result = subprocess.run([str(a) for a in args], **kw)
    if result.returncode != 0:
        fail(f"exit {result.returncode} from: {' '.join(map(str, args))}")
    return result


def fetch_embed() -> Path:
    step(f"Embeddable Python {PY_VERSION}")
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / EMBED_NAME
    if target.exists() and sha256(target) == EMBED_SHA256:
        print(f"cached, SHA-256 OK: {target}")
        return target
    part = target.with_suffix(".part")
    print(f"downloading {EMBED_URL}")
    urllib.request.urlretrieve(EMBED_URL, part)
    got = sha256(part)
    if got != EMBED_SHA256:
        part.unlink()
        fail(f"SHA-256 mismatch for {EMBED_NAME}: got {got}, "
             f"expected {EMBED_SHA256}")
    part.replace(target)
    print("SHA-256 OK")
    return target


def unpack_python(embed_zip: Path) -> Path:
    py_dir = STAGE / "python"
    with zipfile.ZipFile(embed_zip) as z:
        z.extractall(py_dir)
    pths = list(py_dir.glob("python*._pth"))
    if len(pths) != 1:
        fail(f"expected one ._pth in the embeddable zip, found {len(pths)}")
    stdlib = pths[0].stem + ".zip"      # python313._pth -> python313.zip
    # No `import site`: packages live in Lib\site-packages, listed here, and
    # nothing shipped relies on .pth processing.
    pths[0].write_text(f"{stdlib}\n.\nLib\\site-packages\n..\\app\n",
                       encoding="ascii")
    print(f"{pths[0].name}: {stdlib}, ., Lib\\site-packages, ..\\app")
    return py_dir


def install_packages(site: Path) -> None:
    step("Packages from requirements.txt")
    run([sys.executable, "-m", "pip", "install",
         "--disable-pip-version-check", "--no-input",
         "--target", site, "--no-deps", "--no-compile",
         "--only-binary", ":all:", "--platform", "win_amd64",
         "--python-version", PY_VERSION.rsplit(".", 1)[0],
         "--implementation", "cp",
         "-r", ROOT / "requirements.txt"])
    # Console-script wrappers (pyside6-designer and friends): unusable here.
    if (site / "bin").exists():
        shutil.rmtree(site / "bin")


def prune_qt(site: Path) -> None:
    step("Pruning Qt")
    pyside = site / "PySide6"
    before = tree_size(pyside)
    module = re.compile(r"^(?:Qt6(\w+)\.dll|Qt(\w+)\.(?:pyd|pyi))$")
    for f in pyside.iterdir():
        m = module.match(f.name)
        if m and (m.group(1) or m.group(2)) not in QT_KEEP:
            f.unlink()
        elif f.is_file() and f.suffix == ".exe":
            f.unlink()          # designer, qmlls, lupdate ...
    for d in QT_DROP_DIRS:
        if (pyside / d).exists():
            shutil.rmtree(pyside / d)
    plugins = pyside / "plugins"
    if plugins.exists():
        for d in plugins.iterdir():
            if d.name not in QT_KEEP_PLUGINS:
                shutil.rmtree(d)
    # pyqtgraph's bundled example programs.
    if (site / "pyqtgraph" / "examples").exists():
        shutil.rmtree(site / "pyqtgraph" / "examples")
    print(f"PySide6 {before / 1e6:.0f} MB -> {tree_size(pyside) / 1e6:.0f} MB")
    # opengl32sw.dll (~20 MB, Qt's software OpenGL for machines with no GL
    # driver) is deliberately KEPT: nothing here is known to need it, but a
    # VM or a broken driver is exactly where a missing fallback would bite.


def prune_package_tests(site: Path) -> None:
    """The packages' own test suites (numpy's are ~14 MB): never imported
    by the app. numpy.testing is a different folder and stays."""
    dropped = 0
    for d in sorted(site.rglob("tests"), key=lambda p: len(p.parts)):
        if d.is_dir() and d.exists():
            dropped += tree_size(d)
            shutil.rmtree(d)
    print(f"package test suites: {dropped / 1e6:.0f} MB removed")


def copy_app() -> None:
    step("App files")
    app = STAGE / "app"
    app.mkdir()
    for name in APP_FILES:
        shutil.copy2(ROOT / name, app / name)
    for name in TOP_FILES:
        shutil.copy2(ROOT / name, STAGE / name)
    (STAGE / "docs").mkdir()
    for name in DOC_FILES:
        shutil.copy2(ROOT / name, STAGE / name)
    (STAGE / "images").mkdir()
    for name in IMAGE_FILES:
        shutil.copy2(ROOT / name, STAGE / name)
    if not (ROOT / LICENSES_DIR).is_dir():
        raise SystemExit(f"the {LICENSES_DIR}\\ folder is missing: the bundled "
                         f"components' license texts must ship")
    shutil.copytree(ROOT / LICENSES_DIR, STAGE / LICENSES_DIR)
    shutil.copy2(LAUNCHER, STAGE / LAUNCHER.name)
    for extra in EXTRA_LAUNCHERS:
        shutil.copy2(extra, STAGE / extra.name)
    print(f"app\\: {', '.join(APP_FILES)}")
    print(f"top: {', '.join(TOP_FILES)}, {LAUNCHER.name}, "
          f"{', '.join(x.name for x in EXTRA_LAUNCHERS)}")
    print(f"docs\\: {', '.join(Path(n).name for n in DOC_FILES)}")
    print(f"images\\: {', '.join(Path(n).name for n in IMAGE_FILES)}")
    print(f"{LICENSES_DIR}\\: "
          f"{sum(1 for f in (STAGE / LICENSES_DIR).rglob('*') if f.is_file())}"
          f" license texts")


def precompile(py_exe: Path) -> None:
    step("Precompiling")
    # -s/-p: bytecode records the path of its source. Left alone that is this
    # machine's build folder (with the builder's user name in it), shipped to
    # everyone. Record SITREP91\... instead; Python corrects the name to the
    # real path when it loads the file, so tracebacks are unaffected.
    paths = ["-s", str(STAGE), "-p", "SITREP91"]
    run([py_exe, "-m", "compileall", "-q", "-j", "0", *paths,
         "--invalidation-mode", "unchecked-hash",
         STAGE / "python" / "Lib" / "site-packages"])
    run([py_exe, "-m", "compileall", "-q", *paths,
         "--invalidation-mode", "checked-hash", STAGE / "app"])
    print("done")


def check_no_build_paths() -> None:
    """Fail the build if the staged tree carries this machine's paths."""
    step("Checking for build paths")
    needles = {str(ROOT), str(Path.home())}
    needles |= {n.replace("\\", "/") for n in needles}
    needles = [n.encode("utf-8") for n in needles if len(n) > 6]
    kinds = {".pyc", ".py", ".pth", ".json", ".txt", ".cfg", ".ini", ".bat",
             ".md", ".toml"}
    bad = []
    for p in STAGE.rglob("*"):
        if p.is_file() and p.suffix.lower() in kinds:
            data = p.read_bytes()
            if any(n in data for n in needles):
                bad.append(p.relative_to(STAGE))
    if bad:
        fail(f"{len(bad)} file(s) in the release carry a build path "
             f"(first: {bad[0]})")
    print("none found")


def run_suites() -> None:
    step("Offline suites under the embedded interpreter")
    rmtree_inside(TEST_TREE, BUILD)
    shutil.copytree(STAGE, TEST_TREE)
    tests = TEST_TREE / "tests"
    tests.mkdir()
    for name in TESTS:
        shutil.copy2(ROOT / name, tests / name)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    py_exe = TEST_TREE / "python" / "python.exe"
    failed = []
    for name in TESTS:
        r = subprocess.run([str(py_exe), name], cwd=tests, env=env,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        lines = r.stdout.splitlines()
        fails = [ln for ln in lines if ln.startswith("FAIL")]
        tally = next((ln.strip() for ln in reversed(lines)
                      if re.search(r"\d+/\d+", ln)), "(no tally)")
        ok = r.returncode == 0 and not fails
        print(f"{'PASS' if ok else 'FAIL'}  {name:<20} {tally}")
        if not ok:
            failed.append(name)
            for ln in (fails or lines[-15:]):
                print(f"    {ln}")
            if r.stderr.strip():
                print("    stderr: " + r.stderr.strip().splitlines()[-1])
    rmtree_inside(TEST_TREE, BUILD)
    if failed:
        fail(f"suites failed under the embedded interpreter: {failed}")


def make_zip(version: str) -> Path:
    step("Zip")
    if (STAGE / "data").exists():
        fail("the staged tree has a data\\ folder; it must never ship")
    DIST.mkdir(exist_ok=True)
    out = DIST / f"SITREP91-{version}-win64.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=9) as z:
        for f in sorted(STAGE.rglob("*")):
            if f.is_file():
                z.write(f, Path("SITREP91") / f.relative_to(STAGE))
    print(f"{out}: {out.stat().st_size / 1e6:.0f} MB zipped, "
          f"{tree_size(STAGE) / 1e6:.0f} MB unpacked")
    return out


def tree_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def main() -> None:
    if os.name != "nt":
        fail("build on Windows: the suites run under the bundled Windows "
             "interpreter, and requirements.txt's markers are evaluated "
             "against this machine")
    # Line-buffered, so this script's headings and pip's output interleave in
    # the order they happened.
    sys.stdout.reconfigure(line_buffering=True)
    sys.path.insert(0, str(ROOT))
    import runfiles
    version = runfiles.APP_VERSION
    print(f"SITREP91 {version} from {ROOT}")

    embed_zip = fetch_embed()
    step("Staging")
    rmtree_inside(STAGE, BUILD)
    py_dir = unpack_python(embed_zip)
    site = py_dir / "Lib" / "site-packages"
    install_packages(site)
    prune_qt(site)
    prune_package_tests(site)
    copy_app()
    precompile(py_dir / "python.exe")
    run_suites()
    check_no_build_paths()
    make_zip(version)
    # A release candidate carries gui.RC_TAG in its window title (a testing
    # aid); the release build must not. Read as text, so this script never
    # imports Qt.
    m = re.search(r'^RC_TAG = "(.*)"', (ROOT / "gui.py").read_text(
        encoding="utf-8"), re.M)
    tag = m.group(1) if m else "?"
    if tag:
        print(f"\nRELEASE CANDIDATE '{tag}' - gui.RC_TAG is set and shows in "
              f"the window title. Set RC_TAG = \"\" before the release build.")
    print("\nBUILD OK")


if __name__ == "__main__":
    main()
