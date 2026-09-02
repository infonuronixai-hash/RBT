"""Chess mode inside the dashboard: toggle, click-to-move, engine reply, and the
arm executing a move through the normal player. Simulator only.

    python tests_ui_chess.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import chess

import chess_arm
from arm_config import ArmConfig
from ui import ArmApp

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("")
    print(f"== {title} ==")


def pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.update()
        time.sleep(0.005)


def wait_until(app, predicate, timeout=12.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        app.update_idletasks()
        app.update()
        if predicate():
            return True
        time.sleep(0.005)
    return False


map_existed = os.path.exists(chess_arm.MAP_FILE)
cfg = ArmConfig.load(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "app", "arm_config.json"))
app = ArmApp(cfg, start_sim=True)
pump(app, 0.4)

section("toggle")
check("chess off by default", not app.chess_var.get() and not app.chess_panel.winfo_ismapped())
app.chess_var.set(True)
app._toggle_chess()
check("chess panel appears", wait_until(app, lambda: app.chess_panel.winfo_ismapped()))
check("axis diagram hidden", not app.axis_panel.winfo_ismapped())
check("session started, human to move", app.chess is not None and app.chess.state == "HUMAN")
check("32 pieces drawn", len(app.chess_board.position) == 32)
check("sliders still drive the arm", (app._jog(0, 10), pump(app, 0.15), app.pose[0] == 100)[2])

section("click to move, engine replies, arm not taught")
app.chess_arm_var.set(True)
app._chess_click("e2")
check("piece selected with targets", app._chess_selected == "e2"
      and app.chess_board.targets == {"e3", "e4"}, str(app.chess_board.targets))
app._chess_click("e4")
check("move made, engine thinking", app.chess.state == "THINKING", app.chess.state)
check("engine replied and arm step skipped (not taught)",
      wait_until(app, lambda: app.chess.state == "HUMAN"), app.chess.state)
check("two half-moves in history", len(app.chess.history) == 2, str(app.chess.history))
check("last move highlighted", len(app.chess_board.last_move) == 2)
check("no chess sequence left behind", app._chess_seq is None)

section("teach squares, arm plays the next move")
for key, pose in (("a1", [40, 120, 60, 90, 90, 90]), ("h1", [140, 120, 60, 90, 90, 90]),
                  ("a8", [60, 60, 120, 80, 90, 90]), ("h8", [120, 60, 120, 80, 90, 90])):
    app._set_pose(pose)
    app._chess_teach(key)
app._set_pose([40, 105, 70, 95, 90, 90])
app._chess_teach("lift")
app._set_pose([170, 100, 90, 90, 90, 90])
app._chess_teach("bin")
check("square map ready", app.square_map is not None and app.square_map.ready,
      str(app.square_map.missing() if app.square_map else None))
check("status says taught", "taught" in app.chess_map_lbl["text"])

app._chess_click("d2")
app._chess_click("d4")
check("engine thinking again", app.chess.state == "THINKING")
check("arm starts playing the reply",
      wait_until(app, lambda: app.chess.state == "ROBOT" and app._chess_seq is not None
                 and app.player.playing), f"{app.chess.state}")
seq = app._chess_seq
check("planned as a pick-and-place", seq is not None and len(seq) >= 10, f"{len(seq)}")
check("motion panel shows the chess sequence", app.steps.steps and
      any("over" in s[1] for s in app.steps.steps))
tx0 = app.link.tx_count
pump(app, 0.5)
check("poses stream to the board while the arm plays", app.link.tx_count > tx0)
check("arm finishes, back to the human",
      wait_until(app, lambda: app.chess.state == "HUMAN", timeout=seq.duration + 8),
      app.chess.state)
check("chess sequence cleared", app._chess_seq is None)
check("pose ends at home", app.pose == cfg.home_pose(), str(app.pose))

section("manual override during a robot move")
app._chess_click("g1")
app._chess_click("f3")
check("arm playing", wait_until(app, lambda: app.chess.state == "ROBOT" and app.player.playing))
app._on_slider(0, 77)
check("override stops the arm, game continues",
      wait_until(app, lambda: app.chess.state == "HUMAN" and not app.player.playing),
      app.chess.state)
check("manual pose stands", app.pose[0] == 77, str(app.pose[0]))

section("undo, new game, exclusivity with record & play")
n = len(app.chess.history)
app._chess_undo()
check("undo removes a pair", len(app.chess.history) == n - 2, str(app.chess.history))
app.chess_colour.set("black")
app._chess_new_game()
check("as black the engine opens", app.chess.state in ("THINKING", "ROBOT"))
check("board flipped for black", app.chess_board.flipped)
wait_until(app, lambda: app.chess.state == "HUMAN")
app.recplay_var.set(True)
app._toggle_recplay()
check("record & play switches chess off", not app.chess_var.get()
      and wait_until(app, lambda: app.right.winfo_ismapped()))
check("chess panel hidden", not app.chess_panel.winfo_ismapped())
app.recplay_var.set(False)
app._toggle_recplay()
check("axis diagram back", wait_until(app, lambda: app.axis_panel.winfo_ismapped()))

app.seq.dirty = False
app._on_close()
if not map_existed and os.path.exists(chess_arm.MAP_FILE):
    os.remove(chess_arm.MAP_FILE)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
