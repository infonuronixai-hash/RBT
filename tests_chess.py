"""Checks for the chess mode: rules matching, the built-in engine, the session
state machine, and turning moves into arm motion. No camera, no arm.

    python tests_chess.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import chess

import chess_arm
import chess_game as G

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("")
    print(f"== {title} ==")


def observed_after(board, move):
    """What the camera would report once this move is on the board."""
    b = board.copy()
    b.push(move)
    return G.occupancy_of(b)


# ------------------------------------------------------------ camera matching
section("matching occupancy to legal moves")
b = chess.Board()
mv, all_ = G.resolve_move(b, observed_after(b, chess.Move.from_uci("e2e4")))
check("plain pawn push", mv == chess.Move.from_uci("e2e4"), str(mv))
check("only one candidate", len(all_) == 1, str(all_))

b = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2")
mv, _ = G.resolve_move(b, observed_after(b, chess.Move.from_uci("e4d5")))
check("capture (one square changes)", mv == chess.Move.from_uci("e4d5"), str(mv))

b = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
mv, _ = G.resolve_move(b, observed_after(b, chess.Move.from_uci("e1g1")))
check("castling (four squares)", mv == chess.Move.from_uci("e1g1"), str(mv))

b = chess.Board("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3")
mv, _ = G.resolve_move(b, observed_after(b, chess.Move.from_uci("e5f6")))
check("en passant (three squares)", mv == chess.Move.from_uci("e5f6"), str(mv))

b = chess.Board("8/P7/8/8/8/8/8/k6K w - - 0 1")
mv, all_ = G.resolve_move(b, observed_after(b, chess.Move.from_uci("a7a8q")))
check("promotion resolves to a queen", mv == chess.Move.from_uci("a7a8q"), str(mv))
check("all promotions listed", len(all_) == 4, str(len(all_)))

b = chess.Board()
wrong = G.occupancy_of(b)
wrong[4][4] = True                                   # a piece appeared from nowhere
mv, all_ = G.resolve_move(b, wrong)
check("impossible change is refused", mv is None and not all_, str(all_))

# ------------------------------------------------------------------- engine
section("built-in engine")
for depth in (1, 2, 3):
    eng = G.BuiltinEngine(depth=depth, time_limit=8.0)
    b = chess.Board()
    t0 = time.perf_counter()
    m = eng.choose(b)
    dt = time.perf_counter() - t0
    check(f"depth {depth} returns a legal move in time", m in b.legal_moves and dt < 8.0,
          f"{m} in {dt:.2f}s, {eng.nodes} nodes")

b = chess.Board("6k1/5ppp/8/8/8/8/8/4R1K1 w - - 0 1")
m = G.BuiltinEngine(depth=2).choose(b)
check("finds mate in one", m == chess.Move.from_uci("e1e8"), str(m))

b = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/3P4/8/PPP1PPPP/RNBQKBNR w KQkq - 0 2")
m = G.BuiltinEngine(depth=2).choose(b)
check("takes a free pawn", m == chess.Move.from_uci("d4e5"), str(m))

# ------------------------------------------------------------------ session
section("game session")
s = G.GameSession(level=1, human_white=True)
s.start()
check("human to move first", s.state == G.HUMAN and s.human_turn)
check("illegal move rejected", not s.human_move(chess.Move.from_uci("e2e5")))
check("legal move accepted", s.human_move(chess.Move.from_uci("e2e4")))
check("engine now thinking", s.state == G.THINKING, s.state)
events = []
t0 = time.perf_counter()
while time.perf_counter() - t0 < 10 and not any(e[0] == "engine" for e in events):
    events += s.poll()
    time.sleep(0.02)
check("engine replied", any(e[0] == "engine" for e in events), str(events))
check("waiting for the arm", s.state == G.ROBOT and s.pending is not None, s.state)
check("board keeps a pre-move copy", s.pending_before is not None
      and s.pending_before.fullmove_number == 1)
s.robot_done()
check("back to the human", s.state == G.HUMAN and s.pending is None, s.state)
check("history has both moves", len(s.history) == 2, str(s.history))
check("undo takes back the pair", s.undo() and len(s.history) == 0
      and s.board.fullmove_number == 1, str(s.history))

s = G.GameSession(level=1, human_white=False)
s.start()
check("engine opens when human is black", s.state == G.THINKING, s.state)
t0 = time.perf_counter()
while time.perf_counter() - t0 < 10 and s.state == G.THINKING:
    s.poll()
    time.sleep(0.02)
check("engine made the first move", s.state == G.ROBOT and s.board.fullmove_number == 1)

# ---------------------------------------------------------------- square map
section("square map")
m = chess_arm.SquareMap()
check("not ready when empty", not m.ready and "a1" in m.missing())
m.set_corner("a1", [40, 120, 60, 90, 90, 90])
m.set_corner("h1", [140, 120, 60, 90, 90, 90])
m.set_corner("a8", [60, 60, 120, 80, 90, 90])
m.set_corner("h8", [120, 60, 120, 80, 90, 90])
m.set_lift_from([40, 120, 60, 90, 90, 90], [40, 105, 70, 95, 90, 90])
m.set_bin([170, 100, 90, 90, 90, 90])
check("ready once taught", m.ready, str(m.missing()))
check("corner reproduces exactly", m.grip_pose("a1") == [40, 120, 60, 90, 90, 90])
check("opposite corner exact", m.grip_pose("h8") == [120, 60, 120, 80, 90, 90])
mid = m.grip_pose("d4")
check("interior square lies between corners", 40 < mid[0] < 140 and 60 < mid[1] < 120,
      str(mid))
check("hover applies the lift", m.hover_pose("a1") == [40, 105, 70, 95, 90, 90],
      str(m.hover_pose("a1")))

seq = m.plan_move("e2", "e4", home=[90, 90, 90, 90, 90, 20])
check("simple move: start + 8 legs + home", len(seq) == 10, f"{len(seq)} frames")
check("monotonic timeline", all(b_.t > a_.t for a_, b_ in zip(seq.frames, seq.frames[1:])))
check("grips at the source", any(f.label == "grip" and f.angles[-1] == m.grip_closed
                                 for f in seq.frames))
check("releases at the target", any(f.label == "release" and f.angles[-1] == m.grip_open
                                    for f in seq.frames))
cap = m.plan_move("e4", "d5", capture=True, home=[90] * 5 + [20])
check("capture removes the piece first", len(cap) == 16 and "remove d5" in
      [f.label for f in cap.frames], f"{len(cap)} frames")
cas = m.plan_move("e1", "g1", extra=[("h1", "f1")], home=[90] * 5 + [20])
check("castling moves the rook too", len(cas) == 18, f"{len(cas)} frames")

p = os.path.join(os.environ.get("TEMP", "."), "_chess_map.json")
m.save(p)
back = chess_arm.SquareMap.load(p)
check("map round-trips", back.ready and back.grip_pose("c6") == m.grip_pose("c6")
      and back.bin == m.bin)
os.remove(p)

section("move legs")
b = chess.Board("r3k2r/pppppppp/8/8/8/8/PPPPPPPP/R3K2R w KQkq - 0 1")
src, dst, capture, extra = chess_arm.move_legs(b, chess.Move.from_uci("e1g1"))
check("castle kingside carries the rook", extra == [("h1", "f1")] and not capture, str(extra))
b = chess.Board("rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3")
src, dst, capture, extra = chess_arm.move_legs(b, chess.Move.from_uci("e5f6"))
check("en passant bins the pawn from f5", extra == [("f5", "")] and not capture, str(extra))
b = chess.Board("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2")
src, dst, capture, extra = chess_arm.move_legs(b, chess.Move.from_uci("e4d5"))
check("plain capture flagged", capture and not extra)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
