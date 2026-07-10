"""
src/calibration — Phase §3/§4.2 analytics (self_evolving_brain_map.md).

Standalone, strictly READ-ONLY analysis tools that study historical trade
data to calibrate future exits. Nothing in this package writes to
brain_map.db, the journal, or any live state, and nothing here is
imported by the trading execution loop.
"""
