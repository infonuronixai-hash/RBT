"""Turn chess moves into arm motion.

Squares are not computed from kinematics: hobby servos have poor absolute
accuracy but good repeatability, so each square is *taught*. The user parks the
gripper on four corner squares and the rest of the board is interpolated in
joint space between them. A separately taught lift offset raises the tool clear
of the pieces while it travels, and a bin pose is where captured pieces go.

Output is an ordinary Sequence, so the existing player, its interpolation, and
the manual-override safety all apply unchanged.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence as Seq

import paths
from sequence import Sequence

MAP_FILE = os.path.join(paths.data_dir(), "chess_squares.json")

CORNERS = ("a1", "h1", "a8", "h8")
FILES = "abcdefgh"

# timing per leg of a pick-and-place, seconds
T_TRAVEL, T_DESCEND, T_GRIP, T_LIFT = 1.4, 0.7, 0.45, 0.6


def _lerp(a: Seq[float], b: Seq[float], k: float) -> List[float]:
    return [x + (y - x) * k for x, y in zip(a, b)]


class SquareMap:
    """Taught corner poses plus the derived pose for any square."""

    def __init__(self, joints: int = 6):
        self.joints = joints
        self.corners: Dict[str, List[int]] = {}      # a1/h1/a8/h8 -> grip pose
        self.lift: Optional[List[int]] = None        # joint delta from grip to hover
        self.bin: Optional[List[int]] = None         # where captured pieces go
        self.grip_open = 90                          # gripper angle open
        self.grip_closed = 25                        # gripper angle holding a piece

    # -- state ---------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return all(c in self.corners for c in CORNERS) and self.lift is not None

    def missing(self) -> List[str]:
        out = [c for c in CORNERS if c not in self.corners]
        if self.lift is None:
            out.append("lift")
        if self.bin is None:
            out.append("bin")
        return out

    def set_corner(self, name: str, pose: Seq[int]) -> None:
        if name not in CORNERS:
            raise ValueError(name)
        self.corners[name] = [int(a) for a in pose]

    def set_lift_from(self, grip_pose: Seq[int], hover_pose: Seq[int]) -> None:
        """Lift is the joint delta between a taught grip pose and a hover above it."""
        self.lift = [int(h) - int(g) for g, h in zip(grip_pose, hover_pose)]

    def set_bin(self, pose: Seq[int]) -> None:
        self.bin = [int(a) for a in pose]

    # -- geometry ------------------------------------------------------------
    def grip_pose(self, square: str) -> List[int]:
        """Bilinear blend of the four corners in joint space."""
        if not all(c in self.corners for c in CORNERS):
            raise RuntimeError("corners not taught")
        f = FILES.index(square[0]) / 7.0
        r = (int(square[1]) - 1) / 7.0
        bottom = _lerp(self.corners["a1"], self.corners["h1"], f)
        top = _lerp(self.corners["a8"], self.corners["h8"], f)
        pose = _lerp(bottom, top, r)
        return [int(round(a)) for a in pose]

    def hover_pose(self, square: str) -> List[int]:
        g = self.grip_pose(square)
        lift = self.lift or [0] * self.joints
        return [a + d for a, d in zip(g, lift)]

    def _with_grip(self, pose: Seq[int], angle: int) -> List[int]:
        out = list(pose)
        out[-1] = angle
        return out

    # -- planning ------------------------------------------------------------
    def plan_move(self, from_sq: str, to_sq: str, capture: bool = False,
                  home: Optional[Seq[int]] = None,
                  extra: Optional[List[tuple]] = None) -> Sequence:
        """Pick-and-place for one move as a keyframe sequence.

        `extra` carries additional (from, to) pairs played first - the rook in a
        castle, the pawn removed by en passant - each routed through the bin when
        its destination is empty string.
        """
        if not self.ready:
            raise RuntimeError("square map not ready: " + ", ".join(self.missing()))
        seq = Sequence(f"chess {from_sq}{to_sq}", ["base", "shoulder", "elbow",
                                                    "wrist_pitch", "wrist_roll", "gripper"])
        t = 0.0
        op, cl = self.grip_open, self.grip_closed

        def leg(pose, dt, hold=0.0, label=""):
            nonlocal t
            t += dt
            seq.add(pose, t=t, hold=hold, label=label)

        def pick_place(src: str, dst: Optional[str], label: str):
            leg(self._with_grip(self.hover_pose(src), op), T_TRAVEL, 0.1, f"over {src}")
            leg(self._with_grip(self.grip_pose(src), op), T_DESCEND, 0.1, f"down {src}")
            leg(self._with_grip(self.grip_pose(src), cl), T_GRIP, 0.2, "grip")
            leg(self._with_grip(self.hover_pose(src), cl), T_LIFT, 0.1, "lift")
            if dst:
                leg(self._with_grip(self.hover_pose(dst), cl), T_TRAVEL, 0.1, f"over {dst}")
                leg(self._with_grip(self.grip_pose(dst), cl), T_DESCEND, 0.1, f"down {dst}")
                leg(self._with_grip(self.grip_pose(dst), op), T_GRIP, 0.2, "release")
                leg(self._with_grip(self.hover_pose(dst), op), T_LIFT, 0.1, label)
            else:
                bin_pose = self.bin or self.hover_pose("h1")
                leg(self._with_grip(bin_pose, cl), T_TRAVEL, 0.1, "to bin")
                leg(self._with_grip(bin_pose, op), T_GRIP, 0.2, label)

        start = list(home) if home else None
        if start:
            seq.add(self._with_grip(start, op), t=0.0, hold=0.1, label="start")

        for src, dst in (extra or []):
            pick_place(src, dst or None, f"{src}-{dst or 'bin'}")
        if capture:
            pick_place(to_sq, None, f"remove {to_sq}")
        pick_place(from_sq, to_sq, f"{from_sq}-{to_sq}")

        if start:
            leg(list(start), T_TRAVEL, 0.2, "home")    # exact home pose, gripper included
        return seq

    # -- storage -------------------------------------------------------------
    def save(self, path: str = MAP_FILE) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"corners": self.corners, "lift": self.lift, "bin": self.bin,
                       "grip_open": self.grip_open, "grip_closed": self.grip_closed},
                      fh, indent=2)

    @staticmethod
    def load(path: str = MAP_FILE, joints: int = 6) -> "SquareMap":
        m = SquareMap(joints)
        if not os.path.exists(path):
            return m
        try:
            with open(path, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return m
        m.corners = {k: [int(a) for a in v] for k, v in d.get("corners", {}).items()
                     if k in CORNERS}
        m.lift = d.get("lift")
        m.bin = d.get("bin")
        m.grip_open = int(d.get("grip_open", 90))
        m.grip_closed = int(d.get("grip_closed", 25))
        return m


def move_legs(board_before, move) -> tuple:
    """Break a python-chess move into what the arm must physically do.

    Returns (from, to, capture, extra) where extra lists secondary pick-and-place
    pairs: the rook for castling, the removed pawn for en passant.
    """
    import chess
    src = chess.square_name(move.from_square)
    dst = chess.square_name(move.to_square)
    extra = []
    capture = board_before.is_capture(move)
    if board_before.is_en_passant(move):
        taken = chess.square(chess.square_file(move.to_square),
                             chess.square_rank(move.from_square))
        extra.append((chess.square_name(taken), ""))     # pawn to the bin
        capture = False
    if board_before.is_castling(move):
        rank = "1" if chess.square_rank(move.from_square) == 0 else "8"
        if chess.square_file(move.to_square) == 6:        # kingside
            extra.append((f"h{rank}", f"f{rank}"))
        else:
            extra.append((f"a{rank}", f"d{rank}"))
    return src, dst, capture, extra
