"""src.reporting — human-readable artifacts rendered FROM the ledgers.

Read-only by construction: nothing in this package writes to the journal,
the equity event stream or brain_map.db. It reads what the trading path
has already recorded and renders it for a human.
"""
