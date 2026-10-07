"""python -m ledposter [--port N] [--host H]: serve the web app. Port defaults to config web.port.

LEDPOSTER_FAKE_NET=1 (or `offline`) swaps nmcli & co for tests/fake_net.py, so the networking
screens can be developed on a laptop without touching its Wi-Fi.
"""
import argparse
import logging
import logging.handlers
import os
import sys

import uvicorn

from . import config
from .app import create_app


def fake_net_runner(value):
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))
    from fake_net import fake_runner_from_env
    return fake_runner_from_env(value)


def setup_file_log(log_dir, max_bytes=2 << 20, backups=3):
    """App log that survives a reboot: journald is volatile on the read-only root (M8), so
    warnings and errors also go to $LEDPOSTER_DATA/logs/web/web.log, size-capped (logs/ itself is
    root's: update.log and maintenance.log). Skipped when the dir is not writable (dev boxes)."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "web.log"), maxBytes=max_bytes, backupCount=backups)
    except OSError:
        return None
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger().addHandler(h)
    return h


def main():
    p = argparse.ArgumentParser(prog="python -m ledposter", description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, help="default: config web.port (80 on the Pi, 8080 for dev)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    setup_file_log(os.path.join(config.data_dir(), "logs", "web"))
    port = a.port or int(config.load()["web"]["port"])
    fake = os.environ.get("LEDPOSTER_FAKE_NET")
    runner = fake_net_runner(fake) if fake else None
    uvicorn.run(create_app(net_runner=runner), host=a.host, port=port, log_level="info", ws_max_size=1 << 20)


if __name__ == "__main__":
    main()
