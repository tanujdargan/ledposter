#!/usr/bin/env python3
"""Upload the art pack to a wall and build its playlists (stdlib only).

  python3 art/build.py && python3 art/build.py --paintings     # render + download first
  LEDPOSTER_TOKEN=... python3 tools/artpack.py --url http://ledposter.local [--dir art/out]

Reads DIR/manifest.json (written by art/build.py) and uploads every item that is not on the
wall yet (matched by name), with the settings it needs: filter=pixel for the native 128x192
pieces, smooth + fill + crop for the paintings. Waits for the transcodes, then creates or
replaces the playlists:

  Gallery        paintings, 20 s each
  Pixel Scenes   pixel scenes, 18 s each
  Ambient        generative pieces, 30 s each, shuffled
  Everything     all of the above, shuffled
  (personal)     items from art/personal/sources.json go only into the playlist it names

Safe to run again: existing items are reused, playlists are rewritten with the same content.
Create the token in the web app under Settings > API tokens.
"""
import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLISTS = [  # name, categories, seconds per item, shuffle
    ("Gallery", ("painting",), 20, False),
    ("Pixel Scenes", ("scene",), 18, False),
    ("Ambient", ("ambient",), 30, True),
    ("Fun", ("fun",), 18, True),
    ("Claude", ("claude",), 18, False),
    ("Everything", ("painting", "scene", "ambient", "fun", "claude"), None, True),
]
SECONDS = {"painting": 20, "scene": 18, "ambient": 30, "fun": 18, "claude": 18, "personal": 15}


class ApiError(Exception):
    pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__[__doc__.index("\n"):])
    ap.add_argument("--url", required=True, help="the wall, e.g. http://ledposter.local")
    ap.add_argument("--token", default=os.environ.get("LEDPOSTER_TOKEN"),
                    help="API token (default: $LEDPOSTER_TOKEN, which keeps it out of ps)")
    ap.add_argument("--dir", default=os.path.join(HERE, "..", "art", "out"), help="art/build.py output")
    ap.add_argument("--timeout", type=float, default=900, help="seconds to wait for transcodes")
    a = ap.parse_args()
    if not a.token:
        ap.error("set LEDPOSTER_TOKEN or pass --token (create one in Settings > API tokens)")
    base = a.url.rstrip("/")

    def api(method, path, body=None, data=None, ctype="application/json", timeout=30):
        if body is not None:
            data = json.dumps(body).encode()
        req = urllib.request.Request(base + path, method=method, data=data,
                                     headers={"Authorization": f"Bearer {a.token}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("detail")
            except ValueError:
                detail = None
            if e.code == 401:
                raise ApiError("the wall rejected the token (401): create a new one in Settings > API tokens")
            raise ApiError(f"{method} {path}: HTTP {e.code}" + (f": {detail}" if detail else "")) from None
        except urllib.error.URLError as e:
            raise ApiError(f"cannot reach {base}: {e.reason}") from None

    manifest_path = os.path.join(a.dir, "manifest.json")
    try:
        with open(manifest_path) as fh:
            items = json.load(fh)["items"]
    except FileNotFoundError:
        sys.exit(f"error: {manifest_path} not found: run python3 art/build.py (and --paintings) first")
    except (ValueError, KeyError) as e:
        sys.exit(f"error: {manifest_path} is not a valid manifest ({e})")
    missing = [i["file"] for i in items if not os.path.exists(os.path.join(a.dir, i["file"]))]
    if missing:
        sys.exit(f"error: files listed in the manifest are missing (re-run art/build.py): {', '.join(missing)}")

    try:
        run(api, a, items)
    except ApiError as e:
        sys.exit(f"error: {e}")


def multipart(fields, path):
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as fh:
        payload = fh.read()
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                 f'filename="{os.path.basename(path)}"\r\nContent-Type: {ctype}\r\n\r\n'.encode() + payload + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def run(api, a, items):
    library = api("GET", "/api/media")
    by_name = {}
    for m in library:  # prefer a ready copy if a name appears more than once
        if m["name"] not in by_name or m["status"] == "ready":
            by_name[m["name"]] = m
    ids, uploaded, reused = {}, 0, 0
    for it in items:
        have = by_name.get(it["name"])
        if have and have["status"] != "failed":
            ids[it["file"]] = have["id"]
            reused += 1
            continue
        if have:
            print(f"  {it['name']}: previous upload failed ({have.get('error')}); uploading again")
        up = it["upload"]
        fields = {"name": it["name"], "filter": up["filter"], "scale": up["scale"]}
        if up.get("crop"):
            fields["crop"] = ",".join(str(int(v)) for v in up["crop"])
        if up.get("position"):
            fields["position"] = ",".join(str(v) for v in up["position"])
        if up.get("dither"):
            fields["dither"] = "true"
        path = os.path.join(a.dir, it["file"])
        data, ctype = multipart(fields, path)
        m = api("POST", "/api/media", data=data, ctype=ctype, timeout=300)
        ids[it["file"]] = m["id"]
        uploaded += 1
        print(f"  uploaded {it['name']:<52} {os.path.getsize(path) / 1024:>7.0f}K  {up['filter']}/{up['scale']}",
              flush=True)
    print(f"{uploaded} uploaded, {reused} already on the wall")

    # wait for the transcoder (one item at a time on the Pi)
    want = set(ids.values())
    deadline = time.time() + a.timeout
    last = None
    while True:
        status = {m["id"]: m for m in api("GET", "/api/media") if m["id"] in want}
        gone = want - set(status)
        if gone:
            raise ApiError(f"{len(gone)} item(s) disappeared from the library while waiting")
        pending = [m for m in status.values() if m["status"] not in ("ready", "failed")]
        if not pending:
            break
        if len(pending) != last:
            print(f"  waiting for {len(pending)} transcode(s)...", flush=True)
            last = len(pending)
        if time.time() > deadline:
            raise ApiError(f"timed out after {a.timeout:.0f}s with {len(pending)} transcode(s) still running; "
                           "run again later to finish (uploads are not repeated)")
        time.sleep(2)
    failed = [m for m in status.values() if m["status"] == "failed"]
    for m in failed:
        print(f"  FAILED {m['name']}: {m.get('error')}")
    ready = {f: i for f, i in ids.items() if status[i]["status"] == "ready"}
    print(f"{len(ready)} ready, {len(failed)} failed")

    lists = [(name, cats, secs, shuffle) for name, cats, secs, shuffle in PLAYLISTS]
    personal = sorted({it.get("playlist", "Personal") for it in items if it["category"] == "personal"})
    lists += [(name, ("personal",), SECONDS["personal"], False) for name in personal]
    for name, cats, secs, shuffle in lists:
        entries = [{"media": ready[it["file"]], "duration_s": secs or SECONDS[it["category"]]}
                   for it in items if it["category"] in cats and it["file"] in ready
                   and (it["category"] != "personal" or it.get("playlist", "Personal") == name)]
        if not entries:
            print(f"  playlist {name}: nothing to put in it, skipped")
            continue
        api("PUT", "/api/playlists/" + urllib.request.quote(name),
            {"items": entries, "loop": True, "shuffle": shuffle})
        print(f"  playlist {name:<14} {len(entries):>3} items{', shuffled' if shuffle else ''}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
