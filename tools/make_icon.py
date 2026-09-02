"""Render the application icon.

Windows shows this in the taskbar, the Start menu, the installer and the shield
prompt, at sizes from 16 px up, so the glyph is a plain three-segment silhouette
that survives being shrunk rather than a detailed drawing of the arm.

    python tools/make_icon.py

Writes app/assets/icon.ico (and icon.png for the docs).
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import theme as T                                                   # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "assets")
S = 1024                                    # drawn large, downsampled per icon size
SIZES = [16, 24, 32, 48, 64, 128, 256]

SHOULDER = (430, 742)
ELBOW = (322, 432)
WRIST = (612, 306)
TIP_A = (762, 214)
TIP_B = (742, 386)


def _rounded(draw, box, radius, fill, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _arm(draw, upper_w, fore_w, prong_w, colour_upper, colour_fore, colour_grip):
    draw.line([SHOULDER, ELBOW], fill=colour_upper, width=upper_w, joint="curve")
    draw.line([ELBOW, WRIST], fill=colour_fore, width=fore_w, joint="curve")
    draw.line([WRIST, TIP_A], fill=colour_grip, width=prong_w, joint="curve")
    draw.line([WRIST, TIP_B], fill=colour_grip, width=prong_w, joint="curve")
    for (x, y), r in ((SHOULDER, upper_w // 2), (ELBOW, upper_w // 2), (WRIST, fore_w // 2)):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=colour_upper)


def render() -> Image.Image:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Backdrop: the app's own panel colour, so the icon reads as part of the HUD.
    _rounded(draw, [0, 0, S - 1, S - 1], 190, fill=T.PANEL, outline=T.BORDER, width=10)
    _rounded(draw, [26, 26, S - 27, S - 27], 168, fill=None, outline="#0d2438", width=4)

    # Floor line under the arm, matching the kinematic view's horizon.
    draw.line([150, 812, S - 150, 812], fill="#0f3a54", width=8)

    # Glow pass: the same silhouette, fatter and blurred, laid under the solid one.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    _arm(ImageDraw.Draw(glow), 128, 112, 78, T.CYAN, "#35c8ff", T.AMBER)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(34)))

    # Pedestal.
    _rounded(draw, [330, 748, 694, 856], 42, fill="#0d2033", outline=T.BORDER_HI, width=7)
    _rounded(draw, [386, 700, 638, 772], 32, fill="#102b42", outline=T.BORDER_HI, width=6)

    _arm(draw, 74, 60, 30, T.CYAN, "#35c8ff", T.AMBER)

    # Bright cores on the joints - the one detail that still reads at 32 px.
    for (x, y), r in ((SHOULDER, 17), (ELBOW, 17), (WRIST, 13)):
        draw.ellipse([x - r, y - r, x + r, y + r], fill="#c9f7ff")
    return img


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    img = render()
    png = os.path.join(OUT_DIR, "icon.png")
    ico = os.path.join(OUT_DIR, "icon.ico")
    img.resize((256, 256), Image.LANCZOS).save(png)
    img.save(ico, format="ICO", sizes=[(s, s) for s in SIZES])
    print("wrote", os.path.normpath(ico), "and", os.path.normpath(png))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
