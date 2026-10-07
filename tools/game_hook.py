#!/usr/bin/env python3
"""Game hook companion: sends named events to the LED wall, which plays a short
overlay animation on top of whatever it shows (spec: Game hook).

    python3 game_hook.py kill
    python3 game_hook.py --url http://ledposter.local --token lp_... death
    some-detector | python3 game_hook.py --stdin      # one event name per line

Events: kill, killstreak, death, win. The token is an API token from the web
app's Settings (also read from $LEDPOSTER_TOKEN); the URL defaults to
$LEDPOSTER_URL or http://ledposter.local. How events are detected in a game is
out of scope: call send() from your own script, or pipe names into --stdin.

Standard library only, so it runs on any PC with Python 3.8+.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

EVENTS = ("kill", "killstreak", "death", "win")
DEFAULT_URL = "http://ledposter.local"


class HookError(Exception):
    """The wall refused the event or could not be reached; the message says why."""


def send(event, url=None, token=None, timeout=3.0):
    """POSTs one event to {url}/api/event. Returns the reply dict ({"ok", "event", "ms"})."""
    url = (url or os.environ.get("LEDPOSTER_URL") or DEFAULT_URL).rstrip("/")
    token = token or os.environ.get("LEDPOSTER_TOKEN")
    if not token:
        raise HookError("no API token: pass --token or set LEDPOSTER_TOKEN (create one in Settings)")
    req = urllib.request.Request(
        f"{url}/api/event", data=json.dumps({"event": event}).encode(), method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read()).get("detail")
        except (ValueError, AttributeError):
            detail = None
        hint = {401: " (check the token; revoked tokens stop working)", 429: " (rate limited)"}.get(e.code, "")
        raise HookError(f"{e.code}: {detail or e.reason}{hint}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise HookError(f"can't reach {url}: {reason}") from None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Send game events to the LED wall.")
    ap.add_argument("event", nargs="?", choices=EVENTS, help="the event to send")
    ap.add_argument("--stdin", action="store_true", help="read event names from stdin, one per line")
    ap.add_argument("--url", help=f"the wall's address (default $LEDPOSTER_URL or {DEFAULT_URL})")
    ap.add_argument("--token", help="API token (default $LEDPOSTER_TOKEN)")
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds to wait for the wall")
    a = ap.parse_args(argv)
    if bool(a.event) == a.stdin:
        ap.error("give one event, or --stdin")
    failed = 0
    for ev in ([a.event] if a.event else (line.strip() for line in sys.stdin)):
        if not ev:
            continue
        try:
            r = send(ev, a.url, a.token, a.timeout)
            print(f"{ev}: ok, {r.get('ms', 0) / 1000:g}s on the wall", flush=True)
        except HookError as e:
            print(f"{ev}: {e}", file=sys.stderr, flush=True)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
