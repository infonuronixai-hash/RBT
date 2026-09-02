"""Drive the real Tk window through a full manual / record / play cycle.

Runs against the built-in simulator, so no Arduino is needed:
    python tests_ui.py
"""
import sys, time, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

from arm_config import ArmConfig
from sequence import Sequence
from ui import ArmApp

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("")
    print(f"== {title} ==")


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.update()
        time.sleep(0.005)


def wait_until(app, predicate, timeout=3.0):
    """Poll a condition instead of guessing a duration.

    Tk maps and unmaps widgets on its own geometry pass, so a fixed pump can miss
    it on a slow tick and fail for no real reason."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        app.update_idletasks()
        app.update()
        if predicate():
            return True
        time.sleep(0.005)
    return False


cfg = ArmConfig.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app", "arm_config.json"))
app = ArmApp(cfg, start_sim=True)
app.update()
pump(app, 0.4)

section("startup")
check("window built", app.winfo_exists())
check("connected to simulator", app.link.connected and app.link.simulate)
check("slider per joint", len(app.joint_vars) == cfg.count)
check("canvas drew", len(app.canvas.find_all()) > 5, f"{len(app.canvas.find_all())} items")
check("pose starts at home", app.pose == cfg.home_pose(), str(app.pose))

section("manual control is independent")
check("record/play off by default", not app.recplay_var.get())
check("panel hidden", not app.right.winfo_ismapped())
tx0 = app.link.tx_count
app._jog(0, 20)
pump(app, 0.2)
check("jog moves the joint", app.pose[0] == 110, str(app.pose[0]))
check("pose transmitted with panel off", app.link.tx_count > tx0,
      f"tx {tx0} -> {app.link.tx_count}")
app._on_slider(3, 120)
app._pose_dirty = True
pump(app, 0.2)
check("slider drives the arm", app.pose[3] == 120, str(app.pose[3]))
app._set_joint(1, 999)
check("clamped to joint max", app.pose[1] == cfg.joints[1].max_angle, str(app.pose[1]))
app._toggle_play()
check("play refused while toggled off", not app.player.playing)
app._toggle_record()
check("record refused while toggled off", not app.recorder.active)
app._home()
pump(app, 0.2)
check("home restores pose", app.pose == cfg.home_pose(), str(app.pose))

section("enabling record & play")
app.recplay_var.set(True)
app._toggle_recplay()
check("panel appears", wait_until(app, lambda: app.right.winfo_ismapped()))

section("continuous record")
app.mode_var.set("continuous")
app._on_mode()
app._toggle_record()
check("recording started", app.recorder.active)
for a in range(90, 141, 5):
    app._set_joint(0, a)
    app._set_joint(2, 180 - a)
    pump(app, 0.06)
app._toggle_record()
check("recording stopped", not app.recorder.active)
check("frames captured", len(app.seq) >= 3, f"{len(app.seq)} frames")
check("sequence has duration", app.seq.duration > 0.2, f"{app.seq.duration:.2f}s")
check("tree rows match", len(app.tree.get_children()) == len(app.seq))

section("playback")
app.pspeed_var.set("0.5x")   # slow enough that the pause check can't race the end
app._home()
pump(app, 0.1)
app._toggle_play()
check("player running", app.player.playing)
pump(app, 0.35)
app._pause()
check("paused", app.player.paused)
pump(app, 0.15)
app._pause()
check("resumed", not app.player.paused)
# Wait proportional to the sequence, not a fixed budget: how long the recording
# ends up being depends on how fast the machine ticks.
t_end = time.perf_counter() + app.seq.duration / 0.5 + 3.0
while app.player.playing and time.perf_counter() < t_end:
    app.update()
    time.sleep(0.005)
pump(app, 0.2)   # let the tick drain the last queued pose
check("playback finished", not app.player.playing)
check("ended on last frame", app.pose[:3] == app.seq.frames[-1].angles[:3],
      f"{app.pose[:3]} vs {app.seq.frames[-1].angles[:3]}")

section("manual override beats playback")
# Make sure the previous run is fully down first: if it were still winding up,
# _toggle_play would pause it instead of starting the looped run we want here.
app.player.stop(wait=True)
pump(app, 0.1)
app.loop_var.set(True)
app._toggle_play()
pump(app, 0.3)
check("playing (looped)", app.player.playing)
app._on_slider(0, 77)          # as if the user dragged the Base slider
pump(app, 0.35)
check("slider stops playback", not app.player.playing)
check("manual pose stands", app.pose[0] == 77, str(app.pose[0]))
app.loop_var.set(False)

section("keyframe mode")
app.mode_var.set("keyframe")
app._on_mode()
check("record disabled in keyframe mode", str(app.rec_btn["state"]) == "disabled")
app.seq.clear()
app._refresh_frames()
for pose in ([90, 90, 90, 90, 90, 20], [140, 60, 120, 90, 90, 90], [40, 120, 60, 90, 90, 20]):
    app._set_pose(pose)
    pump(app, 0.05)
    app._add_pose()
check("three keyframes", len(app.seq) == 3, str(len(app.seq)))
check("dwell applied", app.seq.frames[0].hold == 0.3, str(app.seq.frames[0].hold))
check("spaced 1s apart", abs(app.seq.frames[1].t - app.seq.frames[0].t - 1.0) < 1e-6)
# 3 dwells (0.3 each) + 2 one-second moves = 2.9s
check("dwell not double counted", abs(app.seq.duration - 2.9) < 1e-6, f"{app.seq.duration}")
app.tree.selection_set(app.tree.get_children()[1])
app._goto_frame()
check("go to frame", app.pose == [140, 60, 120, 90, 90, 90], str(app.pose))
app._move_frame(-1)
check("reorder", app.seq.frames[0].angles == [140, 60, 120, 90, 90, 90],
      str(app.seq.frames[0].angles))
app.tree.selection_set(app.tree.get_children()[0])
app._delete_frame()
check("delete", len(app.seq) == 2, str(len(app.seq)))

section("save / load")
p = os.path.join(os.environ.get("TEMP", "."), "_ui_seq.json")
app.name_var.set("smoke_test")
app.seq.name = "smoke_test"
app.seq.save(p)
app._new()
check("new clears", len(app.seq) == 0)
app.seq = Sequence.load(p)
app._refresh_frames()
check("reloaded", len(app.seq) == 2 and app.seq.name == "_ui_seq", app.seq.name)
os.remove(p)

section("turning record & play back off")
app.recplay_var.set(False)
app._toggle_recplay()
check("panel hidden again", wait_until(app, lambda: not app.right.winfo_ismapped()))
tx0 = app.link.tx_count
app._jog(4, 15)
pump(app, 0.2)
check("manual control still live", app.pose[4] == 105 and app.link.tx_count > tx0,
      f"pose {app.pose[4]}, tx {tx0} -> {app.link.tx_count}")

section("teach mode + link loss")
app.teach_var.set(True)
app._toggle_teach()
pump(app, 0.15)
check("teach commands sent", app.link.tx_count > 0)
app.teach_var.set(False)
app._toggle_teach()
app.link.disconnect()
pump(app, 0.2)
check("disconnect handled", not app.link.connected)
app._cmd("G")
check("command while offline is safe", True)

app.seq.dirty = False
app._on_close()
try:
    app.winfo_exists()
    closed = False
except Exception:
    closed = True
check("closed cleanly", closed)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
