import asyncio
import datetime as dt

import pytest

from ledposter import schedule as s


def t(day, hm):  # day: 0 = Mon 2026-10-05
    h, m = hm.split(":")
    return dt.datetime(2026, 10, 5 + day, int(h), int(m))


def test_in_window_same_day_and_overnight():
    assert s.in_window(s.parse_hm("08:00"), s.parse_hm("07:00"), s.parse_hm("23:00"))
    assert not s.in_window(s.parse_hm("23:00"), s.parse_hm("07:00"), s.parse_hm("23:00"))  # end is exclusive
    assert s.in_window(s.parse_hm("07:00"), s.parse_hm("07:00"), s.parse_hm("23:00"))
    for hm in ("22:00", "23:59", "00:00", "03:30", "06:59"):
        assert s.in_window(s.parse_hm(hm), s.parse_hm("22:00"), s.parse_hm("07:00")), hm
    for hm in ("07:00", "12:00", "21:59"):
        assert not s.in_window(s.parse_hm(hm), s.parse_hm("22:00"), s.parse_hm("07:00")), hm
    assert not s.in_window(300, 300, 300)  # zero-length window is never active
    with pytest.raises(ValueError):
        s.parse_hm("24:00")


def test_power_wanted_days_and_overnight():
    sch = {"enabled": True, "on": "18:00", "off": "02:00", "days": [4]}  # Fridays only
    assert s.power_wanted(sch, t(4, "19:00")) is True
    assert s.power_wanted(sch, t(5, "01:00")) is True  # Saturday 01:00 belongs to Friday's window
    assert s.power_wanted(sch, t(5, "19:00")) is False
    assert s.power_wanted(sch, t(4, "01:00")) is False  # Friday 01:00 is Thursday's window
    assert s.power_wanted({**sch, "enabled": False}, t(4, "19:00")) is None
    assert s.power_wanted({**sch, "days": []}, t(4, "19:00")) is False


def test_quiet_cap_and_clamp():
    sch = {"quiet": {"enabled": True, "start": "22:30", "end": "07:00", "max_brightness": 15}}
    assert s.quiet_cap(sch, t(0, "23:00")) == 15
    assert s.quiet_cap(sch, t(0, "06:59")) == 15
    assert s.quiet_cap(sch, t(0, "07:00")) is None
    assert s.quiet_cap({"quiet": {**sch["quiet"], "enabled": False}}, t(0, "23:00")) is None
    assert s.quiet_cap({}, t(0, "23:00")) is None
    assert s.clamp(80, 15) == 15 and s.clamp(10, 15) == 10 and s.clamp(80, None) == 80


def test_scheduler_edges():
    asyncio.run(scheduler_edges())


async def scheduler_edges():
    sent = []
    sch = {"enabled": True, "on": "07:00", "off": "23:00", "days": list(range(7)),
           "quiet": {"enabled": True, "start": "22:00", "end": "07:00", "max_brightness": 20}}
    user = {"b": 80}
    online = {"ok": True}

    async def send(cmd, **args):
        if online["ok"]:
            sent.append((cmd, args))
        return online["ok"]

    sc = s.Scheduler(lambda: sch, lambda: user["b"], send)
    await sc.tick(t(0, "12:00"))
    assert sent == []  # first tick only records state; the user may have set things by hand
    await sc.tick(t(0, "12:05"))
    assert sent == []
    await sc.tick(t(0, "22:01"))  # quiet hours start: cap the brightness, display still on
    assert sent == [("set_brightness", {"value": 20})] and sc.cap == 20
    await sc.tick(t(0, "23:01"))  # off time
    assert sent[-1] == ("power", {"on": False})
    online["ok"] = False
    await sc.tick(t(1, "07:01"))  # engine offline: nothing recorded, edge kept for retry
    online["ok"] = True
    await sc.tick(t(1, "07:02"))
    assert sent[-2:] == [("power", {"on": True}), ("set_brightness", {"value": 80})]  # restored after quiet hours
    n = len(sent)
    await sc.tick(t(1, "12:00"))
    assert len(sent) == n
