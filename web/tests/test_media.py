import io

import yaml

from conftest import wait_status
from ledposter import transcode
from ledposter.transcode import TranscodeError


def upload(client, path, **fields):
    with open(path, "rb") as f:
        return client.post("/api/media", files={"file": (path.name, f, "application/octet-stream")},
                           data={k: str(v) for k, v in fields.items()})


def test_upload_transcodes_in_background(authed, gif):
    r = upload(authed, gif, name="Test card", speed="2")
    assert r.status_code == 201
    item = r.json()
    assert item["status"] == "queued" and item["name"] == "Test card" and item["size"] == gif.stat().st_size
    item = wait_status(authed, item["id"])
    assert item["status"] == "ready" and item["error"] is None and item["kind"] == "gif"
    info = item["info"]
    assert (info["w"], info["h"]) == (8, 12) and info["frames"] == 5 and info["filter"] == "smooth"
    assert info["duration_ms"] == 500  # 1 s source at 2x speed
    assert (info["source_w"], info["source_h"]) == (32, 48)
    assert authed.get(f"/api/media/{item['id']}/thumb.png").headers["content-type"] == "image/png"
    assert authed.get(f"/api/media/{item['id']}/source").headers["content-type"] == "image/gif"
    r = authed.get(f"/api/media/{item['id']}/frames?count=3")
    assert r.headers["x-frames"] == "3" and r.headers["x-indices"] == "0,2,4"
    assert len(r.content) == 3 * 8 * 12 * 3
    assert [i["id"] for i in authed.get("/api/media").json()] == [item["id"]]


def test_settings_change_retranscodes(authed, gif):
    mid = upload(authed, gif).json()["id"]
    wait_status(authed, mid)
    r = authed.patch(f"/api/media/{mid}", json={"settings": {"scale": "fill", "filter": "pixel", "speed": 0.5}})
    assert r.status_code == 200 and r.json()["status"] == "queued"
    item = wait_status(authed, mid)
    assert item["status"] == "ready" and item["kind"] == "pixel"
    assert item["settings"]["scale"] == "fill" and item["info"]["duration_ms"] == 2000
    assert authed.patch(f"/api/media/{mid}", json={"settings": {"position": [2, 0]}}).status_code == 422
    assert authed.patch(f"/api/media/{mid}", json={"name": "Renamed"}).json()["name"] == "Renamed"


def test_upload_limit_enforced_while_streaming(authed, monkeypatch):
    monkeypatch.setattr("ledposter.app.MAX_BYTES", 1000)
    body = io.BytesIO(b"\0" * 1500)
    r = authed.post("/api/media", files={"file": ("big.mp4", body, "video/mp4")})
    assert r.status_code == 413 and "200MB" in r.json()["detail"] and "Supported formats" in r.json()["detail"]
    assert authed.get("/api/media").json() == []  # nothing left behind
    assert r.request.headers.get("content-length")  # and the early check uses it too
    r = authed.post("/api/media", files={"file": ("big.mp4", io.BytesIO(b"\0" * 2_000_000), "video/mp4")})
    assert r.status_code == 413


def test_failed_transcode_reports_reason_verbatim(authed, tmp_path):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\0" * 1000)
    item = wait_status(authed, upload(authed, bad).json()["id"])
    assert item["status"] == "failed"
    try:
        transcode.transcode(str(bad), str(tmp_path / "direct"), w=8, h=12)
    except TranscodeError as e:  # same message, verbatim, with ffprobe's path to our copy replaced by the name
        assert item["error"] == str(e).replace(str(bad), "broken.mp4")
    assert "Supported formats: GIF, APNG, PNG, JPG, WebP, MP4, WebM, MOV" in item["error"]
    # the engine never sees it, but a retry is possible after e.g. a fixed ffmpeg
    assert authed.post(f"/api/media/{item['id']}/retry").json()["status"] == "queued"
    assert wait_status(authed, item["id"])["status"] == "failed"
    assert authed.post("/api/mode", json={"mode": "media", "media": item["id"]}).status_code == 409


def test_missing_file_and_bad_field(authed):
    assert authed.post("/api/media", data={"scale": "fit"}, files={"x": ("", io.BytesIO(b""))}).status_code == 400
    r = authed.post("/api/media", files={"file": ("a.gif", io.BytesIO(b"GIF89a"))}, data={"scale": "huge"})
    assert r.status_code == 422
    assert authed.post("/api/media", content=b"nope", headers={"content-type": "text/plain"}).status_code == 400


def test_delete_removes_from_playlists(authed, gif, data):
    mid = upload(authed, gif).json()["id"]
    wait_status(authed, mid)
    authed.put("/api/playlists/demo", json={"items": [{"media": mid, "duration_s": 5}]})
    assert authed.delete(f"/api/media/{mid}").status_code == 200
    assert authed.get(f"/api/media/{mid}").status_code == 404
    assert authed.get("/api/playlists").json()["demo"]["items"] == []
    assert yaml.safe_load((data / "config.yaml").read_text())["playlists"]["demo"]["items"] == []
    assert not (data / "media" / mid).exists()
