"""Load the arm's STL mesh and rig it to the joint angles.

An STL is loose triangles: no hierarchy, no joint origins, no materials. What it
does have here is separable solids, so the rig is recovered geometrically:

1. weld vertices and flood-fill to find connected components;
2. order those components along the arm using arc = y + z, which is monotonic
   for this model because the arm rises in Z and then reaches out in Y;
3. cut the ordered list into the six joint groups;
4. take each group's root and tip as the mean of its lowest / highest arc
   vertices, giving a pivot and a link direction;
5. store the direction the model was authored in, so a commanded 90 deg (the
   app's "no bend") straightens the arm rather than showing the exported pose.

Everything is plain Python - the mesh is under a thousand triangles, so it draws
inside the frame budget without numpy.
"""

from __future__ import annotations

import math
import os
import struct
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]
Tri = Tuple[Vec, Vec, Vec]

MESH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets",
                         "robotic_arm_6axis.stl")

# Ordered down the chain. "pedestal" never moves; the rest hang off a joint.
GROUP_ORDER = ("pedestal", "turret", "upper", "fore", "wrist", "gripper")

# Fractions of the total arc length where one group ends and the next begins.
# Derived from this model's component layout; a different arm needs different
# cuts, which is why the procedural model stays as a fallback.
GROUP_CUTS = (0.115, 0.21, 0.45, 0.72, 0.965)

GROUP_COLOURS = {
    "pedestal": "#5d6b7f",
    "turret": "#1f7ae0",
    "upper": "#1f7ae0",
    "fore": "#2f88ee",
    "wrist": "#155aa8",
    "gripper": "#9aa6b8",
}


# ------------------------------------------------------------------- loading
def load_stl(path: str) -> List[Tri]:
    """Read binary or ASCII STL into a triangle list."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if len(raw) < 84:
        raise ValueError("STL too short")

    count = struct.unpack_from("<I", raw, 80)[0]
    if len(raw) == 84 + 50 * count:                       # binary
        tris = []
        off = 84
        for _ in range(count):
            v = struct.unpack_from("<12fH", raw, off)
            off += 50
            tris.append((v[3:6], v[6:9], v[9:12]))
        return tris

    text = raw.decode("ascii", "ignore")                  # ASCII fallback
    verts: List[Vec] = []
    tris = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(verts) == 3:
                tris.append((verts[0], verts[1], verts[2]))
                verts = []
    if not tris:
        raise ValueError("no triangles found")
    return tris


def connected_components(tris: List[Tri], quant: float = 1e-4) -> List[List[int]]:
    """Triangle indices grouped into separate solids, joined by shared vertices."""
    def key(v: Vec):
        return (round(v[0] / quant), round(v[1] / quant), round(v[2] / quant))

    at_vertex = defaultdict(list)
    for i, t in enumerate(tris):
        for v in t:
            at_vertex[key(v)].append(i)

    seen = [False] * len(tris)
    out = []
    for start in range(len(tris)):
        if seen[start]:
            continue
        stack, comp = [start], []
        seen[start] = True
        while stack:
            i = stack.pop()
            comp.append(i)
            for v in tris[i]:
                for j in at_vertex[key(v)]:
                    if not seen[j]:
                        seen[j] = True
                        stack.append(j)
        out.append(comp)
    return out


# ------------------------------------------------------------------ rigging
def _arc(v: Vec) -> float:
    """Position along the arm. Works because this model rises then reaches out."""
    return v[1] + v[2]


def _mean(points: Sequence[Vec]) -> Vec:
    n = max(1, len(points))
    return (sum(p[0] for p in points) / n, sum(p[1] for p in points) / n,
            sum(p[2] for p in points) / n)


def _end_points(verts: Sequence[Vec], frac: float = 0.22) -> Tuple[Vec, Vec]:
    """Mean of the lowest-arc and highest-arc vertices: a link's root and tip."""
    arcs = [_arc(v) for v in verts]
    lo, hi = min(arcs), max(arcs)
    span = max(1e-6, hi - lo)
    root = [v for v, a in zip(verts, arcs) if a <= lo + frac * span]
    tip = [v for v, a in zip(verts, arcs) if a >= hi - frac * span]
    return _mean(root or list(verts)), _mean(tip or list(verts))


class ArmMesh:
    """A rigged mesh: triangles grouped per joint, with pivots and rest angles."""

    def __init__(self, tris: List[Tri]):
        self.groups: Dict[str, List[Tri]] = {name: [] for name in GROUP_ORDER}
        self.pivot: Dict[str, Vec] = {}
        self.rest_angle: Dict[str, float] = {}       # authored bend, degrees about X
        self.link_len: Dict[str, float] = {}
        self.jaw_side: Dict[int, int] = {}

        comps = connected_components(tris)
        arcs = [_arc(_mean([v for i in c for v in tris[i]])) for c in comps]
        lo, hi = min(arcs), max(arcs)
        span = max(1e-6, hi - lo)
        cuts = [lo + f * span for f in GROUP_CUTS]

        for comp, arc in zip(comps, arcs):
            idx = sum(1 for c in cuts if arc > c)
            name = GROUP_ORDER[min(idx, len(GROUP_ORDER) - 1)]
            self.groups[name].extend(tris[i] for i in comp)

        self.raw_faces = sum(len(v) for v in self.groups.values())
        for name in GROUP_ORDER:
            self.groups[name] = merge_coplanar(self.groups[name])
        self.faces = sum(len(v) for v in self.groups.values())

        # geometry of each link, in the pose the file was authored in
        prev_dir: Vec = (0.0, 0.0, 1.0)
        for name in GROUP_ORDER:
            verts = [v for t in self.groups[name] for v in t]
            if not verts:
                self.pivot[name] = (0.0, 0.0, 0.0)
                self.rest_angle[name] = 0.0
                self.link_len[name] = 0.0
                continue
            root, tip = _end_points(verts)
            direction = (tip[0] - root[0], tip[1] - root[1], tip[2] - root[2])
            length = math.sqrt(sum(c * c for c in direction)) or 1.0
            self.pivot[name] = root
            self.link_len[name] = length
            # Rotation about X that takes the parent direction to this one. The
            # arm bends in the YZ plane, so this is a plain 2D angle difference.
            here = math.atan2(-direction[1], direction[2])
            there = math.atan2(-prev_dir[1], prev_dir[2])
            self.rest_angle[name] = math.degrees(here - there)
            prev_dir = direction

        # gripper jaws sit in mirrored pairs; remember which side each is on
        jaw_tris = self.groups["gripper"]
        for i, t in enumerate(jaw_tris):
            cx = (t[0][0] + t[1][0] + t[2][0]) / 3.0
            self.jaw_side[i] = 1 if cx >= 0 else -1
        self.jaw_reach = max((abs(v[0]) for t in jaw_tris for v in t), default=1.0)

        # normalise into the renderer's units: a straightened arm about 3.8 tall
        chain = sum(self.link_len[n] for n in GROUP_ORDER)
        self.scale = 3.8 / max(1e-6, chain)
        base_verts = [v for t in self.groups["pedestal"] for v in t]
        self.floor = min((v[2] for v in base_verts), default=0.0)

    # ------------------------------------------------------------- summary
    def describe(self) -> str:
        bits = [f"scale {self.scale:.4f}   faces {self.raw_faces} tris -> {self.faces} polys"]
        for name in GROUP_ORDER:
            bits.append(f"{name}: {len(self.groups[name])} polys, "
                        f"len {self.link_len[name]:.0f}, rest {self.rest_angle[name]:+.1f}deg")
        return "\n".join(bits)



# ------------------------------------------------------------------ merging
def _normal(t):
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = t
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    n = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    return nx / n, ny / n, nz / n


def merge_coplanar(tris: List[Tri], tol: float = 0.999) -> List[Tuple[Vec, ...]]:
    """Fuse coplanar triangle pairs that share an edge into quads.

    An exported box face is two triangles; drawing it as one polygon halves the
    number of canvas items, which is what actually costs time in this renderer.
    """
    def vkey(v, q=1e-4):
        return (round(v[0] / q), round(v[1] / q), round(v[2] / q))

    normals = [_normal(t) for t in tris]
    edges = defaultdict(list)
    for i, t in enumerate(tris):
        keys = [vkey(v) for v in t]
        for a in range(3):
            e = tuple(sorted((keys[a], keys[(a + 1) % 3])))
            edges[e].append(i)

    used = [False] * len(tris)
    out: List[Tuple[Vec, ...]] = []
    for i, t in enumerate(tris):
        if used[i]:
            continue
        keys = [vkey(v) for v in t]
        partner = None
        for a in range(3):
            e = tuple(sorted((keys[a], keys[(a + 1) % 3])))
            for j in edges[e]:
                if j == i or used[j]:
                    continue
                ni, nj = normals[i], normals[j]
                if ni[0] * nj[0] + ni[1] * nj[1] + ni[2] * nj[2] < tol:
                    continue                      # not coplanar enough
                partner = (j, a)
                break
            if partner:
                break
        if not partner:
            used[i] = True
            out.append(tuple(t))
            continue

        j, a = partner
        shared = {keys[a], keys[(a + 1) % 3]}
        other = next(v for v, k in zip(tris[j], [vkey(x) for x in tris[j]])
                     if k not in shared)
        # t is (apex, e0, e1) rotated so the shared edge comes last
        apex = t[(a + 2) % 3]
        e0, e1 = t[a], t[(a + 1) % 3]
        used[i] = used[j] = True
        out.append((apex, e0, other, e1))
    return out

_CACHE: Optional[ArmMesh] = None
_TRIED = False


def get_mesh(path: str = MESH_FILE) -> Optional[ArmMesh]:
    """Load once. Returns None when the file is missing or unreadable, so the
    caller can fall back to the procedural model."""
    global _CACHE, _TRIED
    if _CACHE is not None or _TRIED:
        return _CACHE
    _TRIED = True
    try:
        _CACHE = ArmMesh(load_stl(path))
    except (OSError, ValueError, struct.error):
        _CACHE = None
    return _CACHE
