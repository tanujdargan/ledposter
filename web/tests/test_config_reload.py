import os
import time

import yaml

from ledposter import config


def write_old(path, text):
    """A finished hand edit: reloads skip a file modified under a second ago (maybe mid-write)."""
    path.write_text(text)
    t = time.time() - 5
    os.utime(path, (t, t))


def test_hand_edit_survives_a_save_from_the_app(authed, data):
    """Editing config.yaml while the app runs, then saving anything from the app,
    keeps the hand edit (the app reloads instead of writing its stale copy back)."""
    client, path = authed, data / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["matrix"]["panels"] = [{"col": 0, "row": 0, "rotate": 180}, {"col": 1, "row": 0, "rotate": 90}]
    write_old(path, yaml.safe_dump(cfg))
    s = client.get("/api/settings").json()
    assert s["matrix"]["panels"][1]["rotate"] == 90
    assert client.put("/api/settings", json=s).status_code == 200
    assert yaml.safe_load(path.read_text())["matrix"]["panels"][1]["rotate"] == 90


def test_unreadable_config_keeps_the_old_one(authed, data):
    path = data / "config.yaml"
    write_old(path, path.read_text())
    os.chmod(path, 0)
    try:
        if os.access(path, os.R_OK):  # root
            return
        assert authed.get("/login").status_code == 200
        assert authed.get("/api/health").status_code == 200
        assert authed.get("/api/auth/state").json()["first_run"] is False
        assert authed.get("/api/settings").status_code == 200  # still logged in: old config
    finally:
        os.chmod(path, 0o640)


def test_bad_yaml_keeps_the_old_one_and_is_not_overwritten(authed, data):
    path = data / "config.yaml"
    write_old(path, "matrix: [\n")
    assert authed.get("/login").status_code == 200
    s = authed.get("/api/settings")
    assert s.status_code == 200
    assert authed.put("/api/settings", json=s.json()).status_code == 200
    assert path.read_text() == "matrix: [\n"  # the broken hand edit is the user's to fix


def test_empty_file_does_not_reopen_first_run(authed, data):
    path = data / "config.yaml"
    for text in ("", "---\n", "# nothing\n"):
        write_old(path, text)
        assert authed.get("/api/auth/state").json()["first_run"] is False
        assert authed.post("/api/auth/setup", json={"password": "x" * 12}).status_code == 409
        assert path.read_text() == text


def test_fresh_file_is_retried_later(authed, data, monkeypatch):
    path = data / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["brightness"]["user"] = 42
    path.write_text(yaml.safe_dump(cfg))  # mtime now: maybe still being written
    assert authed.get("/api/settings").status_code == 200
    assert authed.app.state.cfg["brightness"]["user"] != 42
    t = time.time() - 5
    os.utime(path, (t, t))
    authed.get("/api/health")
    assert authed.app.state.cfg["brightness"]["user"] == 42


def test_null_sections_and_tokens_load(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("version: 1\nweb:\nmatrix:\nplaylists:\nschedule: 3\n")
    cfg = config.load(str(p))
    assert cfg["web"]["port"] == 8080 and cfg["web"]["tokens"] == []
    assert cfg["matrix"]["panel_size"] == 64 and cfg["playlists"] == {} and cfg["schedule"]["on"] == "07:00"
    p.write_text("web:\n  port: 80\n  tokens: null\n")
    assert config.load(str(p))["web"] == {"port": 80, "tokens": [], "admin_password": None}


def test_save_does_not_clobber_a_newer_edit(authed, data):
    """MQTT, save_soon and the shutdown save call st.save() outside any request."""
    st, path = authed.app.state, data / "config.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["brightness"]["user"] = 11
    write_old(path, yaml.safe_dump(cfg))
    st.cfg["brightness"]["user"] = 99
    st.save()
    assert yaml.safe_load(path.read_text())["brightness"]["user"] == 11
    assert st.cfg["brightness"]["user"] == 11  # reloaded: the hand edit wins
    write_old(path, "matrix: [\n")
    st.save()
    assert path.read_text() == "matrix: [\n"


def test_two_saves_in_a_row_both_land(authed, data):
    """A slider's save_soon and a set_mode snapshot taken before either is written: the
    later one must not be mistaken for a stale copy of a hand edit and dropped."""
    st, path = authed.app.state, data / "config.yaml"
    st.cfg["brightness"]["user"] = 21
    a = st.cfg_snapshot()
    st.cfg["brightness"]["user"] = 22
    b = st.cfg_snapshot()
    st.save(a)
    st.save(b)
    assert yaml.safe_load(path.read_text())["brightness"]["user"] == 22
    st.save(a)  # an older snapshot arriving late is ignored
    assert yaml.safe_load(path.read_text())["brightness"]["user"] == 22
