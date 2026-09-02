"""Where the app reads and writes its files.

Run from the source tree everything stays in the project folder, exactly as before.
Run from the packaged build the exe usually sits in Program Files, which a standard
user account cannot write to, so the config and saved sequences move to
%LOCALAPPDATA%\RoboticArmStudio. Under --onefile there is a second reason: the
bundle is unpacked to a temp folder that is deleted on exit, so anything written
next to the modules would vanish between runs.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import List

APP_NAME = "RoboticArmStudio"

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def is_frozen() -> bool:
    """True when running from the PyInstaller build rather than the source tree."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> str:
    """Read-only files shipped inside the build (the temp unpack dir under --onefile)."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return _APP_DIR


def data_dir() -> str:
    """Writable per-user folder, created on demand."""
    if is_frozen():
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        target = os.path.join(base, APP_NAME)
    else:
        target = _APP_DIR
    os.makedirs(target, exist_ok=True)
    return target


def config_file() -> str:
    return os.path.join(data_dir(), "arm_config.json")


def sequence_dir() -> str:
    if is_frozen():
        return os.path.join(data_dir(), "sequences")
    return os.path.join(os.path.dirname(_APP_DIR), "sequences")


def seed_user_data() -> List[str]:
    """Copy the bundled demo sequences into the user's folder on first run.

    Existing files are never overwritten - a user who edited demo_wave_hello.json
    keeps their version. Returns the names actually copied, for the startup log.
    """
    if not is_frozen():
        return []
    dest = sequence_dir()
    os.makedirs(dest, exist_ok=True)
    src = os.path.join(bundle_dir(), "sequences")
    if not os.path.isdir(src):
        return []
    copied = []
    for name in sorted(os.listdir(src)):
        if not name.endswith(".json"):
            continue
        target = os.path.join(dest, name)
        if os.path.exists(target):
            continue
        try:
            shutil.copyfile(os.path.join(src, name), target)
            copied.append(name)
        except OSError:
            pass            # a missing demo is not worth failing the launch over
    return copied
