#!/usr/bin/env python3
"""Long-running soak test, run on the Pi (stdlib only).

Drives the wall through the REST API like a busy user (every mode from
/api/modes, brightness, game events, the odd power cycle) and once a minute
appends a health sample to a CSV: engine fps/amps/limiter, memory of both
processes, CPU temperature, throttling (under-voltage!), service restarts.

  LEDPOSTER_TOKEN=... python3 tools/soak.py [--url http://127.0.0.1] [--hours 8] [--out /data/logs/soak.csv]
"""
import argparse, csv, json, os, random, subprocess, sys, time, urllib.request

p = argparse.ArgumentParser()
p.add_argument("--url", default="http://127.0.0.1")
p.add_argument("--token", default=os.environ.get("LEDPOSTER_TOKEN"), help="default: $LEDPOSTER_TOKEN (keeps it out of ps)")
p.add_argument("--hours", type=float, default=8)
p.add_argument("--act-s", type=float, default=20, help="seconds between actions")
p.add_argument("--out", default="/data/logs/soak.csv")
a = p.parse_args()
if not a.token:
    p.error("set LEDPOSTER_TOKEN or pass --token")


def api(method, path, body=None):
    req = urllib.request.Request(a.url + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {a.token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def rss_mb(name):  # summed over matching processes
    total = 0
    for pid in sh("pgrep", "-f", name).split():
        try:
            with open(f"/proc/{pid}/status") as f:
                total += next(int(l.split()[1]) for l in f if l.startswith("VmRSS"))
        except (OSError, StopIteration):
            pass
    return round(total / 1024, 1)


def restarts(unit):
    return sh("systemctl", "show", "-p", "NRestarts", "--value", unit) or "-"


def pick_mode():
    """A random mode; media and playlist modes get a real item (bare they are a 404)."""
    m = dict(random.choice(modes))
    if m["mode"] == "media":
        ready = [i["id"] for i in api("GET", "/api/media") if i["status"] == "ready"]
        if not ready:
            return {"mode": "effect", "effect": "plasma"}
        m["media"] = random.choice(ready)
    elif m["mode"] == "playlist":
        names = list(api("GET", "/api/playlists"))
        if not names:
            return {"mode": "effect", "effect": "plasma"}
        m["playlist"] = random.choice(names)
    return m


modes = [m["apply"] for m in api("GET", "/api/modes")]
print(f"{len(modes)} modes; soak for {a.hours}h, samples -> {a.out}", flush=True)
os.makedirs(os.path.dirname(a.out), exist_ok=True)
fields = ["t", "mode", "fps", "amps", "limited", "engine_mb", "web_mb", "temp_c", "throttled",
          "load1", "engine_restarts", "web_restarts", "api_errors", "actions"]
new = not os.path.exists(a.out)
out = open(a.out, "a", newline="")
w = csv.DictWriter(out, fields)
if new:
    w.writeheader()

end = time.time() + a.hours * 3600
next_sample = 0
errors = actions = 0
while time.time() < end:
    try:
        r = random.random()
        if r < 0.70:
            api("POST", "/api/mode", pick_mode())
        elif r < 0.85:
            api("POST", "/api/brightness", {"value": random.randint(10, 100)})
        elif r < 0.97:
            api("POST", "/api/event", {"event": random.choice(["kill", "killstreak", "death", "win"])})
        else:  # power cycle
            api("POST", "/api/power", {"on": False}); time.sleep(3); api("POST", "/api/power", {"on": True})
        actions += 1
    except Exception as e:
        errors += 1
        print(f"{time.strftime('%H:%M:%S')} action failed: {e}", flush=True)
    if time.time() >= next_sample:
        next_sample = time.time() + 60
        try:
            s = api("GET", "/api/status")
            eng = s.get("engine") or s
        except Exception as e:
            errors += 1
            eng = {}
            print(f"{time.strftime('%H:%M:%S')} status failed: {e}", flush=True)
        try:
            temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        except OSError:
            temp = ""
        row = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": eng.get("mode_name"),
               "fps": eng.get("fps"), "amps": eng.get("amps"), "limited": eng.get("limited"),
               "engine_mb": rss_mb("ledengine"), "web_mb": rss_mb("python.*ledposter"),
               "temp_c": temp, "throttled": sh("vcgencmd", "get_throttled").split("=")[-1],
               "load1": os.getloadavg()[0], "engine_restarts": restarts("ledposter-engine"),
               "web_restarts": restarts("ledposter-web"), "api_errors": errors, "actions": actions}
        w.writerow(row); out.flush()
        print(" ".join(f"{k}={v}" for k, v in row.items()), flush=True)
    time.sleep(a.act_s * random.uniform(0.5, 1.5))
print(f"done: {actions} actions, {errors} api errors", flush=True)
sys.exit(1 if errors else 0)
