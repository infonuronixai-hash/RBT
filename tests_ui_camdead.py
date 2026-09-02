"""A camera that opens but never delivers must not leave the view on
"starting camera..." forever: the app has to say why and shut it down.

    python tests_ui_camdead.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import vision as V
from arm_config import ArmConfig
from ui import ArmApp

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.update()
        time.sleep(0.005)


def texts(app):
    return " | ".join(str(app.canvas.itemcget(i, "text")) for i in app.canvas.find_all()
                      if app.canvas.type(i) == "text")


class SilentCam:
    """Opened, thread alive, but no frame yet - a camera that is still warming up."""
    fps = 0.0
    error = ""
    alive = True
    stopped = False

    def read(self):
        return None

    def stop(self):
        self.stopped = True


class DeadCam(SilentCam):
    """Thread gave up: the driver stopped delivering."""
    alive = False
    error = "camera stopped delivering frames"


cfg = ArmConfig.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app", "arm_config.json"))
app = ArmApp(cfg, start_sim=True)
pump(app, 0.4)
app.chess_var.set(True)
app._toggle_chess()
pump(app, 0.3)


def attach(cam):
    app._chess_cam = cam
    app._chess_calib = V.Calibration()
    app._chess_watcher = None
    app._chess_calibrating = True
    app._cam_started_at = time.perf_counter()
    app.chess_cam_var.set(True)
    app.view_source.set("CAMERA")
    app._on_view_source()


print("\n== camera still warming up ==")
attach(SilentCam())
pump(app, 0.4)
check("placeholder names the camera", "starting camera 0" in texts(app), texts(app))
check("camera kept while it warms up", app._chess_cam is not None and app.chess_cam_var.get())
app._cam_started_at -= 4.0                         # pretend 4 s have passed
pump(app, 0.2)
check("after 3 s the placeholder explains", "no frames after" in texts(app)
      and "NEXT CAM" in texts(app), texts(app))
check("no error yet, so still waiting", app._chess_cam is not None)

print("\n== camera thread died ==")
dead = DeadCam()
attach(dead)
pump(app, 0.4)
log = app.console.get("1.0", "end")
check("failure logged with the reason", "Camera failed: camera stopped delivering" in log)
check("hint logged", "NEXT CAM" in log)
check("camera shut down", app._chess_cam is None and dead.stopped)
check("toggle reset", not app.chess_cam_var.get())
check("view back on 3D", app.view_source.get() == "3D")
pump(app, 0.3)
check("3D scene painting again", len(app.canvas.find_withtag("dyn3d")) > 0)

print("\n== a second start while running is a no-op ==")
live = SilentCam()
attach(live)
check("start with a camera already open returns True and keeps it",
      app._chess_cam_start() is True and app._chess_cam is live)
app.chess_cam_var.set(False)
app._toggle_chess_cam()
check("clean stop", app._chess_cam is None and live.stopped)

app.seq.dirty = False
app._on_close()
print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
