"""Event-driven strategy modules (Week-1 release lane, 2026-08-16).

NOT `src/strategy/` — `src/strategy.py` (StrategyConstructor, the live spread
builder) already owns that import name; a package with the same name would
shadow it and break every `from src.strategy import ...` in V1."""
