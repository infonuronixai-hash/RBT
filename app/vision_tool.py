"""Standalone board-vision prototype: calibrate a camera and watch a chessboard.

No chess engine and no arm - this exists to answer one question before any of
that gets built: does occupancy detection hold up under your actual lighting?

    python app/vision_tool.py              use the default camera
    python app/vision_tool.py --camera 1   pick a camera
    python app/vision_tool.py --demo       synthetic board, no camera needed
    python app/vision_tool.py --image x.png  run against a still photo

Calibrate by clicking the four board corners in the camera view, capture the
empty board, then press WATCH and move pieces around.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import tkinter as tk
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cv2
import numpy as np

import paths
import theme as T
import vision as V
from ui import enable_dpi_awareness
from widgets import GlowButton, NeonSlider, Panel, SegBar, StatusDot, ToggleSwitch

CALIB_FILE = os.path.join(paths.data_dir(), "board_calibration.json")

STATE_COLOURS = {
    V.IDLE: T.TXT_MUTE,
    V.WATCHING: T.CYAN,
    V.IN_MOTION: T.AMBER,
    V.SETTLING: T.VIOLET,
}


# ------------------------------------------------------------ frame sources
class CameraSource:
    def __init__(self, index: int):
        self.cam = V.Camera(index)
        self.name = f"camera {index}"

    def start(self) -> bool:
        return self.cam.start()

    def read(self):
        return self.cam.read()

    def stop(self):
        self.cam.stop()

    @property
    def error(self):
        return self.cam.error

    @property
    def fps(self):
        return self.cam.fps


class ImageSource:
    """A still photo, replayed as every frame. Useful for tuning offline."""

    def __init__(self, path: str):
        self.path = path
        self.frame = None
        self.error = ""
        self.fps = 0.0
        self.name = os.path.basename(path)

    def start(self) -> bool:
        self.frame = cv2.imread(self.path)
        if self.frame is None:
            self.error = f"Could not read {self.path}"
            return False
        return True

    def read(self):
        return None if self.frame is None else self.frame.copy()

    def stop(self):
        pass


class DemoSource:
    """Synthetic board so the whole pipeline can be exercised with no hardware.

    `nudge` drops a hand over the board for a moment, then reveals the new
    position - the same sequence a real player produces.
    """

    START = ["a1", "b1", "c1", "d1", "e1", "f1", "g1", "h1",
             "a2", "b2", "c2", "d2", "e2", "f2", "g2", "h2",
             "a7", "b7", "c7", "d7", "e7", "f7", "g7", "h7",
             "a8", "b8", "c8", "d8", "e8", "f8", "g8", "h8"]

    def __init__(self):
        self.pieces = list(self.START)
        self.error = ""
        self.fps = 30.0
        self.name = "demo board"
        self._hand_until = 0.0
        self._empty = False

    def start(self) -> bool:
        return True

    def render(self, pieces) -> "np.ndarray":
        size, s = 560, 560 // 8
        img = np.zeros((size, size, 3), np.uint8)
        for r in range(8):
            for c in range(8):
                shade = (168, 178, 186) if (r + c) % 2 == 0 else (74, 92, 104)
                cv2.rectangle(img, (c * s, r * s), ((c + 1) * s, (r + 1) * s), shade, -1)
        for name in pieces:
            c, r = V.square_index(name)
            light = V.RANKS.index(name[1]) < 2          # ranks 1-2 are one colour
            colour = (238, 238, 242) if light else (38, 34, 40)
            cx, cy = c * s + s // 2, r * s + s // 2
            cv2.circle(img, (cx, cy), int(s * 0.32), colour, -1)
            cv2.circle(img, (cx, cy), int(s * 0.32), (20, 20, 20), 1, cv2.LINE_AA)
        return img

    def read(self):
        pieces = [] if self._empty else self.pieces
        img = self.render(pieces)
        if time.perf_counter() < self._hand_until:      # a hand sweeps in
            cv2.ellipse(img, (300, 250), (120, 190), 20, 0, 360, (96, 118, 150), -1)
        return img

    def move(self, src: str, dst: str) -> None:
        if src in self.pieces:
            self.pieces.remove(src)
        if dst in self.pieces:
            self.pieces.remove(dst)                     # capture
        self.pieces.append(dst)

    def hand(self, seconds: float = 0.45) -> None:
        self._hand_until = time.perf_counter() + seconds

    def set_empty(self, empty: bool) -> None:
        self._empty = empty

    def stop(self):
        pass


# --------------------------------------------------------------------- app
class VisionTool(tk.Tk):
    def __init__(self, source, calib: V.Calibration):
        enable_dpi_awareness()
        super().__init__()
        T.init_fonts(self)
        self.source = source
        self.calib = calib
        self.watcher = V.BoardWatcher(calib)
        self.empty_ref = None
        self.running = False
        self.corner_mode = False
        self.corners: List[List[float]] = list(calib.corners)
        self.last_changed: List[str] = []
        self._photo_raw = None
        self._photo_board = None
        self._raw_scale = 1.0
        self._raw_offset = (0, 0)
        self._phase = 0.0
        self._frames = 0

        try:
            dpi = float(self.winfo_fpixels("1i"))
        except tk.TclError:
            dpi = 96.0
        self.tk.call("tk", "scaling", dpi / 72.0)
        self.ui_scale = max(1.0, min(3.0, dpi / 96.0))

        self.title("Board Vision - calibration and detection")
        self.configure(bg=T.VOID)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w, h = min(self.px(1180), int(sw * 0.94)), min(self.px(760), int(sh * 0.90))
        self.geometry(f"{w}x{h}+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2-self.px(16))}")
        self.minsize(self.px(880), self.px(600))

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._log(f"Source: {source.name}", "info")
        if self.calib.ready:
            self._log(f"Loaded calibration from {CALIB_FILE}", "ok")
        else:
            self._log("Not calibrated - press SET CORNERS and click the four board "
                      "corners, clockwise from top-left.", "warn")
        self.after(60, self._tick)

    def px(self, n: float) -> int:
        return int(round(n * self.ui_scale))

    # ---------------------------------------------------------------- build
    def _build(self) -> None:
        p = self.px
        head = tk.Frame(self, bg=T.VOID)
        head.pack(fill="x", padx=p(12), pady=(p(10), 0))
        tk.Label(head, text="BOARD VISION", bg=T.VOID, fg=T.TXT,
                 font=T.ui_font(13, "bold")).pack(side="left")
        tk.Label(head, text=T.spaced("occupancy · motion gate · move inference"),
                 bg=T.VOID, fg=T.TXT_MUTE, font=T.ui_font(7)).pack(side="left", padx=p(12))
        self.fps_lbl = tk.Label(head, text="", bg=T.VOID, fg=T.TXT_DIM,
                                font=T.mono_font(10))
        self.fps_lbl.pack(side="right")

        bar = tk.Frame(self, bg=T.PANEL, highlightthickness=1,
                       highlightbackground=T.BORDER)
        bar.pack(fill="x", padx=p(12), pady=(p(10), 0))
        inner = tk.Frame(bar, bg=T.PANEL)
        inner.pack(fill="x", padx=p(12), pady=p(9))
        self.start_btn = GlowButton(inner, "START", self._toggle_run, kind="accent",
                                    scale=self.ui_scale, width=p(104))
        self.start_btn.pack(side="left")
        GlowButton(inner, "LOCK EXPOSURE", self._lock_exposure,
                   scale=self.ui_scale).pack(side="left", padx=p(6))
        self.corner_btn = GlowButton(inner, "SET CORNERS", self._begin_corners,
                                     scale=self.ui_scale)
        self.corner_btn.pack(side="left", padx=(p(12), p(6)))
        GlowButton(inner, "EMPTY BOARD", self._capture_empty,
                   scale=self.ui_scale).pack(side="left")
        GlowButton(inner, "SAVE CAL", self._save_cal,
                   scale=self.ui_scale).pack(side="left", padx=p(6))

        self.watch_var = tk.BooleanVar(value=False)
        ToggleSwitch(inner, "WATCH", self.watch_var, self._toggle_watch,
                     accent=T.GREEN, scale=self.ui_scale).pack(side="right")
        self.state_dot = StatusDot(inner, scale=self.ui_scale)
        self.state_dot.pack(side="right", padx=(p(14), p(5)))
        self.state_lbl = tk.Label(inner, text="IDLE", bg=T.PANEL, fg=T.TXT_MUTE,
                                  font=T.mono_font(10, "bold"), width=12, anchor="e")
        self.state_lbl.pack(side="right")

        # The log is packed first, against the bottom, so it reserves its height;
        # otherwise the expanding body above squeezes it off the window.
        con = Panel(self, "events", scale=self.ui_scale, pad=8)
        con.pack(side="bottom", fill="x", padx=p(12), pady=(p(10), p(12)))
        self.console = tk.Text(con.body, height=6, bg=T.FIELD, fg=T.TXT_DIM,
                               relief="flat", font=T.mono_font(9), wrap="none",
                               padx=p(8), pady=p(5), highlightthickness=0)
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")
        for tag, colour in (("info", T.TXT_DIM), ("ok", T.GREEN), ("err", T.RED),
                            ("warn", T.AMBER), ("move", T.CYAN), ("stamp", T.TXT_MUTE)):
            self.console.tag_configure(tag, foreground=colour)

        body = tk.Frame(self, bg=T.VOID)
        body.pack(fill="both", expand=True, padx=p(12), pady=(p(10), 0))
        body.columnconfigure(0, weight=5)
        body.columnconfigure(1, weight=4)
        body.rowconfigure(0, weight=1)

        cam = Panel(body, "camera view", scale=self.ui_scale, pad=8)
        cam.grid(row=0, column=0, sticky="nsew", padx=(0, p(10)))
        self.raw_canvas = tk.Canvas(cam.body, bg="#000000", highlightthickness=0,
                                    height=p(330), cursor="tcross")
        self.raw_canvas.pack(fill="both", expand=True)
        self.raw_canvas.bind("<Button-1>", self._on_click)

        right = tk.Frame(body, bg=T.VOID)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        brd = Panel(right, "rectified board", scale=self.ui_scale, pad=8)
        brd.grid(row=0, column=0, sticky="nsew")
        self.board_canvas = tk.Canvas(brd.body, bg="#000000", highlightthickness=0,
                                      height=p(330))
        self.board_canvas.pack(fill="both", expand=True)

        tune = Panel(right, "tuning", scale=self.ui_scale)
        tune.grid(row=1, column=0, sticky="ew", pady=(p(10), 0))
        tf = tune.body
        tf.columnconfigure(0, weight=1)
        mrow = tk.Frame(tf, bg=T.PANEL)
        mrow.grid(row=0, column=0, sticky="ew", pady=(0, p(8)))
        mrow.columnconfigure(1, weight=1)
        tk.Label(mrow, text=T.spaced("motion"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(8, "bold")).grid(row=0, column=0, padx=(0, p(8)))
        self.motion_bar = SegBar(mrow, scale=self.ui_scale, accent=T.AMBER, segments=32)
        self.motion_bar.grid(row=0, column=1, sticky="ew")
        self.motion_lbl = tk.Label(mrow, text="0.0", bg=T.PANEL, fg=T.AMBER,
                                   font=T.mono_font(10, "bold"), width=6, anchor="e")
        self.motion_lbl.grid(row=0, column=2, padx=(p(8), 0))

        self.occ_var = tk.DoubleVar(value=self.calib.occupancy_threshold)
        NeonSlider(tf, "occupancy", self.occ_var, 2, 60, unit="",
                   command=lambda v: setattr(self.calib, "occupancy_threshold", float(v)),
                   scale=self.ui_scale, value_w=52).grid(row=1, column=0, sticky="ew")
        self.mot_var = tk.DoubleVar(value=self.calib.motion_threshold)
        NeonSlider(tf, "motion trip", self.mot_var, 0.5, 12, unit="", accent=T.AMBER,
                   command=lambda v: setattr(self.calib, "motion_threshold", float(v)),
                   scale=self.ui_scale, value_w=52).grid(row=2, column=0, sticky="ew")
        self.set_var = tk.DoubleVar(value=self.calib.settle_seconds * 10)
        NeonSlider(tf, "settle", self.set_var, 3, 40, unit="", accent=T.VIOLET,
                   command=lambda v: setattr(self.calib, "settle_seconds", float(v) / 10),
                   scale=self.ui_scale, value_w=52).grid(row=3, column=0, sticky="ew")

        if isinstance(self.source, DemoSource):
            demo = tk.Frame(tf, bg=T.PANEL)
            demo.grid(row=4, column=0, sticky="ew", pady=(p(10), 0))
            for label, mv in (("e2-e4", ("e2", "e4")), ("d7-d5", ("d7", "d5")),
                              ("e4xd5", ("e4", "d5")), ("O-O", None)):
                GlowButton(demo, label, lambda m=mv: self._demo_move(m),
                           scale=self.ui_scale).pack(side="left", padx=(0, p(5)))

    # ----------------------------------------------------------------- log
    def _log(self, msg: str, tag: str = "info") -> None:
        self.console.configure(state="normal")
        self.console.insert("end", time.strftime("%H:%M:%S "), "stamp")
        self.console.insert("end", f"│ {msg}\n", tag)
        if int(self.console.index("end-1c").split(".")[0]) > 400:
            self.console.delete("1.0", "80.end")
        self.console.see("end")
        self.console.configure(state="disabled")

    # ------------------------------------------------------------- actions
    def _toggle_run(self) -> None:
        if self.running:
            self.source.stop()
            self.running = False
            self.watch_var.set(False)
            self.watcher.pause()
            self.start_btn.configure(text="START")
            self._log("Stopped.", "warn")
            return
        if not self.source.start():
            self._log(self.source.error or "Could not start source.", "err")
            return
        self.running = True
        self.start_btn.configure(text="STOP")
        self._log("Running.", "ok")

    def _lock_exposure(self) -> None:
        if not isinstance(self.source, CameraSource):
            self._log("Only applies to a live camera.", "warn")
            return
        self._log(f"Locked: {self.source.cam.lock_exposure()}", "ok")

    def _begin_corners(self) -> None:
        self.corner_mode = True
        self.corners = []
        self.corner_btn.configure(text="CLICK 1/4")
        self._log("Click the four board corners, clockwise from top-left.", "info")

    def _on_click(self, e) -> None:
        if not self.corner_mode:
            return
        sx, sy = self._raw_offset
        if self._raw_scale <= 0:
            return
        fx = (e.x - sx) / self._raw_scale
        fy = (e.y - sy) / self._raw_scale
        self.corners.append([fx, fy])
        n = len(self.corners)
        if n < 4:
            self.corner_btn.configure(text=f"CLICK {n + 1}/4")
            return
        self.corner_mode = False
        self.corner_btn.configure(text="SET CORNERS")
        self.calib.corners = [list(map(float, c)) for c in V.order_corners(self.corners)]
        self.corners = self.calib.corners
        self._log("Calibrated. Now capture the empty board for best accuracy.", "ok")

    def _capture_empty(self) -> None:
        warped = self._warped()
        if warped is None:
            self._log("Need a running, calibrated source first.", "warn")
            return
        if isinstance(self.source, DemoSource):
            self.source.set_empty(True)
            self.update()
            warped = self._warped()
            self.source.set_empty(False)
        self.empty_ref = warped
        self.watcher.set_empty_reference(warped)
        self._log("Empty-board reference captured.", "ok")

    def _toggle_watch(self) -> None:
        if not self.watch_var.get():
            self.watcher.pause()
            self._log("Paused watching.", "warn")
            return
        warped = self._warped()
        if warped is None:
            self.watch_var.set(False)
            self._log("Calibrate and start the source first.", "warn")
            return
        self.watcher.begin_turn(warped)
        self.last_changed = []
        self._log("Watching. Move a piece and take your hand away.", "ok")

    def _save_cal(self) -> None:
        if not self.calib.ready:
            self._log("Nothing to save - not calibrated.", "warn")
            return
        try:
            self.calib.save(CALIB_FILE)
        except OSError as exc:
            self._log(f"Save failed: {exc}", "err")
            return
        self._log(f"Saved {CALIB_FILE}", "ok")

    def _demo_move(self, mv) -> None:
        src = self.source
        src.hand()
        if mv is None:                                   # castling
            src.move("e1", "g1")
            src.move("h1", "f1")
            self._log("demo: white castles kingside", "info")
        else:
            src.move(*mv)
            self._log(f"demo: {mv[0]}-{mv[1]}", "info")

    # ------------------------------------------------------------- helpers
    def _warped(self):
        frame = self.source.read() if self.running else None
        if frame is None or not self.calib.ready:
            return None
        return V.warp_board(frame, self.calib.matrix())

    def _show(self, canvas: tk.Canvas, bgr, attr: str) -> None:
        cw, ch = canvas.winfo_width(), canvas.winfo_height()
        if cw < 10 or ch < 10 or bgr is None:
            return
        h, w = bgr.shape[:2]
        scale = min(cw / w, ch / h)
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        img = cv2.resize(bgr, (dw, dh), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        photo = tk.PhotoImage(data=f"P6\n{dw} {dh}\n255\n".encode() + rgb.tobytes(),
                              master=self)
        setattr(self, attr, photo)                       # hold a reference
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        canvas.delete("frame")
        canvas.create_image(ox, oy, image=photo, anchor="nw", tags="frame")
        if canvas is self.raw_canvas:
            self._raw_scale = scale
            self._raw_offset = (ox, oy)

    def _draw_corner_marks(self) -> None:
        c = self.raw_canvas
        c.delete("mark")
        sx, sy = self._raw_offset
        pts = [(x * self._raw_scale + sx, y * self._raw_scale + sy)
               for x, y in (self.corners or [])]
        for i, (x, y) in enumerate(pts):
            r = self.px(6)
            c.create_oval(x - r, y - r, x + r, y + r, outline=T.CYAN, width=2, tags="mark")
            c.create_text(x + self.px(10), y - self.px(10), text=str(i + 1),
                          fill=T.CYAN, font=T.mono_font(9, "bold"), tags="mark")
        if len(pts) == 4:
            c.create_polygon([v for p in pts for v in p], outline=T.CYAN, fill="",
                             width=2, tags="mark")
        if self.corner_mode:
            c.create_text(self.px(10), self.px(10), anchor="nw",
                          text=f"click corner {len(pts) + 1} of 4", fill=T.AMBER,
                          font=T.ui_font(9, "bold"), tags="mark")

    # ---------------------------------------------------------------- tick
    def _tick(self) -> None:
        try:
            self._frames += 1
            self._phase = 0.5 + 0.5 * np.sin(time.perf_counter() * 3.4)
            frame = self.source.read() if self.running else None

            if frame is not None:
                self._show(self.raw_canvas, frame, "_photo_raw")
                if self.calib.ready:
                    warped = V.warp_board(frame, self.calib.matrix())
                    self.watcher.update(warped)
                    grid, _ = V.occupancy_grid(warped, self.empty_ref,
                                               self.calib.occupancy_threshold)
                    self._show(self.board_canvas,
                               V.draw_overlay(warped, grid, self.last_changed),
                               "_photo_board")
            self._draw_corner_marks()

            for ev in self.watcher.poll():
                if ev.kind == "move":
                    self.last_changed = ev.squares or []
                    self._log(f"MOVE  {ev.move}", "move")
                elif ev.kind == "change":
                    self.last_changed = ev.squares or []
                    self._log(f"CHANGE  {ev.text}  (needs the legal move list)", "warn")
                elif ev.kind == "state":
                    self._log(ev.text, "info")

            st = self.watcher.state
            colour = STATE_COLOURS.get(st, T.TXT_MUTE)
            self.state_lbl.configure(text=st, fg=colour)
            self.state_dot.set(colour, st not in (V.IDLE,))
            self.state_dot.tick(self._phase)
            m = self.watcher.motion
            self.motion_bar.set(min(1.0, m / max(0.5, self.calib.motion_threshold * 2)))
            self.motion_lbl.configure(text=f"{m:4.1f}")
            self.fps_lbl.configure(
                text=f"{self.source.fps:4.1f} fps   {'CAL' if self.calib.ready else 'NO CAL'}")
        except Exception as exc:
            self._log(f"error: {exc}", "err")
        finally:
            self.after(60, self._tick)

    def _on_close(self) -> None:
        try:
            self.source.stop()
        finally:
            self.destroy()


def main() -> int:
    ap = argparse.ArgumentParser(description="Chessboard vision prototype.")
    ap.add_argument("--camera", type=int, default=0, help="camera index")
    ap.add_argument("--image", default="", help="use a still image instead of a camera")
    ap.add_argument("--demo", action="store_true", help="synthetic board, no camera")
    ap.add_argument("--list", action="store_true", help="list working cameras and exit")
    args = ap.parse_args()

    if args.list:
        found = V.Camera.enumerate()
        print("Cameras that opened:", found or "none")
        return 0 if found else 1

    if args.demo:
        source = DemoSource()
    elif args.image:
        source = ImageSource(args.image)
    else:
        source = CameraSource(args.camera)

    calib = V.Calibration(camera_index=args.camera)
    if os.path.exists(CALIB_FILE):
        try:
            calib = V.Calibration.load(CALIB_FILE)
        except (OSError, ValueError):
            pass
    if args.demo and not calib.ready:
        calib.corners = [[0, 0], [559, 0], [559, 559], [0, 559]]   # demo needs none

    VisionTool(source, calib).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
