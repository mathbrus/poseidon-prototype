<!-- A hand-run demo for a real model: the mock brain scripts nothing for it. -->
# Goal: a small Tetris in Python

Build a playable, self-contained Tetris in this repository, in Python, using
only the standard library (the sandbox cannot install packages).

The first slice is the game engine, not a graphical client:

- a 10×20 board
- the seven tetrominoes, with rotation
- gravity, locking, line clears, and a score
- a way to drive one tick from tests (spawn, move, rotate, step)
- pytest coverage of those behaviours

Scope this into tickets a delivery crew can implement, then request the
`feature-delivery` crew. Do not write the code yourself.
