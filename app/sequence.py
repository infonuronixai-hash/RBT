"""Recording, storage and playback of arm motion sequences.

A sequence is a list of keyframes: a timestamp, one angle per joint, and an
optional dwell. Playback walks that timeline at a fixed tick rate, linearly
interpolating between keyframes, so the same data works for a coarse
pose-to-pose teach-in and for a dense continuous recording.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

FILE_VERSION = 1


# ------------------------------------------------------------------ keyframe
@dataclass
class Keyframe:
    t: float                       # seconds from the start of the sequence
    angles: List[int]
    hold: float = 0.0              # dwell at this pose before moving on
    label: str = ""

    def copy(self) -> "Keyframe":
        return Keyframe(self.t, list(self.angles), self.hold, self.label)

    def to_dict(self) -> dict:
        return {"t": round(self.t, 4), "angles": list(self.angles),
                "hold": round(self.hold, 4), "label": self.label}

    @staticmethod
    def from_dict(d: dict) -> "Keyframe":
        return Keyframe(float(d.get("t", 0.0)),
                        [int(a) for a in d.get("angles", [])],
                        float(d.get("hold", 0.0)),
                        str(d.get("label", "")))


# ------------------------------------------------------------------ sequence
class Sequence:
    def __init__(self, name: str = "Untitled", joint_names: Optional[List[str]] = None,
                 frames: Optional[List[Keyframe]] = None):
        self.name = name
        self.joint_names: List[str] = joint_names or []
        self.frames: List[Keyframe] = frames or []
        self.path: Optional[str] = None
        self.dirty = False

    # ------------------------------------------------------------ properties
    def __len__(self) -> int:
        return len(self.frames)

    @property
    def duration(self) -> float:
        pts = self.timeline()
        return pts[-1][0] if pts else 0.0

    def timeline(self) -> List[tuple]:
        """Expand dwells into a flat [(time, angles), ...] playback timeline."""
        pts: List[tuple] = []
        shift = 0.0
        for f in self.frames:
            t = f.t + shift
            pts.append((t, f.angles))
            if f.hold > 0:
                pts.append((t + f.hold, f.angles))
                shift += f.hold
        return pts

    def time_of(self, index: int) -> float:
        """Playback time at which frame ``index`` begins (dwells included)."""
        shift = 0.0
        for i, f in enumerate(self.frames):
            if i == index:
                return f.t + shift
            shift += f.hold
        return 0.0

    def pose_at(self, t: float) -> Optional[List[int]]:
        """Interpolated pose at time ``t`` seconds."""
        pts = self.timeline()
        if not pts:
            return None
        if t <= pts[0][0]:
            return list(pts[0][1])
        if t >= pts[-1][0]:
            return list(pts[-1][1])
        for i in range(len(pts) - 1):
            t0, a0 = pts[i]
            t1, a1 = pts[i + 1]
            if t0 <= t <= t1:
                span = t1 - t0
                if span <= 1e-6:
                    return list(a1)
                k = (t - t0) / span
                return [int(round(x0 + (x1 - x0) * k)) for x0, x1 in zip(a0, a1)]
        return list(pts[-1][1])

    # --------------------------------------------------------------- editing
    def add(self, angles, t: Optional[float] = None, hold: float = 0.0,
            label: str = "", gap: float = 1.0) -> Keyframe:
        if t is None:
            # ``t`` is pre-dwell time; timeline() adds the accumulated holds, so
            # the previous frame's hold must not be counted here as well.
            last = self.frames[-1] if self.frames else None
            t = 0.0 if last is None else last.t + gap
        kf = Keyframe(float(t), [int(a) for a in angles], float(hold), label)
        self.frames.append(kf)
        self.frames.sort(key=lambda f: f.t)
        self.dirty = True
        return kf

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.frames):
            del self.frames[index]
            self.dirty = True

    def move(self, index: int, delta: int) -> int:
        """Swap a frame with its neighbour, keeping the timestamps in place."""
        j = index + delta
        if not (0 <= index < len(self.frames) and 0 <= j < len(self.frames)):
            return index
        a, b = self.frames[index], self.frames[j]
        a.t, b.t = b.t, a.t
        a.hold, b.hold = b.hold, a.hold
        self.frames[index], self.frames[j] = b, a
        self.dirty = True
        return j

    def clear(self) -> None:
        self.frames = []
        self.dirty = True

    def rescale(self, factor: float) -> None:
        """Stretch or compress the whole sequence in time."""
        if factor <= 0:
            return
        for f in self.frames:
            f.t *= factor
            f.hold *= factor
        self.dirty = True

    def normalise(self) -> None:
        """Shift so the first frame sits at t=0."""
        if not self.frames:
            return
        t0 = self.frames[0].t
        if abs(t0) > 1e-9:
            for f in self.frames:
                f.t -= t0
            self.dirty = True

    def simplify(self, tol: float = 1.5) -> int:
        """Drop frames that linear interpolation already reproduces within ``tol``
        degrees. Shrinks continuous recordings a lot without changing the motion."""
        if len(self.frames) < 3:
            return 0
        keep = [self.frames[0]]
        removed = 0
        i = 1
        while i < len(self.frames) - 1:
            prev, cur, nxt = keep[-1], self.frames[i], self.frames[i + 1]
            span = nxt.t - prev.t
            k = 0.0 if span <= 1e-6 else (cur.t - prev.t) / span
            guess = [p + (n - p) * k for p, n in zip(prev.angles, nxt.angles)]
            if cur.hold > 0 or any(abs(g - c) > tol for g, c in zip(guess, cur.angles)):
                keep.append(cur)
            else:
                removed += 1
            i += 1
        keep.append(self.frames[-1])
        self.frames = keep
        if removed:
            self.dirty = True
        return removed

    # --------------------------------------------------------------- storage
    def to_dict(self) -> dict:
        return {
            "version": FILE_VERSION,
            "name": self.name,
            "joints": self.joint_names,
            "duration": round(self.duration, 3),
            "frames": [f.to_dict() for f in self.frames],
        }

    @staticmethod
    def from_dict(d: dict) -> "Sequence":
        seq = Sequence(str(d.get("name", "Untitled")),
                       [str(j) for j in d.get("joints", [])],
                       [Keyframe.from_dict(f) for f in d.get("frames", [])])
        seq.frames.sort(key=lambda f: f.t)
        return seq

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2)
        self.path = path
        self.name = os.path.splitext(os.path.basename(path))[0]
        self.dirty = False

    @staticmethod
    def load(path: str) -> "Sequence":
        with open(path, "r", encoding="utf-8") as fh:
            seq = Sequence.from_dict(json.load(fh))
        seq.path = path
        seq.name = os.path.splitext(os.path.basename(path))[0]
        seq.dirty = False
        return seq


# ------------------------------------------------------------------ recorder
class Recorder:
    """Samples poses into a Sequence while the user drives the arm."""

    def __init__(self, joint_names: List[str], min_delta: int = 1):
        self.joint_names = joint_names
        self.min_delta = min_delta
        self.seq = Sequence("Recording", list(joint_names))
        self.active = False
        self._t0 = 0.0
        self._last: Optional[List[int]] = None
        self._pending_idle = False

    @property
    def elapsed(self) -> float:
        return (time.perf_counter() - self._t0) if self.active else 0.0

    def start(self, pose) -> None:
        self.seq = Sequence("Recording", list(self.joint_names))
        self.active = True
        self._t0 = time.perf_counter()
        self._last = None
        self.capture(pose, force=True)

    def capture(self, pose, force: bool = False) -> bool:
        """Append a sample. Static poses collapse to a single held frame."""
        if not self.active:
            return False
        pose = [int(a) for a in pose]
        moved = self._last is None or any(
            abs(a - b) >= self.min_delta for a, b in zip(pose, self._last))
        if not force and not moved:
            self._pending_idle = True       # remember the pause, don't store every tick
            return False
        t = self.elapsed
        if self._pending_idle and self.seq.frames:
            # close out the idle stretch so the pause is preserved on playback
            last = self.seq.frames[-1]
            if t - last.t > 0.08:
                self.seq.add(last.angles, t=max(last.t + 0.01, t - 0.05))
            self._pending_idle = False
        self.seq.add(pose, t=t)
        self._last = pose
        return True

    def stop(self, pose=None) -> Sequence:
        if self.active and pose is not None:
            self.capture(pose, force=True)
        self.active = False
        self.seq.normalise()
        return self.seq


# -------------------------------------------------------------------- player
class Player:
    """Plays a Sequence back on a worker thread.

    ``on_pose`` is called from that thread with an interpolated pose; the UI is
    expected to hand it straight to the serial link and mirror it on screen.
    """

    def __init__(self, on_pose: Callable[[List[int]], None],
                 on_progress: Optional[Callable[[float, float, int], None]] = None,
                 on_finish: Optional[Callable[[str], None]] = None,
                 fps: int = 25):
        self.on_pose = on_pose
        self.on_progress = on_progress
        self.on_finish = on_finish
        self.fps = max(5, fps)
        self.speed = 1.0
        self.loop = False
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._seq: Optional[Sequence] = None
        self._cycle = 0

    @property
    def playing(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def start(self, seq: Sequence, speed: float = 1.0, loop: bool = False,
              start_at: float = 0.0) -> bool:
        if self.playing or not seq.frames:
            return False
        self._seq = seq
        self.speed = max(0.05, float(speed))
        self.loop = loop
        self._cycle = 0
        self._stop.clear()
        self._pause.clear()
        self._thread = threading.Thread(target=self._run, args=(start_at,),
                                        name="player", daemon=True)
        self._thread.start()
        return True

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def toggle_pause(self) -> None:
        self.resume() if self.paused else self.pause()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        self._pause.clear()
        if wait and self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    # ------------------------------------------------------------------ core
    def _run(self, start_at: float) -> None:
        seq = self._seq
        assert seq is not None
        total = seq.duration
        dt = 1.0 / self.fps
        reason = "done"
        try:
            while not self._stop.is_set():
                t = max(0.0, start_at)
                start_at = 0.0
                wall0 = time.perf_counter() - (t / self.speed)
                while not self._stop.is_set():
                    if self._pause.is_set():
                        pause_started = time.perf_counter()
                        while self._pause.is_set() and not self._stop.is_set():
                            time.sleep(0.05)
                        wall0 += time.perf_counter() - pause_started
                        continue
                    t = (time.perf_counter() - wall0) * self.speed
                    if t > total:
                        t = total
                    pose = seq.pose_at(t)
                    if pose:
                        self.on_pose(pose)
                    if self.on_progress:
                        self.on_progress(t, total, self._cycle)
                    if t >= total:
                        break
                    time.sleep(dt)
                if self._stop.is_set():
                    reason = "stopped"
                    break
                if not self.loop:
                    break
                self._cycle += 1
        finally:
            self._thread = None
            if self.on_finish:
                self.on_finish(reason)
