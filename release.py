"""Publishes a new MatchReady version to GitHub Releases, where installed copies pick it up.

    release.bat 1.1.0 "What changed"

Steps: set __version__ -> build.py (tests + exe) -> SHA-256 file -> GitHub release with MatchReady.exe and
MatchReady.exe.sha256 attached. Uploading needs a GitHub token with write access to the repo's contents, in
the GITHUB_TOKEN (or GH_TOKEN) environment variable, or the GitHub CLI (`gh`) signed in. Without either, it
builds everything and prints how to upload the two files on github.com.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from matchready.release_update import ASSET, load_repo, parse_version  # noqa: E402

INIT = ROOT / "matchready" / "__init__.py"
STAGE = ROOT / "build" / "stage"


def current_version():
    m = re.search(r'__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8-sig"))
    return m.group(1) if m else "0.0.0"


def set_version(v):
    text = INIT.read_text(encoding="utf-8-sig")
    INIT.write_text(re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{v}"', text), encoding="utf-8")


def api(method, url, token, data=None, content_type="application/json"):
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "User-Agent": "MatchReady-release", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GitHub said {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")


def upload_with_token(repo, tag, notes, files, token):
    rel = api("POST", f"https://api.github.com/repos/{repo}/releases", token,
              {"tag_name": tag, "name": f"MatchReady {tag[1:]}", "body": notes})
    upload = rel["upload_url"].split("{")[0]
    for f in files:
        print(f"Uploading {f.name}…", flush=True)
        api("POST", f"{upload}?name={f.name}", token, f.read_bytes(), "application/octet-stream")
    return rel["html_url"]


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    version, notes = argv[0].lstrip("v"), " ".join(argv[1:]).strip() or "Bug fixes and improvements."
    repo = load_repo()
    if not repo:
        print('Set "repo" in matchready\\release.json first, e.g. {"repo": "your-name/MatchReady"}.')
        return 1
    if not parse_version(version):
        print(f"'{version}' isn't a version number like 1.2.0.")
        return 1
    if parse_version(version) <= parse_version(current_version()):
        print(f"{version} must be newer than the current version ({current_version()}).")
        return 1

    old = current_version()
    set_version(version)
    print(f"Version {old} -> {version}. Building…", flush=True)
    if subprocess.run([sys.executable, str(ROOT / "build.py")], cwd=ROOT).returncode != 0:
        set_version(old)
        print(f"Build failed, nothing was published (version put back to {old}).")
        return 1

    exe = STAGE / ASSET
    sha_file = STAGE / (ASSET + ".sha256")
    sha_file.write_text(f"{hashlib.sha256(exe.read_bytes()).hexdigest()}  {ASSET}\n", encoding="ascii")
    files, tag = [exe, sha_file], f"v{version}"

    if (ROOT / ".git").is_dir():  # commit and push the version bump, so the release tag points at it
        subprocess.run(["git", "add", str(INIT)], cwd=ROOT)
        subprocess.run(["git", "commit", "-q", "-m", f"Release {tag}"], cwd=ROOT)
        if subprocess.run(["git", "push", "-q"], cwd=ROOT).returncode != 0:
            print("Couldn't push to GitHub; the release will point at the last pushed commit.")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        url = upload_with_token(repo, tag, notes, files, token)
    elif shutil.which("gh"):
        r = subprocess.run(["gh", "release", "create", tag, *map(str, files), "--repo", repo,
                            "--title", f"MatchReady {version}", "--notes", notes], cwd=ROOT)
        if r.returncode != 0:
            return 1
        url = f"https://github.com/{repo}/releases/tag/{tag}"
    else:
        print(f"\nBuilt {version}, but there's no GITHUB_TOKEN or GitHub CLI to upload it. To publish by hand:\n"
              f"  1. Open https://github.com/{repo}/releases/new\n"
              f"  2. Tag: {tag}   Title: MatchReady {version}\n"
              f"  3. Attach both files from {STAGE}:\n       {ASSET}\n       {ASSET}.sha256\n"
              f"  4. Publish release")
        return 0
    print(f"\nPublished {url}\nInstalled copies will offer the update the next time they check.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
