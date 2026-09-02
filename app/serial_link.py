"""Threaded USB-serial transport to the Arduino Uno running the RoboArm firmware.

Real hardware is the primary path. A tiny built-in simulator can stand in when
no board is plugged in, so the UI, recorder and player stay testable offline.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import List, Optional, Tuple

try:
    import serial
    from serial.tools import list_ports
except ImportError:                                    # pragma: no cover
    serial = None
    list_ports = None

DEFAULT_BAUD = 115200
RESET_WAIT = 1.8          # the Uno reboots when the port opens; wait it out
PYSERIAL_HINT = "pyserial is not installed. Run:  python -m pip install pyserial"


class _Simulator:
    """Answers the firmware protocol in-process so the app runs without a board."""

    def __init__(self, joints: int = 6):
        self.joints = joints
        self.pos = [90] * joints
        self.pos[-1] = 20
        self.attached = True

    def handle(self, line: str) -> List[str]:
        parts = line.strip().split()
        if not parts:
            return []
        cmd = parts[0].upper()
        if cmd in ("PING", "INFO"):
            return [f"OK PONG Simulator 1.0 {self.joints}"]
        if cmd == "G":
            return ["POS " + " ".join(str(a) for a in self.pos)]
        if cmd == "M":
            for i, tok in enumerate(parts[1 : self.joints + 1]):
                if tok == "-":
                    continue
                try:
                    self.pos[i] = max(0, min(180, int(tok)))
                except ValueError:
                    pass
            return ["OK"]
        if cmd in ("S", "J") and len(parts) >= 3:
            try:
                i, v = int(parts[1]), int(parts[2])
            except ValueError:
                return ["ERR args"]
            if 0 <= i < self.joints:
                self.pos[i] = max(0, min(180, v if cmd == "S" else self.pos[i] + v))
                return ["OK"]
            return ["ERR joint"]
        if cmd == "HOME":
            self.pos = [90] * self.joints
            self.pos[-1] = 20
            return ["OK"]
        if cmd == "DET":
            self.attached = False
            return ["OK"]
        if cmd == "ATT":
            self.attached = True
            return ["OK"]
        if cmd == "LIMS":
            return [f"LIM {i} 0 180" for i in range(self.joints)]
        return ["OK"]


class SerialLink:
    """Non-blocking serial link. The UI polls :meth:`poll` from its Tk timer."""

    def __init__(self) -> None:
        self._ser = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._rx: "queue.Queue[str]" = queue.Queue()
        self._tx_lock = threading.Lock()
        self.simulate = False
        self._sim: Optional[_Simulator] = None
        self.connected = False
        self.port = ""
        self.firmware = ""
        self.last_error = ""
        self.tx_count = 0
        self.rx_count = 0

    # ------------------------------------------------------------- discovery
    @staticmethod
    def available_ports() -> List[Tuple[str, str]]:
        """[(device, human label)] with likely Arduino boards listed first."""
        if list_ports is None:
            return []
        found = []
        for p in list_ports.comports():
            desc = (p.description or "").strip()
            hint = f"{desc} {p.manufacturer or ''} {p.hwid or ''}".lower()
            likely = any(k in hint for k in ("arduino", "ch340", "ch910", "usb-serial",
                                             "wch", "ftdi", "cp210", "usb serial"))
            found.append((p.device, f"{p.device} - {desc or 'serial port'}", likely))
        found.sort(key=lambda t: (not t[2], t[0]))
        return [(dev, label) for dev, label, _ in found]

    @staticmethod
    def autodetect() -> Optional[str]:
        ports = SerialLink.available_ports()
        return ports[0][0] if ports else None

    # ------------------------------------------------------------ connection
    def connect(self, port: str, baud: int = DEFAULT_BAUD, simulate: bool = False,
                joints: int = 6) -> bool:
        self.disconnect()
        self.last_error = ""
        self.simulate = simulate

        if simulate:
            self._sim = _Simulator(joints)
            self.connected = True
            self.port = "SIMULATOR"
            self.firmware = "Simulator 1.0"
            self._rx.put("RDY Simulator 1.0")
            return True

        if serial is None:
            self.last_error = PYSERIAL_HINT
            return False

        try:
            self._ser = serial.Serial(port=port, baudrate=baud, timeout=0.1,
                                      write_timeout=2.0)
        except Exception as exc:                        # serial.SerialException & friends
            self.last_error = f"{exc}"
            self._ser = None
            return False

        # The Uno resets on DTR; discard the boot noise before we start talking.
        time.sleep(RESET_WAIT)
        try:
            self._ser.reset_input_buffer()
            self._ser.reset_output_buffer()
        except Exception:
            pass

        self.port = port
        self.connected = True
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, name="serial-rx", daemon=True)
        self._reader.start()
        self.send("PING")
        return True

    def disconnect(self) -> None:
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=1.0)
        self._reader = None
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self._sim = None
        self.connected = False
        self.simulate = False
        self.firmware = ""
        self.port = ""

    # --------------------------------------------------------------- traffic
    def send(self, line: str) -> bool:
        if not self.connected:
            return False
        if self.simulate:
            for reply in (self._sim.handle(line) if self._sim else []):
                self._rx.put(reply)
            self.tx_count += 1
            return True
        if self._ser is None:
            return False
        try:
            with self._tx_lock:
                self._ser.write((line.strip() + "\n").encode("ascii", "ignore"))
            self.tx_count += 1
            return True
        except Exception as exc:
            self.last_error = f"write failed: {exc}"
            self._rx.put(f"ERR link {exc}")
            self.connected = False
            return False

    def send_pose(self, servo_angles) -> bool:
        return self.send("M " + " ".join(str(int(a)) for a in servo_angles))

    def poll(self, limit: int = 200) -> List[str]:
        """Drain received lines. Call from the UI thread only."""
        out = []
        for _ in range(limit):
            try:
                out.append(self._rx.get_nowait())
            except queue.Empty:
                break
        return out

    # ------------------------------------------------------------ rx thread
    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self._ser.read(256) if self._ser else b""
            except Exception as exc:
                self._rx.put(f"ERR link {exc}")
                self.connected = False
                return
            if not chunk:
                continue
            buf.extend(chunk)
            while b"\n" in buf:
                raw, _, rest = buf.partition(b"\n")
                buf = bytearray(rest)
                line = raw.decode("ascii", "ignore").strip()
                if line:
                    self.rx_count += 1
                    self._rx.put(line)


def parse_pos(line: str) -> Optional[List[int]]:
    """'POS 90 90 90 90 90 20' -> [90, 90, 90, 90, 90, 20]"""
    if not line.upper().startswith("POS"):
        return None
    try:
        return [int(float(tok)) for tok in line.split()[1:]]
    except ValueError:
        return None
