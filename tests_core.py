"""Headless checks for the record/play engine and the serial layer."""
import os, sys, time, json
sys.path.insert(0, r"d:\RG\app")

from arm_config import ArmConfig, Joint
from sequence import Sequence, Keyframe, Recorder, Player
from serial_link import SerialLink, parse_pos

fails = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)

print("\n== config ==")
cfg = ArmConfig.load(r"d:\RG\app\arm_config.json")
check("6 joints", cfg.count == 6, str([j.name for j in cfg.joints]))
check("home pose", cfg.home_pose() == [90, 90, 90, 90, 90, 20], str(cfg.home_pose()))
check("clamp low", cfg.joints[1].clamp(-30) == 15)
check("clamp high", cfg.joints[5].clamp(999) == 110)
j = Joint("t", "T", 0, 180, 90, invert=True, trim=5)
check("invert+trim roundtrip", j.from_servo(j.to_servo(120)) == 120,
      f"120 -> {j.to_servo(120)} -> {j.from_servo(j.to_servo(120))}")

print("\n== sequence timing ==")
s = Sequence("t", ["a", "b"])
s.add([0, 0], t=0.0)
s.add([100, 50], t=2.0)
check("duration", abs(s.duration - 2.0) < 1e-6, f"{s.duration}")
check("midpoint interp", s.pose_at(1.0) == [50, 25], str(s.pose_at(1.0)))
check("before start", s.pose_at(-5) == [0, 0])
check("after end", s.pose_at(99) == [100, 50])
s.frames[0].hold = 1.0
check("hold extends duration", abs(s.duration - 3.0) < 1e-6, f"{s.duration}")
check("held pose", s.pose_at(0.5) == [0, 0], str(s.pose_at(0.5)))
check("post-hold interp", s.pose_at(2.0) == [50, 25], str(s.pose_at(2.0)))
s.frames[0].hold = 0.0
s.rescale(2.0)
check("rescale", abs(s.duration - 4.0) < 1e-6, f"{s.duration}")

print("\n== simplify ==")
s2 = Sequence("t", ["a"])
for i in range(21):
    s2.add([i * 5], t=i * 0.1)
before = len(s2)
removed = s2.simplify(1.0)
check("collinear frames collapse", len(s2) == 2 and removed == before - 2,
      f"{before} -> {len(s2)}")
check("motion preserved", s2.pose_at(1.0) == [50], str(s2.pose_at(1.0)))

print("\n== save / load ==")
p = os.path.join(os.environ["TEMP"], "_seq_test.json")
s2.save(p)
back = Sequence.load(p)
check("roundtrip frames", [f.to_dict() for f in back.frames] == [f.to_dict() for f in s2.frames])
check("name from filename", back.name == "_seq_test", back.name)
os.remove(p)

print("\n== recorder ==")
rec = Recorder(["a", "b"])
rec.start([10, 10])
for k in range(1, 6):
    time.sleep(0.03)
    rec.capture([10 + k * 4, 10])
time.sleep(0.05)
rec.capture([10, 10])          # unchanged -> idle, not stored
out = rec.stop([30, 10])
check("recorder captured", len(out) >= 6, f"{len(out)} frames")
check("starts at zero", abs(out.frames[0].t) < 1e-9, f"{out.frames[0].t}")
check("monotonic times", all(b.t >= a.t for a, b in zip(out.frames, out.frames[1:])))
check("recorder inactive after stop", rec.active is False)

print("\n== player ==")
seen, done = [], []
pl = Player(on_pose=seen.append, on_finish=done.append, fps=50)
s3 = Sequence("t", ["a"])
s3.add([0], t=0.0)
s3.add([100], t=0.4)
t0 = time.perf_counter()
pl.start(s3, speed=1.0)
while pl.playing and time.perf_counter() - t0 < 3:
    time.sleep(0.01)
el = time.perf_counter() - t0
check("player ran", len(seen) > 5, f"{len(seen)} poses")
check("ends at final pose", seen[-1] == [100], str(seen[-1]))
check("realtime-ish", 0.3 < el < 0.9, f"{el:.2f}s")
check("finish callback", done == ["done"], str(done))

seen.clear()
t0 = time.perf_counter()
pl.start(s3, speed=4.0)
while pl.playing and time.perf_counter() - t0 < 3:
    time.sleep(0.005)
check("speed 4x is faster", time.perf_counter() - t0 < 0.35, f"{time.perf_counter()-t0:.2f}s")

seen.clear()
pl.start(s3, speed=0.5, loop=True)
time.sleep(0.3)
pl.pause()
time.sleep(0.2)
n = len(seen)
time.sleep(0.2)
check("pause halts output", len(seen) - n <= 1, f"{len(seen)-n} extra")
pl.resume()
time.sleep(0.2)
check("resume continues", len(seen) > n)
pl.stop(wait=True)
check("stop ends thread", not pl.playing)

print("\n== serial (simulator) ==")
link = SerialLink()
check("connect sim", link.connect("", simulate=True, joints=6))
link.send("PING")
lines = link.poll()
check("PONG received", any("PONG" in l for l in lines), str(lines))
link.send_pose([10, 20, 30, 40, 50, 60])
link.poll()
link.send("G")
pos = [parse_pos(l) for l in link.poll()]
pos = [p for p in pos if p]
check("pose echoed back", pos and pos[0] == [10, 20, 30, 40, 50, 60], str(pos))
check("parse_pos rejects junk", parse_pos("OK") is None and parse_pos("POS x") is None)
link.disconnect()
check("disconnect", not link.connected)
check("port listing works", isinstance(SerialLink.available_ports(), list))

print("\n" + ("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}"))
sys.exit(1 if fails else 0)
