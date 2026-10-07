"""python -m ledposter [--port N] [--host H]: serve the web app. Port defaults to config web.port."""
import argparse
import logging

import uvicorn

from . import config
from .app import create_app


def main():
    p = argparse.ArgumentParser(prog="python -m ledposter", description=__doc__)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, help="default: config web.port (80 on the Pi, 8080 for dev)")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    port = a.port or int(config.load()["web"]["port"])
    uvicorn.run(create_app(), host=a.host, port=port, log_level="info", ws_max_size=1 << 20)


if __name__ == "__main__":
    main()
