"""Bootstrap package — DAG-leaf global state for the Tabvis session.

Currently hosts ``state.py`` (the session / cost / duration /
stats hub). Bootstrap modules must stay import-leaves (no intra-repo imports beyond pure
leaf re-exports) so they can be loaded before the rest of the spine.
"""
