"""Real-time 3D view of the arm, drawn as shaded polygons on a Tk canvas.

Deliberately dependency-free: no numpy, no GL context. The model is a few
hundred quads, so plain Python vector maths keeps up comfortably at 20 fps and
the arm app stays installable with nothing but pyserial.

The parts hang off a kinematic chain driven by the same joint angles the arm
receives, so what you see is the commanded pose rather than an animation.
Convention matches the rest of the app: Z is up, links extend along local +Z,
and a joint at 90 deg means "straight on" (no bend).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

import mesh
import theme as T

Vec = Tuple[float, float, float]
Mat = Tuple[float, ...]          # row-major 4x4

# --------------------------------------------------------------- matrix maths
IDENTITY: Mat = (1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


def mat_mul(a: Mat, b: Mat) -> Mat:
    out = []
    for r in range(4):
        ar = a[r * 4:r * 4 + 4]
        for c in range(4):
            out.append(ar[0] * b[c] + ar[1] * b[4 + c] + ar[2] * b[8 + c] + ar[3] * b[12 + c])
    return tuple(out)


def mat_translate(x: float, y: float, z: float) -> Mat:
    return (1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1)


def mat_rot_x(deg: float) -> Mat:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (1, 0, 0, 0, 0, c, -s, 0, 0, s, c, 0, 0, 0, 0, 1)


def mat_rot_y(deg: float) -> Mat:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, 0, s, 0, 0, 1, 0, 0, -s, 0, c, 0, 0, 0, 0, 1)


def mat_rot_z(deg: float) -> Mat:
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return (c, -s, 0, 0, s, c, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)


def apply(m: Mat, p: Vec) -> Vec:
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(v: Vec) -> Vec:
    n = math.sqrt(dot(v, v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


# --------------------------------------------------------------------- solids
def box(w: float, d: float, h: float, cx: float = 0, cy: float = 0, cz: float = 0):
    """Axis-aligned box centred on (cx, cy) spanning cz..cz+h. Returns verts, quads."""
    x0, x1 = cx - w / 2, cx + w / 2
    y0, y1 = cy - d / 2, cy + d / 2
    z0, z1 = cz, cz + h
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    q = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4),
         (2, 3, 7, 6), (1, 2, 6, 5), (3, 0, 4, 7)]
    return v, q


def xform(solid, m: Mat):
    """Bake a transform into a solid's vertices - used to lay joint barrels on
    their side so they read as real rotary joints rather than upright cans."""
    verts, quads = solid
    return [apply(m, v) for v in verts], quads


def prism(radius: float, h: float, sides: int = 14, cz: float = 0, taper: float = 1.0):
    """Regular prism standing on cz, optionally tapering toward the top."""
    v, q = [], []
    for i in range(sides):
        a = 2 * math.pi * i / sides
        v.append((radius * math.cos(a), radius * math.sin(a), cz))
    for i in range(sides):
        a = 2 * math.pi * i / sides
        v.append((radius * taper * math.cos(a), radius * taper * math.sin(a), cz + h))
    for i in range(sides):
        j = (i + 1) % sides
        q.append((i, j, sides + j, sides + i))
    q.append(tuple(range(sides - 1, -1, -1)))          # bottom cap
    q.append(tuple(range(sides, sides * 2)))           # top cap
    return v, q


# ------------------------------------------------------------------ the model
class Part:
    __slots__ = ("verts", "quads", "colour", "group")

    def __init__(self, verts, quads, colour: str, group: str):
        self.verts = verts
        self.quads = quads
        self.colour = colour
        self.group = group


STEEL = "#5d6b7f"
DARK = "#151b26"
BLUE = "#1f7ae0"
BLUE_DK = "#155aa8"
JAW = "#9aa6b8"
ACCENT = "#e8462f"

# Link lengths in model units. Links are deliberately short relative to their
# cross-section - a 5:1 box reads as a mast, a 2.5:1 one reads as a robot arm.
H_BASE, L1, L2, L3 = 0.74, 1.05, 0.90, 0.38

_BARREL = mat_rot_x(90.0)        # lay a prism on its side, axis along Y


def _pose_frames(pose: Sequence[float]) -> Dict[str, Mat]:
    """Transform for each link in the chain, from the joint angles."""
    base, shoulder, elbow, wpitch, wroll = (float(pose[i]) for i in range(5))

    f_base = mat_rot_z(base - 90.0)
    f_col = mat_mul(f_base, mat_translate(0, 0, 0))
    # A link points along +Z; 90 deg means no bend, so rotate by (90 - angle).
    f_upper = mat_mul(mat_mul(f_base, mat_translate(0, 0, H_BASE)),
                      mat_rot_y(90.0 - shoulder))
    f_fore = mat_mul(mat_mul(f_upper, mat_translate(0, 0, L1)),
                     mat_rot_y(90.0 - elbow))
    f_wrist = mat_mul(mat_mul(f_fore, mat_translate(0, 0, L2)),
                      mat_rot_y(90.0 - wpitch))
    f_tool = mat_mul(f_wrist, mat_rot_z(wroll - 90.0))
    return {"base": f_base, "column": f_col, "upper": f_upper,
            "fore": f_fore, "wrist": f_wrist, "tool": f_tool}


def build_parts(pose: Sequence[float]) -> List[Tuple[Part, Mat]]:
    """The arm as (part, transform) pairs for the current pose."""
    f = _pose_frames(pose)
    grip = float(pose[5])
    out: List[Tuple[Part, Mat]] = []

    def add(solid, colour, group, frame):
        v, q = solid
        out.append((Part(v, q, colour, group), frame))

    # --- pedestal: wide floor pad, dark ring, then the rotating turntable
    add(prism(0.86, 0.07, 24, cz=-0.07), STEEL, "base", IDENTITY)
    add(prism(0.70, 0.17, 22, cz=0.0), DARK, "base", IDENTITY)
    add(prism(0.62, 0.26, 22, cz=0.17, taper=0.93), STEEL, "base", f["base"])

    # --- shoulder pedestal and its motor box
    add(box(0.52, 0.60, H_BASE - 0.43, cz=0.43), BLUE, "base", f["base"])
    add(box(0.20, 0.30, 0.24, cx=-0.34, cz=0.50), DARK, "base", f["base"])
    add(box(0.54, 0.10, 0.05, cz=H_BASE - 0.13), ACCENT, "base", f["base"])

    # --- shoulder joint barrel, lying across the arm
    add(xform(prism(0.27, 0.52, 16, cz=-0.26), _BARREL), DARK, "shoulder",
        mat_mul(f["base"], mat_translate(0, 0, H_BASE)))

    # --- upper arm: thick blue body with a darker spine and end housings
    add(box(0.40, 0.44, L1 - 0.16, cz=0.08), BLUE, "upper", f["upper"])
    add(box(0.44, 0.16, L1 - 0.34, cz=0.17), BLUE_DK, "upper", f["upper"])
    add(box(0.12, 0.46, 0.06, cz=L1 * 0.55), ACCENT, "upper", f["upper"])

    # --- elbow barrel
    add(xform(prism(0.23, 0.46, 16, cz=-0.23), _BARREL), DARK, "elbow",
        mat_mul(f["upper"], mat_translate(0, 0, L1)))

    # --- forearm
    add(box(0.33, 0.37, L2 - 0.14, cz=0.07), BLUE, "fore", f["fore"])
    add(box(0.37, 0.13, L2 - 0.30, cz=0.15), BLUE_DK, "fore", f["fore"])
    add(box(0.16, 0.26, 0.20, cx=0.0, cy=-0.24, cz=L2 * 0.30), DARK, "fore", f["fore"])

    # --- wrist barrel, block and roll collar
    add(xform(prism(0.18, 0.34, 14, cz=-0.17), _BARREL), DARK, "wrist",
        mat_mul(f["fore"], mat_translate(0, 0, L2)))
    add(box(0.26, 0.28, L3 - 0.06, cz=0.04), BLUE, "wrist", f["wrist"])
    add(prism(0.17, 0.11, 16, cz=L3 - 0.08), STEEL, "wrist", f["tool"])

    # --- gripper: two jaws that open with the gripper angle
    span = 0.06 + 0.13 * max(0.0, min(1.0, (grip - 10.0) / 100.0))
    add(box(0.30, 0.20, 0.08, cz=L3 + 0.02), DARK, "gripper", f["tool"])
    for side in (1, -1):
        add(box(0.07, 0.17, 0.24, cx=side * span, cz=L3 + 0.10), JAW, "gripper", f["tool"])
        add(box(0.06, 0.15, 0.07, cx=side * (span - 0.035), cz=L3 + 0.34), JAW,
            "gripper", f["tool"])
    return out


def tool_point(pose: Sequence[float]) -> Vec:
    f = _pose_frames(pose)
    return apply(f["tool"], (0, 0, L3 + 0.41))


# Joint centres, for the labelled axis diagram.
JOINT_ORDER = ("base", "shoulder", "elbow", "wrist_pitch", "wrist_roll", "gripper")


def joint_points(pose: Sequence[float]) -> Dict[str, Vec]:
    m = mesh.get_mesh()
    if m is not None:
        return mesh_joint_points(pose, m)   # labels must follow whatever is drawn
    f = _pose_frames(pose)
    return {
        "base": apply(f["base"], (0, 0, 0.30)),
        "shoulder": apply(f["base"], (0, 0, H_BASE)),
        "elbow": apply(f["upper"], (0, 0, L1)),
        "wrist_pitch": apply(f["fore"], (0, 0, L2)),
        "wrist_roll": apply(f["wrist"], (0, 0, L3 - 0.02)),
        "gripper": tool_point(pose),
    }


# ----------------------------------------------------------------- rendering
VIEWS = {"FRONT": (0.0, 6.0), "SIDE": (90.0, 6.0), "TOP": (0.0, 84.0),
         "ISO": (38.0, 22.0)}
LIGHT = norm((-0.45, -0.62, 0.85))

# Shading a face used to call T.blend twice, and each call parses and formats hex
# strings. With hundreds of faces a frame that is real time, so the ramps are
# precomputed once per colour and indexed by light level.
_SHADE_STEPS = 32
_SHADE_CACHE: Dict[str, list] = {}


def _shades(colour: str) -> list:
    lut = _SHADE_CACHE.get(colour)
    if lut is None:
        dark = T.blend(colour, "#05070c", 0.62)
        lut = []
        for i in range(_SHADE_STEPS):
            fill = T.blend(dark, colour, i / (_SHADE_STEPS - 1.0))
            lut.append((fill, T.blend(fill, "#000000", 0.35)))
        _SHADE_CACHE[colour] = lut
    return lut


class Renderer:
    """Projects and paints the arm onto a canvas."""

    def __init__(self):
        self.az, self.el = VIEWS["ISO"]
        self.dist = 4.35
        self.target: Vec = (0.0, 0.0, 1.35)
        self._fitted = False
        self.mode = "REALTIME"
        self.isolate: Optional[str] = None
        self.use_mesh = True
        # The pedestal and floor are nearly half the polygons and only change
        # when the camera does, so they live on their own cached canvas layer.
        self._static_key = None
        self.faces = 0

    def model_parts(self, pose: Sequence[float]):
        """Scanned mesh when it loads, hand-built solids otherwise."""
        if self.use_mesh:
            m = mesh.get_mesh()
            if m is not None:
                return build_mesh_parts(pose, m)
        return build_parts(pose)

    def set_view(self, name: str) -> None:
        if name in VIEWS:
            self.az, self.el = VIEWS[name]

    def orbit(self, d_az: float, d_el: float) -> None:
        self.az = (self.az + d_az) % 360.0
        self.el = max(-15.0, min(88.0, self.el + d_el))

    def zoom(self, factor: float) -> None:
        self.dist = max(2.6, min(14.0, self.dist * factor))

    # -- camera ------------------------------------------------------------
    def fit(self, pose: Sequence[float]) -> None:
        """Frame the model: centre on its bounds and back off far enough to see it."""
        pts = [apply(f, v) for part, f in self.model_parts(pose) for v in part.verts]
        if not pts:
            return
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        self.target = tuple((lo[i] + hi[i]) / 2.0 for i in range(3))
        radius = max(math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3))) / 2.0, 0.5)
        self.dist = radius * 1.95
        self._fitted = True
        self._static_key = None

    def _camera(self, w: int, h: int):
        target = self.target
        ca, sa = math.cos(math.radians(self.az)), math.sin(math.radians(self.az))
        ce, se = math.cos(math.radians(self.el)), math.sin(math.radians(self.el))
        fwd = (ce * ca, ce * sa, se)                       # target -> eye
        eye = (target[0] + fwd[0] * self.dist, target[1] + fwd[1] * self.dist,
               target[2] + fwd[2] * self.dist)
        right = norm(cross((0.0, 0.0, 1.0), fwd))
        up = cross(fwd, right)
        focal = min(w, h) * 0.95
        return eye, right, up, fwd, focal, w * 0.5, h * 0.52

    def project_world(self, p: Vec, w: int, h: int):
        """Screen position of a world point, for overlay labels."""
        return self._project(self._camera(w, h), p)

    def _project(self, cam, p: Vec):
        eye, right, up, fwd, focal, cx, cy = cam
        v = sub(p, eye)
        z = -dot(v, fwd)                                   # depth in front of eye
        if z < 0.05:
            return None
        return (cx + focal * dot(v, right) / z, cy - focal * dot(v, up) / z, z)

    # -- paint -------------------------------------------------------------
    def draw(self, canvas, pose: Sequence[float], w: int, h: int,
             show_floor: bool = True) -> None:
        if not self._fitted:
            self.fit(pose)
        cam = self._camera(w, h)
        static_key = (round(self.az, 2), round(self.el, 2), round(self.dist, 3),
                      w, h, self.mode, self.isolate, show_floor)
        redraw_static = static_key != self._static_key
        if redraw_static:
            self._static_key = static_key
            canvas.delete("static3d")
            if show_floor:
                self._draw_floor(canvas, cam)

        canvas.delete("dyn3d")
        faces = []
        for part, frame in self.model_parts(pose):
            if part.group == "pedestal" and not redraw_static:
                continue                           # already on the static layer
            world = [apply(frame, v) for v in part.verts]
            screen = [self._project(cam, p) for p in world]
            for quad in part.quads:
                pts = [screen[i] for i in quad]
                if any(p is None for p in pts):
                    continue
                wpts = [world[i] for i in quad]
                normal = norm(cross(sub(wpts[1], wpts[0]), sub(wpts[2], wpts[0])))
                eye = cam[0]
                if dot(normal, sub(wpts[0], eye)) > 0:     # back face
                    continue
                depth = sum(p[2] for p in pts) / len(pts)
                faces.append((depth, pts, normal, part.colour, part.group))
        self._paint(canvas, faces)
        if self.mode != "WIREFRAME" and self.isolate is None:
            self._draw_tool_marker(canvas, cam, pose)

    def _paint(self, canvas, faces) -> None:
        faces.sort(key=lambda f: -f[0])                    # painter's algorithm
        self.faces = len(faces)
        wire = self.mode == "WIREFRAME"
        ghost = self.mode == "TRANSPARENT"

        for _depth, pts, normal, colour, group in faces:
            tag = "static3d" if group == "pedestal" else "dyn3d"
            flat = [c for p in pts for c in p[:2]]
            dim = self.isolate is not None and group != self.isolate
            if wire:
                canvas.create_polygon(flat, fill="", outline=T.dim(T.CYAN, 0.7 if dim else 0.3),
                                      width=1, tags=tag)
                continue
            lit = 0.30 + 0.70 * max(0.0, dot(normal, LIGHT))
            shade, edge = _shades(colour)[int(lit * (_SHADE_STEPS - 1))]
            if dim:
                shade = edge = T.blend(shade, T.VOID, 0.72)
            if ghost:
                canvas.create_polygon(flat, fill=T.blend(shade, T.VOID, 0.72),
                                      outline=T.dim(T.CYAN, 0.55), width=1, tags=tag)
            else:
                canvas.create_polygon(flat, fill=shade, outline=edge, width=1, tags=tag)

        canvas.tag_lower("static3d")

    def _draw_floor(self, canvas, cam) -> None:
        grid = T.blend(T.BORDER, T.VOID, 0.45)
        span, step = 3.2, 0.4
        n = int(span / step)
        for i in range(-n, n + 1):
            x = i * step
            a = self._project(cam, (x, -span, 0.0))
            b = self._project(cam, (x, span, 0.0))
            if a and b:
                canvas.create_line(a[0], a[1], b[0], b[1], fill=grid, tags="static3d")
            a = self._project(cam, (-span, x, 0.0))
            b = self._project(cam, (span, x, 0.0))
            if a and b:
                canvas.create_line(a[0], a[1], b[0], b[1], fill=grid, tags="static3d")
        # glow rings under the pedestal, like a machine pad
        for r, fade in ((0.95, 0.55), (1.35, 0.72), (1.8, 0.84)):
            pts = []
            for i in range(41):
                a = 2 * math.pi * i / 40
                p = self._project(cam, (r * math.cos(a), r * math.sin(a), 0.005))
                if p:
                    pts.extend(p[:2])
            if len(pts) >= 6:
                canvas.create_line(pts, fill=T.dim(T.CYAN, fade), smooth=True, tags="static3d")

    def _draw_tool_marker(self, canvas, cam, pose) -> None:
        p = self._project(cam, tool_point(pose))
        if not p:
            return
        x, y = p[0], p[1]
        canvas.create_oval(x - 11, y - 11, x + 11, y + 11,
                           outline=T.dim(T.TEAL, 0.5), width=1, tags="dyn3d")
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            canvas.create_line(x + dx * 6, y + dy * 6, x + dx * 13, y + dy * 13,
                               fill=T.TEAL, tags="dyn3d")


# =========================================================== mesh-backed model
def mat_scale(s: float) -> Mat:
    return (s, 0, 0, 0, 0, s, 0, 0, 0, 0, s, 0, 0, 0, 0, 1)


def rot_about(rot: Mat, point: Vec) -> Mat:
    """Apply a rotation around an arbitrary point rather than the origin."""
    return mat_mul(mat_translate(*point),
                   mat_mul(rot, mat_translate(-point[0], -point[1], -point[2])))


def mat_rot_axis(axis: Vec, deg: float) -> Mat:
    """Rodrigues rotation about an arbitrary unit axis - used for the wrist roll,
    whose axis follows the forearm rather than a world axis."""
    x, y, z = norm(axis)
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    t = 1.0 - c
    return (t * x * x + c, t * x * y - s * z, t * x * z + s * y, 0,
            t * x * y + s * z, t * y * y + c, t * y * z - s * x, 0,
            t * x * z - s * y, t * y * z + s * x, t * z * z + c, 0,
            0, 0, 0, 1)


def build_mesh_parts(pose: Sequence[float], m) -> List[Tuple[Part, Mat]]:
    """The scanned arm, posed by the joint angles.

    Each pitch joint rotates about X through its own pivot, and the authored bend
    baked into the mesh is subtracted so that "all 90" straightens the arm, the
    same convention the procedural model and the 2D views use.
    """
    base, shoulder, elbow, wpitch, wroll, grip = (float(pose[i]) for i in range(6))

    def bend(name: str, angle: float) -> Mat:
        return rot_about(mat_rot_x(angle - 90.0), m.pivot[name])

    f_ped = IDENTITY
    f_turret = rot_about(mat_rot_z(base - 90.0), m.pivot["turret"])
    f_upper = mat_mul(f_turret, bend("upper", shoulder))
    f_fore = mat_mul(f_upper, bend("fore", elbow))
    f_wrist = mat_mul(f_fore, bend("wrist", wpitch))

    # roll turns the tool about the line running out through the gripper
    axis = sub(m.pivot["gripper"], m.pivot["wrist"])
    f_grip = mat_mul(mat_mul(f_wrist, bend("gripper", 90.0)),
                     rot_about(mat_rot_axis(axis, wroll - 90.0), m.pivot["gripper"]))

    frames = {"pedestal": f_ped, "turret": f_turret, "upper": f_upper,
              "fore": f_fore, "wrist": f_wrist, "gripper": f_grip}

    # place the model on the floor at the origin, scaled into view units
    place = mat_mul(mat_scale(m.scale),
                    mat_translate(-m.pivot["turret"][0], -m.pivot["turret"][1], -m.floor))

    out: List[Tuple[Part, Mat]] = []
    open_frac = max(0.0, min(1.0, (grip - 10.0) / 100.0))
    jaw_shift = m.jaw_reach * 0.45 * open_frac
    for name in mesh.GROUP_ORDER:
        tris = m.groups[name]
        if not tris:
            continue
        colour = mesh.GROUP_COLOURS[name]
        frame = mat_mul(place, frames[name])
        if name == "gripper":
            # jaws are mirrored pairs; slide each outward as the gripper opens
            for i, t in enumerate(tris):
                side = m.jaw_side.get(i, 1)
                shifted = mat_mul(frame, mat_translate(side * jaw_shift, 0, 0))
                out.append((Part(list(t), [tuple(range(len(t)))], colour, name),
                            shifted))
        else:
            verts: List[Vec] = []
            quads = []
            for t in tris:
                k = len(verts)
                verts.extend(t)
                # faces are triangles or merged quads, so index by actual length
                quads.append(tuple(range(k, k + len(t))))
            out.append((Part(verts, quads, colour, name), frame))
    return out


def mesh_joint_points(pose: Sequence[float], m) -> Dict[str, Vec]:
    """Joint centres of the posed mesh, for the labelled axis diagram."""
    parts = build_mesh_parts(pose, m)
    frames = {}
    for part, frame in parts:
        frames.setdefault(part.group, frame)
    names = ("turret", "upper", "fore", "wrist", "gripper")
    out = {}
    for joint, group in zip(JOINT_ORDER, names + ("gripper",)):
        frame = frames.get(group)
        out[joint] = apply(frame, m.pivot[group]) if frame else (0.0, 0.0, 0.0)
    tip = frames.get("gripper")
    if tip:
        p = m.pivot["gripper"]
        out["gripper"] = apply(tip, (p[0], p[1] + m.link_len["gripper"], p[2]))
    return out
