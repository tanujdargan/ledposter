"""ledposter web app package: FastAPI backend (app.py), transcoder, engine client."""
import os
import subprocess

__version__ = "0.5.0"
try:  # a checkout: show the exact tag/commit in Settings
    _root = os.path.join(os.path.dirname(__file__), "..", "..")
    _desc = subprocess.run(["git", "-C", _root, "describe", "--tags", "--always", "--dirty"],
                           capture_output=True, text=True, timeout=2).stdout.strip()
    if _desc:
        __version__ = f"{__version__} ({_desc})"
except (OSError, subprocess.SubprocessError):
    pass
