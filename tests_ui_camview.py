"""The in-app camera view in chess mode, driven by a synthetic board instead of a
webcam: frames flow through the watcher, the Kinetic View switches to CAMERA and
paints raw + rectified images, and switching back restores the 3D scene.

    python tests_ui_camview.py
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


class StubCam(DemoSource):
    """DemoSource already renders a board; give it the Camera surface the UI uses."""
    fps = 30.0

    def stop(self):
        self.stopped = True


cfg = ArmConfig.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app", "arm_config.json"))
app = ArmApp(cfg, start_sim=True)
pump(app, 0.4)
app.chess_var.set(True)
app._toggle_chess()
pump(app, 0.3)

print("\n== camera view ==")
check("source starts on 3D", app.view_source.get() == "3D")
# Draw the camera view directly with no camera attached: choosing CAM in the UI
# would now try to open real hardware, which a test must never do.
app._draw_camera_view(app.canvas, app.canvas.winfo_width(), app.canvas.winfo_height())
check("placeholder when no camera",
      any("camera is off" in str(app.canvas.itemcget(i, "text"))
          for i in app.canvas.find_all() if app.canvas.type(i) == "text"))

# inject a synthetic camera exactly where _chess_cam_start would put a real one
cam = StubCam()
cam.start()
calib = V.Calibration(corners=[[0, 0], [559, 0], [559, 559], [0, 559]],
                      motion_threshold=2.0, settle_seconds=0.4)
app._chess_cam = cam
app._chess_calib = calib
app._chess_calibrating = False
app._chess_watcher = V.BoardWatcher(calib)
app._chess_cam_armed = False
app.chess_cam_var.set(True)
app.view_source.set("CAMERA")
app._on_view_source()
pump(app, 0.4)

imgs = [i for i in app.canvas.find_all() if app.canvas.type(i) == "image"]
check("frames reach the view", app._cam_raw is not None and app._cam_warped is not None)
check("raw and rectified images painted", len(imgs) == 2, f"{len(imgs)} images")
texts = [str(app.canvas.itemcget(i, "text")) for i in app.canvas.find_all()
         if app.canvas.type(i) == "text"]
check("detector state shown", any(t in ("WATCHING", "IN_MOTION", "SETTLING", "IDLE")
                                   for t in texts), str(texts))
check("watcher armed for the human's turn", app._chess_cam_armed
      and app._chess_watcher.state == V.WATCHING, app._chess_watcher.state)
check("fps readout is live, not IDLE", app._fps_text() != "IDLE")

# a move on the synthetic board is read off the camera and played
cam.hand()
cam.move("e2", "e4")
pump(app, 0.5)                      # hand in shot
pump(app, 0.9)                      # hand gone, board settles
check("camera entered the human's move", app.chess.history[:1] == ["e4"],
      str(app.chess.history))

end = time.perf_counter() + 12
while time.perf_counter() < end and app.chess.state != "HUMAN":
    app.update()
    time.sleep(0.005)
check("engine replied", app.chess.state == "HUMAN" and len(app.chess.history) == 2,
      str(app.chess.history))

print("\n== back to 3D ==")
app.view_source.set("3D")
app._on_view_source()
pump(app, 0.3)
check("3D scene repainted", len(app.canvas.find_withtag("dyn3d")) > 0,
      f"{len(app.canvas.find_withtag('dyn3d'))} dyn items")
check("camera images gone", not [i for i in app.canvas.find_all()
                                 if app.canvas.type(i) == "image"])

app.chess_cam_var.set(False)
app._toggle_chess_cam()
check("toggle off stops the camera", getattr(cam, "stopped", False)
      and app._chess_cam is None and app.view_source.get() == "3D")

app.seq.dirty = False
app._on_close()
print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
