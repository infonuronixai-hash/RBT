"""Board vision: watch a chessboard through a fixed camera and report moves.

The approach deliberately avoids recognising pieces. Telling a knight from a
bishop from above is hard and fails often; deciding whether a square is occupied
is easy and robust. Since the game state is known, "e2 emptied and e4 filled"
identifies the move on its own.

Turn detection is a motion gate. Frame-to-frame difference over the board tells
us when a hand is in shot; we never analyse during motion, only once the scene
has been still for a moment. That removes the need for the player to press
anything.

Runs on its own thread and publishes events through a queue, the same shape as
serial_link.SerialLink, so a Tk main loop can drain it from a timer callback.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

try:
    import cv2
    import numpy as np
except ImportError:                                   # vision stays optional
    cv2 = None
    np = None

FILES = "abcdefgh"
RANKS = "12345678"
WARP_PX = 480                    # warped board is WARP_PX square, 60 px per square
MARGIN = 0.18                    # ignore this fraction at each square's edge


# ------------------------------------------------------------------- squares
def square_name(col: int, row: int) -> str:
    """Column/row in warped-image order (row 0 = rank 8) -> 'e4'."""
    return f"{FILES[col]}{RANKS[7 - row]}"


def square_index(name: str) -> Tuple[int, int]:
    return FILES.index(name[0]), 7 - RANKS.index(name[1])


# ----------------------------------------------------------------- geometry
def order_corners(pts) -> "np.ndarray":
    """Sort four clicked points into top-left, top-right, bottom-right, bottom-left."""
    pts = np.asarray(pts, dtype="float32").reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], dtype="float32")


def warp_matrix(corners) -> "np.ndarray":
    dst = np.array([[0, 0], [WARP_PX - 1, 0], [WARP_PX - 1, WARP_PX - 1],
                    [0, WARP_PX - 1]], dtype="float32")
    return cv2.getPerspectiveTransform(order_corners(corners), dst)


def warp_board(frame, matrix) -> "np.ndarray":
    return cv2.warpPerspective(frame, matrix, (WARP_PX, WARP_PX))


def square_patch(warped, col: int, row: int):
    """Centre region of one square, trimmed so neighbouring pieces don't bleed in."""
    s = WARP_PX // 8
    m = int(s * MARGIN)
    y0, y1 = row * s + m, (row + 1) * s - m
    x0, x1 = col * s + m, (col + 1) * s - m
    return warped[y0:y1, x0:x1]


# ---------------------------------------------------------------- occupancy
def occupancy_grid(warped, empty_ref=None, threshold: float = 14.0):
    """8x8 bool grid: is something standing on each square?

    Two ways to decide, in order of reliability:

    * With `empty_ref` (a warp of the board photographed empty during setup) a
      square is occupied when it no longer looks like its own empty self. This
      copes with the checkerboard pattern, which naive thresholding cannot.
    * Without it, fall back to local texture: an empty square is flat, a piece
      introduces edges and brightness variation.
    """
    # Stay in colour. Converting to grey first makes a piece invisible whenever its
    # luminance happens to match the square beneath it - a red piece on a mid-grey
    # dark square is a real case, and in greyscale the two are the same number.
    colour = warped.ndim == 3
    ref = empty_ref if empty_ref is not None else None
    if ref is not None and (ref.ndim == 3) != colour:
        ref = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY) if colour is False else \
            cv2.cvtColor(ref, cv2.COLOR_GRAY2BGR)

    grid = np.zeros((8, 8), dtype=bool)
    score = np.zeros((8, 8), dtype=np.float32)
    for row in range(8):
        for col in range(8):
            patch = square_patch(warped, col, row).astype(np.float32)
            if ref is not None:
                base = square_patch(ref, col, row).astype(np.float32)
                delta = np.abs(patch - base)
                if colour:
                    delta = delta.mean(axis=2)       # distance across B, G and R
                # Median resists a few stray bright pixels; a piece covers most
                # of the trimmed patch, so the median sits inside the piece.
                score[row, col] = float(np.median(delta))
            else:
                # No reference: a bare square is flat, a piece adds variation.
                # Take the loudest channel so a colour difference still registers.
                score[row, col] = float(max(patch[:, :, ch].std() for ch in range(3))
                                        if colour else patch.std())
            grid[row, col] = score[row, col] > threshold
    return grid, score


def grid_diff(before, after) -> List[Tuple[int, int, bool, bool]]:
    """Squares whose occupancy changed, as (col, row, was, now)."""
    out = []
    for row in range(8):
        for col in range(8):
            if before[row, col] != after[row, col]:
                out.append((col, row, bool(before[row, col]), bool(after[row, col])))
    return out


def infer_move(before, after) -> Optional[str]:
    """Turn an occupancy change into 'e2e4', when it is unambiguous.

    Only the plain two-square case is decided here. Captures, castling and en
    passant produce different change counts and need the legal move list to
    resolve, which belongs with the game logic rather than the camera.
    """
    changed = grid_diff(before, after)
    if len(changed) != 2:
        return None
    emptied = [c for c in changed if c[2] and not c[3]]
    filled = [c for c in changed if not c[2] and c[3]]
    if len(emptied) != 1 or len(filled) != 1:
        return None
    src = square_name(emptied[0][0], emptied[0][1])
    dst = square_name(filled[0][0], filled[0][1])
    return f"{src}{dst}"


def describe_change(before, after) -> str:
    """Human-readable summary for the log when a change is not a simple move."""
    changed = grid_diff(before, after)
    if not changed:
        return "no change"
    bits = []
    for col, row, was, now in changed:
        bits.append(f"{square_name(col, row)}{'-' if was and not now else '+'}")
    return f"{len(changed)} squares: " + " ".join(sorted(bits))


# ------------------------------------------------------------- calibration
@dataclass
class Calibration:
    corners: List[List[float]] = field(default_factory=list)   # 4 x (x, y) in frame
    camera_index: int = 0
    frame_size: Tuple[int, int] = (1280, 720)
    occupancy_threshold: float = 14.0
    motion_threshold: float = 2.2
    settle_seconds: float = 1.2

    @property
    def ready(self) -> bool:
        return len(self.corners) == 4

    def matrix(self):
        return warp_matrix(self.corners) if self.ready else None

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"corners": self.corners, "camera_index": self.camera_index,
                       "frame_size": list(self.frame_size),
                       "occupancy_threshold": self.occupancy_threshold,
                       "motion_threshold": self.motion_threshold,
                       "settle_seconds": self.settle_seconds}, fh, indent=2)

    @staticmethod
    def load(path: str) -> "Calibration":
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        c = Calibration()
        c.corners = [list(map(float, p)) for p in d.get("corners", [])]
        c.camera_index = int(d.get("camera_index", 0))
        fs = d.get("frame_size", [1280, 720])
        c.frame_size = (int(fs[0]), int(fs[1]))
        c.occupancy_threshold = float(d.get("occupancy_threshold", 14.0))
        c.motion_threshold = float(d.get("motion_threshold", 2.2))
        c.settle_seconds = float(d.get("settle_seconds", 1.2))
        return c


# ------------------------------------------------------------------ camera
class Camera:
    """Background capture thread that always holds the newest frame.

    Reading on demand from the main thread would hand back buffered stale frames
    and stall the UI, so the thread drains continuously and keeps only the latest.
    """

    def __init__(self, index: int = 0, width: int = 1280, height: int = 720):
        self.index = index
        self.width, self.height = width, height
        self._cap = None
        self.backend = None
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.error = ""
        self.fps = 0.0
        self.frames = 0

    @staticmethod
    def available() -> bool:
        return cv2 is not None

    @staticmethod
    def _backends() -> list:
        """Capture backends to try, best first.

        DSHOW is the usual Windows recommendation, but on OpenCV 5 it refuses to
        capture by index and, when it does open, runs slower than the default
        (measured 22 fps against 31). So try the default first and keep DSHOW as
        a fallback for older builds where it is the one that works.
        """
        if cv2 is None:
            return []
        order = [cv2.CAP_ANY]
        if os.name == "nt":
            order += [getattr(cv2, "CAP_MSMF", cv2.CAP_ANY), cv2.CAP_DSHOW]
        return order

    @staticmethod
    def _open(index: int):
        """First backend that both opens and delivers a frame."""
        for backend in Camera._backends():
            cap = cv2.VideoCapture(index, backend)
            if cap.isOpened() and cap.read()[0]:
                return cap, backend
            cap.release()
        return None, None

    @staticmethod
    def enumerate(limit: int = 5) -> List[int]:
        """Indices that actually open. Windows lists phantom devices, so probe."""
        if cv2 is None:
            return []
        found = []
        for i in range(limit):
            cap, _ = Camera._open(i)
            if cap is not None:
                found.append(i)
                cap.release()
        return found

    def start(self) -> bool:
        if cv2 is None:
            self.error = "opencv-python is not installed. Run: pip install opencv-python"
            return False
        self.stop()
        self._cap, self.backend = Camera._open(self.index)
        if self._cap is None:
            self.error = (f"Could not open camera {self.index}. Check it is not in use by "
                          "another app, and that Settings > Privacy > Camera allows "
                          "desktop apps.")
            return False

        # MJPEG first: many webcams manage only a few fps at 720p in raw YUY2.
        # But a driver that accepts the settings and then never delivers is a real
        # thing, so prove a frame arrives; if not, reopen with plain defaults.
        if not self._apply_and_verify(tuned=True):
            self._cap.release()
            self._cap, self.backend = Camera._open(self.index)
            if self._cap is None or not self._apply_and_verify(tuned=False):
                self.error = (f"Camera {self.index} opened but delivered no frames. "
                              "Close other apps using it, or try NEXT CAM.")
                if self._cap is not None:
                    self._cap.release()
                self._cap = None
                return False

        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="camera", daemon=True)
        self._thread.start()
        self.error = ""
        return True

    def _apply_and_verify(self, tuned: bool, wait: float = 2.0) -> bool:
        """Set capture properties and wait for the first good frame."""
        if tuned:
            try:
                self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            except Exception:
                pass
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            try:
                self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)     # avoid stale frames
            except Exception:
                pass
        deadline = time.perf_counter() + wait
        while time.perf_counter() < deadline:
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None and frame.size:
                with self._lock:
                    self._frame = frame
                self.frames += 1
                return True
            time.sleep(0.05)
        return False

    @property
    def alive(self) -> bool:
        """True while the capture thread is still delivering."""
        return bool(self._thread and self._thread.is_alive())

    def lock_exposure(self) -> str:
        """Pin focus, exposure and white balance.

        Auto-exposure is the single biggest cause of false detections: a hand
        entering shot makes the camera re-meter, every square changes at once and
        the whole board looks like it moved. Not all drivers honour these.
        """
        if self._cap is None:
            return "no camera"
        done = []
        # DSHOW wants 0.25 for manual exposure, MSMF wants 0. Try both and keep
        # whichever the driver accepts.
        for prop, values, label in ((cv2.CAP_PROP_AUTOFOCUS, (0,), "autofocus"),
                                    (cv2.CAP_PROP_AUTO_EXPOSURE, (0.25, 0),
                                     "auto-exposure"),
                                    (cv2.CAP_PROP_AUTO_WB, (0,), "auto-white-balance")):
            for value in values:
                try:
                    if self._cap.set(prop, value):
                        done.append(label)
                        break
                except Exception:
                    pass
        return ", ".join(done) if done else "driver refused all locks"

    def read(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
        self._cap = None
        with self._lock:
            self._frame = None

    def _loop(self) -> None:
        last = time.perf_counter()
        failures = 0
        while not self._stop.is_set():
            if self._cap is None:
                return
            try:
                ok, frame = self._cap.read()
            except Exception as exc:              # cv2.error from a flaky driver
                ok, frame = False, None
                self.error = f"camera read failed: {exc}"
            if not ok:
                # A driver hiccup is common on laptop webcams; give it a moment
                # rather than killing the thread, but give up if it never recovers.
                failures += 1
                if failures >= 150:               # ~3 s of nothing
                    self.error = self.error or "camera stopped delivering frames"
                    with self._lock:
                        self._frame = None        # a stale frame must not look live
                    return
                time.sleep(0.02)
                continue
            failures = 0
            with self._lock:
                self._frame = frame
            self.frames += 1
            now = time.perf_counter()
            dt = now - last
            last = now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)


# ----------------------------------------------------------- turn detection
IDLE = "IDLE"                # not watching (robot's turn, or paused)
WATCHING = "WATCHING"        # board still, waiting for the player
IN_MOTION = "IN_MOTION"      # hand in shot - never analyse now
SETTLING = "SETTLING"        # motion stopped, waiting for it to stay stopped


@dataclass
class VisionEvent:
    kind: str                # move | change | motion | state | error
    text: str = ""
    move: Optional[str] = None
    squares: Optional[list] = None


class BoardWatcher:
    """Motion-gated turn detection over a calibrated board.

    Feed it warped frames; it decides when the scene has settled and compares
    occupancy against the reference taken at the start of the player's turn.
    """

    def __init__(self, calib: Calibration):
        self.calib = calib
        self.state = IDLE
        self.motion = 0.0
        self.reference = None            # occupancy grid at start of turn
        self.empty_ref = None            # warp of the empty board, for occupancy
        self._prev_grey = None
        self._still_since = 0.0
        self._events: "queue.Queue[VisionEvent]" = queue.Queue()
        self.last_grid = None
        self.last_score = None

    # -- lifecycle ---------------------------------------------------------
    def begin_turn(self, warped) -> None:
        """Snapshot the board and start watching for the player's move."""
        self.reference, _ = occupancy_grid(warped, self.empty_ref,
                                           self.calib.occupancy_threshold)
        self._prev_grey = None
        # Clear the motion reading too. With no frame history there is no motion,
        # and a stale value from before the turn would trip the gate immediately.
        self.motion = 0.0
        self.state = WATCHING
        self._emit("state", "watching for your move")

    def pause(self) -> None:
        self.state = IDLE
        self._prev_grey = None

    def set_empty_reference(self, warped) -> None:
        self.empty_ref = warped.copy()

    def poll(self, limit: int = 32) -> List[VisionEvent]:
        out = []
        for _ in range(limit):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def _emit(self, kind: str, text: str = "", move=None, squares=None) -> None:
        self._events.put(VisionEvent(kind, text, move, squares))

    # -- per-frame ---------------------------------------------------------
    def update(self, warped, now: Optional[float] = None) -> None:
        now = time.perf_counter() if now is None else now
        grey = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY) if warped.ndim == 3 else warped
        grey = cv2.GaussianBlur(grey, (5, 5), 0)        # kill sensor noise

        self.motion = (float(np.mean(cv2.absdiff(grey, self._prev_grey)))
                       if self._prev_grey is not None else 0.0)
        self._prev_grey = grey

        if self.state == IDLE:
            return

        moving = self.motion > self.calib.motion_threshold
        if moving:
            if self.state != IN_MOTION:
                self.state = IN_MOTION
                self._emit("state", "movement over the board")
            self._still_since = 0.0
            return

        # not moving
        if self.state == IN_MOTION:
            self.state = SETTLING
            self._still_since = now
            return

        if self.state == SETTLING:
            if now - self._still_since < self.calib.settle_seconds:
                return
            self._resolve(warped)

    def _resolve(self, warped) -> None:
        """The scene has been still long enough - compare and report."""
        grid, score = occupancy_grid(warped, self.empty_ref,
                                     self.calib.occupancy_threshold)
        self.last_grid, self.last_score = grid, score
        if self.reference is None:
            self.reference = grid
            self.state = WATCHING
            return

        changed = grid_diff(self.reference, grid)
        if not changed:
            self.state = WATCHING                      # hover or a piece put back
            self._emit("state", "nothing changed - still your move")
            return

        move = infer_move(self.reference, grid)
        squares = [square_name(c, r) for c, r, _, _ in changed]
        if move:
            self._emit("move", f"detected {move}", move=move, squares=squares)
        else:
            # castling, capture or a misread - the game logic decides using the
            # legal move list; the camera only reports what it saw.
            self._emit("change", describe_change(self.reference, grid), squares=squares)
        self.reference = grid
        self.state = WATCHING


# ------------------------------------------------------------------ helpers
def draw_overlay(warped, grid=None, changed=None):
    """Grid lines, square labels and occupancy dots, for the live view."""
    img = warped.copy()
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    s = WARP_PX // 8
    for i in range(9):
        cv2.line(img, (i * s, 0), (i * s, WARP_PX), (70, 70, 70), 1)
        cv2.line(img, (0, i * s), (WARP_PX, i * s), (70, 70, 70), 1)
    changed = set(changed or [])
    for row in range(8):
        for col in range(8):
            name = square_name(col, row)
            cx, cy = col * s + s // 2, row * s + s // 2
            if grid is not None and grid[row, col]:
                cv2.circle(img, (cx, cy), int(s * 0.3), (255, 210, 0), 2)
            if name in changed:
                cv2.rectangle(img, (col * s + 2, row * s + 2),
                              ((col + 1) * s - 2, (row + 1) * s - 2), (0, 90, 255), 3)
            cv2.putText(img, name, (col * s + 4, (row + 1) * s - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (120, 120, 120), 1, cv2.LINE_AA)
    return img
