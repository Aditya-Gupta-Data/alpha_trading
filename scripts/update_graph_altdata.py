"""CLI wrapper to run the AltData GraphRAG Injector (`src/analysis/altdata_graph.py`).

Reads `data/lake/alt_data/{gem_tenders,agmarknet_mandi,vahan_registrations}.csv`
and idempotently upserts relational edges (`graph_edges`, `graph_nodes`) and
momentum modifiers (`altdata_momentum_modifiers`) into `data/brain_map.db`.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.analysis.altdata_graph import AltDataGraphInjector  # noqa: E402


def run_update(
    db_path: Optional[Path | str] = None,
    conn: Optional[sqlite3.Connection] = None,
    lake_dir: Optional[Path | str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Execute the AltData GraphRAG injector idempotently."""
    injector = AltDataGraphInjector(
        db_path=db_path,
        conn=conn,
        lake_dir=lake_dir,
    )
    return injector.run(dry_run=dry_run)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inject AltData (GeM, Agmarknet, Vahan) edges and momentum modifiers into brain_map.db"
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="Path to SQLite brain_map.db (defaults to data/brain_map.db)",
    )
    parser.add_argument(
        "--lake-dir",
        default=None,
        help="Path to AltData CSV directory (defaults to data/lake/alt_data)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute edges and velocities without mutating brain_map.db",
    )
    args = parser.parse_args(argv)
    summary = run_update(
        db_path=args.db_path,
        lake_dir=args.lake_dir,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
