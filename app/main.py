"""Robotic Arm Studio - entry point.

    python app/main.py                 pick the port in the UI
    python app/main.py --port COM5     connect on startup
    python app/main.py --list          list serial ports and exit
    python app/main.py --sim           run against the built-in simulator
    python app/main.py --open FILE     load a sequence on startup
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import paths                                # noqa: E402
from arm_config import ArmConfig            # noqa: E402
from serial_link import SerialLink          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Control an Arduino Uno robotic arm.")
    ap.add_argument("--port", default="", help="serial port, e.g. COM5 or /dev/ttyUSB0")
    ap.add_argument("--sim", action="store_true", help="run without hardware")
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--config", default="", help="path to an alternate config JSON")
    ap.add_argument("--open", dest="open_path", default="", metavar="FILE",
                    help="load a sequence JSON on startup")
    args = ap.parse_args()

    if args.list:
        ports = SerialLink.available_ports()
        if not ports:
            print("No serial ports found. Is the Arduino plugged in and its driver installed?")
            return 1
        print("Serial ports (most likely board first):")
        for dev, label in ports:
            print(f"  {label}")
        return 0

    paths.seed_user_data()      # first run of an installed build has no sequences yet

    cfg = ArmConfig.load(args.config) if args.config else ArmConfig.load()

    try:
        from ui import ArmApp
    except ImportError as exc:
        print(f"Could not start the UI: {exc}")
        print("Tkinter ships with python.org builds; on Linux install python3-tk.")
        return 1

    app = ArmApp(cfg, start_port=args.port or cfg.port, start_sim=args.sim,
                 open_path=args.open_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
