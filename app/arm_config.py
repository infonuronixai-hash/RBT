"""Joint definitions and user-tunable arm configuration.

The config lives in a JSON file (see paths.config_file) so limits and trims
survive restarts and can be hand-edited without touching code.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import List

import paths

CONFIG_FILE = paths.config_file()


@dataclass
class Joint:
    name: str
    label: str
    min_angle: int = 0
    max_angle: int = 180
    home: int = 90
    invert: bool = False          # flip direction if the servo is mounted backwards
    trim: int = 0                 # mechanical zero offset, added before sending

    def clamp(self, angle: float) -> int:
        return int(max(self.min_angle, min(self.max_angle, round(angle))))

    def to_servo(self, angle: float) -> int:
        """Logical joint angle -> raw servo angle actually sent to the board."""
        a = self.clamp(angle)
        if self.invert:
            a = self.min_angle + self.max_angle - a
        return int(max(0, min(180, a + self.trim)))

    def from_servo(self, raw: float) -> int:
        """Raw servo angle reported by the board -> logical joint angle."""
        a = raw - self.trim
        if self.invert:
            a = self.min_angle + self.max_angle - a
        return self.clamp(a)


DEFAULT_JOINTS = [
    Joint("base",       "Base rotate",   0, 180, 90),
    Joint("shoulder",   "Shoulder",     15, 165, 90),
    Joint("elbow",      "Elbow",         0, 180, 90),
    Joint("wrist_pitch", "Wrist pitch",  0, 180, 90),
    Joint("wrist_roll", "Wrist roll",    0, 180, 90),
    Joint("gripper",    "Gripper",      10, 110, 20),
]


@dataclass
class ArmConfig:
    joints: List[Joint] = field(default_factory=lambda: [Joint(**asdict(j)) for j in DEFAULT_JOINTS])
    port: str = ""
    baud: int = 115200
    speed_dps: int = 120          # servo slew rate pushed to the firmware
    send_hz: int = 25             # how often live slider moves are transmitted
    record_hz: int = 20           # sampling rate for continuous recording

    # -------------------------------------------------------------- helpers
    @property
    def count(self) -> int:
        return len(self.joints)

    def home_pose(self) -> List[int]:
        return [j.home for j in self.joints]

    def clamp_pose(self, pose) -> List[int]:
        return [j.clamp(a) for j, a in zip(self.joints, pose)]

    def to_servo_pose(self, pose) -> List[int]:
        return [j.to_servo(a) for j, a in zip(self.joints, pose)]

    def from_servo_pose(self, pose) -> List[int]:
        return [j.from_servo(a) for j, a in zip(self.joints, pose)]

    # -------------------------------------------------------------- storage
    @classmethod
    def load(cls, path: str = CONFIG_FILE) -> "ArmConfig":
        if not os.path.exists(path):
            cfg = cls()
            cfg.save(path)
            return cfg
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return cls()
        joints = [Joint(**j) for j in data.get("joints", [])] or None
        cfg = cls(joints=joints or [Joint(**asdict(j)) for j in DEFAULT_JOINTS])
        for key in ("port", "baud", "speed_dps", "send_hz", "record_hz"):
            if key in data:
                setattr(cfg, key, data[key])
        return cfg

    def save(self, path: str = CONFIG_FILE) -> None:
        data = asdict(self)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            pass
