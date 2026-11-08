"""Builds MatchReady.exe. Used by build.bat and by the in-app updater.

Steps: stamp source hashes -> run tests -> PyInstaller into build/stage -> install into dist/
(if dist/MatchReady.exe is locked because the app is running, the staged build is left for the
app to install on restart).
"""
import json
import msvcrt
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from matchready.updater import BUILD_INFO_NAME, file_hashes  # noqa: E402

STAGE = ROOT / "build" / "stage"
DIST = ROOT / "dist"


def run(*args):
    print(">", " ".join(str(a) for a in args), flush=True)
    return subprocess.run([str(a) for a in args], cwd=ROOT).returncode


def main():
    # One build at a time (the app may auto-build while you also run build.bat).
    (ROOT / "build").mkdir(exist_ok=True)
    lock = open(ROOT / "build" / ".build.lock", "a+b")
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("BUILD FAILED: another build is already running", flush=True)
        return 2
    try:
        return _build()
    finally:
        lock.close()  # releases the lock


def _build():
    info = {"built_at": time.time(), "source_dir": str(ROOT), "files": file_hashes(ROOT)}
    (ROOT / "matchready" / BUILD_INFO_NAME).write_text(json.dumps(info, indent=1), encoding="utf-8")

    if "--skip-tests" not in sys.argv and run(sys.executable, "-m", "pytest", "-q", "tests") != 0:
        print("BUILD FAILED: tests failed", flush=True)
        return 1

    if run(sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
           "--name", "MatchReady", "--collect-data", "customtkinter",
           # Pillow is only here because customtkinter imports it; its AVIF/WebP/ICC plugins are the biggest
           # extensions in the bundle and the app never opens those formats. Fewer bytes = faster rebuilds.
           "--exclude-module", "PIL._avif", "--exclude-module", "PIL._webp",
           "--exclude-module", "PIL._imagingcms", "--exclude-module", "PIL.AvifImagePlugin",
           "--exclude-module", "PIL.WebPImagePlugin",
           # PyInstaller's Pillow hook drags numpy in (~26 MB); Pillow only uses it for optional array conversion.
           "--exclude-module", "numpy",
           "--icon", ROOT / "matchready" / "icon.ico",
           "--add-data", f"{ROOT / 'matchready' / 'icon.ico'};matchready",
           "--add-data", f"{ROOT / 'matchready' / 'targets.json'};matchready",
           "--add-data", f"{ROOT / 'matchready' / 'release.json'};matchready",
           "--add-data", f"{ROOT / 'matchready' / BUILD_INFO_NAME};matchready",
           "--distpath", STAGE, "--workpath", ROOT / "build" / "work", "--specpath", ROOT / "build",
           ROOT / "MatchReady.py") != 0:
        print("BUILD FAILED: PyInstaller error", flush=True)
        return 1
    shutil.copy2(ROOT / "matchready" / "targets.json", STAGE / "targets.json")
    (STAGE / BUILD_INFO_NAME).write_text(json.dumps(info, indent=1), encoding="utf-8")

    DIST.mkdir(exist_ok=True)
    try:
        shutil.copy2(STAGE / "MatchReady.exe", DIST / "MatchReady.exe")
        shutil.copy2(STAGE / "targets.json", DIST / "targets.json")
        print(f"Built and installed {DIST / 'MatchReady.exe'}", flush=True)
    except PermissionError:
        print("MatchReady is running, so the new build was staged in build\\stage. Use 'Restart now' in the "
              "app's update banner, or close the app and run build.bat again.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
