"""In-app board calibration for the chess camera, with a synthetic camera:
the feed shows uncalibrated, four clicks calibrate it, the file is saved, and
detection then reads a move straight off the board.

    python tests_ui_calib.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import vision as V
from arm_config import ArmConfig
from ui import ArmApp
from vision_tool import DemoSource

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
    return [str(app.canvas.itemcget(i, "text")) for i in app.canvas.find_all()
            if app.canvas.type(i) == "text"]


def images(app):
    return [i for i in app.canvas.find_all() if app.canvas.type(i) == "image"]


class StubCam(DemoSource):
    fps = 30.0

    def stop(self):
        self.stopped = True


cfg = ArmConfig.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app", "arm_config.json"))
app = ArmApp(cfg, start_sim=True)
pump(app, 0.4)
calib_file = app._calib_path()
backup = None
if os.path.exists(calib_file):                  # never clobber a real calibration
    backup = open(calib_file, "rb").read()
    os.remove(calib_file)

app.chess_var.set(True)
app._toggle_chess()
pump(app, 0.3)

print("\n== uncalibrated camera shows its feed ==")
cam = StubCam()
cam.start()
# what _chess_cam_start would set up when no calibration file exists
app._chess_cam = cam
app._chess_calib = V.Calibration()
app._chess_watcher = None
app._chess_calibrating = True
app._chess_corners = []
app.chess_cam_var.set(True)
app.view_source.set("CAMERA")
app._on_view_source()
pump(app, 0.4)
check("raw feed painted without calibration", len(images(app)) == 1, f"{len(images(app))}")
check("asks for corner 1 of 4", any("CORNER 1 OF 4" in t for t in texts(app)), str(texts(app)))
check("click mapping recorded", app._cam_raw_scale > 0 and app._cam_raw is not None)

print("\n== four clicks calibrate ==")
# clicks arrive in canvas pixels; map frame corners through the recorded fit
sc, (ox, oy) = app._cam_raw_scale, app._cam_raw_offset
for fx, fy in ((2, 2), (557, 2), (557, 557), (2, 557)):
    class E:                                         # a minimal Tk event stand-in
        x = int(fx * sc + ox)
        y = int(fy * sc + oy)
    app._view_press(E)
    pump(app, 0.05)
check("calibration complete", not app._chess_calibrating and app._chess_calib.ready)
check("corners ordered top-left first",
      app._chess_calib.corners[0][0] < 10 and app._chess_calib.corners[0][1] < 10,
      str(app._chess_calib.corners))
check("watcher created", app._chess_watcher is not None)
check("calibration saved to disk", os.path.exists(calib_file))
# The in-app calibration keeps the default 1.2 s settle time, which is right for
# a person's hand but slow for a test; the watcher shares this object.
app._chess_calib.settle_seconds = 0.4
app._chess_calib.motion_threshold = 2.0
pump(app, 0.4)
check("raw + rectified now painted", len(images(app)) == 2, f"{len(images(app))}")
check("no more corner prompt", not any("CORNER" in t for t in texts(app)))

print("\n== empty-board reference and detection ==")
cam.set_empty(True)
pump(app, 0.2)
app._chess_empty_board()
cam.set_empty(False)
check("empty reference captured", app._chess_watcher.empty_ref is not None)
# Clearing and refilling the board is itself "motion" to the watcher, so give it
# the settle time to come back to WATCHING before the real move.
pump(app, 1.2)
check("armed", app._chess_cam_armed and app._chess_watcher.state == V.WATCHING,
      app._chess_watcher.state)
cam.hand()
cam.move("e2", "e4")
pump(app, 2.0)
check("move read off the calibrated feed", app.chess.history[:1] == ["e4"],
      str(app.chess.history))

print("\n== recalibrate and stop ==")
app._chess_calibrate()
check("recalibrate re-arms clicking", app._chess_calibrating and app._chess_watcher is None)
app.chess_cam_var.set(False)
app._toggle_chess_cam()
check("stop clears camera and returns to 3D", app._chess_cam is None
      and app.view_source.get() == "3D" and getattr(cam, "stopped", False))

app.seq.dirty = False
app._on_close()
if backup is not None:
    open(calib_file, "wb").write(backup)
elif os.path.exists(calib_file):
    os.remove(calib_file)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
