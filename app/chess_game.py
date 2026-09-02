"""Chess game logic: legality, an opponent, and matching camera diffs to moves.

Legality and move generation come from python-chess. The opponent is a small
alpha-beta search that needs no external binary; if a Stockfish executable is
configured it is used instead, which is a very large strength jump.

Nothing here touches the UI or the arm. The session is a state machine that the
UI polls, and the engine thinks on a worker thread and posts its answer back.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

try:
    import chess
    import chess.engine
except ImportError:                                    # feature stays optional
    chess = None

# ------------------------------------------------------------------ evaluation
PIECE_VALUE = {1: 100, 2: 320, 3: 330, 4: 500, 5: 900, 6: 0}

# Piece-square tables, white's view, a1 = index 0. Small nudges that make the
# search prefer developed pieces and central pawns over shuffling.
_PST_PAWN = [0, 0, 0, 0, 0, 0, 0, 0,
             5, 10, 10, -20, -20, 10, 10, 5,
             5, -5, -10, 0, 0, -10, -5, 5,
             0, 0, 0, 20, 20, 0, 0, 0,
             5, 5, 10, 25, 25, 10, 5, 5,
             10, 10, 20, 30, 30, 20, 10, 10,
             50, 50, 50, 50, 50, 50, 50, 50,
             0, 0, 0, 0, 0, 0, 0, 0]
_PST_KNIGHT = [-50, -40, -30, -30, -30, -30, -40, -50,
               -40, -20, 0, 5, 5, 0, -20, -40,
               -30, 5, 10, 15, 15, 10, 5, -30,
               -30, 0, 15, 20, 20, 15, 0, -30,
               -30, 5, 15, 20, 20, 15, 5, -30,
               -30, 0, 10, 15, 15, 10, 0, -30,
               -40, -20, 0, 0, 0, 0, -20, -40,
               -50, -40, -30, -30, -30, -30, -40, -50]
_PST_CENTRE = [-10 if (i % 8 in (0, 7) or i // 8 in (0, 7)) else 5 for i in range(64)]
_PST_KING = [20, 30, 10, 0, 0, 10, 30, 20,
             20, 20, 0, 0, 0, 0, 20, 20,
             -10, -20, -20, -20, -20, -20, -20, -10,
             -20, -30, -30, -40, -40, -30, -30, -20,
             -30, -40, -40, -50, -50, -40, -40, -30,
             -30, -40, -40, -50, -50, -40, -40, -30,
             -30, -40, -40, -50, -50, -40, -40, -30,
             -30, -40, -40, -50, -50, -40, -40, -30]
_PST = {1: _PST_PAWN, 2: _PST_KNIGHT, 3: _PST_CENTRE, 4: [0] * 64, 5: _PST_CENTRE,
        6: _PST_KING}


def evaluate(board) -> int:
    """Static score from the side-to-move's point of view, in centipawns."""
    if board.is_checkmate():
        return -100000
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    score = 0
    for sq, piece in board.piece_map().items():
        idx = sq if piece.color == chess.WHITE else chess.square_mirror(sq)
        val = PIECE_VALUE[piece.piece_type] + _PST[piece.piece_type][idx]
        score += val if piece.color == chess.WHITE else -val
    return score if board.turn == chess.WHITE else -score


# ---------------------------------------------------------------------- engine
class BuiltinEngine:
    """Alpha-beta negamax with capture-only quiescence. Depth 1-4 is playable
    in pure Python; depth 3 answers in a second or two on a laptop."""

    def __init__(self, depth: int = 3, time_limit: float = 6.0):
        self.depth = max(1, min(4, depth))
        self.time_limit = time_limit
        self.nodes = 0
        self._deadline = 0.0

    def _quiesce(self, board, alpha: int, beta: int, ply: int) -> int:
        stand = evaluate(board)
        if stand >= beta or ply > 4:
            return stand
        alpha = max(alpha, stand)
        for mv in board.legal_moves:
            if not board.is_capture(mv):
                continue
            board.push(mv)
            score = -self._quiesce(board, -beta, -alpha, ply + 1)
            board.pop()
            if score >= beta:
                return score
            alpha = max(alpha, score)
        return alpha

    def _search(self, board, depth: int, alpha: int, beta: int) -> int:
        self.nodes += 1
        if depth == 0 or board.is_game_over():
            return self._quiesce(board, alpha, beta, 0)
        if time.perf_counter() > self._deadline:
            return evaluate(board)
        best = -10 ** 9
        moves = list(board.legal_moves)
        # captures and checks first: better pruning for the same node budget
        moves.sort(key=lambda m: (board.is_capture(m), board.gives_check(m)), reverse=True)
        for mv in moves:
            board.push(mv)
            score = -self._search(board, depth - 1, -beta, -alpha)
            board.pop()
            if score > best:
                best = score
            alpha = max(alpha, score)
            if alpha >= beta:
                break
        return best

    def choose(self, board) -> Optional["chess.Move"]:
        moves = list(board.legal_moves)
        if not moves:
            return None
        self.nodes = 0
        self._deadline = time.perf_counter() + self.time_limit
        random.shuffle(moves)                          # vary play between games
        best_move, best_score = moves[0], -10 ** 9
        for mv in moves:
            board.push(mv)
            score = -self._search(board, self.depth - 1, -10 ** 9, -best_score)
            board.pop()
            if score > best_score:
                best_move, best_score = mv, score
        return best_move


class StockfishEngine:
    """Thin wrapper; only used when an executable is configured and exists."""

    def __init__(self, path: str, think_time: float = 1.0):
        self.path = path
        self.think_time = think_time
        self._engine = None

    def _open(self):
        if self._engine is None:
            self._engine = chess.engine.SimpleEngine.popen_uci(self.path)
        return self._engine

    def choose(self, board):
        result = self._open().play(board, chess.engine.Limit(time=self.think_time))
        return result.move

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None


def make_engine(level: int = 2, stockfish_path: str = ""):
    if stockfish_path and os.path.exists(stockfish_path):
        return StockfishEngine(stockfish_path, think_time=0.4 * max(1, level))
    return BuiltinEngine(depth=max(1, min(4, level)))


# ------------------------------------------------------------ camera matching
def occupancy_of(board) -> List[List[bool]]:
    """8x8 grid in the camera's orientation: row 0 = rank 8, col 0 = file a."""
    grid = [[False] * 8 for _ in range(8)]
    for sq in board.piece_map():
        grid[7 - chess.square_rank(sq)][chess.square_file(sq)] = True
    return grid


def resolve_move(board, observed) -> Tuple[Optional["chess.Move"], List["chess.Move"]]:
    """Which legal move turns the current position into the observed occupancy?

    Every legal move is played on a scratch board and its resulting occupancy
    compared with what the camera saw. Captures, castling, en passant and
    promotion all fall out of this without special cases, because occupancy
    after the move is simply computed rather than inferred.

    Returns (unique match or None, all matches).
    """
    matches = []
    for mv in board.legal_moves:
        board.push(mv)
        same = occupancy_of(board) == observed
        board.pop()
        if same:
            matches.append(mv)
    # promotions all leave the same occupancy - prefer the queen, keep the rest
    if len(matches) > 1 and all(m.promotion for m in matches):
        queen = [m for m in matches if m.promotion == chess.QUEEN]
        if queen:
            return queen[0], matches
    return (matches[0] if len(matches) == 1 else None), matches


# --------------------------------------------------------------------- session
IDLE = "IDLE"
HUMAN = "HUMAN"
THINKING = "THINKING"
ROBOT = "ROBOT"
OVER = "OVER"


class GameSession:
    """One game: human vs engine, with the robot playing the engine's moves.

    The UI drives it: `human_move` when the person moves, `poll` every tick to
    pick up the engine's reply, `robot_done` when the arm has finished.
    """

    def __init__(self, level: int = 2, human_white: bool = True,
                 stockfish_path: str = ""):
        if chess is None:
            raise RuntimeError("python-chess is not installed. Run: pip install chess")
        self.board = chess.Board()
        self.human_white = human_white
        self.engine = make_engine(level, stockfish_path)
        self.state = IDLE
        self.last_move: Optional[chess.Move] = None
        self.pending: Optional[chess.Move] = None      # engine's move awaiting the arm
        self.pending_before = None                     # position before that move
        self.history: List[str] = []
        self.result = ""
        self.think_ms = 0.0
        self._replies: "queue.Queue" = queue.Queue()
        self._thread: Optional[threading.Thread] = None

    # -- helpers -------------------------------------------------------------
    @property
    def human_turn(self) -> bool:
        return self.board.turn == (chess.WHITE if self.human_white else chess.BLACK)

    def start(self) -> None:
        self.board.reset()
        self.history = []
        self.last_move = self.pending = None
        self.result = ""
        self.state = HUMAN if self.human_turn else THINKING
        if self.state == THINKING:
            self._think()

    def legal_targets(self, from_sq: int) -> List[int]:
        return [m.to_square for m in self.board.legal_moves if m.from_square == from_sq]

    def _finish_if_over(self) -> bool:
        if self.board.is_game_over():
            outcome = self.board.outcome()
            if outcome is None or outcome.winner is None:
                self.result = "draw"
            else:
                human_won = outcome.winner == (chess.WHITE if self.human_white else chess.BLACK)
                self.result = "you win" if human_won else "robot wins"
            self.state = OVER
            return True
        return False

    # -- turns ---------------------------------------------------------------
    def human_move(self, move) -> bool:
        if self.state != HUMAN or move not in self.board.legal_moves:
            return False
        self.history.append(self.board.san(move))
        self.board.push(move)
        self.last_move = move
        if not self._finish_if_over():
            self._think()
        return True

    def _think(self) -> None:
        self.state = THINKING
        board = self.board.copy()

        def run():
            t0 = time.perf_counter()
            try:
                mv = self.engine.choose(board)
            except Exception as exc:                   # engine crashed: pick anything
                mv = next(iter(board.legal_moves), None)
                self._replies.put(("error", str(exc)))
            self._replies.put(("move", mv, (time.perf_counter() - t0) * 1000.0))

        self._thread = threading.Thread(target=run, name="chess-engine", daemon=True)
        self._thread.start()

    def poll(self) -> List[tuple]:
        """Drain engine replies. When a move arrives it is applied to the board
        and left in `pending` for the arm; the UI then calls `robot_done`."""
        events = []
        while True:
            try:
                item = self._replies.get_nowait()
            except queue.Empty:
                break
            if item[0] == "move":
                mv, ms = item[1], item[2]
                self.think_ms = ms
                if mv is None:
                    self._finish_if_over()
                    events.append(("over", self.result))
                    continue
                self.pending_before = self.board.copy()   # the arm plans from this
                self.history.append(self.board.san(mv))
                self.board.push(mv)
                self.last_move = mv
                self.pending = mv
                self.state = ROBOT
                events.append(("engine", mv, ms))
            else:
                events.append(item)
        return events

    def robot_done(self) -> None:
        """The arm has physically played `pending` (or was told to skip it)."""
        self.pending = None
        if not self._finish_if_over():
            self.state = HUMAN

    def undo(self) -> bool:
        """Take back the last full move pair, human's and engine's."""
        if self.state not in (HUMAN, OVER) or len(self.board.move_stack) < 1:
            return False
        for _ in range(2 if len(self.board.move_stack) >= 2 else 1):
            self.board.pop()
            if self.history:
                self.history.pop()
        self.last_move = self.board.peek() if self.board.move_stack else None
        self.result = ""
        self.state = HUMAN if self.human_turn else THINKING
        if self.state == THINKING:
            self._think()
        return True

    def resign(self) -> None:
        self.result = "robot wins"
        self.state = OVER

    def close(self) -> None:
        if isinstance(self.engine, StockfishEngine):
            self.engine.close()
