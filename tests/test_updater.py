import json

from matchready.updater import Updater, changed_files, file_hashes


def make_project(tmp_path):
    (tmp_path / "matchready" / "probes").mkdir(parents=True)
    (tmp_path / "matchready" / "gui.py").write_text("a = 1\n")
    (tmp_path / "matchready" / "probes" / "wifi.py").write_text("b = 1\n")
    (tmp_path / "matchready" / "targets.json").write_text("{}")
    (tmp_path / "matchready" / "__pycache__").mkdir()
    (tmp_path / "matchready" / "__pycache__" / "x.py").write_text("ignored")
    (tmp_path / "matchready" / "_build_info.json").write_text("{}")
    (tmp_path / "MatchReady.py").write_text("main()\n")
    return tmp_path


def test_hashes_track_source_only(tmp_path):
    root = make_project(tmp_path)
    h = file_hashes(root)
    assert set(h) == {"MatchReady.py", "matchready/gui.py", "matchready/probes/wifi.py", "matchready/targets.json"}


def test_detects_edit_add_and_delete(tmp_path):
    root = make_project(tmp_path)
    before = file_hashes(root)
    (root / "matchready" / "gui.py").write_text("a = 2\n")
    (root / "matchready" / "new.py").write_text("")
    (root / "matchready" / "probes" / "wifi.py").unlink()
    assert changed_files(before, file_hashes(root)) == [
        "matchready/gui.py", "matchready/new.py", "matchready/probes/wifi.py"]


def test_updater_states(tmp_path):
    root = make_project(tmp_path)
    u = Updater()
    u.frozen, u.root, u.baseline = True, root, file_hashes(root)
    assert u.check()[0] == "current"
    (root / "matchready" / "gui.py").write_text("a = 3\n")
    state, current, changed = u.check()
    assert state == "changed" and changed == ["matchready/gui.py"]
    # A finished build of exactly this code waiting in build/stage -> ready to install.
    stage = root / "build" / "stage"
    stage.mkdir(parents=True)
    (stage / "MatchReady.exe").write_bytes(b"")
    (stage / "_build_info.json").write_text(json.dumps({"files": current}))
    assert u.check()[0] == "staged"
