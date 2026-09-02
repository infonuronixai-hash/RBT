"""Dashboard interface: manual control, 3D kinetic view, telemetry, record/play."""

from __future__ import annotations

import math
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import List, Optional

import paths
import robot3d as R3
import theme as T
from arm_config import ArmConfig, CONFIG_FILE
from sequence import Player, Recorder, Sequence
from serial_link import SerialLink, parse_pos
from widgets import (ChessBoard, DataTable, GlowButton, IconButton, NeonSlider, Panel,
                     RadialGauge, SegBar, Segmented, StatTile, StatusDot, StepList,
                     ToggleSwitch)

SEQ_DIR = paths.sequence_dir()

PRESETS = (
    ("HOME", None),                                   # filled from config at runtime
    ("READY", [90, 120, 60, 100, 90, 20]),
    ("REACH", [90, 55, 120, 75, 90, 60]),
    ("PARK", [90, 150, 30, 60, 90, 15]),
)


def enable_dpi_awareness() -> None:
    """Opt in to real pixels on Windows.

    Without this a scaled display (200% is common on laptops) hands the process a
    virtualised desktop - 2560x1600 is reported as 1280x800 - so the window is laid
    out too large for the space Windows thinks exists and spills off the screen.
    Must run before the first Tk window is created.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)      # per-monitor v1
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()           # older Windows
    except Exception:
        pass                                                    # not fatal, just blurrier


def process_memory_mb() -> Optional[float]:
    """Resident set size, or None where we cannot ask cheaply."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        # Declare the types: GetCurrentProcess returns a 64-bit pseudo-handle, and
        # without an explicit restype ctypes truncates it to a 32-bit int, so the
        # call silently fails.
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        get_info = ctypes.windll.psapi.GetProcessMemoryInfo
        get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        get_info.restype = wintypes.BOOL

        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        if get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return counters.WorkingSetSize / (1024 * 1024)
    except Exception:
        pass
    return None


class ArmApp(tk.Tk):
    def __init__(self, cfg: Optional[ArmConfig] = None, start_port: str = "",
                 start_sim: bool = False, open_path: str = ""):
        enable_dpi_awareness()
        super().__init__()
        T.init_fonts(self)
        self.ui_scale = self._setup_scaling()
        self.cfg = cfg or ArmConfig.load()
        self.link = SerialLink()
        self.joint_names = [j.name for j in self.cfg.joints]

        self.pose: List[int] = self.cfg.home_pose()
        self.seq = Sequence("Untitled", list(self.joint_names))
        self.recorder = Recorder(self.joint_names)
        self._play_q: "queue.Queue[List[int]]" = queue.Queue()
        self.player = Player(on_pose=self._play_q.put,
                             on_progress=self._on_progress,
                             on_finish=self._on_play_finish)

        self._suppress = False          # guards slider callbacks during programmatic updates
        self._pose_dirty = False
        self._last_send = 0.0
        self._last_sample = 0.0
        self._progress = (0.0, 0.0, 0)
        self._play_msg: Optional[str] = None
        self._ignore_play = False       # discard poses queued by a cancelled run
        self._tick_ms = 25
        self._frame_no = 0
        self._phase = 0.0               # 0..1 animation pulse
        self._started = time.time()
        self._render_ms = 0.0
        self._view_fps = 0.0
        self._last_draw = time.perf_counter()
        self._prev_pose = list(self.pose)
        self._prev_pose_t = time.perf_counter()
        self._joint_speed = [0.0] * self.cfg.count
        self._drag_from: Optional[tuple] = None
        self._scene_key = None          # last drawn 3D state; skip identical redraws
        self._last_render_at = 0.0
        # chess mode: session, taught squares, the arm's current chess sequence,
        # and an optional camera watching the physical board
        self.chess = None
        self.square_map = None
        self._chess_seq = None
        self._chess_selected = None
        self._chess_cam = None
        self._chess_watcher = None
        self._chess_cam_armed = False
        self._cam_raw = None            # latest camera frame and its rectified board
        self._cam_warped = None
        self._photo_cam_raw = None      # PhotoImage refs must be held or Tk drops them
        self._photo_cam_board = None
        self._chess_calib = None        # board calibration for the chess camera
        self._chess_corners = []        # corners clicked so far while calibrating
        self._chess_calibrating = False
        self._cam_raw_scale = 1.0       # how the raw feed was fitted, for click mapping
        self._cam_raw_offset = (0, 0)
        self._axis_key = None

        self.renderer = R3.Renderer()
        self._axis_renderer = R3.Renderer()   # reused so its static layer caches

        self.title("Robotic Arm Studio - Arduino Uno")
        self._size_window(1340, 810)
        self.configure(bg=T.VOID)

        self._build_style()
        self._build_menu()
        self._build_layout()
        self._refresh_ports(select=start_port or self.cfg.port)
        self._refresh_frames()
        self._log("System initialised.", "ok")
        self._log("Select the Arduino port and connect.", "info")

        if open_path:
            try:
                self.seq = Sequence.load(open_path)
                self._refresh_frames()
                self._log(f"Loaded {os.path.basename(open_path)} - {len(self.seq)} frames, "
                          f"{self.seq.duration:.1f}s", "ok")
            except (OSError, ValueError) as exc:
                self._log(f"Could not open {open_path}: {exc}", "err")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Control-s>", lambda e: self._save())
        self.bind("<Control-o>", lambda e: self._open())
        self.bind("<F5>", lambda e: self._toggle_play())
        self.after(self._tick_ms, self._tick)

        if start_sim:
            self.sim_var.set(True)
            self._toggle_connect()
        elif start_port:
            self._toggle_connect()

    # ------------------------------------------------------------------ chrome
    def _setup_scaling(self) -> float:
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except tk.TclError:
            dpi = 96.0
        if not 48.0 <= dpi <= 480.0:
            dpi = 96.0
        self.tk.call("tk", "scaling", dpi / 72.0)   # points -> pixels
        return max(1.0, min(3.0, dpi / 96.0))

    def px(self, n: float) -> int:
        return int(round(n * self.ui_scale))

    def _size_window(self, want_w: int, want_h: int) -> None:
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(self.px(want_w), int(sw * 0.96))
        h = min(self.px(want_h), int(sh * 0.92))
        x = max(0, (sw - w) // 2)
        y = max(0, (sh - h) // 2 - self.px(14))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.minsize(min(self.px(1020), w), min(self.px(640), h))

    def _build_style(self) -> None:
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        p = self.px
        st.configure(".", background=T.PANEL, foreground=T.TXT, fieldbackground=T.FIELD,
                     bordercolor=T.BORDER, lightcolor=T.PANEL, darkcolor=T.PANEL,
                     focuscolor=T.PANEL)
        st.configure("HUD.TCombobox", fieldbackground=T.FIELD, background=T.FIELD,
                     foreground=T.TXT, arrowcolor=T.CYAN, bordercolor=T.BORDER,
                     selectbackground=T.FIELD, selectforeground=T.CYAN, padding=p(4))
        st.map("HUD.TCombobox", fieldbackground=[("readonly", T.FIELD)],
               bordercolor=[("focus", T.BORDER_HI), ("hover", T.BORDER_HI)],
               foreground=[("readonly", T.TXT)])
        self.option_add("*TCombobox*Listbox.background", T.FIELD)
        self.option_add("*TCombobox*Listbox.foreground", T.TXT)
        self.option_add("*TCombobox*Listbox.selectBackground", T.dim(T.CYAN, 0.6))
        self.option_add("*TCombobox*Listbox.selectForeground", T.TXT)
        st.configure("HUD.Treeview", background=T.FIELD, fieldbackground=T.FIELD,
                     foreground=T.TXT_DIM, rowheight=p(22), borderwidth=0,
                     font=T.mono_font(9))
        st.map("HUD.Treeview", background=[("selected", T.dim(T.CYAN, 0.66))],
               foreground=[("selected", T.TXT)])
        st.configure("HUD.Treeview.Heading", background=T.PANEL, foreground=T.TXT_MUTE,
                     borderwidth=0, relief="flat", font=T.ui_font(7, "bold"),
                     padding=(p(3), p(4)))
        st.configure("HUD.Vertical.TScrollbar", background=T.PANEL_HI, troughcolor=T.FIELD,
                     bordercolor=T.PANEL, arrowcolor=T.TXT_MUTE, width=p(9))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        opts = dict(background=T.PANEL, foreground=T.TXT,
                    activebackground=T.dim(T.CYAN, 0.55), activeforeground=T.TXT,
                    borderwidth=0, font=T.ui_font(9))
        m_file = tk.Menu(menubar, tearoff=0, **opts)
        m_file.add_command(label="New sequence", accelerator="Ctrl+N", command=self._new)
        m_file.add_command(label="Open...", accelerator="Ctrl+O", command=self._open)
        m_file.add_command(label="Save", accelerator="Ctrl+S", command=self._save)
        m_file.add_command(label="Save as...", command=self._save_as)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=m_file)

        m_arm = tk.Menu(menubar, tearoff=0, **opts)
        m_arm.add_command(label="Home pose", command=self._home)
        m_arm.add_command(label="Hold (attach servos)", command=lambda: self._cmd("ATT"))
        m_arm.add_command(label="Relax (detach servos)", command=self._relax)
        m_arm.add_separator()
        m_arm.add_command(label="Read position from board", command=lambda: self._cmd("G"))
        m_arm.add_command(label="Push limits to board", command=self._push_limits)
        m_arm.add_command(label="Store settings in EEPROM", command=lambda: self._cmd("SAVE"))
        m_arm.add_separator()
        m_arm.add_command(label="Joint setup...", command=self._joint_setup)
        menubar.add_cascade(label="Arm", menu=m_arm)

        m_help = tk.Menu(menubar, tearoff=0, **opts)
        m_help.add_command(label="Serial protocol reference", command=self._show_protocol)
        m_help.add_command(label="About", command=self._show_about)
        menubar.add_cascade(label="Help", menu=m_help)
        self.config(menu=menubar)
        self.bind("<Control-n>", lambda e: self._new())

    # ------------------------------------------------------------------ layout
    def _build_layout(self) -> None:
        p = self.px
        self._build_header()
        self._build_toolbar()
        self._build_statusbar()             # packed to the bottom before the body

        body = tk.Frame(self, bg=T.VOID)
        body.pack(fill="both", expand=True, padx=p(10), pady=(p(8), 0))
        body.columnconfigure(0, weight=0, minsize=p(286))
        body.columnconfigure(1, weight=1, minsize=p(420))
        body.columnconfigure(2, weight=0, minsize=p(318))
        body.rowconfigure(0, weight=1)
        self._build_left(body)
        self._build_center(body)
        self._build_right(body)
        self._apply_recplay_visibility()
        self._on_mode()

    def _build_header(self) -> None:
        p = self.px
        head = tk.Frame(self, bg=T.VOID)
        head.pack(fill="x", padx=p(12), pady=(p(8), 0))

        mark = tk.Canvas(head, width=p(36), height=p(36), bg=T.VOID, highlightthickness=0)
        mark.pack(side="left")
        c = p(18)
        for i, g in enumerate(T.glow_ramp(T.CYAN, T.VOID, 3)):
            r = p(14) - i * p(2)
            mark.create_oval(c - r, c - r, c + r, c + r, outline=g, width=p(2))
        mark.create_line(c, c - p(9), c, c + p(3), fill=T.CYAN, width=p(2))
        mark.create_line(c, c + p(3), c + p(7), c + p(8), fill=T.TEAL, width=p(2))
        mark.create_oval(c - p(2), c + p(1), c + p(2), c + p(5), fill=T.AMBER, outline="")

        title = tk.Frame(head, bg=T.VOID)
        title.pack(side="left", padx=(p(10), 0))
        tk.Label(title, text="ROBOTIC ARM STUDIO", bg=T.VOID, fg=T.TXT,
                 font=T.ui_font(14, "bold")).pack(anchor="w")
        tk.Label(title, text=T.spaced("6-axis servo control  ·  arduino uno"), bg=T.VOID,
                 fg=T.TXT_MUTE, font=T.ui_font(7)).pack(anchor="w")

        right = tk.Frame(head, bg=T.VOID)
        right.pack(side="right")
        self.clock_lbl = tk.Label(right, text="", bg=T.VOID, fg=T.TXT,
                                  font=T.mono_font(15))
        self.clock_lbl.pack(anchor="e")
        state = tk.Frame(right, bg=T.VOID)
        state.pack(anchor="e")
        self.state_dot = StatusDot(state, scale=self.ui_scale)
        self.state_dot.configure(bg=T.VOID)
        self.state_dot.pack(side="left")
        self.state_lbl = tk.Label(state, text=T.spaced("system idle"), bg=T.VOID,
                                  fg=T.TXT_MUTE, font=T.ui_font(8, "bold"))
        self.state_lbl.pack(side="left")

    def _build_toolbar(self) -> None:
        p = self.px
        bar = tk.Frame(self, bg=T.PANEL, highlightthickness=1,
                       highlightbackground=T.BORDER)
        bar.pack(fill="x", padx=p(10), pady=(p(8), 0))
        inner = tk.Frame(bar, bg=T.PANEL)
        inner.pack(fill="x", padx=p(10), pady=p(7))

        tk.Label(inner, text=T.spaced("port"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(8, "bold")).pack(side="left", padx=(0, p(8)))
        self.port_var = tk.StringVar()
        self.port_cb = ttk.Combobox(inner, textvariable=self.port_var, width=28,
                                    state="readonly", style="HUD.TCombobox",
                                    font=T.mono_font(9))
        self.port_cb.pack(side="left")
        GlowButton(inner, "SCAN", self._refresh_ports, scale=self.ui_scale
                   ).pack(side="left", padx=p(6))
        self.connect_btn = GlowButton(inner, "CONNECT", self._toggle_connect, kind="accent",
                                      scale=self.ui_scale, width=p(106))
        self.connect_btn.pack(side="left")
        self.link_dot = StatusDot(inner, scale=self.ui_scale)
        self.link_dot.pack(side="left", padx=(p(12), p(4)))
        self.conn_lbl = tk.Label(inner, text="OFFLINE", bg=T.PANEL, fg=T.TXT_MUTE,
                                 font=T.mono_font(9, "bold"))
        self.conn_lbl.pack(side="left")

        self.teach_var = tk.BooleanVar(value=False)
        ToggleSwitch(inner, "TEACH MODE", self.teach_var, self._toggle_teach,
                     accent=T.VIOLET, scale=self.ui_scale).pack(side="right")
        self.recplay_var = tk.BooleanVar(value=False)
        ToggleSwitch(inner, "RECORD & PLAY", self.recplay_var, self._toggle_recplay,
                     accent=T.MAGENTA, scale=self.ui_scale).pack(side="right", padx=(0, p(14)))
        self.chess_var = tk.BooleanVar(value=False)
        ToggleSwitch(inner, "CHESS", self.chess_var, self._toggle_chess,
                     accent=T.GREEN, scale=self.ui_scale).pack(side="right", padx=(0, p(14)))
        self.sim_var = tk.BooleanVar(value=False)
        ToggleSwitch(inner, "SIMULATOR", self.sim_var, accent=T.AMBER,
                     scale=self.ui_scale).pack(side="right", padx=(0, p(14)))

    # -------------------------------------------------------------- left column
    def _build_left(self, parent) -> None:
        p = self.px
        left = tk.Frame(parent, bg=T.VOID)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, p(8)))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)

        jp = Panel(left, "joint control", scale=self.ui_scale, pad=8)
        jp.grid(row=0, column=0, sticky="ew")
        jf = jp.body
        jf.columnconfigure(0, weight=1)

        self.joint_vars: List[tk.DoubleVar] = []
        self.joint_scales: List[NeonSlider] = []
        for i, j in enumerate(self.cfg.joints):
            var = tk.DoubleVar(value=self.pose[i])
            self.joint_vars.append(var)
            sl = NeonSlider(jf, j.label, var, j.min_angle, j.max_angle,
                            command=lambda v, idx=i: self._on_slider(idx, v),
                            accent=T.JOINT_COLOURS[i % len(T.JOINT_COLOURS)],
                            scale=self.ui_scale, label_w=82, value_w=48, height=28)
            sl.grid(row=i, column=0, sticky="ew")
            self.joint_scales.append(sl)

        tk.Frame(jf, bg=T.blend(T.BORDER, T.PANEL, 0.3), height=1).grid(
            row=6, column=0, sticky="ew", pady=p(8))

        self.speed_var = tk.DoubleVar(value=self.cfg.speed_dps)
        NeonSlider(jf, "slew rate", self.speed_var, 10, 400, command=self._on_speed,
                   accent=T.TEAL, scale=self.ui_scale, unit=" d/s", label_w=82,
                   value_w=62, height=30).grid(row=7, column=0, sticky="ew")

        jog = tk.Frame(jf, bg=T.PANEL)
        jog.grid(row=8, column=0, sticky="ew", pady=(p(6), 0))
        tk.Label(jog, text=T.spaced("jog step"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left")
        self.jog_var = tk.IntVar(value=5)
        self.jog_choice = tk.StringVar(value="5")
        Segmented(jog, [("1", "1°"), ("2", "2°"), ("5", "5°"), ("10", "10°")],
                  self.jog_choice, command=self._on_jog_step, scale=self.ui_scale
                  ).pack(side="right")

        presets = tk.Frame(jf, bg=T.PANEL)
        presets.grid(row=9, column=0, sticky="ew", pady=(p(8), 0))
        for name, _pose in PRESETS:
            GlowButton(presets, name, lambda n=name: self._apply_preset(n),
                       scale=self.ui_scale, pad=9).pack(side="left", padx=(0, p(4)))

        qa = Panel(left, "quick actions", scale=self.ui_scale, pad=8)
        qa.grid(row=1, column=0, sticky="ew", pady=(p(8), 0))
        qf = qa.body
        for i, (glyph, label, cmd, accent) in enumerate((
                ("⌂", "HOME", self._home, T.CYAN),
                ("✋", "HOLD", lambda: self._cmd("ATT"), T.CYAN),
                ("⏻", "RELAX", self._relax, T.AMBER),
                ("⤓", "READ POS", lambda: self._cmd("G"), T.CYAN))):
            qf.columnconfigure(i, weight=1)
            IconButton(qf, glyph, label, cmd, accent=accent, scale=self.ui_scale, size=52
                       ).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else p(5), 0))

        gp = Panel(left, "gripper control", scale=self.ui_scale, pad=8)
        gp.grid(row=2, column=0, sticky="nsew", pady=(p(8), 0))
        gf = gp.body
        gf.columnconfigure(1, weight=1)
        self.grip_gauge = RadialGauge(gf, scale=self.ui_scale, accent=T.AMBER,
                                      size=72, caption="OPEN")
        self.grip_gauge.grid(row=0, column=0, rowspan=2, sticky="w")
        btns = tk.Frame(gf, bg=T.PANEL)
        btns.grid(row=0, column=1, sticky="e")
        GlowButton(btns, "−", lambda: self._jog(5, -self._jog_step()),
                   scale=self.ui_scale, width=p(38)).pack(side="left", padx=(0, p(5)))
        GlowButton(btns, "+", lambda: self._jog(5, self._jog_step()),
                   scale=self.ui_scale, width=p(38)).pack(side="left")
        self.grip_slider = NeonSlider(gf, "open", self.joint_vars[5],
                                      self.cfg.joints[5].min_angle,
                                      self.cfg.joints[5].max_angle,
                                      command=lambda v: self._on_slider(5, v),
                                      accent=T.AMBER, scale=self.ui_scale,
                                      label_w=42, value_w=46, height=28)
        self.grip_slider.grid(row=1, column=1, sticky="ew", pady=(p(6), 0))

    # ------------------------------------------------------------ centre column
    def _build_center(self, parent) -> None:
        p = self.px
        mid = self._mid = tk.Frame(parent, bg=T.VOID)
        mid.grid(row=0, column=1, sticky="nsew", padx=(0, p(8)))
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=3)
        mid.rowconfigure(1, weight=2)

        vp = Panel(mid, "kinetic view - realtime 3d", scale=self.ui_scale, pad=6)
        vp.grid(row=0, column=0, sticky="nsew")
        vf = vp.body
        vf.columnconfigure(0, weight=1)
        vf.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(vf, bg=T.VOID, highlightthickness=0, height=p(290))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self._draw_scene())
        self.canvas.bind("<ButtonPress-1>", self._view_press)
        self.canvas.bind("<B1-Motion>", self._view_drag)
        self.canvas.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_from", None))
        self.canvas.bind("<MouseWheel>", self._view_wheel)

        # Two rows: both segmented controls on one line overflow the centre column.
        tabs = tk.Frame(vf, bg=T.PANEL)
        tabs.grid(row=1, column=0, sticky="ew", pady=(p(6), 0))
        self.render_mode = tk.StringVar(value="REALTIME")
        Segmented(tabs, [(m, m) for m in ("REALTIME", "WIREFRAME", "TRANSPARENT",
                                          "ISOLATE")],
                  self.render_mode, command=self._on_render_mode,
                  scale=self.ui_scale).pack(side="left")

        views = tk.Frame(vf, bg=T.PANEL)
        views.grid(row=2, column=0, sticky="ew", pady=(p(5), 0))
        tk.Label(views, text=T.spaced("view angle"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left")
        self.view_angle = tk.StringVar(value="ISO")
        Segmented(views, [(v, v) for v in ("FRONT", "SIDE", "TOP", "ISO")],
                  self.view_angle, command=self._on_view_angle,
                  scale=self.ui_scale).pack(side="right")
        # what the panel shows: the 3D model, or the chess camera feed
        self.view_source = tk.StringVar(value="3D")
        Segmented(views, [("3D", "3D"), ("CAMERA", "CAM")], self.view_source,
                  command=self._on_view_source, accent=T.VIOLET,
                  scale=self.ui_scale).pack(side="right", padx=(0, p(8)))

        # The axis diagram and the sequence editor share this slot: the editor
        # appears only while RECORD & PLAY is on, so manual mode stays uncluttered.
        self.axis_panel = Panel(mid, "joint map & axis diagram", scale=self.ui_scale, pad=6)
        self.axis_panel.grid(row=1, column=0, sticky="nsew", pady=(p(8), 0))
        af = self.axis_panel.body
        af.columnconfigure(1, weight=1)
        af.rowconfigure(0, weight=1)
        legend = tk.Frame(af, bg=T.PANEL)
        legend.grid(row=0, column=0, sticky="nw", padx=(p(4), p(8)))
        for i, j in enumerate(self.cfg.joints):
            row = tk.Frame(legend, bg=T.PANEL)
            row.pack(anchor="w", pady=p(2))
            dot = tk.Canvas(row, width=p(9), height=p(9), bg=T.PANEL, highlightthickness=0)
            dot.create_oval(p(1), p(1), p(8), p(8),
                            fill=T.JOINT_COLOURS[i % len(T.JOINT_COLOURS)], outline="")
            dot.pack(side="left")
            tk.Label(row, text=j.label, bg=T.PANEL, fg=T.TXT_DIM,
                     font=T.ui_font(8)).pack(side="left", padx=p(5))
        self.axis_canvas = tk.Canvas(af, bg=T.VOID, highlightthickness=0, height=p(140))
        self.axis_canvas.grid(row=0, column=1, sticky="nsew")
        self.axis_canvas.bind("<Configure>", lambda e: self._draw_axis_map())

        self.right = Panel(mid, "sequence editor", accent=T.MAGENTA,
                           scale=self.ui_scale, pad=8)
        self.right.grid(row=1, column=0, sticky="nsew", pady=(p(8), 0))
        self._build_editor(self.right.body)

        self.chess_panel = Panel(mid, "chess", accent=T.GREEN, scale=self.ui_scale, pad=8)
        self.chess_panel.grid(row=1, column=0, sticky="nsew", pady=(p(8), 0))
        self._build_chess(self.chess_panel.body)

    def _build_editor(self, kf) -> None:
        p = self.px
        kf.columnconfigure(0, weight=1)
        kf.rowconfigure(1, weight=1)

        head = tk.Frame(kf, bg=T.PANEL)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, p(6)))
        head.columnconfigure(1, weight=1)
        tk.Label(head, text=T.spaced("name"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).grid(row=0, column=0)
        self.name_var = tk.StringVar(value=self.seq.name)
        ent = tk.Entry(head, textvariable=self.name_var, bg=T.FIELD, fg=T.TXT,
                       insertbackground=T.CYAN, relief="flat", font=T.mono_font(9),
                       highlightthickness=1, highlightbackground=T.BORDER,
                       highlightcolor=T.BORDER_HI)
        ent.grid(row=0, column=1, sticky="ew", padx=p(6), ipady=p(3))
        ent.bind("<FocusOut>", lambda e: setattr(self.seq, "name", self.name_var.get()))
        for i, (txt, cmd) in enumerate((("NEW", self._new), ("OPEN", self._open),
                                        ("SAVE", self._save))):
            GlowButton(head, txt, cmd, scale=self.ui_scale, width=p(52)
                       ).grid(row=0, column=2 + i, padx=(p(3), 0))

        cols = ("n", "t", "hold", "pose", "label")
        self.tree = ttk.Treeview(kf, columns=cols, show="headings", selectmode="browse",
                                 style="HUD.Treeview", height=4)
        for c, txt, w, anchor in (("n", "#", 28, "center"), ("t", "TIME", 50, "e"),
                                  ("hold", "HOLD", 46, "e"), ("pose", "POSE", 180, "w"),
                                  ("label", "NOTE", 92, "w")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=p(w), minwidth=p(w * 0.6), anchor=anchor,
                             stretch=(c == "pose"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.tag_configure("odd", background=T.blend(T.FIELD, T.PANEL, 0.45))
        sb = ttk.Scrollbar(kf, orient="vertical", command=self.tree.yview,
                           style="HUD.Vertical.TScrollbar")
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self._goto_frame())

        fb = tk.Frame(kf, bg=T.PANEL)
        fb.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(p(6), 0))
        for txt, cmd, kind in (("ADD POSE", self._add_pose, "accent"),
                               ("UPDATE", self._update_frame, "ghost"),
                               ("GO TO", self._goto_frame, "ghost"),
                               ("DELETE", self._delete_frame, "ghost"),
                               ("▲", lambda: self._move_frame(-1), "ghost"),
                               ("▼", lambda: self._move_frame(1), "ghost")):
            GlowButton(fb, txt, cmd, kind=kind, scale=self.ui_scale, pad=9
                       ).pack(side="left", padx=(0, p(4)))

        fb2 = tk.Frame(kf, bg=T.PANEL)
        fb2.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(p(5), 0))
        self.mode_var = tk.StringVar(value="continuous")
        Segmented(fb2, [("continuous", "CONTINUOUS"), ("keyframe", "KEYFRAME")],
                  self.mode_var, command=self._on_mode, accent=T.MAGENTA,
                  scale=self.ui_scale).pack(side="left")
        for txt, cmd in (("TIMING", self._edit_timing), ("SIMPLIFY", self._simplify),
                         ("RESCALE", self._rescale), ("CLEAR", self._clear_frames)):
            GlowButton(fb2, txt, cmd, scale=self.ui_scale, pad=8
                       ).pack(side="right", padx=(p(3), 0))

    # ------------------------------------------------------------- right column
    def _build_right(self, parent) -> None:
        p = self.px
        col = tk.Frame(parent, bg=T.VOID)
        col.grid(row=0, column=2, sticky="nsew")
        col.columnconfigure(0, weight=1)
        col.rowconfigure(2, weight=1)
        col.rowconfigure(3, weight=1)

        tp = Panel(col, "joint telemetry", scale=self.ui_scale, pad=8)
        tp.grid(row=0, column=0, sticky="ew")
        self.telemetry = DataTable(tp.body, ("JOINT", "ANGLE", "SPEED", "LOAD", "TEMP"),
                                   (104, 50, 58, 42, 42), scale=self.ui_scale,
                                   rows_visible=6)
        self.telemetry.pack(fill="x")

        sp = Panel(col, "system status", scale=self.ui_scale, pad=8)
        sp.grid(row=1, column=0, sticky="ew", pady=(p(8), 0))
        sf = sp.body
        sf.columnconfigure((0, 1), weight=1)
        self.tiles = {}
        for i, (key, label, accent) in enumerate((("voltage", "VOLTAGE", T.GREEN),
                                                  ("current", "CURRENT", T.CYAN),
                                                  ("temp", "TEMP", T.AMBER),
                                                  ("uptime", "UPTIME", T.VIOLET))):
            tile = StatTile(sf, label, "--", accent=accent, scale=self.ui_scale)
            tile.grid(row=i // 2, column=i % 2, sticky="ew",
                      padx=(0 if i % 2 == 0 else p(5), 0), pady=(0 if i < 2 else p(5), 0))
            self.tiles[key] = tile
        tk.Label(sf, text="voltage · current · temp need an INA219 and a thermistor",
                 bg=T.PANEL, fg=T.TXT_MUTE, font=T.ui_font(7)).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(p(6), 0))

        mp = Panel(col, "motion sequence", accent=T.MAGENTA, scale=self.ui_scale, pad=8)
        mp.grid(row=2, column=0, sticky="nsew", pady=(p(8), 0))
        mf = mp.body
        mf.columnconfigure(0, weight=1)
        mf.rowconfigure(0, weight=1)
        self.steps = StepList(mf, scale=self.ui_scale)
        self.steps.grid(row=0, column=0, sticky="nsew")

        trans = tk.Frame(mf, bg=T.PANEL)
        trans.grid(row=1, column=0, sticky="ew", pady=(p(6), 0))
        self.rec_btn = GlowButton(trans, "● REC", self._toggle_record, kind="danger",
                                  scale=self.ui_scale, width=p(80))
        self.rec_btn.pack(side="left")
        self.play_btn = GlowButton(trans, "▶ PLAY", self._toggle_play, kind="accent",
                                   scale=self.ui_scale, width=p(92))
        self.play_btn.pack(side="left", padx=p(5))
        self.stop_btn = GlowButton(trans, "■", self._stop_all, scale=self.ui_scale,
                                   width=p(44))
        self.stop_btn.pack(side="left")
        self.stop_btn.configure(state="disabled")
        self.loop_var = tk.BooleanVar(value=False)
        ToggleSwitch(trans, "LOOP", self.loop_var, scale=self.ui_scale).pack(side="right")

        prog = tk.Frame(mf, bg=T.PANEL)
        prog.grid(row=2, column=0, sticky="ew", pady=(p(6), 0))
        prog.columnconfigure(0, weight=1)
        self.prog = SegBar(prog, scale=self.ui_scale, accent=T.CYAN, segments=30)
        self.prog.grid(row=0, column=0, sticky="ew")
        self.time_lbl = tk.Label(prog, text="0.0 / 0.0s", bg=T.PANEL, fg=T.CYAN,
                                 font=T.mono_font(9, "bold"), width=13, anchor="e")
        self.time_lbl.grid(row=0, column=1, padx=(p(6), 0))

        rate = tk.Frame(mf, bg=T.PANEL)
        rate.grid(row=3, column=0, sticky="ew", pady=(p(5), 0))
        tk.Label(rate, text=T.spaced("rate"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left", padx=(0, p(5)))
        self.pspeed_var = tk.StringVar(value="1.0x")
        ttk.Combobox(rate, textvariable=self.pspeed_var, width=6, state="readonly",
                     style="HUD.TCombobox", font=T.mono_font(9),
                     values=["0.25x", "0.5x", "0.75x", "1.0x", "1.5x", "2.0x", "3.0x"]
                     ).pack(side="left")
        self.rec_state = tk.Label(rate, text="", bg=T.PANEL, fg=T.MAGENTA,
                                  font=T.mono_font(8, "bold"))
        self.rec_state.pack(side="right")

        lp = Panel(col, "log console", scale=self.ui_scale, pad=8)
        lp.grid(row=3, column=0, sticky="nsew", pady=(p(8), 0))
        lf = lp.body
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)
        self.console = tk.Text(lf, height=5, width=24, bg=T.FIELD, fg=T.TXT_DIM,
                               relief="flat",
                               font=T.mono_font(8), wrap="none", padx=p(6), pady=p(4),
                               highlightthickness=0)
        self.console.grid(row=0, column=0, sticky="nsew")
        self.console.configure(state="disabled")
        for tag, colour in (("info", T.TXT_DIM), ("tx", T.dim(T.CYAN, 0.45)),
                            ("rx", T.TEAL), ("ok", T.GREEN), ("err", T.RED),
                            ("warn", T.AMBER), ("stamp", T.TXT_MUTE)):
            self.console.tag_configure(tag, foreground=colour)
        GlowButton(lf, "CLEAR", self._clear_log, scale=self.ui_scale, width=p(58)
                   ).grid(row=1, column=0, sticky="e", pady=(p(5), 0))

    def _build_statusbar(self) -> None:
        p = self.px
        bar = tk.Frame(self, bg=T.PANEL, highlightthickness=1,
                       highlightbackground=T.BORDER)
        bar.pack(side="bottom", fill="x", padx=p(10), pady=(p(8), p(10)))
        inner = tk.Frame(bar, bg=T.PANEL)
        inner.pack(fill="x", padx=p(14), pady=p(5))
        self.status_cells = {}
        for key, label in (("fps", "FRAME RATE"), ("render", "RENDER"),
                           ("memory", "MEMORY"), ("board", "ARDUINO UNO"),
                           ("servos", "SERVO COUNT"), ("mode", "COMMAND MODE")):
            cell = tk.Frame(inner, bg=T.PANEL)
            cell.pack(side="left", padx=(0, p(28)))
            tk.Label(cell, text=T.spaced(label), bg=T.PANEL, fg=T.TXT_MUTE,
                     font=T.ui_font(7, "bold")).pack(anchor="w")
            val = tk.Label(cell, text="--", bg=T.PANEL, fg=T.TXT,
                           font=T.mono_font(9, "bold"))
            val.pack(anchor="w")
            self.status_cells[key] = val
        self.io_lbl = tk.Label(inner, text="", bg=T.PANEL, fg=T.TXT_MUTE,
                               font=T.mono_font(9))
        self.io_lbl.pack(side="right")

    # ================================================================ plumbing
    def _log(self, msg: str, tag: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self.console.configure(state="normal")
        self.console.insert("end", f"{stamp} ", "stamp")
        self.console.insert("end", f"{msg}\n", tag)
        if int(self.console.index("end-1c").split(".")[0]) > 400:
            self.console.delete("1.0", "80.end")
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_log(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def _cmd(self, line: str, quiet: bool = False) -> bool:
        if not self.link.connected:
            if not quiet:
                self._log("Not connected.", "warn")
            return False
        ok = self.link.send(line)
        if not quiet:
            self._log(f"» {line}", "tx")
        return ok

    def _jog_step(self) -> int:
        try:
            return max(1, int(self.jog_var.get()))
        except (tk.TclError, ValueError):
            return 5

    def _on_jog_step(self) -> None:
        try:
            self.jog_var.set(int(self.jog_choice.get()))
        except (tk.TclError, ValueError):
            pass

    def _apply_preset(self, name: str) -> None:
        pose = dict(PRESETS).get(name)
        if pose is None:
            pose = self.cfg.home_pose()
        self._manual_override()
        self._set_pose(self.cfg.clamp_pose(pose))
        self._log(f"Preset {name}", "info")

    # ---------------------------------------------------------- connection
    def _refresh_ports(self, select: str = "") -> None:
        ports = SerialLink.available_ports()
        self._port_map = {label: dev for dev, label in ports}
        labels = list(self._port_map.keys())
        self.port_cb["values"] = labels
        if not labels:
            self.port_var.set("")
            self.port_cb.set("no serial ports found")
            return
        chosen = next((lbl for lbl, dev in self._port_map.items() if dev == select), labels[0])
        self.port_var.set(chosen)

    def _selected_port(self) -> str:
        return self._port_map.get(self.port_var.get(), "")

    def _toggle_connect(self) -> None:
        if self.link.connected:
            self._stop_all()
            self.link.disconnect()
            self._set_conn_ui(False)
            self._log("Link closed.", "warn")
            return
        sim = self.sim_var.get()
        port = self._selected_port()
        if not sim and not port:
            messagebox.showwarning("No port",
                                   "No serial port selected.\n\nPlug in the Arduino Uno and "
                                   "press SCAN, or switch on SIMULATOR to try the app "
                                   "without hardware.")
            return
        self._log(f"Opening {'simulator' if sim else port} at {self.cfg.baud} baud...", "info")
        self.update_idletasks()
        if not self.link.connect(port, self.cfg.baud, simulate=sim, joints=self.cfg.count):
            self._log(f"Connect failed: {self.link.last_error}", "err")
            messagebox.showerror("Connection failed", self.link.last_error or "Unknown error")
            return
        self._set_conn_ui(True)
        if not sim:
            self.cfg.port = port
            self.cfg.save()
        self._push_limits(quiet=True)
        self._cmd(f"SPD {int(self.speed_var.get())}", quiet=True)
        self._cmd("G", quiet=True)

    def _set_conn_ui(self, on: bool) -> None:
        self.connect_btn.configure(text="DISCONNECT" if on else "CONNECT")
        if on:
            where = "SIM" if self.link.simulate else self.link.port
            self.link_dot.set(T.AMBER if self.link.simulate else T.GREEN, True)
            self.conn_lbl.configure(text=f"LINK {where.upper()}",
                                    fg=T.AMBER if self.link.simulate else T.GREEN)
        else:
            self.link_dot.set(T.TXT_MUTE, False)
            self.conn_lbl.configure(text="OFFLINE", fg=T.TXT_MUTE)
            self.teach_var.set(False)

    # ---------------------------------------------------------- mode toggle
    def _toggle_recplay(self) -> None:
        if self.recplay_var.get() and self.chess_var.get():
            self.chess_var.set(False)              # one workbench at a time
            self._chess_leave()
        self._apply_recplay_visibility()
        if self.recplay_var.get():
            self._log("Motion capture online. Manual control stays live throughout.", "ok")
        else:
            self._log("Manual control only. Sliders and jog keys drive the arm.", "info")

    def _apply_recplay_visibility(self) -> None:
        """One workbench at a time in the lower centre slot: chess, the sequence
        editor, or the axis diagram. Manual control is untouched by any of them."""
        chess_on = self.chess_var.get()
        rec_on = self.recplay_var.get() and not chess_on
        if not rec_on:
            if self.player.playing and self._chess_seq is None:
                self.player.stop()
                self._ignore_play = True
            if self.recorder.active:
                self._toggle_record()
        for panel, show in ((self.chess_panel, chess_on), (self.right, rec_on),
                            (self.axis_panel, not chess_on and not rec_on)):
            if show:
                panel.grid()
            else:
                panel.grid_remove()
        # the board needs more height than the diagram does
        self._mid.rowconfigure(0, weight=2 if chess_on else 3)
        self._mid.rowconfigure(1, weight=3 if chess_on else 2)

    def _manual_override(self) -> None:
        if self.player.playing:
            self.player.stop()
            self._ignore_play = True
            self._log("Manual override - playback stopped.", "warn")

    # ------------------------------------------------------------- joint I/O
    def _show_joint(self, idx: int, angle: int) -> None:
        self._suppress = True
        self.joint_vars[idx].set(angle)
        self._suppress = False
        self.joint_scales[idx].redraw()
        if idx == 5:                                   # the gripper appears twice
            self.grip_slider.redraw()

    def _on_slider(self, idx: int, value) -> None:
        if self._suppress:
            return
        self._manual_override()
        angle = self.cfg.joints[idx].clamp(float(value))
        if angle != self.pose[idx]:
            self.pose[idx] = angle
            self._pose_dirty = True
        if idx == 5:
            self.grip_slider.redraw()
            self.joint_scales[5].redraw()

    def _jog(self, idx: int, delta: int) -> None:
        self._manual_override()
        self._set_joint(idx, self.pose[idx] + delta)

    def _set_joint(self, idx: int, angle) -> None:
        a = self.cfg.joints[idx].clamp(angle)
        self.pose[idx] = a
        self._show_joint(idx, a)
        self._pose_dirty = True

    def _set_pose(self, pose, send: bool = True) -> None:
        for i, a in enumerate(pose[: self.cfg.count]):
            self._set_joint(i, a)
        if not send:
            self._pose_dirty = False

    def _flush_pose(self) -> None:
        if not self._pose_dirty or not self.link.connected or self.teach_var.get():
            return
        now = time.perf_counter()
        if now - self._last_send < 1.0 / max(1, self.cfg.send_hz):
            return
        self._last_send = now
        self._pose_dirty = False
        self.link.send_pose(self.cfg.to_servo_pose(self.pose))

    def _on_speed(self, _value=None) -> None:
        v = int(float(self.speed_var.get()))
        if v != self.cfg.speed_dps:
            self.cfg.speed_dps = v
            if self.link.connected:
                self.link.send(f"SPD {v}")

    def _home(self) -> None:
        self._manual_override()
        self._set_pose(self.cfg.home_pose())
        self._log("Home pose", "info")

    def _relax(self) -> None:
        if self._cmd("DET"):
            self._log("Servos relaxed - the arm will drop under its own weight.", "warn")

    def _push_limits(self, quiet: bool = False) -> None:
        if not self.link.connected:
            if not quiet:
                self._log("Not connected.", "warn")
            return
        for i, j in enumerate(self.cfg.joints):
            # Limits live in joint space; the firmware clamps in servo space, so map
            # both ends through the invert/trim transform and keep the outer bounds.
            lo, hi = sorted((j.to_servo(j.min_angle), j.to_servo(j.max_angle)))
            lo, hi = max(0, lo), min(180, hi)
            if lo >= hi:
                lo, hi = 0, 180
            self.link.send(f"LIM {i} {lo} {hi}")
        if not quiet:
            self._log("Travel limits pushed to the board.", "ok")

    def _toggle_teach(self) -> None:
        on = self.teach_var.get()
        if not self.link.connected:
            self.teach_var.set(False)
            self._log("Connect first.", "warn")
            return
        if on:
            self._cmd("TEACH 1")
            self._cmd("STREAM 60")
            self._log("Teach mode on: servos relaxed, pots A0-A5 drive the pose.", "ok")
        else:
            self._cmd("STREAM 0")
            self._cmd("TEACH 0")
            self._log("Teach mode off: servos hold the last pose.", "ok")

    # -------------------------------------------------------------- recording
    def _on_mode(self) -> None:
        keyframe = self.mode_var.get() == "keyframe"
        self.rec_btn.configure(state="disabled" if keyframe else "normal")

    def _toggle_record(self) -> None:
        if not self.recplay_var.get():
            self._log("Switch on RECORD & PLAY first.", "warn")
            return
        if self.recorder.active:
            seq = self.recorder.stop(self.pose)
            seq.name = self.name_var.get() or "Recording"
            self.seq = seq
            dropped = self.seq.simplify(1.0)
            self._refresh_frames()
            self.rec_btn.configure(text="● REC")
            self.rec_state.configure(text="")
            self._log(f"Recorded {len(self.seq)} frames over {self.seq.duration:.1f}s "
                      f"({dropped} redundant frames trimmed).", "ok")
            self._update_transport()
            return
        if self.player.playing:
            self._log("Stop playback before recording.", "warn")
            return
        if self.seq.frames and not messagebox.askyesno(
                "Overwrite", "Recording replaces the current sequence. Continue?"):
            return
        self.recorder.start(self.pose)
        self.rec_btn.configure(text="■ END")
        self._last_sample = 0.0
        self._log("Recording... move the arm, or hand-guide it in teach mode.", "ok")
        self._update_transport()

    def _pump_record(self) -> None:
        if not self.recorder.active or self.mode_var.get() != "continuous":
            return
        now = time.perf_counter()
        if now - self._last_sample < 1.0 / max(1, self.cfg.record_hz):
            return
        self._last_sample = now
        self.recorder.capture(self.pose)
        self.rec_state.configure(
            text=f"REC {self.recorder.elapsed:4.1f}s·{len(self.recorder.seq)}f")

    # -------------------------------------------------------------- playback
    def _toggle_play(self) -> None:
        if not self.recplay_var.get():
            self._log("Switch on RECORD & PLAY first.", "warn")
            return
        if self.player.playing:
            self.player.toggle_pause()
            self._update_transport()
            return
        if not self.seq.frames:
            self._log("Nothing to play - record a move or add some poses first.", "warn")
            return
        if self.recorder.active:
            self._log("Stop recording first.", "warn")
            return
        speed = float(self.pspeed_var.get().rstrip("x"))
        sel = self._selected_index()
        start_at = self.seq.time_of(sel) if sel else 0.0
        self._ignore_play = False
        self.player.start(self.seq, speed=speed, loop=self.loop_var.get(), start_at=start_at)
        self._log(f"Playing '{self.seq.name}' - {len(self.seq)} frames, "
                  f"{self.seq.duration:.1f}s at {speed}x"
                  f"{' (loop)' if self.loop_var.get() else ''}.", "ok")
        self._update_transport()

    def _pause(self) -> None:
        if self.player.playing:
            self.player.toggle_pause()
            self._log("Paused." if self.player.paused else "Resumed.", "info")
            self._update_transport()

    def _stop_all(self) -> None:
        if self.player.playing:
            self.player.stop()
            self._ignore_play = True
        if self.recorder.active:
            self._toggle_record()
        self._update_transport()

    def _pump_player(self) -> None:
        pose = None
        while True:
            try:
                pose = self._play_q.get_nowait()
            except queue.Empty:
                break
        if pose is None or self._ignore_play:
            return                    # cancelled run: let the manual pose stand
        for i, a in enumerate(pose[: self.cfg.count]):
            a = self.cfg.joints[i].clamp(a)
            self.pose[i] = a
            self._show_joint(i, a)
        if self.link.connected and not self.teach_var.get():
            self.link.send_pose(self.cfg.to_servo_pose(self.pose))

    def _on_progress(self, t: float, total: float, cycle: int) -> None:
        self._progress = (t, total, cycle)

    def _on_play_finish(self, reason: str) -> None:
        self._play_msg = reason

    def _update_transport(self) -> None:
        playing = self.player.playing
        recording = self.recorder.active
        self.play_btn.configure(
            text="▶ RESUME" if playing and self.player.paused
            else ("❚❚ PAUSE" if playing else "▶ PLAY"))
        self.stop_btn.configure(state="normal" if (playing or recording) else "disabled")
        self.rec_btn.configure(
            state="disabled" if (playing or self.mode_var.get() == "keyframe") else "normal")

    # ------------------------------------------------------------- keyframes
    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(self.tree.index(sel[0]))
        except (ValueError, tk.TclError):
            return None

    def _refresh_frames(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for i, f in enumerate(self.seq.frames):
            pose = " ".join(f"{a:3d}" for a in f.angles)
            self.tree.insert("", "end", values=(i + 1, f"{f.t:.2f}", f"{f.hold:.2f}",
                                                pose, f.label),
                             tags=("odd",) if i % 2 else ())
        self.name_var.set(self.seq.name)

    def _add_pose(self) -> None:
        hold = 0.3 if self.mode_var.get() == "keyframe" else 0.0
        self.seq.add(self.pose, hold=hold, gap=1.0)
        self.seq.joint_names = list(self.joint_names)
        self._refresh_frames()
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[-1])
            self.tree.see(kids[-1])
        self._log(f"Added pose {len(self.seq)}: {self.pose}", "ok")

    def _update_frame(self) -> None:
        i = self._selected_index()
        if i is None:
            self._log("Select a frame first.", "warn")
            return
        self.seq.frames[i].angles = list(self.pose)
        self.seq.dirty = True
        self._refresh_frames()
        self._log(f"Frame {i + 1} updated to the current pose.", "ok")

    def _goto_frame(self) -> None:
        i = self._selected_index()
        if i is None:
            return
        self._set_pose(self.seq.frames[i].angles)
        self._log(f"Moved to frame {i + 1}.", "info")

    def _delete_frame(self) -> None:
        i = self._selected_index()
        if i is None:
            self._log("Select a frame first.", "warn")
            return
        self.seq.remove(i)
        self._refresh_frames()

    def _move_frame(self, delta: int) -> None:
        i = self._selected_index()
        if i is None:
            return
        j = self.seq.move(i, delta)
        self._refresh_frames()
        kids = self.tree.get_children()
        if 0 <= j < len(kids):
            self.tree.selection_set(kids[j])

    def _edit_timing(self) -> None:
        i = self._selected_index()
        if i is None:
            self._log("Select a frame first.", "warn")
            return
        f = self.seq.frames[i]
        t = simpledialog.askfloat("Frame time", "Start time (seconds):",
                                  initialvalue=round(f.t, 2), minvalue=0.0, parent=self)
        if t is None:
            return
        hold = simpledialog.askfloat("Dwell", "Hold at this pose (seconds):",
                                     initialvalue=round(f.hold, 2), minvalue=0.0, parent=self)
        f.t = t
        if hold is not None:
            f.hold = hold
        self.seq.frames.sort(key=lambda k: k.t)
        self.seq.dirty = True
        self._refresh_frames()

    def _simplify(self) -> None:
        if len(self.seq) < 3:
            return
        tol = simpledialog.askfloat("Simplify", "Angle tolerance in degrees:",
                                    initialvalue=1.5, minvalue=0.1, maxvalue=20.0, parent=self)
        if tol is None:
            return
        n = self.seq.simplify(tol)
        self._refresh_frames()
        self._log(f"Removed {n} redundant frames.", "ok")

    def _rescale(self) -> None:
        if not self.seq.frames:
            return
        f = simpledialog.askfloat("Rescale time",
                                  "Multiply all timings by (0.5 = twice as fast):",
                                  initialvalue=1.0, minvalue=0.05, maxvalue=20.0, parent=self)
        if f is None:
            return
        self.seq.rescale(f)
        self._refresh_frames()
        self._log(f"Sequence rescaled by {f}x -> {self.seq.duration:.1f}s", "ok")

    def _clear_frames(self) -> None:
        if self.seq.frames and messagebox.askyesno("Clear", "Delete every frame?"):
            self.seq.clear()
            self._refresh_frames()

    # ------------------------------------------------------------ file I/O
    def _new(self) -> None:
        if self.seq.dirty and self.seq.frames and not messagebox.askyesno(
                "Discard", "The current sequence has unsaved changes. Discard them?"):
            return
        self.seq = Sequence("Untitled", list(self.joint_names))
        self._refresh_frames()
        self._log("New sequence.", "info")

    def _open(self) -> None:
        os.makedirs(SEQ_DIR, exist_ok=True)
        path = filedialog.askopenfilename(title="Open sequence", initialdir=SEQ_DIR,
                                          filetypes=[("Arm sequence", "*.json"), ("All", "*.*")])
        if not path:
            return
        try:
            seq = Sequence.load(path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Open failed", str(exc))
            return
        if seq.joint_names and len(seq.joint_names) != self.cfg.count:
            messagebox.showwarning("Joint mismatch",
                                   f"This file was recorded with {len(seq.joint_names)} joints, "
                                   f"the current arm has {self.cfg.count}. Playback may be wrong.")
        self.seq = seq
        self._refresh_frames()
        self._log(f"Loaded {os.path.basename(path)} - {len(seq)} frames, {seq.duration:.1f}s", "ok")

    def _save(self) -> None:
        if not self.seq.path:
            self._save_as()
            return
        self.seq.name = self.name_var.get() or self.seq.name
        try:
            self.seq.save(self.seq.path)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._log(f"Saved {self.seq.path}", "ok")

    def _save_as(self) -> None:
        if not self.seq.frames:
            self._log("Nothing to save.", "warn")
            return
        os.makedirs(SEQ_DIR, exist_ok=True)
        path = filedialog.asksaveasfilename(
            title="Save sequence", initialdir=SEQ_DIR, defaultextension=".json",
            initialfile=f"{self.name_var.get() or 'sequence'}.json",
            filetypes=[("Arm sequence", "*.json")])
        if not path:
            return
        self.seq.name = self.name_var.get() or self.seq.name
        try:
            self.seq.save(path)
        except OSError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self._refresh_frames()
        self._log(f"Saved {path}", "ok")

    # ------------------------------------------------------------- dialogs
    def _joint_setup(self) -> None:
        JointSetup(self, self.cfg, on_apply=self._apply_joint_setup)

    def _apply_joint_setup(self) -> None:
        for i, j in enumerate(self.cfg.joints):
            sl = self.joint_scales[i]
            sl.lo, sl.hi = float(j.min_angle), float(j.max_angle)
            self.pose[i] = j.clamp(self.pose[i])
        self.grip_slider.lo = float(self.cfg.joints[5].min_angle)
        self.grip_slider.hi = float(self.cfg.joints[5].max_angle)
        self._set_pose(self.pose, send=False)
        self.cfg.save()
        self._push_limits(quiet=True)
        self._log("Joint setup applied and saved.", "ok")

    def _show_protocol(self) -> None:
        messagebox.showinfo("Serial protocol", (
            "Serial protocol - 115200 baud, one command per line\n\n"
            "  PING              handshake, replies OK PONG <name> <ver> <joints>\n"
            "  G                 read position -> POS a0 a1 a2 a3 a4 a5\n"
            "  M a0 a1 ... a5    move all joints ('-' keeps one where it is)\n"
            "  S <i> <angle>     move one joint\n"
            "  J <i> <delta>     jog one joint\n"
            "  SPD <deg/s>       slew rate, 0-600 (0 = instant)\n"
            "  HOME              go to the home pose\n"
            "  ATT / DET         attach / relax servos\n"
            "  LIM <i> <lo> <hi> travel limits    LIMS  list them\n"
            "  SAVE / LOAD       limits + speed to/from EEPROM\n"
            "  TEACH 0|1         relax and read pots on A0-A5\n"
            "  STREAM <ms>       push POS lines every <ms> ms (0 = off)\n"))

    def _show_about(self) -> None:
        messagebox.showinfo("About",
                            "Robotic Arm Studio\n\n"
                            "Drives a 6-axis hobby-servo arm through an Arduino Uno over "
                            "USB serial, with continuous and keyframe motion recording, "
                            "interpolated playback and a live 3D kinetic view.\n\n"
                            f"Config: {CONFIG_FILE}\nSequences: {SEQ_DIR}")

    # ------------------------------------------------------------- 3D view
    def _on_render_mode(self) -> None:
        mode = self.render_mode.get()
        self.renderer.mode = "REALTIME" if mode == "ISOLATE" else mode
        self._invalidate_views()
        self.renderer.isolate = "fore" if mode == "ISOLATE" else None

    def _on_view_angle(self) -> None:
        self.renderer.set_view(self.view_angle.get())
        self._invalidate_views()

    def _view_press(self, e) -> None:
        if self.view_source.get() == "CAMERA":
            if self._chess_calibrating and self._cam_raw is not None:
                fx = (e.x - self._cam_raw_offset[0]) / max(1e-6, self._cam_raw_scale)
                fy = (e.y - self._cam_raw_offset[1]) / max(1e-6, self._cam_raw_scale)
                h, w = self._cam_raw.shape[:2]
                if 0 <= fx < w and 0 <= fy < h:
                    self._chess_add_corner(fx, fy)
            return
        self._drag_from = (e.x, e.y)
    def _view_drag(self, e) -> None:
        if not self._drag_from:
            return
        dx, dy = e.x - self._drag_from[0], e.y - self._drag_from[1]
        self._drag_from = (e.x, e.y)
        self.renderer.orbit(-dx * 0.4, dy * 0.3)

    def _view_wheel(self, e) -> None:
        self.renderer.zoom(0.9 if e.delta > 0 else 1.11)

    def _invalidate_views(self) -> None:
        self._scene_key = self._axis_key = None

    def _scene_state(self, w: int, h: int):
        """Everything the 3D view depends on. Redrawing a static scene is pure waste."""
        r = self.renderer
        live = self.recorder.active or self.player.playing
        return (tuple(self.pose), round(r.az, 1), round(r.el, 1), round(r.dist, 2),
                r.mode, r.isolate, w, h, round(self._phase, 1) if live else 0,
                self.recorder.active, self.player.playing)

    def _on_view_source(self) -> None:
        if self.view_source.get() == "CAMERA" and self._chess_cam is None:
            if self._chess_cam_start():
                self.chess_cam_var.set(True)
            else:
                self.view_source.set("3D")
        # a full repaint either way: the camera wipes the 3D layers, and the 3D
        # view must rebuild its cached static layer once it comes back
        self.canvas.delete("all")
        self.renderer._static_key = None
        self._invalidate_views()
    def _draw_scene(self) -> None:
        c = self.canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        if self.view_source.get() == "CAMERA":
            self._draw_camera_view(c, w, h)
            return
        key = self._scene_state(w, h)
        if key == self._scene_key:
            return
        self._scene_key = key
        t0 = time.perf_counter()
        c.delete("hud")                 # renderer keeps its own static/dynamic layers
        self.renderer.draw(c, self.pose, w, h)
        p = self.px

        bw, bh = p(108), p(72)
        c.create_rectangle(p(10), p(10), p(10) + bw, p(10) + bh,
                           fill=T.blend(T.PANEL, T.VOID, 0.35),
                           outline=T.dim(T.CYAN, 0.6), tags="hud")
        c.create_text(p(18), p(23), anchor="w", text="LIVE VIEW", fill=T.CYAN,
                      font=T.ui_font(8, "bold"), tags="hud")
        for i, (k, v) in enumerate((("FPS", self._fps_text()),
                                    ("RES", f"{w}x{h}"),
                                    ("FACES", str(self.renderer.faces)))):
            c.create_text(p(18), p(38) + i * p(13), anchor="w", text=f"{k}: {v}",
                          fill=T.TXT_MUTE, font=T.mono_font(7), tags="hud")

        cx, cy, s = w - p(46), p(42), p(15)
        for dx, dy, label in ((0, -1.45, "TOP"), (-1.5, 0.8, "FRONT"), (1.5, 0.8, "RIGHT")):
            c.create_text(cx + dx * s, cy + dy * s, text=label, fill=T.TXT_MUTE,
                          font=T.ui_font(6, "bold"), tags="hud")
        c.create_polygon(cx, cy - s, cx + s, cy - s * 0.5, cx, cy, cx - s, cy - s * 0.5,
                         fill=T.dim(T.CYAN, 0.75), outline=T.dim(T.CYAN, 0.4), tags="hud")
        c.create_polygon(cx - s, cy - s * 0.5, cx, cy, cx, cy + s, cx - s, cy + s * 0.5,
                         fill=T.dim(T.CYAN, 0.85), outline=T.dim(T.CYAN, 0.4), tags="hud")
        c.create_polygon(cx + s, cy - s * 0.5, cx, cy, cx, cy + s, cx + s, cy + s * 0.5,
                         fill=T.dim(T.CYAN, 0.8), outline=T.dim(T.CYAN, 0.4), tags="hud")

        if self.recorder.active:
            c.create_oval(p(12), h - p(26), p(24), h - p(14),
                          fill=T.blend(T.MAGENTA, "#ffffff", self._phase * 0.5),
                          outline="", tags="hud")
            c.create_text(p(31), h - p(20), anchor="w", text="RECORDING", fill=T.MAGENTA,
                          font=T.ui_font(8, "bold"), tags="hud")
        elif self.player.playing:
            c.create_text(p(12), h - p(20), anchor="w", text="▶ PLAYING", fill=T.GREEN,
                          font=T.ui_font(8, "bold"), tags="hud")

        self._render_ms = (time.perf_counter() - t0) * 1000.0
        now = time.perf_counter()
        self._last_render_at = now
        dt = now - self._last_draw
        self._last_draw = now
        if dt > 0:
            self._view_fps = 0.85 * self._view_fps + 0.15 * (1.0 / dt)

    def _fps_text(self, suffix: str = "") -> str:
        """A static scene is not redrawn at all, so report IDLE rather than 0 fps."""
        if time.perf_counter() - self._last_render_at > 0.4:
            return "IDLE"
        return f"{self._view_fps:.0f}{suffix}"

    def _draw_axis_map(self) -> None:
        """Labelled wireframe of the arm, from a fixed side view."""
        c = self.axis_canvas
        w, h = c.winfo_width(), c.winfo_height()
        if w < 80 or h < 60:
            return
        key = (tuple(self.pose), w, h)
        if key == self._axis_key:
            return
        self._axis_key = key
        c.delete("all")
        rend = self._axis_renderer
        rend.set_view("SIDE")
        rend.mode = "WIREFRAME"
        rend.draw(c, self.pose, w, h, show_floor=False)
        pts = R3.joint_points(self.pose)

        # Joints can project within a few pixels of each other, so leader lines fan
        # out to alternating sides and each label is pushed clear of the last one
        # placed on its side.
        placed = {1: [], -1: []}
        gap = self.px(13)
        for i, name in enumerate(R3.JOINT_ORDER):
            p3 = rend.project_world(pts[name], w, h)
            if not p3:
                continue
            x, y = p3[0], p3[1]
            colour = T.JOINT_COLOURS[i % len(T.JOINT_COLOURS)]
            c.create_oval(x - 3, y - 3, x + 3, y + 3, fill=colour, outline="")
            side = 1 if i % 2 == 0 else -1
            ly = y
            for taken in placed[side]:
                if abs(ly - taken) < gap:
                    ly = taken + gap
            placed[side].append(ly)
            lx = x + side * self.px(34)
            c.create_line(x, y, lx - side * self.px(6), ly, lx, ly,
                          fill=T.blend(colour, T.VOID, 0.45))
            c.create_text(lx + side * 3, ly, anchor="w" if side > 0 else "e",
                          text=self.cfg.joints[i].label, fill=T.TXT_DIM,
                          font=T.ui_font(7))

    # ------------------------------------------------------------------ tick
    def _tick(self) -> None:
        try:
            self._frame_no += 1
            self._phase = 0.5 + 0.5 * math.sin(time.perf_counter() * 3.4)
            self._pump_serial()
            self._pump_player()
            if self._play_msg:
                self._log(f"Playback {self._play_msg}.", "info")
                if self._chess_seq is not None:
                    self._chess_robot_finished(self._play_msg)
                self._play_msg = None
            self._pump_chess()
            self._pump_record()
            self._flush_pose()
            self._track_speed()
            if self._frame_no % 2 == 0:          # ~20 fps for the heavy views
                self._draw_scene()
                if not self.recplay_var.get():
                    self._draw_axis_map()
                self._animate_chrome()
            self._update_status()
        except Exception as exc:
            self._log(f"UI error: {exc}", "err")
        finally:
            self.after(self._tick_ms, self._tick)

    def _track_speed(self) -> None:
        """Per-joint angular velocity, measured from the commanded pose."""
        now = time.perf_counter()
        dt = now - self._prev_pose_t
        if dt < 0.15:
            return
        for i in range(self.cfg.count):
            v = abs(self.pose[i] - self._prev_pose[i]) / dt
            self._joint_speed[i] = 0.6 * self._joint_speed[i] + 0.4 * v
        self._prev_pose = list(self.pose)
        self._prev_pose_t = now

    def _animate_chrome(self) -> None:
        self.link_dot.tick(self._phase)
        self.state_dot.tick(self._phase)
        if self.recorder.active:
            self.rec_btn.pulse(self._phase)
        self.clock_lbl.configure(text=time.strftime("%H:%M:%S"))
        g = self.cfg.joints[5]
        span = max(1, g.max_angle - g.min_angle)
        self.grip_gauge.set((self.pose[5] - g.min_angle) / span)

    def _pump_serial(self) -> None:
        was = self.link.connected
        for line in self.link.poll():
            pos = parse_pos(line)
            if pos is not None:
                if self.teach_var.get():
                    logical = self.cfg.from_servo_pose(pos[: self.cfg.count])
                    for i, a in enumerate(logical):
                        self.pose[i] = a
                        self._show_joint(i, a)
                    self._pose_dirty = False
                elif not self.player.playing and not self.recorder.active:
                    self._set_pose(self.cfg.from_servo_pose(pos[: self.cfg.count]), send=False)
                continue
            up = line.upper()
            if up.startswith("RDY") or "PONG" in up:
                self.link.firmware = line.replace("OK PONG", "").replace("RDY", "").strip()
                self._log(f"« {line}", "ok")
            elif up.startswith("ERR"):
                self._log(f"« {line}", "err")
            elif up.startswith("OK"):
                pass
            else:
                self._log(f"« {line}", "rx")
        if was and not self.link.connected:
            self._set_conn_ui(False)
            self._log("Link lost - check the USB cable.", "err")

    def _update_status(self) -> None:
        t, total, cycle = self._progress
        if self.player.playing:
            self.prog.set(0 if total <= 0 else t / total)
            self.time_lbl.configure(text=f"{t:.1f}/{total:.1f}s"
                                         + (f" #{cycle + 1}" if cycle else ""))
        elif not self.recorder.active:
            self.time_lbl.configure(text=f"0.0/{self.seq.duration:.1f}s")
            self.prog.set(0)

        if self.recorder.active:
            state, colour = "recording", T.MAGENTA
        elif self.player.playing:
            state, colour = "playing", T.GREEN
        else:
            state, colour = "system idle", T.TXT_MUTE
        self.state_lbl.configure(text=T.spaced(state), fg=colour)
        self.state_dot.set(colour, state != "system idle")

        # Angle and speed are measured; load and temp need sensors this arm lacks,
        # so they stay blank rather than showing invented numbers.
        rows = []
        for i, j in enumerate(self.cfg.joints):
            rows.append((T.JOINT_COLOURS[i % len(T.JOINT_COLOURS)],
                         [j.label, f"{self.pose[i]}°", f"{self._joint_speed[i]:.0f} d/s",
                          "--", "--"]))
        self.telemetry.set_rows(rows)

        up = int(time.time() - self._started)
        self.tiles["uptime"].set(f"{up // 3600:02d}:{up % 3600 // 60:02d}:{up % 60:02d}")

        shown = self._chess_seq if self._chess_seq is not None else self.seq
        steps, active = [], -1
        for i, f in enumerate(shown.frames):
            label = f.label or f"Frame {i + 1}"
            steps.append((f"{int(f.t) // 60:02d}:{int(f.t) % 60:02d}."
                          f"{int(f.t * 10) % 10}", label))
        if self.player.playing and shown.frames:
            elapsed = self._progress[0]
            active = max(0, sum(1 for f in shown.frames if f.t <= elapsed) - 1)
        self.steps.set_steps(steps, active)

        self.status_cells["fps"].configure(text=self._fps_text(suffix=" FPS"))
        self.status_cells["render"].configure(text=f"{self._render_ms:.1f} ms")
        mem = process_memory_mb()
        self.status_cells["memory"].configure(text=f"{mem:.0f} MB" if mem else "--")
        self.status_cells["board"].configure(
            text="ONLINE" if self.link.connected else "OFFLINE",
            fg=T.GREEN if self.link.connected else T.TXT_MUTE)
        self.status_cells["servos"].configure(text=str(self.cfg.count))
        self.status_cells["mode"].configure(
            text="TEACH" if self.teach_var.get() else
                 ("PLAYBACK" if self.player.playing else "REALTIME"))
        self.io_lbl.configure(
            text=f"{self.link.firmware or 'no board'}   TX {self.link.tx_count}  "
                 f"RX {self.link.rx_count}")
        self._update_transport()


    # ------------------------------------------------------------------ chess
    def _build_chess(self, cf) -> None:
        p = self.px
        cf.columnconfigure(0, weight=1)
        cf.columnconfigure(1, weight=0)
        cf.rowconfigure(0, weight=1)

        self.chess_board = ChessBoard(cf, scale=self.ui_scale, on_square=self._chess_click)
        self.chess_board.grid(row=0, column=0, sticky="nsew")

        side = tk.Frame(cf, bg=T.PANEL)
        side.grid(row=0, column=1, sticky="ns", padx=(p(10), 0))

        self.chess_status = tk.Label(side, text="CHESS OFF", bg=T.PANEL, fg=T.TXT_MUTE,
                                     font=T.ui_font(10, "bold"), anchor="w")
        self.chess_status.pack(anchor="w")
        self.chess_sub = tk.Label(side, text="", bg=T.PANEL, fg=T.TXT_MUTE,
                                  font=T.ui_font(7), anchor="w", justify="left",
                                  wraplength=p(170))
        self.chess_sub.pack(anchor="w", pady=(0, p(6)))

        row = tk.Frame(side, bg=T.PANEL)
        row.pack(anchor="w", pady=(0, p(4)))
        tk.Label(row, text=T.spaced("level"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left", padx=(0, p(6)))
        self.chess_level = tk.StringVar(value="2")
        Segmented(row, [("1", "1"), ("2", "2"), ("3", "3")], self.chess_level,
                  accent=T.GREEN, scale=self.ui_scale).pack(side="left")
        row = tk.Frame(side, bg=T.PANEL)
        row.pack(anchor="w", pady=(0, p(6)))
        tk.Label(row, text=T.spaced("you"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left", padx=(0, p(6)))
        self.chess_colour = tk.StringVar(value="white")
        Segmented(row, [("white", "WHITE"), ("black", "BLACK")], self.chess_colour,
                  accent=T.GREEN, scale=self.ui_scale).pack(side="left")

        btns = tk.Frame(side, bg=T.PANEL)
        btns.pack(anchor="w", pady=(0, p(6)))
        GlowButton(btns, "NEW GAME", self._chess_new_game, kind="accent",
                   scale=self.ui_scale, pad=8).pack(side="left")
        GlowButton(btns, "UNDO", self._chess_undo, scale=self.ui_scale, pad=8
                   ).pack(side="left", padx=(p(4), 0))
        GlowButton(btns, "RESIGN", self._chess_resign, scale=self.ui_scale, pad=8
                   ).pack(side="left", padx=(p(4), 0))

        self.chess_arm_var = tk.BooleanVar(value=True)
        ToggleSwitch(side, "ARM PLAYS", self.chess_arm_var, accent=T.CYAN,
                     scale=self.ui_scale).pack(anchor="w")
        self.chess_cam_var = tk.BooleanVar(value=False)
        ToggleSwitch(side, "CAMERA", self.chess_cam_var, self._toggle_chess_cam,
                     accent=T.VIOLET, scale=self.ui_scale).pack(anchor="w", pady=(p(2), p(3)))
        camrow = tk.Frame(side, bg=T.PANEL)
        camrow.pack(anchor="w", pady=(0, p(4)))
        for label, cmd in (("CALIBRATE", self._chess_calibrate),
                           ("EMPTY", self._chess_empty_board),
                           ("NEXT CAM", self._chess_next_cam)):
            GlowButton(camrow, label, cmd, scale=self.ui_scale, pad=5
                       ).pack(side="left", padx=(0, p(3)))

        teach = tk.Frame(cf, bg=T.PANEL)
        teach.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(p(5), 0))
        tk.Label(teach, text=T.spaced("teach"), bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(side="left", padx=(0, p(5)))
        for label, key in (("a1", "a1"), ("h1", "h1"), ("a8", "a8"), ("h8", "h8"),
                           ("HOVER", "lift"), ("BIN", "bin")):
            GlowButton(teach, label, lambda k=key: self._chess_teach(k), scale=self.ui_scale,
                       pad=5).pack(side="left", padx=(0, p(3)))
        self.chess_map_lbl = tk.Label(teach, text="", bg=T.PANEL, fg=T.TXT_MUTE,
                                      font=T.ui_font(7))
        self.chess_map_lbl.pack(side="left", padx=(p(6), 0))

        # wraplength keeps a long move list from widening the whole centre column
        self.chess_moves = tk.Label(cf, text="", bg=T.PANEL, fg=T.TXT_DIM,
                                    font=T.mono_font(8), anchor="w", justify="left",
                                    wraplength=p(440))
        self.chess_moves.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(p(3), 0))

    # -- mode ------------------------------------------------------------------
    def _toggle_chess(self) -> None:
        if self.chess_var.get():
            try:
                import chess_game  # noqa: F401  (needs python-chess)
            except ImportError as exc:
                self.chess_var.set(False)
                self._log(f"Chess needs python-chess: pip install chess  ({exc})", "err")
                return
            if self.recplay_var.get():
                self.recplay_var.set(False)
            self._apply_recplay_visibility()
            import chess_arm
            self.square_map = chess_arm.SquareMap.load(joints=self.cfg.count)
            self._chess_map_status()
            if self.chess is None:
                self._chess_new_game()
            self._log("Chess mode on. Click a piece, then its destination.", "ok")
        else:
            self._chess_leave()
            self._apply_recplay_visibility()
            self._log("Chess mode off.", "info")

    def _chess_leave(self) -> None:
        """Stop anything chess-related that is running; keep the game itself."""
        if self.chess_cam_var.get():
            self.chess_cam_var.set(False)
        self._chess_cam_stop()
        if self._chess_seq is not None and self.player.playing:
            self.player.stop()
            self._ignore_play = True
        self._chess_seq = None

    def _chess_new_game(self) -> None:
        import chess_game
        if self.chess is not None:
            self.chess.close()
        level = int(self.chess_level.get())
        self.chess = chess_game.GameSession(level=level,
                                            human_white=self.chess_colour.get() == "white")
        self.chess.start()
        self._chess_selected = None
        self.chess_board.flipped = self.chess_colour.get() == "black"
        self._chess_cam_armed = False
        self._chess_refresh()
        self._log(f"New game - level {level}, you are {self.chess_colour.get()}.", "ok")

    def _chess_undo(self) -> None:
        if self.chess and self.chess.undo():
            self._chess_selected = None
            self._chess_cam_armed = False
            self._chess_refresh()
            self._log("Took back the last move pair.", "info")

    def _chess_resign(self) -> None:
        if self.chess and self.chess.state != "OVER":
            self.chess.resign()
            self._chess_refresh()
            self._log("You resigned.", "warn")

    # -- human input -------------------------------------------------------------
    def _chess_click(self, square: str) -> None:
        import chess
        if not self.chess or self.chess.state != "HUMAN":
            return
        sq = chess.parse_square(square)
        board = self.chess.board
        if self._chess_selected is not None:
            src = chess.parse_square(self._chess_selected)
            move = chess.Move(src, sq)
            piece = board.piece_at(src)
            if piece and piece.piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
                move = chess.Move(src, sq, promotion=chess.QUEEN)
            if move in board.legal_moves:
                self._chess_human(move)
                return
        piece = board.piece_at(sq)
        if piece and piece.color == board.turn:
            self._chess_selected = square
            targets = [chess.square_name(t) for t in self.chess.legal_targets(sq)]
            self.chess_board.set_selection(square, targets)
        else:
            self._chess_selected = None
            self.chess_board.set_selection(None, ())

    def _chess_human(self, move) -> None:
        san = self.chess.board.san(move)
        if self.chess.human_move(move):
            self._chess_selected = None
            self.chess_board.set_selection(None, ())
            self.chess_board.set_changed(())
            self._chess_refresh()
            self._log(f"You: {san}", "info")

    # -- engine + arm ----------------------------------------------------------
    def _pump_chess(self) -> None:
        if not self.chess:
            return
        for ev in self.chess.poll():
            if ev[0] == "engine":
                mv, ms = ev[1], ev[2]
                self._log(f"Robot: {self.chess.history[-1]}  ({ms / 1000:.1f}s)", "ok")
                self._chess_refresh()
                self._chess_robot_play(mv)
            elif ev[0] == "error":
                self._log(f"Engine error: {ev[1]}", "err")
            elif ev[0] == "over":
                self._chess_refresh()
        self._pump_chess_camera()

    def _chess_robot_play(self, move) -> None:
        """Have the arm play the engine's move, or skip physically if it cannot."""
        import chess_arm
        if not self.chess_arm_var.get():
            self._chess_robot_finished("skipped")
            return
        if self.square_map is None or not self.square_map.ready:
            self._log("Arm not taught for this board - move shown on the board only. "
                      "Use TEACH SQUARES.", "warn")
            self._chess_robot_finished("skipped")
            return
        src, dst, capture, extra = chess_arm.move_legs(self.chess.pending_before, move)
        try:
            seq = self.square_map.plan_move(src, dst, capture=capture,
                                            home=self.cfg.home_pose(), extra=extra)
        except RuntimeError as exc:
            self._log(f"Could not plan the arm move: {exc}", "err")
            self._chess_robot_finished("skipped")
            return
        self._chess_seq = seq
        self._ignore_play = False
        self.player.start(seq, speed=1.0, loop=False)
        self._log(f"Arm playing {src}-{dst}: {len(seq)} frames, {seq.duration:.1f}s", "info")
        self._chess_refresh()

    def _chess_robot_finished(self, reason: str) -> None:
        if reason == "stopped":
            self._log("Arm move interrupted - finish it by hand, then continue.", "warn")
        self._chess_seq = None
        if self.chess and self.chess.state == "ROBOT":
            self.chess.robot_done()
        self._chess_cam_armed = False
        self._chess_refresh()

    # -- camera ----------------------------------------------------------------
    def _toggle_chess_cam(self) -> None:
        if self.chess_cam_var.get():
            if not self._chess_cam_start():
                self.chess_cam_var.set(False)
        else:
            self._chess_cam_stop()

    def _calib_path(self) -> str:
        import paths as _paths
        return os.path.join(_paths.data_dir(), "board_calibration.json")

    def _chess_cam_start(self, index: Optional[int] = None) -> bool:
        """Open a camera and show its feed. Calibration is not required to start:
        an uncalibrated feed is exactly what you need in order to calibrate."""
        if self._chess_cam is not None and index is None:
            return True                                # already running; do not reopen
        try:
            import vision
        except ImportError:
            self._log("Camera needs opencv-python: pip install opencv-python", "err")
            return False
        self._cam_started_at = time.perf_counter()
        path = self._calib_path()
        calib = vision.Calibration()
        if os.path.exists(path):
            try:
                calib = vision.Calibration.load(path)
            except (OSError, ValueError):
                pass
        if index is not None:
            calib.camera_index = index

        cam = vision.Camera(calib.camera_index)
        if not cam.start():
            found = [i for i in vision.Camera.enumerate() if i != calib.camera_index]
            if not found:
                self._log(cam.error or "No camera found.", "err")
                return False
            calib.camera_index = found[0]
            cam = vision.Camera(found[0])
            if not cam.start():
                self._log(cam.error, "err")
                return False

        self._chess_cam = cam
        self._chess_calib = calib
        self._chess_watcher = vision.BoardWatcher(calib) if calib.ready else None
        self._chess_corners = []
        self._chess_calibrating = not calib.ready
        self._chess_cam_armed = False
        self.view_source.set("CAMERA")
        self.canvas.delete("all")
        self.renderer._static_key = None
        self._invalidate_views()
        if calib.ready:
            self._log(f"Camera {calib.camera_index} watching the board. Make your move "
                      "and take your hand away.", "ok")
        else:
            self._log(f"Camera {calib.camera_index} on. Click the four board corners in "
                      "the Kinetic View, clockwise from top-left.", "warn")
        return True

    def _chess_cam_stop(self) -> None:
        if self._chess_cam:
            self._chess_cam.stop()
        self._chess_cam = self._chess_watcher = None
        self._chess_cam_armed = False
        self._chess_calibrating = False
        self._cam_raw = self._cam_warped = None
        if self.view_source.get() == "CAMERA":
            self.view_source.set("3D")
            self.canvas.delete("all")
            self.renderer._static_key = None
            self._invalidate_views()

    def _chess_next_cam(self) -> None:
        """Cycle to the next camera that opens - laptops list the lid webcam first."""
        try:
            import vision
        except ImportError:
            return
        found = vision.Camera.enumerate()
        if not found:
            self._log("No camera found.", "err")
            return
        current = self._chess_calib.camera_index if self._chess_calib else -1
        nxt = found[(found.index(current) + 1) % len(found)] if current in found else found[0]
        self._chess_cam_stop()
        if self._chess_cam_start(index=nxt):
            self.chess_cam_var.set(True)

    def _chess_calibrate(self) -> None:
        if self._chess_cam is None and not self._chess_cam_start():
            return
        self.chess_cam_var.set(True)
        self._chess_corners = []
        self._chess_calibrating = True
        self._chess_watcher = None
        self._log("Click the four board corners in the Kinetic View, clockwise from "
                  "top-left.", "warn")

    def _chess_add_corner(self, fx: float, fy: float) -> None:
        """One calibration click, in camera-frame pixels."""
        import vision
        self._chess_corners.append([float(fx), float(fy)])
        n = len(self._chess_corners)
        if n < 4:
            self._log(f"Corner {n} of 4 set.", "info")
            return
        calib = self._chess_calib or vision.Calibration()
        calib.corners = [list(map(float, c)) for c in vision.order_corners(self._chess_corners)]
        if self._cam_raw is not None:
            h, w = self._cam_raw.shape[:2]
            calib.frame_size = (w, h)
        self._chess_calib = calib
        self._chess_calibrating = False
        self._chess_watcher = vision.BoardWatcher(calib)
        self._chess_cam_armed = False
        try:
            calib.save(self._calib_path())
            self._log("Board calibrated and saved. Press EMPTY with the board cleared for "
                      "the most reliable detection, then set up the pieces.", "ok")
        except OSError as exc:
            self._log(f"Calibrated, but could not save: {exc}", "err")

    def _chess_empty_board(self) -> None:
        if self._chess_watcher is None or self._cam_warped is None:
            self._log("Calibrate first, then press EMPTY with no pieces on the board.", "warn")
            return
        self._chess_watcher.set_empty_reference(self._cam_warped)
        self._log("Empty-board reference captured.", "ok")
    def _pump_chess_camera(self) -> None:
        cam = self._chess_cam
        if not cam:
            return
        frame = cam.read()
        if frame is None:
            dead = not getattr(cam, "alive", True)
            waited = time.perf_counter() - getattr(self, "_cam_started_at", 0.0)
            if dead or (getattr(cam, "error", "") and waited > 3.0):
                reason = cam.error or "camera stopped delivering frames"
                self._log(f"Camera failed: {reason}", "err")
                self._log("Try NEXT CAM, or close any other app using the camera.", "warn")
                self.chess_cam_var.set(False)
                self._chess_cam_stop()
            return
        self._cam_raw = frame
        if self._chess_watcher is None or self._chess_calib is None or not self.chess:
            self._cam_warped = None
            return
        import chess_game
        import vision
        warped = vision.warp_board(frame, self._chess_calib.matrix())
        self._cam_warped = warped
        human = self.chess.state == "HUMAN"
        if human and not self._chess_cam_armed:
            self._chess_watcher.begin_turn(warped)     # snapshot at the start of the turn
            self._chess_cam_armed = True
            self.chess_board.set_changed(())           # rings from the last turn are stale
        elif not human and self._chess_watcher.state != vision.IDLE:
            self._chess_watcher.pause()               # the arm is moving: ignore it
        if human:
            self._chess_watcher.update(warped)
        for ev in self._chess_watcher.poll():
            if ev.kind not in ("move", "change") or not human:
                continue
            grid = self._chess_watcher.last_grid
            if grid is None:
                continue
            observed = [[bool(grid[r][c]) for c in range(8)] for r in range(8)]
            mv, candidates = chess_game.resolve_move(self.chess.board, observed)
            if mv is not None:
                self._log(f"Camera saw {ev.text}", "rx")
                self._chess_human(mv)
            else:
                self.chess_board.set_changed(ev.squares or ())
                self._log(f"Camera saw {ev.text} but that matches "
                          f"{len(candidates)} legal moves - click the move instead.", "warn")
    # -- teaching --------------------------------------------------------------
    def _chess_teach(self, key: str) -> None:
        if self.square_map is None:
            import chess_arm
            self.square_map = chess_arm.SquareMap.load(joints=self.cfg.count)
        m = self.square_map
        if key in ("a1", "h1", "a8", "h8"):
            m.set_corner(key, self.pose)
            self._log(f"Taught {key} = {self.pose}", "ok")
        elif key == "lift":
            if "a1" not in m.corners:
                self._log("Teach a1 first: park the gripper on a1, press a1, raise it to a "
                          "safe travel height, then press HOVER.", "warn")
                return
            m.set_lift_from(m.corners["a1"], self.pose)
            self._log(f"Hover lift = {m.lift}", "ok")
        elif key == "bin":
            m.set_bin(self.pose)
            self._log("Taught the capture bin.", "ok")
        try:
            m.save()
        except OSError as exc:
            self._log(f"Could not save square map: {exc}", "err")
        self._chess_map_status()

    def _chess_map_status(self) -> None:
        m = self.square_map
        if m is None:
            self.chess_map_lbl.configure(text="")
            return
        missing = m.missing()
        if not missing:
            self.chess_map_lbl.configure(text="board taught - arm will play", fg=T.GREEN)
        else:
            self.chess_map_lbl.configure(text="still need: " + ", ".join(missing),
                                         fg=T.TXT_MUTE)

    # -- display ---------------------------------------------------------------
    def _chess_refresh(self) -> None:
        import chess
        if not self.chess:
            return
        board = self.chess.board
        pos = {chess.square_name(sq): pc.symbol() for sq, pc in board.piece_map().items()}
        last = ()
        if self.chess.last_move:
            last = (chess.square_name(self.chess.last_move.from_square),
                    chess.square_name(self.chess.last_move.to_square))
        check_sq = chess.square_name(board.king(board.turn)) if board.is_check() else None
        self.chess_board.set_position(pos, last, check_sq)

        st = self.chess.state
        if st == "HUMAN":
            text, colour = "YOUR MOVE", T.CYAN
            sub = "check!" if board.is_check() else "click a piece, then a square"
        elif st == "THINKING":
            text, colour, sub = "THINKING", T.AMBER, "engine is choosing a move"
        elif st == "ROBOT":
            text, colour = "ROBOT MOVING", T.GREEN
            sub = "arm is playing its move" if self._chess_seq else "applying move"
        elif st == "OVER":
            text = self.chess.result.upper()
            colour = T.GREEN if "you" in self.chess.result else T.MAGENTA
            sub = "press NEW GAME"
        else:
            text, colour, sub = "IDLE", T.TXT_MUTE, ""
        self.chess_status.configure(text=text, fg=colour)
        self.chess_sub.configure(text=sub)

        hist = self.chess.history
        pairs = []
        for i in range(0, len(hist), 2):
            w = hist[i]
            b = hist[i + 1] if i + 1 < len(hist) else ""
            pairs.append(f"{i // 2 + 1}. {w} {b}")
        self.chess_moves.configure(text="  ".join(pairs[-6:]))


    # ---------------------------------------------------------- camera view
    def _photo_from_bgr(self, bgr, dw: int, dh: int):
        """OpenCV frame -> Tk PhotoImage via a PPM header; no Pillow needed."""
        import cv2
        img = cv2.resize(bgr, (max(1, dw), max(1, dh)), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        return tk.PhotoImage(data=f"P6\n{dw} {dh}\n255\n".encode() + rgb.tobytes(),
                             master=self)

    def _draw_camera_view(self, c, w: int, h: int) -> None:
        """Raw feed (with the calibration quad) beside the rectified board.
        While calibrating, the raw feed fills the panel and clicks place corners."""
        p = self.px
        c.delete("all")
        self._scene_key = None                         # 3D must repaint when we return
        if self._chess_cam is None:
            c.create_text(w / 2, h / 2, fill=T.TXT_MUTE, font=T.ui_font(9), justify="center",
                          text="camera is off\nswitch on CAMERA in the chess panel")
            return
        if self._cam_raw is None:
            waited = time.perf_counter() - getattr(self, "_cam_started_at", time.perf_counter())
            msg = f"starting camera {getattr(self._chess_calib, 'camera_index', 0)}..."
            if waited > 3.0:
                msg += (f"\nno frames after {waited:.0f}s - another app may be using it,"
                        "\nor try NEXT CAM")
            elif waited > 1.0:
                msg += f"  ({waited:.0f}s)"
            c.create_text(w / 2, h / 2, fill=T.TXT_MUTE if waited < 3 else T.AMBER,
                          font=T.ui_font(9), justify="center", text=msg)
            return
        import vision
        calibrated = (self._chess_watcher is not None and self._chess_calib is not None
                      and not self._chess_calibrating)
        gap = p(10)
        board_side = min(h - p(8), int(w * 0.42)) if calibrated else 0
        raw_w = w - (board_side + gap if calibrated else 0)
        rh, rw = self._cam_raw.shape[:2]
        scale = min(raw_w / rw, (h - p(8)) / rh)
        dw, dh = int(rw * scale), int(rh * scale)
        ox, oy = (0 if calibrated else (raw_w - dw) // 2), (h - dh) // 2
        self._cam_raw_scale, self._cam_raw_offset = scale, (ox, oy)

        self._photo_cam_raw = self._photo_from_bgr(self._cam_raw, dw, dh)
        c.create_image(ox, oy, image=self._photo_cam_raw, anchor="nw")

        corners = (self._chess_corners if self._chess_calibrating
                   else (self._chess_calib.corners if self._chess_calib else []))
        pts = [(x * scale + ox, y * scale + oy) for x, y in corners]
        for i, (x, y) in enumerate(pts):
            r = p(6)
            c.create_oval(x - r, y - r, x + r, y + r, outline=T.CYAN, width=2)
            c.create_text(x + p(9), y - p(9), text=str(i + 1), fill=T.CYAN,
                          font=T.mono_font(8, "bold"))
        if len(pts) == 4:
            c.create_polygon([v for pt in pts for v in pt], outline=T.CYAN, fill="", width=2)

        if self._chess_calibrating:
            bw, bh = p(300), p(26)
            c.create_rectangle((w - bw) / 2, h - bh - p(4), (w + bw) / 2, h - p(4),
                               fill=T.blend(T.PANEL, T.VOID, 0.25), outline=T.dim(T.AMBER, 0.5))
            c.create_text(w / 2, h - bh / 2 - p(4), fill=T.AMBER, font=T.ui_font(8, "bold"),
                          text=f"CLICK BOARD CORNER {len(pts) + 1} OF 4  (clockwise from top-left)")
            self._mark_view_frame()
            return

        grid, _ = vision.occupancy_grid(self._cam_warped, self._chess_watcher.empty_ref,
                                        self._chess_calib.occupancy_threshold)
        overlay = vision.draw_overlay(self._cam_warped, grid,
                                      self.chess_board.changed if self.chess else ())
        self._photo_cam_board = self._photo_from_bgr(overlay, board_side, board_side)
        c.create_image(w - board_side, (h - board_side) // 2, image=self._photo_cam_board,
                       anchor="nw")

        watcher = self._chess_watcher
        state = watcher.state
        colour = {"WATCHING": T.CYAN, "IN_MOTION": T.AMBER, "SETTLING": T.VIOLET}.get(
            state, T.TXT_MUTE)
        c.create_rectangle(p(8), p(8), p(8) + p(124), p(8) + p(46),
                           fill=T.blend(T.PANEL, T.VOID, 0.35), outline=T.dim(colour, 0.5))
        c.create_text(p(16), p(20), anchor="w", text=state, fill=colour,
                      font=T.ui_font(8, "bold"))
        c.create_text(p(16), p(38), anchor="w", fill=T.TXT_MUTE, font=T.mono_font(7),
                      text=f"{self._chess_cam.fps:4.1f} fps   motion {watcher.motion:4.1f}")
        self._mark_view_frame()

    def _mark_view_frame(self) -> None:
        """Count a painted frame toward the FPS readout, whichever view drew it."""
        now = time.perf_counter()
        dt = now - self._last_draw
        self._last_draw = self._last_render_at = now
        if dt > 0:
            self._view_fps = 0.85 * self._view_fps + 0.15 * (1.0 / dt)
    # ----------------------------------------------------------------- close
    def _on_close(self) -> None:
        if self.seq.dirty and self.seq.frames and not messagebox.askyesno(
                "Quit", "The sequence has unsaved changes. Quit anyway?"):
            return
        try:
            self._chess_leave()
            self.player.stop(wait=True)
            if self.link.connected:
                self.link.send("STREAM 0")
                self.link.send("TEACH 0")
            self.cfg.save()
            self.link.disconnect()
        finally:
            self.destroy()


# ---------------------------------------------------------------- setup dialog
class JointSetup(tk.Toplevel):
    """Per-joint travel limits, home pose, direction and mechanical trim."""

    def __init__(self, master: ArmApp, cfg: ArmConfig, on_apply):
        super().__init__(master)
        self.cfg = cfg
        self.on_apply = on_apply
        self.title("Joint setup")
        self.configure(bg=T.PANEL)
        self.transient(master)
        self.resizable(False, False)
        p = master.px

        frm = tk.Frame(self, bg=T.PANEL, padx=p(16), pady=p(14))
        frm.pack(fill="both", expand=True)
        for c, head in enumerate(("JOINT", "MIN", "MAX", "HOME", "TRIM", "INVERT")):
            tk.Label(frm, text=head, bg=T.PANEL, fg=T.TXT_MUTE,
                     font=T.ui_font(8, "bold")).grid(row=0, column=c, padx=p(7),
                                                     pady=(0, p(8)))
        self.vars = []
        for r, j in enumerate(cfg.joints, start=1):
            tk.Label(frm, text=j.label, bg=T.PANEL, fg=T.TXT, width=13, anchor="w",
                     font=T.ui_font(9)).grid(row=r, column=0, sticky="w", padx=p(6))
            row = {}
            for c, (key, val, lo, hi) in enumerate(
                    (("min_angle", j.min_angle, 0, 180), ("max_angle", j.max_angle, 0, 180),
                     ("home", j.home, 0, 180), ("trim", j.trim, -40, 40)), start=1):
                v = tk.IntVar(value=val)
                tk.Spinbox(frm, from_=lo, to=hi, textvariable=v, width=5, bg=T.FIELD,
                           fg=T.TXT, relief="flat", buttonbackground=T.PANEL_HI,
                           insertbackground=T.CYAN, font=T.mono_font(10),
                           highlightthickness=1, highlightbackground=T.BORDER,
                           ).grid(row=r, column=c, padx=p(7), pady=p(3))
                row[key] = v
            inv = tk.BooleanVar(value=j.invert)
            ToggleSwitch(frm, "", inv, scale=master.ui_scale).grid(row=r, column=5)
            row["invert"] = inv
            self.vars.append(row)

        tk.Label(frm, text="Trim shifts the mechanical zero; invert flips a servo mounted "
                           "the other way round.", bg=T.PANEL, fg=T.TXT_MUTE,
                 font=T.ui_font(8), wraplength=p(420), justify="left"
                 ).grid(row=len(cfg.joints) + 1, column=0, columnspan=6, sticky="w",
                        pady=(p(14), p(10)))
        btns = tk.Frame(frm, bg=T.PANEL)
        btns.grid(row=len(cfg.joints) + 2, column=0, columnspan=6, sticky="e")
        GlowButton(btns, "CANCEL", self.destroy, scale=master.ui_scale).pack(side="right")
        GlowButton(btns, "APPLY", self._apply, kind="accent", scale=master.ui_scale
                   ).pack(side="right", padx=p(8))
        self.grab_set()

    def _apply(self) -> None:
        for j, row in zip(self.cfg.joints, self.vars):
            lo = int(row["min_angle"].get())
            hi = int(row["max_angle"].get())
            if lo >= hi:
                messagebox.showerror("Bad range", f"{j.label}: min must be below max.",
                                     parent=self)
                return
            j.min_angle, j.max_angle = lo, hi
            j.home = max(lo, min(hi, int(row["home"].get())))
            j.trim = int(row["trim"].get())
            j.invert = bool(row["invert"].get())
        self.on_apply()
        self.destroy()
