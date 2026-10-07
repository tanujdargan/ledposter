"""Shared fixtures: a fake engine speaking docs/protocol.md on Unix sockets, a temp
LEDPOSTER_DATA with a tiny 8x12 canvas (2x3 panels of 4px) so transcodes are instant,
and a TestClient running the app's lifespan."""
import os
import shutil
import subprocess
import tempfile

import pytest
import yaml
from fastapi.testclient import TestClient

from fake_engine import FakeEngine  # noqa: E402
from fake_net import FakeNet  # noqa: E402
from ledposter.app import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def sockdir():
    d = tempfile.mkdtemp(prefix="lp", dir="/tmp")  # short: Unix socket paths are limited to ~100 chars
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def engine(sockdir):
    e = FakeEngine(sockdir).start()
    yield e
    e.stop()


@pytest.fixture
def data(tmp_path, sockdir):
    cfg = {"version": 1,
           "matrix": {"panel_size": 4, "panels": [{"col": 0, "row": 0}, {"col": 1, "row": 0}, {"col": 1, "row": 1},
                                                   {"col": 0, "row": 1}, {"col": 0, "row": 2}, {"col": 1, "row": 2}]},
           "engine": {"socket": os.path.join(sockdir, "engine.sock"),
                      "preview_socket": os.path.join(sockdir, "frames.sock")}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


@pytest.fixture
def fakenet():
    """Fake NetworkManager; the app never runs nmcli & co on the test machine."""
    return FakeNet()


@pytest.fixture
def client(data, engine, fakenet):
    with TestClient(create_app(str(data), net_runner=fakenet.run)) as c:
        c.fakenet = fakenet
        yield c


@pytest.fixture
def authed(client):
    """Client with the admin password set and a session cookie."""
    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    return client


@pytest.fixture
def gif(tmp_path):
    p = tmp_path / "anim.gif"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=32x48:r=5:d=1", str(p)], check=True)
    return p


def wait_status(client, mid, timeout=20):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        item = client.get(f"/api/media/{mid}").json()
        if item["status"] in ("ready", "failed"):
            return item
        time.sleep(0.05)
    raise AssertionError(f"{mid} still {item['status']}")
