"""Updates for people running a published MatchReady.exe, from the project's GitHub Releases.

The app asks GitHub for the latest release. If its version is newer than ours, it downloads MatchReady.exe,
checks it against the SHA-256 published with the release (refusing anything that doesn't match), then swaps
it in and restarts (see updater.swap_and_restart). Publishing a release: see release.py.

Which repository to check comes from matchready/release.json ({"repo": "owner/name"}); empty = off.
"""
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ASSET = "MatchReady.exe"
API = "https://api.github.com/repos/{repo}/releases/latest"
TIMEOUT = 15


def load_repo():
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "matchready"
    try:
        repo = json.loads((base / "release.json").read_text(encoding="utf-8")).get("repo", "").strip()
    except (OSError, ValueError, AttributeError):
        return ""
    return repo if re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else ""


def parse_version(text):
    """'v1.2.3' / '1.2' -> (1, 2, 3) / (1, 2, 0); None if it isn't a version."""
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (text or "").strip())
    return tuple(int(g or 0) for g in m.groups()) if m else None


def _get(url, accept="application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "MatchReady-updater"})
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def pick_update(release, current_version):
    """From a GitHub 'latest release' JSON object: the update to install, or None if there's nothing newer.
    -> {"version", "url", "size", "sha256", "sha256_url", "page", "notes"}"""
    if not release or release.get("draft") or release.get("prerelease"):
        return None
    new, cur = parse_version(release.get("tag_name")), parse_version(current_version)
    if not new or not cur or new <= cur:
        return None
    assets = {a.get("name"): a for a in release.get("assets", [])}
    exe = assets.get(ASSET)
    if not exe:
        return None
    digest = exe.get("digest") or ""  # GitHub computes this for every uploaded asset
    sha = digest[7:].lower() if digest.startswith("sha256:") else None
    side = assets.get(ASSET + ".sha256")
    if not sha and not side:
        return None  # never install something we can't verify
    return {"version": ".".join(map(str, new)), "url": exe["browser_download_url"], "size": exe.get("size"),
            "sha256": sha, "sha256_url": side["browser_download_url"] if side else None,
            "page": release.get("html_url", ""), "notes": (release.get("body") or "").strip()}


def check(repo, current_version):
    """Ask GitHub for the latest release. Raises OSError/ValueError on network or API trouble."""
    with _get(API.format(repo=repo)) as r:
        return pick_update(json.loads(r.read().decode("utf-8")), current_version)


def updates_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "MatchReady" / "updates"


def download(update, on_progress=lambda frac: None, folder=None):
    """Download and verify the new exe. Returns its path. Raises ValueError if the hash doesn't match."""
    sha = update.get("sha256")
    if not sha:
        with _get(update["sha256_url"], accept="application/octet-stream") as r:
            sha = r.read().decode("utf-8", "replace").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
        raise ValueError("the release has no valid SHA-256")
    folder = Path(folder or updates_dir())
    folder.mkdir(parents=True, exist_ok=True)
    final = folder / f"MatchReady-{update['version']}.exe"
    part = final.with_suffix(".part")
    h, done = hashlib.sha256(), 0
    with _get(update["url"], accept="application/octet-stream") as r, open(part, "wb") as f:
        total = update.get("size") or int(r.headers.get("Content-Length") or 0)
        while chunk := r.read(256 * 1024):
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if total:
                on_progress(min(1.0, done / total))
    if h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise ValueError("the download didn't match the release's SHA-256")
    os.replace(part, final)
    return final


def can_replace(target):
    """Whether we may overwrite the running exe's folder (not e.g. Program Files without admin)."""
    folder = Path(target).parent
    probe = folder / f".matchready_write_test_{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False
