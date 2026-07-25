"""
src/view_positions.py — terminal view of open paper positions
==============================================================

The quick-SSH check: prints every open approved paper trade (equity
plans and options spreads) as an ASCII table, straight from
data/journal.jsonl. Strictly read-only — a plain file read, no database
connection, no locks, nothing mutated.

    python3 -m src.view_positions
"""

from src.positions import active_positions, format_table


def main() -> None:
    print(format_table(active_positions()))


if __name__ == "__main__":
    main()
