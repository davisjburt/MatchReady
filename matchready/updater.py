"""Detects edits to the app's source code and rebuilds/restarts into the new version.

Frozen exe: the build embeds per-file source hashes and the project folder (see build.py). While the
app runs it compares those to the files on disk; on a change it runs build.py (tests + PyInstaller into
build/stage), then a helper script swaps the exe in after the app exits (a running exe is locked).
From source: the baseline is the code as it was at startup, and applying = relaunching Python.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

BUILD_INFO_NAME = "_build_info.json"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0)
BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)


def tracked_files(root):
    root = Path(root)
    files = [root / "MatchReady.py", root / "requirements.txt", root / "matchready" / "targets.json",
             root / "matchready" / "release.json", root / "matchready" / "icon.ico"]
    files += [p for p in (root / "matchready").rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(p for p in files if p.is_file())


def file_hashes(root):
    """{relative posix path: sha256} of everything that ends up in the exe."""
    root = Path(root)
    out = {}
    for p in tracked_files(root):
        try:
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            pass
    return out


def changed_files(old, new):
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


class Updater:
    def __init__(self):
        self.frozen = getattr(sys, "frozen", False)
        self.root, self.baseline = None, {}
        if self.frozen:
            info = self._read_json(Path(getattr(sys, "_MEIPASS", "")) / "matchready" / BUILD_INFO_NAME)
            if info:
                self.root, self.baseline = Path(info["source_dir"]), info["files"]
        else:
            self.root = Path(__file__).resolve().parent.parent
            self.baseline = file_hashes(self.root)

    @staticmethod
    def _read_json(path):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @property
    def venv_python(self):
        return self.root / ".venv" / "Scripts" / "python.exe"

    @property
    def stage(self):
        return self.root / "build" / "stage"

    @property
    def log_path(self):
        return self.root / "build" / "update.log"

    @property
    def enabled(self):
        if not self.root or not (self.root / "matchready").is_dir():
            return False
        return not self.frozen or (self.venv_python.is_file() and (self.root / "build.py").is_file())

    def staged_files(self):
        """Hashes of a finished build waiting in build/stage, if any."""
        info = self._read_json(self.stage / BUILD_INFO_NAME)
        return info["files"] if info and (self.stage / "MatchReady.exe").is_file() else None

    def check(self):
        """-> (state, current_hashes, changed_file_list). state: 'current' | 'changed' | 'staged'."""
        current = file_hashes(self.root)
        changed = changed_files(self.baseline, current)
        if not changed:
            return "current", current, []
        if self.frozen and self.staged_files() == current:
            return "staged", current, changed
        return "changed", current, changed

    def rebuild(self, on_line=lambda line: None):
        """Run build.py (tests + package). Returns True on success. Blocks; call from a thread."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.Popen([str(self.venv_python), "-u", str(self.root / "build.py")], cwd=self.root,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    errors="replace", creationflags=NO_WINDOW)
            for line in proc.stdout:
                log.write(line)
                on_line(line.rstrip())
            return proc.wait() == 0

    def restart(self):
        """Launch the updated app once this process exits. Caller must quit right after."""
        if not self.frozen:
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            subprocess.Popen([str(pyw if pyw.is_file() else exe), str(self.root / "MatchReady.py")],
                             cwd=self.root, creationflags=DETACHED)
            return
        target = Path(sys.executable)
        swap_and_restart(self.stage / "MatchReady.exe", target,
                         extra=f'copy /y "{self.stage / "targets.json"}" "{target.with_name("targets.json")}" >nul 2>&1')


def swap_and_restart(new_exe, target, extra="", delete_source=False):
    """Once this process exits, copy new_exe over target (a running exe is locked) and launch it.
    `extra` is a cmd line run after a successful copy. Caller must quit right after."""
    # PyInstaller one-file runs as a bootloader parent + child; both hold the exe open.
    pids = [os.getpid()]
    try:
        pids.append(psutil.Process().parent().pid)
    except (psutil.Error, AttributeError):
        pass
    wait = "\n".join(
        f':wait{i}\ntasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul && (ping -n 2 127.0.0.1 >nul & goto wait{i})'
        for i, pid in enumerate(pids))
    cleanup = f'del "{new_exe}" >nul 2>&1' if delete_source else ""
    script = f"""@echo off
{wait}
set tries=0
:copy
copy /y "{new_exe}" "{target}" >nul 2>&1 && goto copied
set /a tries+=1
if %tries% geq 20 goto launch
ping -n 2 127.0.0.1 >nul
goto copy
:copied
{extra}
{cleanup}
:launch
rem a fresh one-file app, not a "child" of the exiting one (whose temp folder is already gone)
set PYINSTALLER_RESET_ENVIRONMENT=1
set _MEIPASS2=
start "" "{target}"
del "%~f0"
"""
    path = Path(tempfile.gettempdir()) / f"matchready_update_{os.getpid()}.cmd"
    path.write_text(script, encoding="utf-8")
    _spawn_outside_job(["cmd", "/c", str(path)])


def clean_env():
    """Our environment minus PyInstaller's bootloader state. Inherited _PYI_* variables make a newly
    launched one-file exe think it is the child of this process and load python3xx.dll from this
    process's _MEI temp folder, which is deleted as soon as we exit ("Failed to load Python DLL")."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("_PYI_", "_MEIPASS"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def _spawn_outside_job(args):
    """Start a hidden process that survives our exit. PyInstaller's bootloader puts the app in a
    kill-on-close job object, so a normal child would die with us before it can swap the exe."""
    try:
        subprocess.Popen(args, creationflags=NO_WINDOW | BREAKAWAY, close_fds=True, env=clean_env())
        return
    except OSError:
        pass  # job doesn't allow breakaway: have WMI create it instead (never part of our job)
    cmdline = subprocess.list2cmdline(args).replace("'", "''")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    "$s = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
                    f"Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                    f"-Arguments @{{CommandLine='{cmdline}'; ProcessStartupInformation=$s}}"],
                   creationflags=NO_WINDOW, timeout=30)
