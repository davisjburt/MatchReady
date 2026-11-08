import hashlib

import pytest

from matchready import release_update as ru


def release(tag="v1.2.0", digest=True, side=False, **kw):
    data = b"new exe"
    exe = {"name": "MatchReady.exe", "browser_download_url": "https://x/MatchReady.exe", "size": len(data)}
    if digest:
        exe["digest"] = "sha256:" + hashlib.sha256(data).hexdigest().upper()
    assets = [exe]
    if side:
        assets.append({"name": "MatchReady.exe.sha256", "browser_download_url": "https://x/MatchReady.exe.sha256"})
    return {"tag_name": tag, "assets": assets, "html_url": "https://github.com/o/r/releases/tag/" + tag,
            "body": "Smoother charts", **kw}


def test_parse_version():
    assert ru.parse_version("v1.2.3") == (1, 2, 3)
    assert ru.parse_version("1.2") == (1, 2, 0)
    assert ru.parse_version("2") == (2, 0, 0)
    assert ru.parse_version("nightly") is None and ru.parse_version(None) is None


def test_offers_only_newer_verifiable_releases():
    upd = ru.pick_update(release(), "1.0.0")
    assert upd["version"] == "1.2.0" and upd["sha256"] == hashlib.sha256(b"new exe").hexdigest()
    assert upd["notes"] == "Smoother charts"
    assert ru.pick_update(release("v1.0.0"), "1.0.0") is None          # same version
    assert ru.pick_update(release("v0.9.0"), "1.0.0") is None          # older
    assert ru.pick_update(release(prerelease=True), "1.0.0") is None
    assert ru.pick_update(release(draft=True), "1.0.0") is None
    assert ru.pick_update(release(digest=False), "1.0.0") is None      # no way to verify it
    side = ru.pick_update(release(digest=False, side=True), "1.0.0")
    assert side["sha256"] is None and side["sha256_url"].endswith(".sha256")
    assert ru.pick_update({"tag_name": "v9.0.0", "assets": []}, "1.0.0") is None


def test_download_verifies_hash(tmp_path):
    src = tmp_path / "MatchReady.exe"
    src.write_bytes(b"x" * 600_000)
    good = {"version": "1.2.0", "url": src.as_uri(), "size": src.stat().st_size,
            "sha256": hashlib.sha256(src.read_bytes()).hexdigest()}
    steps = []
    out = ru.download(good, steps.append, folder=tmp_path / "updates")
    assert out.read_bytes() == src.read_bytes() and out.name == "MatchReady-1.2.0.exe"
    assert steps and steps[-1] == 1.0
    bad = {**good, "version": "1.3.0", "sha256": "0" * 64}
    with pytest.raises(ValueError):
        ru.download(bad, folder=tmp_path / "updates")
    assert not list((tmp_path / "updates").glob("MatchReady-1.3.0*"))   # nothing half-written left behind


def test_download_reads_sidecar_hash(tmp_path):
    src = tmp_path / "MatchReady.exe"
    src.write_bytes(b"payload")
    sha = tmp_path / "MatchReady.exe.sha256"
    sha.write_text(hashlib.sha256(b"payload").hexdigest() + "  MatchReady.exe\n")
    upd = {"version": "1.2.0", "url": src.as_uri(), "size": 7, "sha256": None, "sha256_url": sha.as_uri()}
    assert ru.download(upd, folder=tmp_path / "u").read_bytes() == b"payload"


def test_repo_config_is_validated(tmp_path, monkeypatch):
    (tmp_path / "matchready").mkdir()
    cfg = tmp_path / "matchready" / "release.json"
    monkeypatch.setattr(ru.sys, "_MEIPASS", str(tmp_path), raising=False)
    cfg.write_text('{"repo": "someone/MatchReady"}')
    assert ru.load_repo() == "someone/MatchReady"
    cfg.write_text('{"repo": "https://evil.example/x"}')
    assert ru.load_repo() == ""
    cfg.write_text('{"repo": ""}')
    assert ru.load_repo() == ""
