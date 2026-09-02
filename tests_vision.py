"""Checks for the board vision core, using synthetic images - no camera needed.

    python tests_vision.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import cv2
import numpy as np

from vision import (WARP_PX, BoardWatcher, Calibration, IN_MOTION, SETTLING, WATCHING,
                    describe_change, grid_diff, infer_move, occupancy_grid,
                    order_corners, square_index, square_name, warp_board, warp_matrix)

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("")
    print(f"== {title} ==")


def make_board(pieces=(), size=WARP_PX, piece_colour=(30, 30, 210)):
    """Synthetic top-down board; each named square gets a disc standing on it."""
    img = np.zeros((size, size, 3), np.uint8)
    s = size // 8
    for r in range(8):
        for c in range(8):
            shade = (205, 205, 205) if (r + c) % 2 == 0 else (85, 85, 85)
            cv2.rectangle(img, (c * s, r * s), ((c + 1) * s, (r + 1) * s), shade, -1)
    for name in pieces:
        c, r = square_index(name)
        cv2.circle(img, (c * s + s // 2, r * s + s // 2), int(s * 0.30), piece_colour, -1)
    return img


# --------------------------------------------------------------- naming
section("square naming")
check("a1 is bottom-left", square_index("a1") == (0, 7), str(square_index("a1")))
check("h8 is top-right", square_index("h8") == (7, 0), str(square_index("h8")))
check("e4 round-trips", square_name(*square_index("e4")) == "e4")
check("all 64 round-trip",
      all(square_name(*square_index(f"{f}{r}")) == f"{f}{r}"
          for f in "abcdefgh" for r in "12345678"))

# ------------------------------------------------------------- geometry
section("perspective calibration")
board = make_board(["e2", "d7", "a1", "h8"])
src = np.float32([[0, 0], [WARP_PX - 1, 0], [WARP_PX - 1, WARP_PX - 1], [0, WARP_PX - 1]])
quad = [[120, 80], [520, 110], [560, 470], [90, 440]]      # board as a camera sees it
M = cv2.getPerspectiveTransform(src, np.float32(quad))
frame = cv2.warpPerspective(board, M, (640, 520))

check("shuffled corners get ordered",
      np.allclose(order_corners([quad[2], quad[0], quad[3], quad[1]]), np.float32(quad)))

recovered = warp_board(frame, warp_matrix(quad))
inner = slice(40, WARP_PX - 40)
err = float(np.mean(np.abs(recovered[inner, inner].astype(float)
                           - board[inner, inner].astype(float))))
check("warp recovers the board", err < 12.0, f"mean abs error {err:.1f}")

# ------------------------------------------------------------ occupancy
section("occupancy detection")
empty = make_board()
occupied_squares = ["e2", "d7", "a1", "h8", "c5"]
withpieces = make_board(occupied_squares)

grid, score = occupancy_grid(withpieces, empty)
found = sorted(square_name(c, r) for r in range(8) for c in range(8) if grid[r, c])
check("finds exactly the occupied squares", found == sorted(occupied_squares), str(found))
check("empty board reads as empty",
      not occupancy_grid(empty, empty)[0].any())

# the checkerboard is why a reference matters, but texture should still work
grid_tex, _ = occupancy_grid(withpieces, None, threshold=14.0)
found_tex = sorted(square_name(c, r) for r in range(8) for c in range(8) if grid_tex[r, c])
check("texture fallback finds them too", found_tex == sorted(occupied_squares),
      str(found_tex))

margin = float(min(score[square_index(s)[1], square_index(s)[0]] for s in occupied_squares))
noise = float(max(score[r, c] for r in range(8) for c in range(8)
                  if square_name(c, r) not in occupied_squares))
check("clear separation between occupied and empty", margin > noise * 3,
      f"weakest piece {margin:.0f} vs loudest empty {noise:.0f}")

# ----------------------------------------------------------------- diff
section("move inference")
before = occupancy_grid(make_board(["e2", "d7"]), empty)[0]
after = occupancy_grid(make_board(["e4", "d7"]), empty)[0]
check("simple move", infer_move(before, after) == "e2e4", str(infer_move(before, after)))
check("diff lists two squares", len(grid_diff(before, after)) == 2)
check("no change gives no move", infer_move(before, before) is None)

# a capture leaves only the source empty - one square changes, not two
cap_before = occupancy_grid(make_board(["e4", "d5"]), empty)[0]
cap_after = occupancy_grid(make_board(["d5"]), empty)[0]
check("capture is not guessed", infer_move(cap_before, cap_after) is None)
check("capture is still described", "e4-" in describe_change(cap_before, cap_after),
      describe_change(cap_before, cap_after))

# castling moves four squares; the camera must refuse to guess
cas_before = occupancy_grid(make_board(["e1", "h1"]), empty)[0]
cas_after = occupancy_grid(make_board(["g1", "f1"]), empty)[0]
check("castling is not guessed", infer_move(cas_before, cas_after) is None)
check("castling reports four squares",
      len(grid_diff(cas_before, cas_after)) == 4,
      describe_change(cas_before, cas_after))

# ------------------------------------------------------- turn detection
section("motion-gated turn detection")
calib = Calibration(corners=quad, motion_threshold=2.0, settle_seconds=0.30)
w = BoardWatcher(calib)
w.set_empty_reference(empty)

start = make_board(["e2", "d7"])
w.begin_turn(start)
check("starts watching", w.state == WATCHING, w.state)

t = 100.0
for _ in range(3):                       # quiet board
    t += 0.05
    w.update(start, now=t)
check("stays watching while still", w.state == WATCHING, w.state)
check("no events while nothing happens",
      all(e.kind == "state" for e in w.poll()))

hand = start.copy()                      # a hand sweeps across the board
cv2.rectangle(hand, (100, 60), (380, 430), (60, 90, 140), -1)
t += 0.05
w.update(hand, now=t)
check("motion detected", w.state == IN_MOTION, f"{w.state} motion={w.motion:.1f}")

moved = make_board(["e4", "d7"])         # hand leaves, piece has moved
t += 0.05
w.update(moved, now=t)
check("still in motion as the hand withdraws", w.state == IN_MOTION, w.state)
t += 0.05
w.update(moved, now=t)
check("enters settling once still", w.state == SETTLING, w.state)

t += 0.10                                # not settled long enough yet
w.update(moved, now=t)
check("waits out the settle time", w.state == SETTLING, w.state)
check("no move reported early", not any(e.kind == "move" for e in w.poll()))

t += 0.40                                # now past settle_seconds
w.update(moved, now=t)
events = w.poll()
moves = [e for e in events if e.kind == "move"]
check("move reported after settling", len(moves) == 1, str([e.kind for e in events]))
check("correct move", moves and moves[0].move == "e2e4",
      moves[0].move if moves else "none")
check("returns to watching", w.state == WATCHING, w.state)

# hovering a hand without moving anything must not produce a move
t += 0.05
w.update(hand, now=t)
t += 0.05
w.update(moved, now=t)
t += 0.05
w.update(moved, now=t)
t += 0.50
w.update(moved, now=t)
events = w.poll()
check("hover produces no phantom move", not any(e.kind == "move" for e in events),
      str([e.text for e in events]))

# a capture during play is reported as a change for the game logic to resolve
t += 0.05
w.update(hand, now=t)
taken = make_board(["e4"])
t += 0.05
w.update(taken, now=t)
t += 0.05
w.update(taken, now=t)
t += 0.50
w.update(taken, now=t)
events = w.poll()
check("ambiguous change is reported, not guessed",
      any(e.kind == "change" for e in events),
      str([f"{e.kind}:{e.text}" for e in events]))

section("a new turn starts clean")
# Capturing an empty-board reference (or any prior activity) leaves a large motion
# reading behind. Starting a turn must not inherit it and trip the gate at once.
w.state = WATCHING
w.motion = 99.0
w.begin_turn(start)
check("motion reset by begin_turn", w.motion == 0.0, str(w.motion))
t += 0.05
w.update(start, now=t)
check("stays watching, not tripped by stale motion", w.state == WATCHING, w.state)

section("paused watcher")
w.pause()
t += 0.05
w.update(hand, now=t)
check("ignores everything while paused", w.state == "IDLE", w.state)

print("")
print("ALL PASS" if not fails else f"{len(fails)} FAILURES: {fails}")
sys.exit(1 if fails else 0)
