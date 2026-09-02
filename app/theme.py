"""Palette, fonts and colour maths for the HUD interface.

Tk has no alpha channel, so every glow and every dimmed tone here is a solid
colour blended against the background up front. `blend` and `glow_ramp` do that
mixing once; widgets just draw with the results.
"""

from __future__ import annotations

import tkinter.font as tkfont
from typing import List, Tuple

# ------------------------------------------------------------------ palette
VOID      = "#03060c"      # window backdrop
BG        = "#05090f"      # page background
PANEL     = "#080e18"      # panel fill
PANEL_HI  = "#0b1524"      # raised / hovered fill
FIELD     = "#060c15"      # inset fill (tracks, inputs, tables)
BORDER    = "#123048"      # panel edge
BORDER_HI = "#1d5b80"      # active edge

TXT       = "#d8f3ff"      # primary text
TXT_DIM   = "#6c8ea3"      # secondary text
TXT_MUTE  = "#3a5566"      # tertiary / disabled

CYAN      = "#00e2ff"      # primary accent
TEAL      = "#00ffc8"
MAGENTA   = "#ff2f92"      # record
AMBER     = "#ffb020"      # gripper / warnings
GREEN     = "#00ff9c"      # ok
RED       = "#ff3b5c"      # error
VIOLET    = "#9d6bff"      # secondary data

JOINT_COLOURS = (CYAN, CYAN, "#35c8ff", "#5aa8ff", VIOLET, AMBER)


# ------------------------------------------------------------ colour maths
def _to_rgb(c: str) -> Tuple[int, int, int]:
    c = c.lstrip("#")
    if len(c) == 3:                       # "#fff" shorthand
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        raise ValueError(f"not a hex colour: #{c}")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def _to_hex(rgb) -> str:
    r, g, b = (max(0, min(255, int(round(v)))) for v in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def blend(a: str, b: str, t: float) -> str:
    """Mix `a` toward `b`; t=0 gives a, t=1 gives b."""
    t = max(0.0, min(1.0, t))
    ra, ga, ba = _to_rgb(a)
    rb, gb, bb = _to_rgb(b)
    return _to_hex((ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t))


def dim(colour: str, amount: float, over: str = BG) -> str:
    """Fade a colour toward the background - our stand-in for opacity."""
    return blend(colour, over, amount)


def glow_ramp(colour: str, over: str = BG, steps: int = 3) -> List[str]:
    """Colours for stacked strokes, faintest first, that read as a glow."""
    return [blend(colour, over, 0.86 - 0.4 * i / max(1, steps - 1)) for i in range(steps)]


# ------------------------------------------------------------------- fonts
_FAMILY_UI = None
_FAMILY_MONO = None


def init_fonts(root) -> None:
    """Pick the best available families once a Tk root exists."""
    global _FAMILY_UI, _FAMILY_MONO
    have = set(tkfont.families(root))
    for name in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Roboto", "DejaVu Sans"):
        if name in have:
            _FAMILY_UI = name
            break
    else:
        _FAMILY_UI = "TkDefaultFont"
    for name in ("Cascadia Mono", "Consolas", "JetBrains Mono", "DejaVu Sans Mono", "Courier New"):
        if name in have:
            _FAMILY_MONO = name
            break
    else:
        _FAMILY_MONO = "TkFixedFont"


def ui_font(size: int = 10, weight: str = "normal") -> tuple:
    return (_FAMILY_UI or "Segoe UI", size, weight)


def mono_font(size: int = 10, weight: str = "normal") -> tuple:
    return (_FAMILY_MONO or "Consolas", size, weight)


def spaced(text: str, gap: str = " ") -> str:
    """Letter-spaced caps for panel headers - Tk cannot letter-space natively."""
    return gap.join(text.upper())
