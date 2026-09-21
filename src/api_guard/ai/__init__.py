"""The advisory layer.

Nothing in here can change a build result. The verdict is computed before any
of this runs, and `tests/test_boundaries.py` enforces that the modules which
decide pass/fail never import from this package.

The reason is not squeamishness about LLMs. A deployment gate has to give the
same answer twice for the same commit, and a model does not. So the
deterministic tools decide, and this explains what they decided.
"""
