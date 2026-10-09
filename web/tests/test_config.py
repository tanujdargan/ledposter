import os
import stat

import pytest
import yaml

from ledposter import config


def test_load_fills_defaults_and_keeps_unknown_keys(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("version: 1\nmatrix: {panel_size: 32, panels: [{col: 0, row: 0}]}\nextra: {keep: me}\n"
                 "playlists: {demo: {items: [{media: a}]}}\n")
    cfg = config.load(str(p))
    assert cfg["matrix"]["panel_size"] == 32 and cfg["matrix"]["pwm_bits"] == 7 and cfg["matrix"]["limit_refresh_rate_hz"] == 100
    assert cfg["extra"] == {"keep": "me"}
    assert cfg["playlists"] == {"demo": {"items": [{"media": "a"}]}}  # not filled with defaults
    assert cfg["web"]["admin_password"] is None and cfg["schedule"]["quiet"]["max_brightness"] == 20
    assert config.canvas_size(cfg) == (32, 32)
    assert config.load(str(tmp_path / "missing.yaml"))["version"] == 1


def test_save_is_atomic_and_private(tmp_path):
    p = tmp_path / "config.yaml"
    cfg = config.load(str(p))
    cfg["version"] = 3
    cfg["web"]["admin_password"] = "scrypt$x"
    config.save(cfg, str(p))
    assert stat.S_IMODE(p.stat().st_mode) == 0o640  # group ledposter: the engine reads it after dropping root
    text = p.read_text()
    assert text.startswith("version: 3\n")  # version preserved, first
    assert yaml.safe_load(text)["web"]["admin_password"] == "scrypt$x"
    assert [f for f in os.listdir(tmp_path) if f != "config.yaml"] == []  # no temp file left behind

    # A failed write never touches the existing file and leaves no temp file.
    cfg["web"]["admin_password"] = object()  # not YAML-serialisable
    with pytest.raises(yaml.YAMLError):
        config.save(cfg, str(p))
    assert p.read_text() == text
    assert [f for f in os.listdir(tmp_path) if f != "config.yaml"] == []


def test_save_creates_dir(tmp_path):
    p = tmp_path / "deeper" / "config.yaml"
    config.save({"matrix": {}}, str(p))
    assert yaml.safe_load(p.read_text()) == {"version": 1, "matrix": {}}
