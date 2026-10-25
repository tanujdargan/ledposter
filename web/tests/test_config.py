import os
import stat
from pathlib import Path

import pytest
import yaml

from ledposter import config


def test_load_fills_defaults_and_keeps_unknown_keys(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("version: 1\nmatrix: {panel_size: 32, panels: [{col: 0, row: 0}]}\nextra: {keep: me}\n"
                 "playlists: {demo: {items: [{media: a}]}}\n")
    cfg = config.load(str(p))
    assert cfg["matrix"]["panel_size"] == 32 and cfg["matrix"]["pwm_bits"] == 6 and cfg["matrix"]["limit_refresh_rate_hz"] == 130
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
    assert stat.S_IMODE(p.stat().st_mode) == 0o640  # group ledwall: the engine reads it after dropping root
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


def test_clock_overlay_sizes_migrate_to_px(tmp_path):
    p = tmp_path / "config.yaml"
    for old, px in [({"time_size": "xl", "date_size": "l", "date": True}, (46, 16)),
                    ({"time_size": "s", "date_size": "m", "date": False}, (24, 0)),
                    ({"time_size": "l"}, (39, 9)),  # no date keys: the default date
                    ({"date": True}, (32, 9)),
                    ({}, (32, 9)),
                    ({"time_px": 20, "date_px": 0, "time_size": "xl", "date": True}, (20, 0))]:  # px wins
        p.write_text(yaml.safe_dump({"clock_overlay": {"enabled": True, "position": "center", **old}}))
        co = config.load(str(p))["clock_overlay"]
        assert (co["time_px"], co["date_px"]) == px, old
        assert co["enabled"] is True and co["position"] == "center"
        assert not {"time_size", "date_size", "date"} & co.keys()


def test_clock_px_ranges_match_the_engine_fonts():
    h = (Path(__file__).parents[2] / "engine/src/clockfont.h").read_text()
    for group, (lo, hi) in (("kClockTime", config.CLOCK_TIME_PX), ("kClockDate", config.CLOCK_DATE_PX)):
        assert f"constexpr int {group}MinPx = {lo}, {group}MaxPx = {hi};" in h
    for px in [*config.CLOCK_TIME_NAMES.values(), config.DEFAULTS["clock_overlay"]["time_px"]]:
        assert config.CLOCK_TIME_PX[0] <= px <= config.CLOCK_TIME_PX[1]
    for px in [*config.CLOCK_DATE_NAMES.values(), config.DEFAULTS["clock_overlay"]["date_px"]]:
        assert config.CLOCK_DATE_PX[0] <= px <= config.CLOCK_DATE_PX[1]
