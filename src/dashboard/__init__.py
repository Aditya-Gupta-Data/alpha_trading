"""src/dashboard — the read-only Streamlit showcase (decision #111, 2026-09-24).
MANUAL OFFLINE TOOL: not on any cron, imports nothing from the live paths
except read helpers, writes nothing. `data.py` is the pure data layer
(tested, no streamlit); `app.py` is the UI (needs `streamlit`)."""
