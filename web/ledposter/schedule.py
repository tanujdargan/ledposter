"""On/off schedule and quiet-hours brightness cap: pure functions over the config's
`schedule:` section, plus the tick the app runs every few seconds.

Windows are [start, end) in local wall-clock minutes; start > end spans midnight.
`days` are weekdays (0 = Monday) of the day a window *starts* on, so a Friday
23:00-02:00 window still applies at 01:00 on Saturday.
"""
import datetime as dt


def parse_hm(s):
    h, m = str(s).split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time {s!r}")
    return h * 60 + m


def in_window(now_min, start_min, end_min):
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def _active(start, end, days, now):
    s, e, m = parse_hm(start), parse_hm(end), now.hour * 60 + now.minute
    if not in_window(m, s, e):
        return False
    started_today = s < e or m >= s
    day = now.weekday() if started_today else (now.weekday() - 1) % 7
    return day in (days if days is not None else range(7))


def power_wanted(sch, now):
    """True/False while the schedule is enabled, None when it has no opinion."""
    if not sch.get("enabled"):
        return None
    return _active(sch["on"], sch["off"], sch.get("days"), now)


def quiet_cap(sch, now):
    """Brightness cap (0-100) during quiet hours, else None."""
    q = sch.get("quiet") or {}
    if not q.get("enabled"):
        return None
    return int(q["max_brightness"]) if _active(q["start"], q["end"], None, now) else None


def clamp(user_brightness, cap):
    return min(int(user_brightness), cap) if cap is not None else int(user_brightness)


class Scheduler:
    """Edge-triggered: sends `power` when the schedule's wanted state changes, and
    re-sends brightness when the quiet cap starts or ends. Never fights a manual
    change made between edges. `send(cmd, **args)` returns False if the engine was
    unreachable, in which case the edge is retried on the next tick."""

    def __init__(self, get_schedule, get_user_brightness, send):
        self.get_schedule, self.get_user_brightness, self.send = get_schedule, get_user_brightness, send
        self.prev_power = None
        self.prev_cap = None
        self.cap = None

    async def tick(self, now=None):
        now = now or dt.datetime.now()
        sch = self.get_schedule()
        wanted = power_wanted(sch, now)
        if wanted is None or self.prev_power is None or wanted == self.prev_power:
            self.prev_power = wanted
        elif await self.send("power", on=wanted):
            self.prev_power = wanted
        self.cap = quiet_cap(sch, now)
        if self.cap != self.prev_cap:
            if await self.send("set_brightness", value=clamp(self.get_user_brightness(), self.cap)):
                self.prev_cap = self.cap
