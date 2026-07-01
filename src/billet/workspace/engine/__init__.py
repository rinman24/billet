"""Pure Workspace engines: port allocation and ssh-config rendering.

Engines hold the deterministic, side-effect-free policy of the Workspace subsystem. They
depend only on ``contracts`` (and ``shared``), never on the access layer, so they are
exhaustively unit-testable without any I/O.
"""
