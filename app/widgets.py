"""Canvas-drawn HUD widgets.

ttk cannot produce glows, capsule tracks or animated switches, so the controls
that carry the look are drawn by hand on tk.Canvas. Each one owns a Tk variable
where that makes sense, so the surrounding code keeps working with plain
DoubleVar / BooleanVar / StringVar bindings.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable, List, Optional, Sequence

import theme as T


def _round_rect(cv: tk.Canvas, x0, y0, x1, y1, r, **kw):
    """Rounded rectangle as a smoothed polygon (Tk has no native one)."""
    r = max(0, min(r, abs(x1 - x0) / 2, abs(y1 - y0) / 2))
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


# --------------------------------------------------------------------- panel
class Panel(tk.Frame):
    """Bordered section with a spaced-caps title and corner brackets."""

    def __init__(self, parent, title: str = "", accent: str = T.CYAN, scale: float = 1.0,
                 pad: int = 12, **kw):
        super().__init__(parent, bg=T.PANEL, highlightthickness=0, **kw)
        self.accent = accent
        self._s = scale
        p = lambda n: int(round(n * scale))

        # children are inset so the frame canvas's border and brackets stay visible
        self._edge = tk.Canvas(self, bg=T.PANEL, highlightthickness=0, height=p(26))
        self._edge.pack(fill="x", side="top", padx=p(3), pady=(p(3), 0))
        self._edge.bind("<Configure>", self._draw_edge)
        self._title = title

        self.body = tk.Frame(self, bg=T.PANEL)
        self.body.pack(fill="both", expand=True, padx=p(pad), pady=(0, p(pad)))

        self._frame = tk.Canvas(self, bg=T.PANEL, highlightthickness=0)
        self._frame.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lower(self._frame)     # widget stacking; Canvas.lower() means items
        self.bind("<Configure>", self._draw_frame)

    def _draw_edge(self, _e=None) -> None:
        c = self._edge
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        p = lambda n: int(round(n * self._s))
        y = h // 2
        c.create_rectangle(p(12), y - p(5), p(15), y + p(5), fill=self.accent, outline="")
        tx = p(23)
        if self._title:
            c.create_text(tx, y + p(1), text=T.spaced(self._title), anchor="w",
                          fill=T.dim(self.accent, 0.25, T.TXT), font=T.ui_font(8, "bold"))
            tx += c.bbox("all")[2] - tx + p(12) if c.bbox("all") else p(60)
        c.create_line(tx, y, w - p(12), y, fill=T.blend(T.BORDER, T.PANEL, 0.15))

    def _draw_frame(self, _e=None) -> None:
        c = self._frame
        c.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        p = lambda n: int(round(n * self._s))
        c.create_rectangle(1, 1, w - 2, h - 2, outline=T.BORDER, width=1)
        arm = p(14)
        hi = T.dim(self.accent, 0.45)
        for cx, cy, dx, dy in ((1, 1, 1, 1), (w - 2, 1, -1, 1),
                               (1, h - 2, 1, -1), (w - 2, h - 2, -1, -1)):
            c.create_line(cx, cy, cx + dx * arm, cy, fill=hi, width=p(2))
            c.create_line(cx, cy, cx, cy + dy * arm, fill=hi, width=p(2))


# -------------------------------------------------------------------- button
class GlowButton(tk.Canvas):
    """Flat capsule button with a hover glow. Mimics enough of the ttk API that
    callers can still do ``btn['state']`` and ``btn.configure(state=...)``."""

    KINDS = {
        "ghost":  (T.CYAN, T.TXT),
        "accent": (T.CYAN, "#02121a"),
        "danger": (T.MAGENTA, "#1a0410"),
        "warn":   (T.AMBER, "#1a1002"),
    }

    def __init__(self, parent, text: str, command: Optional[Callable] = None,
                 kind: str = "ghost", scale: float = 1.0, width: int = 0, pad: int = 14):
        self._s = scale
        self._p = lambda n: int(round(n * scale))
        self.kind = kind
        self.text = text
        self.command = command
        self._state = "normal"
        self._hover = False
        self._down = False
        self._pulse = 0.0

        f = T.ui_font(9, "bold")
        probe = tk.Label(parent, text=text, font=f)
        tw = probe.winfo_reqwidth()
        probe.destroy()
        w = width or tw + self._p(pad) * 2
        super().__init__(parent, width=w, height=self._p(30), bg=T.PANEL,
                         highlightthickness=0, cursor="hand2")
        self._font = f
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<Button-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Configure>", lambda e: self._draw())
        self._draw()

    # -- ttk-ish surface ---------------------------------------------------
    def configure(self, cnf=None, **kw):
        if "state" in kw:
            self._state = str(kw.pop("state"))
            self._hover = False
            self.configure_cursor()
            self._draw()
        if "text" in kw:
            self.text = kw.pop("text")
            self._draw()
        if kw or cnf:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def configure_cursor(self) -> None:
        super().configure(cursor="" if self._state == "disabled" else "hand2")

    def cget(self, key):
        if key == "state":
            return self._state
        if key == "text":
            return self.text
        return super().cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    # -- interaction -------------------------------------------------------
    def _enter(self, _e):
        if self._state != "disabled":
            self._hover = True
            self._draw()

    def _leave(self, _e):
        self._hover = self._down = False
        self._draw()

    def _press(self, _e):
        if self._state != "disabled":
            self._down = True
            self._draw()

    def _release(self, _e):
        was = self._down
        self._down = False
        self._draw()
        if was and self._state != "disabled" and self.command:
            self.command()

    def pulse(self, phase: float) -> None:
        """Animated attention state, used while recording."""
        self._pulse = phase
        self._draw()

    # -- paint -------------------------------------------------------------
    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4:
            return
        accent, fg = self.KINDS.get(self.kind, self.KINDS["ghost"])
        p = self._p
        r = h // 2

        if self._state == "disabled":
            _round_rect(self, 1, 1, w - 2, h - 2, r, fill=T.blend(T.PANEL, T.FIELD, 0.6),
                        outline=T.blend(T.BORDER, T.PANEL, 0.4))
            self.create_text(w / 2, h / 2, text=self.text, fill=T.TXT_MUTE, font=self._font)
            return

        solid = self.kind in ("accent", "danger", "warn")
        lift = 0.0
        if self._pulse:
            lift = 0.18 * self._pulse
        if self._hover:
            lift += 0.22
        if self._down:
            lift += 0.12

        if solid:
            body = T.blend(accent, "#ffffff", min(0.35, lift))
            for i, g in enumerate(T.glow_ramp(accent, T.PANEL, 3)):
                _round_rect(self, 1 + i, 1 + i, w - 2 - i, h - 2 - i, r, fill="", outline=g)
            _round_rect(self, p(3), p(3), w - p(3), h - p(3), r, fill=body, outline="")
            self.create_text(w / 2, h / 2, text=self.text, fill=fg,
                             font=self._font)
        else:
            edge = T.blend(T.BORDER_HI, accent, min(1.0, 0.25 + lift * 2))
            fill = T.blend(T.FIELD, accent, min(0.22, 0.04 + lift * 0.5))
            _round_rect(self, 1, 1, w - 2, h - 2, r, fill=fill, outline=edge)
            self.create_text(w / 2, h / 2, text=self.text,
                             fill=T.blend(T.TXT, accent, min(1.0, lift * 2.2)),
                             font=self._font)


# -------------------------------------------------------------------- slider
class NeonSlider(tk.Canvas):
    """Label + glowing track + value readout, all in one canvas.

    Drives a DoubleVar and reports drags through `command(value)` so it can slot
    straight in where a ttk.Scale used to be.
    """

    def __init__(self, parent, label: str, variable: tk.DoubleVar, lo: float, hi: float,
                 command: Optional[Callable[[float], None]] = None, accent: str = T.CYAN,
                 scale: float = 1.0, label_w: int = 92, value_w: int = 62, unit: str = "°",
                 height: int = 32):
        self._s = scale
        p = lambda n: int(round(n * scale))
        self._p = p
        super().__init__(parent, width=p(label_w + 84 + value_w), height=p(height),
                         bg=T.PANEL, highlightthickness=0)
        self.label = label
        self.var = variable
        self.lo, self.hi = float(lo), float(hi)
        self.command = command
        self.accent = accent
        self.unit = unit
        self.label_w = p(label_w)
        self.value_w = p(value_w)
        self._hover = False
        self._drag = False
        self._last_drawn = None

        self.bind("<Configure>", lambda e: self.redraw(force=True))
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<MouseWheel>", self._wheel)

    # -- geometry ----------------------------------------------------------
    def _track_span(self):
        w = self.winfo_width()
        x0 = self.label_w + self._p(6)
        x1 = w - self.value_w - self._p(10)
        return x0, max(x0 + self._p(20), x1)

    def _value_at(self, x: float) -> float:
        x0, x1 = self._track_span()
        k = (x - x0) / max(1.0, x1 - x0)
        return self.lo + max(0.0, min(1.0, k)) * (self.hi - self.lo)

    def _x_of(self, v: float) -> float:
        x0, x1 = self._track_span()
        k = (float(v) - self.lo) / max(1e-6, self.hi - self.lo)
        return x0 + max(0.0, min(1.0, k)) * (x1 - x0)

    # -- interaction -------------------------------------------------------
    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self.redraw(force=True)

    def _emit(self, x: float) -> None:
        v = round(self._value_at(x))
        if v != round(self.var.get()):
            self.var.set(v)
        if self.command:
            self.command(v)
        self.redraw(force=True)

    def _press(self, e):
        self._drag = True
        self._emit(e.x)

    def _motion(self, e):
        if self._drag:
            self._emit(e.x)

    def _release(self, _e):
        self._drag = False
        self.redraw(force=True)

    def _wheel(self, e):
        step = 1 if abs(e.delta) < 200 else 2
        v = round(self.var.get()) + (step if e.delta > 0 else -step)
        v = max(self.lo, min(self.hi, v))
        self.var.set(v)
        if self.command:
            self.command(v)
        self.redraw(force=True)

    # -- paint -------------------------------------------------------------
    def redraw(self, force: bool = False) -> None:
        try:
            v = float(self.var.get())
        except tk.TclError:
            return
        key = (round(v, 2), self._hover, self._drag, self.winfo_width())
        if not force and key == self._last_drawn:
            return
        self._last_drawn = key

        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40:
            return
        p = self._p
        cy = h / 2
        x0, x1 = self._track_span()
        hx = self._x_of(v)
        live = self._hover or self._drag

        self.create_text(p(2), cy, text=self.label.upper(), anchor="w",
                         fill=T.TXT if live else T.TXT_DIM, font=T.ui_font(8, "bold"))

        # trough + ticks
        _round_rect(self, x0, cy - p(4), x1, cy + p(4), p(4),
                    fill=T.FIELD, outline=T.blend(T.BORDER, T.PANEL, 0.25))
        for i in range(11):
            tx = x0 + (x1 - x0) * i / 10
            tall = p(7) if i % 5 == 0 else p(4)
            self.create_line(tx, cy + p(7), tx, cy + p(7) + tall * 0.5,
                             fill=T.blend(T.BORDER, T.PANEL, 0.35 if i % 5 else 0.0))

        # filled portion, stacked for glow
        if hx > x0 + 1:
            for i, g in enumerate(T.glow_ramp(self.accent, T.PANEL, 3)):
                inset = p(4) - i * p(1.5)
                _round_rect(self, x0, cy - inset, hx, cy + inset, inset, fill=g, outline="")
            _round_rect(self, x0, cy - p(1.5), hx, cy + p(1.5), p(1.5),
                        fill=T.blend(self.accent, "#ffffff", 0.35 if live else 0.1), outline="")

        # handle
        hw, hh = p(5), p(11)
        for i, g in enumerate(T.glow_ramp(self.accent, T.PANEL, 3)):
            e = (2 - i) * p(2)
            _round_rect(self, hx - hw - e, cy - hh - e, hx + hw + e, cy + hh + e,
                        p(3) + e, fill="", outline=g)
        _round_rect(self, hx - hw, cy - hh, hx + hw, cy + hh, p(3),
                    fill=T.blend(self.accent, "#ffffff", 0.55 if live else 0.25),
                    outline=T.blend(self.accent, "#ffffff", 0.8))

        # value
        self.create_text(w - p(4), cy, anchor="e", text=f"{int(round(v)):3d}{self.unit}",
                         fill=T.blend(self.accent, "#ffffff", 0.3) if live else T.TXT,
                         font=T.mono_font(12, "bold"))


# -------------------------------------------------------------------- switch
class ToggleSwitch(tk.Canvas):
    """Sliding switch bound to a BooleanVar."""

    def __init__(self, parent, text: str, variable: tk.BooleanVar,
                 command: Optional[Callable] = None, accent: str = T.CYAN,
                 scale: float = 1.0):
        self._s = scale
        p = lambda n: int(round(n * scale))
        self._p = p
        self.var = variable
        self.text = text
        self.command = command
        self.accent = accent
        self._hover = False

        f = T.ui_font(9)
        probe = tk.Label(parent, text=text, font=f)
        tw = probe.winfo_reqwidth()
        probe.destroy()
        super().__init__(parent, width=p(40) + p(8) + tw, height=p(26),
                         bg=T.PANEL, highlightthickness=0, cursor="hand2")
        self._font = f
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Configure>", lambda e: self.redraw())
        self._trace = variable.trace_add("write", lambda *a: self.redraw())
        self.redraw()

    def _set_hover(self, on):
        self._hover = on
        self.redraw()

    def _toggle(self, _e):
        self.var.set(not bool(self.var.get()))
        if self.command:
            self.command()

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        p = self._p
        h = self.winfo_height() or p(26)
        on = bool(self.var.get())
        tw, th = p(36), p(16)
        cy = h / 2
        x0, y0, x1, y1 = p(2), cy - th / 2, p(2) + tw, cy + th / 2

        if on:
            for i, g in enumerate(T.glow_ramp(self.accent, T.PANEL, 3)):
                _round_rect(self, x0 - i, y0 - i, x1 + i, y1 + i, th / 2 + i,
                            fill="", outline=g)
            _round_rect(self, x0, y0, x1, y1, th / 2,
                        fill=T.dim(self.accent, 0.55), outline=T.blend(self.accent, "#ffffff", 0.2))
        else:
            _round_rect(self, x0, y0, x1, y1, th / 2, fill=T.FIELD,
                        outline=T.BORDER_HI if self._hover else T.BORDER)

        kr = th / 2 - p(2)
        kx = (x1 - kr - p(2)) if on else (x0 + kr + p(2))
        self.create_oval(kx - kr, cy - kr, kx + kr, cy + kr,
                         fill=T.blend(self.accent, "#ffffff", 0.6) if on else T.TXT_MUTE,
                         outline="")
        self.create_text(x1 + p(8), cy, anchor="w", text=self.text,
                         fill=T.TXT if on else (T.TXT_DIM if self._hover else T.TXT_MUTE),
                         font=self._font)


# ----------------------------------------------------------------- segmented
class Segmented(tk.Canvas):
    """Two-or-more option pill bound to a StringVar."""

    def __init__(self, parent, options: Sequence[tuple], variable: tk.StringVar,
                 command: Optional[Callable] = None, accent: str = T.CYAN,
                 scale: float = 1.0):
        self._s = scale
        p = lambda n: int(round(n * scale))
        self._p = p
        self.options = list(options)          # [(value, label), ...]
        self.var = variable
        self.command = command
        self.accent = accent
        self._hover_idx = -1

        f = T.ui_font(9, "bold")
        probe = tk.Label(parent, font=f)
        widths = []
        for _v, lbl in self.options:
            probe.configure(text=lbl)
            widths.append(probe.winfo_reqwidth())
        probe.destroy()
        self._font = f
        self._seg_w = [w + p(24) for w in widths]
        super().__init__(parent, width=sum(self._seg_w) + p(6), height=p(30),
                         bg=T.PANEL, highlightthickness=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        self.bind("<Configure>", lambda e: self.redraw())
        variable.trace_add("write", lambda *a: self.redraw())
        self.redraw()

    def _index_at(self, x):
        acc = self._p(3)
        for i, w in enumerate(self._seg_w):
            if acc <= x < acc + w:
                return i
            acc += w
        return -1

    def _set_hover(self, i):
        if i != self._hover_idx:
            self._hover_idx = i
            self.redraw()

    def _motion(self, e):
        self._set_hover(self._index_at(e.x))

    def _click(self, e):
        i = self._index_at(e.x)
        if i >= 0:
            self.var.set(self.options[i][0])
            if self.command:
                self.command()

    def redraw(self) -> None:
        if not self.winfo_exists():
            return
        self.delete("all")
        p = self._p
        h = self.winfo_height() or p(30)
        w = sum(self._seg_w) + p(6)
        _round_rect(self, 1, 1, w - 2, h - 2, p(6), fill=T.FIELD, outline=T.BORDER)
        cur = self.var.get()
        x = p(3)
        for i, ((val, lbl), sw) in enumerate(zip(self.options, self._seg_w)):
            active = val == cur
            if active:
                for j, g in enumerate(T.glow_ramp(self.accent, T.FIELD, 3)):
                    _round_rect(self, x + 1 + j, p(3) + j, x + sw - 1 - j, h - p(3) - j,
                                p(5), fill="", outline=g)
                _round_rect(self, x + p(2), p(4), x + sw - p(2), h - p(4), p(5),
                            fill=T.dim(self.accent, 0.72), outline="")
            self.create_text(x + sw / 2, h / 2, text=lbl,
                             fill=T.blend(self.accent, "#ffffff", 0.55) if active
                             else (T.TXT_DIM if i == self._hover_idx else T.TXT_MUTE),
                             font=self._font)
            x += sw


# ------------------------------------------------------------------ readouts
class StatusDot(tk.Canvas):
    """Pulsing state lamp."""

    def __init__(self, parent, scale: float = 1.0):
        p = lambda n: int(round(n * scale))
        self._p = p
        d = p(16)
        super().__init__(parent, width=d, height=d, bg=T.PANEL, highlightthickness=0)
        self.colour = T.TXT_MUTE
        self.active = False
        self._phase = 0.0
        self.redraw()

    def set(self, colour: str, active: bool = True) -> None:
        self.colour, self.active = colour, active
        self.redraw()

    def tick(self, phase: float) -> None:
        if self.active:
            self._phase = phase
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        d = self.winfo_width() or self._p(16)
        c = d / 2
        r = self._p(3.5)
        if self.active:
            halo = r + self._p(3) + self._p(2) * self._phase
            self.create_oval(c - halo, c - halo, c + halo, c + halo,
                             fill=T.dim(self.colour, 0.82 - 0.12 * self._phase), outline="")
            self.create_oval(c - r - 1, c - r - 1, c + r + 1, c + r + 1,
                             fill=T.dim(self.colour, 0.45), outline="")
        self.create_oval(c - r, c - r, c + r, c + r, fill=self.colour, outline="")


class SegBar(tk.Canvas):
    """Segmented progress meter."""

    def __init__(self, parent, scale: float = 1.0, accent: str = T.CYAN, segments: int = 48):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.accent = accent
        self.segments = segments
        self.value = 0.0
        super().__init__(parent, width=p(120), height=p(14), bg=T.PANEL,
                         highlightthickness=0)
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, fraction: float) -> None:
        f = max(0.0, min(1.0, fraction))
        if abs(f - self.value) > 0.002:
            self.value = f
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        p = self._p
        gap = max(1, p(2))
        sw = (w - gap * (self.segments - 1)) / self.segments
        lit = self.value * self.segments
        for i in range(self.segments):
            x = i * (sw + gap)
            on = i < lit
            col = (T.blend(self.accent, "#ffffff", 0.25) if i > lit - 2 and on
                   else T.dim(self.accent, 0.35) if on else T.blend(T.FIELD, T.BORDER, 0.4))
            self.create_rectangle(x, p(3), x + sw, h - p(3), fill=col, outline="")


# ------------------------------------------------------------- radial gauge
class RadialGauge(tk.Canvas):
    """Arc gauge with a big percentage in the middle - used for the gripper."""

    def __init__(self, parent, scale: float = 1.0, accent: str = T.AMBER,
                 size: int = 118, caption: str = ""):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.accent = accent
        self.caption = caption
        self.value = 0.0
        super().__init__(parent, width=p(size), height=p(size), bg=T.PANEL,
                         highlightthickness=0)
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, fraction: float, caption: str = None) -> None:
        f = max(0.0, min(1.0, fraction))
        changed = abs(f - self.value) > 0.004 or (caption is not None
                                                  and caption != self.caption)
        if caption is not None:
            self.caption = caption
        if changed:
            self.value = f
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20:
            return
        p = self._p
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - p(7)
        wide = p(9)
        self.create_arc(cx - r, cy - r, cx + r, cy + r, start=225, extent=-270,
                        style="arc", outline=T.blend(T.FIELD, T.BORDER, 0.6), width=wide)
        if self.value > 0.001:
            for i, g in enumerate(T.glow_ramp(self.accent, T.PANEL, 3)):
                self.create_arc(cx - r, cy - r, cx + r, cy + r, start=225,
                                extent=-270 * self.value, style="arc", outline=g,
                                width=wide + p(6) - i * p(2))
            self.create_arc(cx - r, cy - r, cx + r, cy + r, start=225,
                            extent=-270 * self.value, style="arc",
                            outline=T.blend(self.accent, "#ffffff", 0.25), width=wide)
        self.create_text(cx, cy - p(6), text=f"{int(round(self.value * 100))}%",
                         fill=T.TXT, font=T.mono_font(15, "bold"))
        if self.caption:
            self.create_text(cx, cy + p(13), text=self.caption, fill=T.TXT_MUTE,
                             font=T.ui_font(7, "bold"))


# ---------------------------------------------------------------- stat tile
class StatTile(tk.Frame):
    """Small bordered readout: label above, value below, with an accent bar."""

    def __init__(self, parent, label: str, value: str = "--", accent: str = T.CYAN,
                 scale: float = 1.0):
        super().__init__(parent, bg=T.FIELD, highlightthickness=1,
                         highlightbackground=T.BORDER)
        p = lambda n: int(round(n * scale))
        tk.Frame(self, bg=accent, width=p(3)).pack(side="left", fill="y")
        inner = tk.Frame(self, bg=T.FIELD)
        inner.pack(side="left", fill="both", expand=True, padx=p(7), pady=p(5))
        tk.Label(inner, text=T.spaced(label), bg=T.FIELD, fg=T.TXT_MUTE,
                 font=T.ui_font(7, "bold")).pack(anchor="w")
        self.value_lbl = tk.Label(inner, text=value, bg=T.FIELD, fg=accent,
                                  font=T.mono_font(11, "bold"))
        self.value_lbl.pack(anchor="w")

    def set(self, value: str) -> None:
        if self.value_lbl["text"] != value:
            self.value_lbl.configure(text=value)


# -------------------------------------------------------------- icon button
class IconButton(tk.Canvas):
    """Larger square action button: glyph over a caption."""

    def __init__(self, parent, glyph: str, label: str, command=None,
                 accent: str = T.CYAN, scale: float = 1.0, size: int = 60):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.glyph, self.label, self.command, self.accent = glyph, label, command, accent
        self._hover = False
        super().__init__(parent, width=p(size), height=p(size), bg=T.PANEL,
                         highlightthickness=0, cursor="hand2")
        self.bind("<Enter>", lambda e: self._set(True))
        self.bind("<Leave>", lambda e: self._set(False))
        self.bind("<ButtonRelease-1>", self._click)
        self.bind("<Configure>", lambda e: self.redraw())

    def _set(self, on):
        self._hover = on
        self.redraw()

    def _click(self, _e):
        if self.command:
            self.command()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        p = self._p
        fill = T.blend(T.FIELD, self.accent, 0.16 if self._hover else 0.05)
        edge = T.blend(T.BORDER, self.accent, 0.7 if self._hover else 0.2)
        _round_rect(self, 1, 1, w - 2, h - 2, p(8), fill=fill, outline=edge)
        self.create_text(w / 2, h * 0.38, text=self.glyph,
                         fill=T.blend(self.accent, "#ffffff", 0.3 if self._hover else 0.0),
                         font=T.ui_font(14))
        self.create_text(w / 2, h * 0.76, text=self.label,
                         fill=T.TXT if self._hover else T.TXT_DIM,
                         font=T.ui_font(7, "bold"))


# --------------------------------------------------------------- data table
class DataTable(tk.Canvas):
    """Fixed-column readout table with a colour dot per row."""

    def __init__(self, parent, headers, widths, scale: float = 1.0, row_h: int = 21,
                 rows_visible: int = 6):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.headers = headers
        self.widths = [p(x) for x in widths]
        self.row_h = p(row_h)
        self.rows = []
        super().__init__(parent, width=sum(self.widths) + p(6), bg=T.PANEL,
                         highlightthickness=0,
                         height=p(row_h) * rows_visible + p(26))
        self.bind("<Configure>", lambda e: self.redraw())

    def set_rows(self, rows) -> None:
        """rows: [(dot_colour, [cell, cell, ...]), ...]"""
        if rows != self.rows:
            self.rows = rows
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w = self.winfo_width()
        if w < 40:
            return
        p = self._p
        x0, y = p(2), p(9)
        x = x0
        for i, head in enumerate(self.headers):
            if i == 0:
                self.create_text(x + p(2), y, text=head, anchor="w", fill=T.TXT_MUTE,
                                 font=T.ui_font(7, "bold"))
            else:
                self.create_text(x + self.widths[i] - p(4), y, text=head, anchor="e",
                                 fill=T.TXT_MUTE, font=T.ui_font(7, "bold"))
            x += self.widths[i]
        self.create_line(x0, y + p(8), w - p(2), y + p(8),
                         fill=T.blend(T.BORDER, T.PANEL, 0.3))

        for r, (dot, cells) in enumerate(self.rows):
            ry = y + p(21) + r * self.row_h
            if r % 2:
                self.create_rectangle(x0, ry - self.row_h / 2 + p(1), w - p(2),
                                      ry + self.row_h / 2 - p(1),
                                      fill=T.blend(T.PANEL, T.FIELD, 0.55), outline="")
            x = x0
            for i, cell in enumerate(cells):
                if i == 0:
                    self.create_oval(x + p(2), ry - p(3), x + p(8), ry + p(3),
                                     fill=dot, outline="")
                    self.create_text(x + p(14), ry, text=cell, anchor="w", fill=T.TXT,
                                     font=T.ui_font(8))
                else:
                    unavailable = cell == "--"
                    self.create_text(x + self.widths[i] - p(4), ry, text=cell, anchor="e",
                                     fill=T.TXT_MUTE if unavailable else T.TXT_DIM,
                                     font=T.mono_font(9))
                x += self.widths[i]


# ---------------------------------------------------------------- step list
class StepList(tk.Canvas):
    """Motion-sequence timeline: connected dots with times and labels."""

    def __init__(self, parent, scale: float = 1.0, row_h: int = 23):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.row_h = p(row_h)
        self.steps = []
        self.active = -1
        super().__init__(parent, width=p(190), bg=T.PANEL, highlightthickness=0)
        self.bind("<Configure>", lambda e: self.redraw())

    def set_steps(self, steps, active: int = -1) -> None:
        """steps: [(time_text, label), ...]"""
        if steps != self.steps or active != self.active:
            self.steps, self.active = steps, active
            self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 40 or h < 20:
            return
        p = self._p
        if not self.steps:
            self.create_text(w / 2, h / 2, text="no sequence loaded", fill=T.TXT_MUTE,
                             font=T.ui_font(8))
            return
        visible = max(1, int((h - p(10)) // self.row_h))
        first = 0
        if self.active >= visible - 1:
            first = min(len(self.steps) - visible, self.active - visible // 2)
        first = max(0, first)
        rail = p(12)
        for i in range(first, min(len(self.steps), first + visible)):
            t, label = self.steps[i]
            y = p(12) + (i - first) * self.row_h
            if i > first:
                self.create_line(rail, y - self.row_h, rail, y,
                                 fill=T.blend(T.BORDER, T.PANEL, 0.2))
            live = i == self.active
            r = p(5) if live else p(3)
            if live:
                for g in T.glow_ramp(T.CYAN, T.PANEL, 2):
                    self.create_oval(rail - r - p(3), y - r - p(3), rail + r + p(3),
                                     y + r + p(3), fill=g, outline="")
            self.create_oval(rail - r, y - r, rail + r, y + r,
                             fill=T.CYAN if live else T.blend(T.BORDER, T.CYAN, 0.35),
                             outline="")
            self.create_text(rail + p(14), y, text=t, anchor="w",
                             fill=T.TXT_DIM if live else T.TXT_MUTE,
                             font=T.mono_font(8))
            self.create_text(rail + p(62), y, text=label, anchor="w",
                             fill=T.CYAN if live else T.TXT_DIM,
                             font=T.ui_font(8, "bold" if live else "normal"))


# -------------------------------------------------------------- chess board
class ChessBoard(tk.Canvas):
    """Clickable board. Pieces are Unicode glyphs; the widget knows nothing
    about rules - it reports clicks and paints whatever position it is given."""

    GLYPH = {"P": "♙", "N": "♘", "B": "♗", "R": "♖", "Q": "♕", "K": "♔",
             "p": "♟", "n": "♞", "b": "♝", "r": "♜", "q": "♛", "k": "♚"}

    def __init__(self, parent, scale: float = 1.0, on_square=None, flipped: bool = False):
        p = lambda n: int(round(n * scale))
        self._p = p
        self.on_square = on_square
        self.flipped = flipped
        self.position: dict = {}          # "e4" -> "P"
        self.selected = None
        self.targets: set = set()
        self.last_move: tuple = ()
        self.check_sq = None
        self.changed: set = set()         # camera-observed squares, drawn as rings
        super().__init__(parent, width=p(300), height=p(300), bg=T.PANEL,
                         highlightthickness=0, cursor="hand2")
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda e: self.redraw())

    # -- geometry ----------------------------------------------------------
    def _cell(self):
        w, h = self.winfo_width(), self.winfo_height()
        s = max(8, (min(w, h) - self._p(8)) // 8)
        ox = (w - s * 8) // 2
        oy = (h - s * 8) // 2
        return s, ox, oy

    def _square_at(self, x, y):
        s, ox, oy = self._cell()
        col, row = (x - ox) // s, (y - oy) // s
        if not (0 <= col < 8 and 0 <= row < 8):
            return None
        f = 7 - col if self.flipped else col
        r = row if self.flipped else 7 - row
        return "abcdefgh"[f] + str(r + 1)

    def _xy(self, square):
        s, ox, oy = self._cell()
        f = "abcdefgh".index(square[0])
        r = int(square[1]) - 1
        col = 7 - f if self.flipped else f
        row = r if self.flipped else 7 - r
        return ox + col * s, oy + row * s, s

    def _click(self, e):
        sq = self._square_at(e.x, e.y)
        if sq and self.on_square:
            self.on_square(sq)

    # -- state -------------------------------------------------------------
    def set_position(self, position: dict, last_move=(), check_sq=None) -> None:
        self.position = dict(position)
        self.last_move = tuple(last_move)
        self.check_sq = check_sq
        self.redraw()

    def set_selection(self, square, targets) -> None:
        self.selected = square
        self.targets = set(targets or ())
        self.redraw()

    def set_changed(self, squares) -> None:
        self.changed = set(squares or ())
        self.redraw()

    # -- paint -------------------------------------------------------------
    def redraw(self) -> None:
        self.delete("all")
        s, ox, oy = self._cell()
        if s < 8:
            return
        p = self._p
        light, dark = T.blend(T.FIELD, T.CYAN, 0.22), T.blend(T.FIELD, T.PANEL, 0.35)
        glyph_font = T.ui_font(max(8, int(s * 0.62 / max(1.0, self._p(1)))), "normal")
        for r in range(8):
            for f in range(8):
                sq = "abcdefgh"[f] + str(r + 1)
                x, y, _ = self._xy(sq)
                fill = light if (r + f) % 2 else dark
                if sq in self.last_move:
                    fill = T.blend(fill, T.AMBER, 0.32)
                if sq == self.selected:
                    fill = T.blend(fill, T.CYAN, 0.55)
                if sq == self.check_sq:
                    fill = T.blend(fill, T.RED, 0.55)
                self.create_rectangle(x, y, x + s, y + s, fill=fill, outline="")
                if sq in self.targets:
                    r_ = s * 0.16
                    self.create_oval(x + s / 2 - r_, y + s / 2 - r_, x + s / 2 + r_,
                                     y + s / 2 + r_, fill=T.dim(T.CYAN, 0.35), outline="")
                if sq in self.changed:
                    self.create_rectangle(x + 2, y + 2, x + s - 2, y + s - 2,
                                          outline=T.MAGENTA, width=2)
                piece = self.position.get(sq)
                if piece:
                    white = piece.isupper()
                    self.create_text(x + s / 2 + 1, y + s / 2 + 1, text=self.GLYPH[piece],
                                     fill="#000000", font=glyph_font)
                    self.create_text(x + s / 2, y + s / 2, text=self.GLYPH[piece],
                                     fill="#f4f6fa" if white else "#1a1f2b", font=glyph_font)
        # coordinates
        for i in range(8):
            f = "abcdefgh"[7 - i if self.flipped else i]
            r = str(i + 1 if self.flipped else 8 - i)
            self.create_text(ox + i * s + p(4), oy + 8 * s - p(5), text=f, anchor="sw",
                             fill=T.TXT_MUTE, font=T.ui_font(6))
            self.create_text(ox + p(3), oy + i * s + p(3), text=r, anchor="nw",
                             fill=T.TXT_MUTE, font=T.ui_font(6))
        self.create_rectangle(ox, oy, ox + 8 * s, oy + 8 * s, outline=T.BORDER_HI)
