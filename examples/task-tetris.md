# Task: Tetris engine

Add a small, self-contained Tetris engine to this repository, using only the
Python standard library.

1. Create `tetris.py` at the repository root with:
   - a 10-column × 20-row board
   - the seven tetrominoes (I, O, T, S, Z, J, L) and clockwise rotation
   - `Game` with at least:
     - `spawn()` — places a new piece at the top; returns False on game over
     - `move(dx, dy)` — translation; ignored when the move would collide
     - `rotate()` — clockwise rotation with wall kicks omitted is fine;
       ignored when the rotation would collide
     - `step()` — one gravity tick; locks the piece when it cannot fall,
       clears completed lines, adds 100 points per cleared line, and
       spawns the next piece
     - `score: int` and `game_over: bool`
   - a `if __name__ == "__main__"` block is optional; do not start an
     interactive loop that would hang pytest
2. Create `tests/test_tetris.py` asserting at least:
   - a spawned piece occupies cells on the board
   - `move(1, 0)` shifts the piece right when the destination is empty
   - `rotate()` on a T piece changes its occupied cells
   - locking a piece that completes a line clears that line and increases
     `score` by 100
   - `spawn()` after a full top row sets `game_over`

Keep the diff to these two files. The repository's whole test suite must
pass before you deliver. Work on a `feature/<slug>` branch; never deliver
to `main`.
