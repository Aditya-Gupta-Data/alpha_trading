"""
src/analysis/altdata_graph.py — AltData GraphRAG Injector & Momentum Modifier Engine
====================================================================================

Translates flat alternative-data CSV telemetry in `data/lake/alt_data/` (`gem_tenders.csv`,
`agmarknet_mandi.csv`, `vahan_registrations.csv`) into relational GraphRAG edges
(`graph_edges`, `graph_nodes`) and equity momentum modifiers (`altdata_momentum_modifiers`)
inside `data/brain_map.db`.

Wiring Rules:
  1. GeM Tenders (`gem_tenders.csv`):
     * Edge: `[TICKER] --WON_CONTRACT--> [GOVT_ENTITY]`
     * Weight: `log(bid_value)` (natural log of contract value in INR by default).
  2. Agmarknet Mandi Prices (`agmarknet_mandi.csv`):
     * Static dependency map (`AGRI_DEPENDENCY_MAP`): maps agricultural staples
       (`Wheat`, `Sugar`, `Tomatoes`, `Onions`) to downstream FMCG/QSR equities.
     * Calculates 7-day price velocity: `v_7d = (P_t - P_{t-7}) / P_{t-7}`.
     * Edge: `[COMMODITY] --INPUT_COST_PRESSURE--> [TICKER]`
     * Weight: inversely scaled to price velocity (`weight = -v_7d * scale`), so rising
       raw-material prices exert negative momentum pressure (`weight < 0`).
  3. Vahan Auto Registrations (`vahan_registrations.csv`):
     * Aggregates daily registrations (`EV` + `ICE`) per `ticker`.
     * Calculates 7-day registration velocity: `v_7d = (R_t - R_{t-7}) / R_{t-7}`.
     * Edge: `[TICKER] --VEHICLE_SALES_MOMENTUM--> VAHAN_REGISTRATIONS` (plus row in
       `altdata_momentum_modifiers`).

Standing Constraints:
  * Provenance firewall preserved: all edges written by this module carry
    `source = 'altdata_telemetry'` and `extractor = 'altdata_graph'`; only a
    relation promoted by `scripts/run_proving_court.py --promote` (table
    `altdata_class_promotions`) is written as `'outcome_derived'`.
  * 100% idempotent SQLite upserts (`ON CONFLICT DO UPDATE`).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import math
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src import brain_map, graph_engine
from src.ingestion.alt_data.agmarknet_mandi import normalize_agri_commodity
from src.ingestion.alt_data.base_scraper import ALT_DATA_LAKE
from src.ingestion.alt_data.gem_tenders import match_listed_ticker, parse_bid_value_inr
from src.ingestion.alt_data.vahan_dashboard import normalize_maker
from src.knowledge_graph import graphrag

logger = logging.getLogger(__name__)

ALTDATA_SOURCE_TAG = "altdata_telemetry"
ALTDATA_EXTRACTOR_TAG = "altdata_graph"
DEFAULT_ALTDATA_DECAY_LAMBDA = 0.03

# Static commodity -> downstream NSE equity dependency dictionary
AGRI_DEPENDENCY_MAP: Dict[str, List[str]] = {
    "Wheat": ["ITC.NS", "BRITANNIA.NS", "NESTLEIND.NS", "HINDUNILVR.NS"],
    "Sugar": ["ITC.NS", "BRITANNIA.NS", "NESTLEIND.NS", "DABUR.NS", "TATACONSUM.NS"],
    "Tomatoes": ["NESTLEIND.NS", "HINDUNILVR.NS", "ITC.NS", "JUBLFOOD.NS"],
    "Onions": ["JUBLFOOD.NS", "DEVYANI.NS", "WESTLIFE.NS", "ITC.NS"],
}

COMMODITY_EQUITY_MAP = AGRI_DEPENDENCY_MAP

# Category keyword heuristics when a GeM row does not explicitly carry a buyer/ministry column
_GEM_CATEGORY_MINISTRY_RULES: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:radar|defence|defense|ammo|ammunition|missile|naval|sonar|artillery|armoured)\b", re.I), "MINISTRY_OF_DEFENCE"),
    (re.compile(r"\b(?:railway|locomotive|wagon|coach|signalling|track|rvnl|ircon)\b", re.I), "MINISTRY_OF_RAILWAYS"),
    (re.compile(r"\b(?:solar|transformer|substation|gis|turbine|boiler|grid|power|transmission)\b", re.I), "MINISTRY_OF_POWER"),
    (re.compile(r"\b(?:bus|buses|electric\s+bus|utility\s+vehicle|ambulance|fleet|highway|toll)\b", re.I), "MORTH_STATE_TRANSPORT"),
    (re.compile(r"\b(?:server|cloud|software|network|datacenter|cyber|it\s+system)\b", re.I), "MEITY_DIGITAL_INDIA"),
)

_ALTDATA_MODIFIERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS altdata_momentum_modifiers (
    date              TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    signal_type       TEXT NOT NULL,
    source_entity     TEXT NOT NULL,
    velocity_7d       REAL,
    edge_weight       REAL NOT NULL,
    momentum_modifier REAL NOT NULL,
    metadata_json     TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (date, ticker, signal_type, source_entity)
);
CREATE INDEX IF NOT EXISTS idx_altdata_modifiers_ticker_date
    ON altdata_momentum_modifiers (ticker, date);
"""


def ensure_altdata_graph_schema(conn: sqlite3.Connection) -> None:
    """Ensure `graph_edges`, `graph_nodes`, and `altdata_momentum_modifiers` exist in `brain_map.db`."""
    graphrag.ensure_schema(conn)
    conn.executescript(_ALTDATA_MODIFIERS_SCHEMA)
    conn.commit()


def _normalize_entity_token(raw: Any, default: str = "GOVT_OF_INDIA") -> str:
    cleaned = re.sub(r"[^A-Z0-9&_]+", "_", str(raw or "").strip().upper())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or default


def resolve_govt_entity(row: Dict[str, Any], default_entity: str = "GOVT_OF_INDIA") -> str:
    """Resolve the target `[GOVT_ENTITY]` node ID from a GeM tender row."""
    for key in (
        "govt_entity",
        "buyer",
        "buyer_name",
        "ministry",
        "department",
        "organization",
        "organisation",
        "procuring_entity",
    ):
        val = row.get(key)
        if val and str(val).strip():
            return _normalize_entity_token(val, default=default_entity)

    category = str(row.get("item_category") or row.get("category") or "").strip()
    for pattern, entity in _GEM_CATEGORY_MINISTRY_RULES:
        if pattern.search(category):
            return entity
    return default_entity


def resolve_gem_ticker(row: Dict[str, Any]) -> str:
    """Resolve canonical `.NS` ticker from a GeM CSV row."""
    raw_ticker = str(row.get("ticker") or "").strip().upper()
    if raw_ticker:
        return raw_ticker if raw_ticker.endswith(".NS") else f"{raw_ticker}.NS"
    matched = match_listed_ticker(str(row.get("winning_bidder") or ""))
    return matched


def compute_gem_edge_weight(bid_value: float, log_base: float = math.e) -> float:
    """Compute log-scaled edge weight from `bid_value` (in INR).

    Uses `math.log(bid_value, log_base)` (natural log `ln(bid_value)` by default).
    Returns `0.0` if `bid_value <= 1.0`.
    """
    val = float(bid_value or 0.0)
    if val <= 1.0:
        return 0.0
    if log_base == math.e:
        return round(math.log(val), 6)
    return round(math.log(val, log_base), 6)


def compute_input_cost_pressure_weight(velocity_7d: float, scale: float = 1.0) -> float:
    """Scale `INPUT_COST_PRESSURE` edge weight inversely to 7-day commodity price velocity.

    Higher commodity costs (`velocity_7d > 0`) produce a negative weight (`< 0`),
    representing margin headwind / negative momentum pressure on consumer equities.
    Falling commodity costs (`velocity_7d < 0`) produce a positive weight (`> 0`).
    """
    val = -float(velocity_7d) * float(scale)
    return round(0.0 if val == 0.0 else val, 6)


def compute_7d_velocity(
    dated_values: Sequence[Tuple[str, float]],
    window_days: int = 7,
) -> Optional[Dict[str, Any]]:
    """Compute 7-day point-to-point velocity `(V_t - V_{t-7}) / V_{t-7}` over `(date, value)` pairs.

    If multiple observations share the same date, they are averaged first (for prices)
    before calling this helper, or passed pre-aggregated.
    `base_date` is the latest observation on or before `latest_date - window_days`. Returns None
    (abstain) when no observation is that old, or when the nearest one is more than
    `2 * window_days` old: a shorter or much longer span is never reported as a 7-day velocity.
    """
    parsed: List[Tuple[dt.date, str, float]] = []
    for raw_d, raw_v in dated_values:
        if raw_v is None:
            continue
        try:
            d_str = str(raw_d).strip()[:10]
            d_obj = dt.date.fromisoformat(d_str)
            v_flt = float(raw_v)
        except (ValueError, TypeError):
            continue
        if v_flt <= 0:
            continue
        parsed.append((d_obj, d_str, v_flt))

    if len(parsed) < 2:
        return None

    parsed.sort(key=lambda x: x[0])
    latest_dt, latest_str, latest_val = parsed[-1]
    prior = [p for p in parsed[:-1] if p[0] < latest_dt]
    if not prior:
        return None

    window = max(1, int(window_days))
    target_dt = latest_dt - dt.timedelta(days=window)
    on_or_before_target = [p for p in prior if p[0] <= target_dt]
    if not on_or_before_target:
        return None
    base_dt, base_str, base_val = on_or_before_target[-1]
    if (latest_dt - base_dt).days > 2 * window:
        return None

    if base_val <= 0:
        return None

    velocity = (latest_val - base_val) / base_val
    return {
        "latest_date": latest_str,
        "base_date": base_str,
        "span_days": (latest_dt - base_dt).days,
        "latest_value": round(latest_val, 4),
        "base_value": round(base_val, 4),
        "velocity_7d": round(velocity, 6),
    }


def _read_csv_rows(csv_path: Path) -> List[Dict[str, str]]:
    if not csv_path.is_file():
        return []
    try:
        text = csv_path.read_text(encoding="utf-8")
        return list(csv.DictReader(io.StringIO(text)))
    except OSError:
        return []


def _edge_source_for(conn: sqlite3.Connection, relation: str) -> str:
    """`outcome_derived` for a relation the proving court promoted, else telemetry."""
    try:
        row = conn.execute(
            "SELECT 1 FROM altdata_class_promotions WHERE relation = ?", (relation,)
        ).fetchone()
    except sqlite3.Error:
        row = None
    return "outcome_derived" if row else ALTDATA_SOURCE_TAG


def _upsert_graph_edge_and_nodes(
    conn: sqlite3.Connection,
    source_node: str,
    source_type: str,
    relation: str,
    target_node: str,
    target_type: str,
    weight: float,
    confidence_score: float,
    polarity: int,
    edge_class: str,
    context_dict: Dict[str, Any],
    valid_from: str,
    decay_lambda: float = DEFAULT_ALTDATA_DECAY_LAMBDA,
) -> None:
    """Idempotently upsert `graph_nodes` and `graph_edges` for an AltData telemetry edge."""
    graphrag.upsert_node(
        conn,
        node_id=source_node,
        node_type=source_type,
        display_name=source_node,
        in_universe=1 if source_node.endswith(".NS") else 0,
        now=valid_from,
        increment_mention=True,
    )
    graphrag.upsert_node(
        conn,
        node_id=target_node,
        node_type=target_type,
        display_name=target_node,
        in_universe=1 if target_node.endswith(".NS") else 0,
        now=valid_from,
        increment_mention=True,
    )

    edge_source = _edge_source_for(conn, relation)
    context_str = json.dumps(context_dict, sort_keys=True)
    conf_clamped = round(max(0.0, min(1.0, float(confidence_score))), 6)
    weight_rounded = round(float(weight), 6)

    graph_engine.add_edge(
        conn,
        source_node=source_node,
        relation=relation,
        target_node=target_node,
        confidence_score=conf_clamped,
        context=context_str,
        decay_lambda=decay_lambda,
        valid_from=valid_from,
        source=edge_source,
    )
    conn.execute(
        """
        UPDATE graph_edges
        SET edge_class = ?,
            polarity = ?,
            weight = ?,
            evidence_count = COALESCE(evidence_count, 1),
            extractor = ?,
            source = ?
        WHERE source_node = ? AND relation = ? AND target_node = ?
        """,
        (
            edge_class,
            int(polarity),
            weight_rounded,
            ALTDATA_EXTRACTOR_TAG,
            edge_source,
            source_node,
            relation,
            target_node,
        ),
    )


def _upsert_momentum_modifier(
    conn: sqlite3.Connection,
    date: str,
    ticker: str,
    signal_type: str,
    source_entity: str,
    velocity_7d: Optional[float],
    edge_weight: float,
    momentum_modifier: float,
    metadata: Dict[str, Any],
    updated_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO altdata_momentum_modifiers (
            date, ticker, signal_type, source_entity,
            velocity_7d, edge_weight, momentum_modifier,
            metadata_json, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (date, ticker, signal_type, source_entity) DO UPDATE SET
            velocity_7d = excluded.velocity_7d,
            edge_weight = excluded.edge_weight,
            momentum_modifier = excluded.momentum_modifier,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            str(date)[:10],
            ticker,
            signal_type,
            source_entity,
            None if velocity_7d is None else round(float(velocity_7d), 6),
            round(float(edge_weight), 6),
            round(float(momentum_modifier), 6),
            json.dumps(metadata, sort_keys=True),
            updated_at,
        ),
    )


class AltDataGraphInjector:
    """Translates GeM, Agmarknet, and Vahan AltData CSVs into `brain_map.db` GraphRAG edges."""

    def __init__(
        self,
        db_path: Path | str | None = None,
        conn: sqlite3.Connection | None = None,
        lake_dir: Path | str | None = None,
        dependency_map: Optional[Dict[str, List[str]]] = None,
        log_base: float = math.e,
        cost_pressure_scale: float = 1.0,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else brain_map.DEFAULT_DB_PATH
        self._external_conn = conn
        self.lake_dir = Path(lake_dir) if lake_dir is not None else ALT_DATA_LAKE
        self.dependency_map: Dict[str, List[str]] = (
            dict(dependency_map) if dependency_map is not None else dict(AGRI_DEPENDENCY_MAP)
        )
        self.log_base = float(log_base)
        self.cost_pressure_scale = float(cost_pressure_scale)

    def _get_conn(self) -> Tuple[sqlite3.Connection, bool]:
        if self._external_conn is not None:
            ensure_altdata_graph_schema(self._external_conn)
            return self._external_conn, False
        conn = brain_map.connect(self.db_path)
        ensure_altdata_graph_schema(conn)
        return conn, True

    def inject_gem_tenders(
        self,
        rows: Optional[Iterable[Dict[str, Any]]] = None,
        csv_path: Optional[Path] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Map GeM CSV rows into `[TICKER] --WON_CONTRACT--> [GOVT_ENTITY]` weighted by `log(bid_value)`."""
        target_csv = csv_path or (self.lake_dir / "gem_tenders.csv")
        raw_rows = list(rows) if rows is not None else _read_csv_rows(target_csv)

        # Group contracts by (ticker, govt_entity) deduplicating on bid_number
        grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
        seen_bids: set[Tuple[str, str, str]] = set()

        for r in raw_rows:
            ticker = resolve_gem_ticker(r)
            if not ticker:
                continue
            bid_val = parse_bid_value_inr(r.get("bid_value"))
            if bid_val is None or bid_val <= 1.0:
                continue
            govt_entity = resolve_govt_entity(r)
            bid_no = str(r.get("bid_number") or "").strip()
            dedup_key = (ticker, govt_entity, bid_no or f"{r.get('date')}:{bid_val}")
            if dedup_key in seen_bids:
                continue
            seen_bids.add(dedup_key)

            day = str(r.get("date") or dt.date.today().isoformat())[:10]
            bucket = grouped.setdefault(
                (ticker, govt_entity),
                {
                    "total_bid_value": 0.0,
                    "contract_count": 0,
                    "latest_date": day,
                    "categories": [],
                    "bid_numbers": [],
                },
            )
            bucket["total_bid_value"] += float(bid_val)
            bucket["contract_count"] += 1
            if day > bucket["latest_date"]:
                bucket["latest_date"] = day
            cat = str(r.get("item_category") or "").strip()
            if cat and cat not in bucket["categories"]:
                bucket["categories"].append(cat)
            if bid_no and bid_no not in bucket["bid_numbers"]:
                bucket["bid_numbers"].append(bid_no)

        edges_written: List[Dict[str, Any]] = []
        conn, owns_conn = self._get_conn()
        try:
            for (ticker, govt_entity), info in sorted(grouped.items()):
                total_val = round(info["total_bid_value"], 2)
                weight = compute_gem_edge_weight(total_val, log_base=self.log_base)
                # Normalize confidence onto (0, 1] where ln(1000 Cr = 1e10) ~ 23.03
                conf = round(min(1.0, max(0.05, weight / 25.0)), 6)
                modifier = round(min(1.0, weight / 25.0), 6)
                day = info["latest_date"]
                meta = {
                    "bid_value_inr": total_val,
                    "log_weight": weight,
                    "log10_weight": round(math.log10(total_val), 6) if total_val > 0 else 0.0,
                    "contract_count": info["contract_count"],
                    "categories": info["categories"][:5],
                    "bid_numbers": info["bid_numbers"][:5],
                    "date": day,
                }
                if not dry_run:
                    _upsert_graph_edge_and_nodes(
                        conn,
                        source_node=ticker,
                        source_type="company",
                        relation="WON_CONTRACT",
                        target_node=govt_entity,
                        target_type="policy",
                        weight=weight,
                        confidence_score=conf,
                        polarity=1,
                        edge_class="demand",
                        context_dict=meta,
                        valid_from=day,
                    )
                    _upsert_momentum_modifier(
                        conn,
                        date=day,
                        ticker=ticker,
                        signal_type="WON_CONTRACT",
                        source_entity=govt_entity,
                        velocity_7d=None,
                        edge_weight=weight,
                        momentum_modifier=modifier,
                        metadata=meta,
                        updated_at=day,
                    )
                edges_written.append(
                    {
                        "source_node": ticker,
                        "relation": "WON_CONTRACT",
                        "target_node": govt_entity,
                        "bid_value_inr": total_val,
                        "weight": weight,
                        "confidence_score": conf,
                        "date": day,
                    }
                )
            if not dry_run:
                conn.commit()
        finally:
            if owns_conn:
                conn.close()

        return {
            "source": "gem_tenders",
            "rows_read": len(raw_rows),
            "edges_upserted": len(edges_written),
            "edges": edges_written,
            "dry_run": bool(dry_run),
        }

    def inject_agmarknet_mandi(
        self,
        rows: Optional[Iterable[Dict[str, Any]]] = None,
        csv_path: Optional[Path] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Compute 7-day commodity price velocity and inject `INPUT_COST_PRESSURE` edges."""
        target_csv = csv_path or (self.lake_dir / "agmarknet_mandi.csv")
        raw_rows = list(rows) if rows is not None else _read_csv_rows(target_csv)

        # Group daily prices by (commodity, date)
        by_comm_date: Dict[str, Dict[str, List[float]]] = {}
        for r in raw_rows:
            comm = normalize_agri_commodity(str(r.get("commodity") or ""))
            if not comm or comm not in self.dependency_map:
                continue
            day = str(r.get("date") or "").strip()[:10]
            if not day:
                continue
            raw_px = (
                r.get("modal_price_quintal")
                if r.get("modal_price_quintal") not in (None, "")
                else r.get("modal_price")
                if r.get("modal_price") not in (None, "")
                else r.get("modal_price_kg")
            )
            try:
                px = float(str(raw_px).replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            if px <= 0:
                continue
            by_comm_date.setdefault(comm, {}).setdefault(day, []).append(px)

        edges_written: List[Dict[str, Any]] = []
        velocities: Dict[str, Dict[str, Any]] = {}
        conn, owns_conn = self._get_conn()
        try:
            for comm, date_map in sorted(by_comm_date.items()):
                daily_means = [
                    (d, sum(vals) / len(vals)) for d, vals in sorted(date_map.items()) if vals
                ]
                vel_info = compute_7d_velocity(daily_means, window_days=7)
                if vel_info is None:
                    continue
                velocities[comm] = vel_info
                vel_7d = float(vel_info["velocity_7d"])
                weight = compute_input_cost_pressure_weight(
                    vel_7d, scale=self.cost_pressure_scale
                )
                polarity = -1 if vel_7d > 0 else (1 if vel_7d < 0 else 0)
                conf = round(min(1.0, max(0.05, abs(weight))), 6)
                modifier = round(max(-1.0, min(1.0, weight)), 6)
                day = vel_info["latest_date"]

                for raw_ticker in self.dependency_map.get(comm, []):
                    ticker = (
                        raw_ticker.upper()
                        if raw_ticker.upper().endswith(".NS")
                        else f"{raw_ticker.upper()}.NS"
                    )
                    meta = {
                        "commodity": comm,
                        "latest_date": vel_info["latest_date"],
                        "base_date": vel_info["base_date"],
                        "latest_price_quintal": vel_info["latest_value"],
                        "base_price_quintal": vel_info["base_value"],
                        "velocity_7d": vel_7d,
                        "edge_weight": weight,
                    }
                    if not dry_run:
                        _upsert_graph_edge_and_nodes(
                            conn,
                            source_node=comm,
                            source_type="commodity",
                            relation="INPUT_COST_PRESSURE",
                            target_node=ticker,
                            target_type="company",
                            weight=weight,
                            confidence_score=conf,
                            polarity=polarity,
                            edge_class="macro_sensitivity",
                            context_dict=meta,
                            valid_from=day,
                        )
                        _upsert_momentum_modifier(
                            conn,
                            date=day,
                            ticker=ticker,
                            signal_type="INPUT_COST_PRESSURE",
                            source_entity=comm,
                            velocity_7d=vel_7d,
                            edge_weight=weight,
                            momentum_modifier=modifier,
                            metadata=meta,
                            updated_at=day,
                        )
                    edges_written.append(
                        {
                            "source_node": comm,
                            "relation": "INPUT_COST_PRESSURE",
                            "target_node": ticker,
                            "velocity_7d": vel_7d,
                            "weight": weight,
                            "polarity": polarity,
                            "date": day,
                        }
                    )
            if not dry_run:
                conn.commit()
        finally:
            if owns_conn:
                conn.close()

        return {
            "source": "agmarknet_mandi",
            "rows_read": len(raw_rows),
            "commodities_evaluated": len(velocities),
            "velocities": velocities,
            "edges_upserted": len(edges_written),
            "edges": edges_written,
            "dry_run": bool(dry_run),
        }

    def inject_vahan_registrations(
        self,
        rows: Optional[Iterable[Dict[str, Any]]] = None,
        csv_path: Optional[Path] = None,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """Compute 7-day registration velocity per auto `ticker` and inject `VEHICLE_SALES_MOMENTUM`."""
        target_csv = csv_path or (self.lake_dir / "vahan_registrations.csv")
        raw_rows = list(rows) if rows is not None else _read_csv_rows(target_csv)

        # Sum daily registrations (EV + ICE) per (ticker, date)
        by_ticker_date: Dict[str, Dict[str, float]] = {}
        ev_by_ticker_date: Dict[str, Dict[str, float]] = {}

        for r in raw_rows:
            ticker = str(r.get("ticker") or "").strip().upper()
            if not ticker:
                maker_info = normalize_maker(str(r.get("maker") or ""))
                if maker_info:
                    ticker = maker_info[1]
            if not ticker:
                continue
            if not ticker.endswith(".NS"):
                ticker = f"{ticker}.NS"

            day = str(r.get("date") or "").strip()[:10]
            if not day:
                continue
            try:
                reg = float(str(r.get("registrations") or "0").replace(",", "").strip())
            except (ValueError, TypeError):
                continue
            if reg < 0:
                continue

            by_ticker_date.setdefault(ticker, {})[day] = (
                by_ticker_date.get(ticker, {}).get(day, 0.0) + reg
            )
            if str(r.get("fuel_type") or "").strip().upper() == "EV":
                ev_by_ticker_date.setdefault(ticker, {})[day] = (
                    ev_by_ticker_date.get(ticker, {}).get(day, 0.0) + reg
                )

        edges_written: List[Dict[str, Any]] = []
        conn, owns_conn = self._get_conn()
        try:
            for ticker, date_totals in sorted(by_ticker_date.items()):
                daily_series = [(d, tot) for d, tot in sorted(date_totals.items()) if tot > 0]
                vel_info = compute_7d_velocity(daily_series, window_days=7)
                if vel_info is None:
                    continue

                vel_7d = float(vel_info["velocity_7d"])
                weight = round(vel_7d, 6)
                polarity = 1 if vel_7d >= 0 else -1
                conf = round(min(1.0, max(0.05, abs(weight))), 6)
                modifier = round(max(-1.0, min(1.0, weight)), 6)
                day = vel_info["latest_date"]
                ev_latest = ev_by_ticker_date.get(ticker, {}).get(day, 0.0)
                ev_share = (
                    round((ev_latest / vel_info["latest_value"]) * 100.0, 2)
                    if vel_info["latest_value"] > 0
                    else 0.0
                )

                meta = {
                    "ticker": ticker,
                    "latest_date": vel_info["latest_date"],
                    "base_date": vel_info["base_date"],
                    "latest_registrations": vel_info["latest_value"],
                    "base_registrations": vel_info["base_value"],
                    "velocity_7d": vel_7d,
                    "ev_share_pct": ev_share,
                }
                target_node = "VAHAN_REGISTRATIONS"
                if not dry_run:
                    _upsert_graph_edge_and_nodes(
                        conn,
                        source_node=ticker,
                        source_type="company",
                        relation="VEHICLE_SALES_MOMENTUM",
                        target_node=target_node,
                        target_type="macro",
                        weight=weight,
                        confidence_score=conf,
                        polarity=polarity,
                        edge_class="demand",
                        context_dict=meta,
                        valid_from=day,
                    )
                    _upsert_momentum_modifier(
                        conn,
                        date=day,
                        ticker=ticker,
                        signal_type="VEHICLE_SALES_MOMENTUM",
                        source_entity=target_node,
                        velocity_7d=vel_7d,
                        edge_weight=weight,
                        momentum_modifier=modifier,
                        metadata=meta,
                        updated_at=day,
                    )
                edges_written.append(
                    {
                        "source_node": ticker,
                        "relation": "VEHICLE_SALES_MOMENTUM",
                        "target_node": target_node,
                        "velocity_7d": vel_7d,
                        "weight": weight,
                        "polarity": polarity,
                        "date": day,
                    }
                )
            if not dry_run:
                conn.commit()
        finally:
            if owns_conn:
                conn.close()

        return {
            "source": "vahan_registrations",
            "rows_read": len(raw_rows),
            "edges_upserted": len(edges_written),
            "edges": edges_written,
            "dry_run": bool(dry_run),
        }

    def run(self, dry_run: bool = False) -> Dict[str, Any]:
        """Run all 3 AltData GraphRAG injectors (`GeM`, `Agmarknet`, `Vahan`) idempotently."""
        gem_res = self.inject_gem_tenders(dry_run=dry_run)
        agmark_res = self.inject_agmarknet_mandi(dry_run=dry_run)
        vahan_res = self.inject_vahan_registrations(dry_run=dry_run)
        total_edges = (
            gem_res["edges_upserted"]
            + agmark_res["edges_upserted"]
            + vahan_res["edges_upserted"]
        )
        return {
            "ok": True,
            "total_edges_upserted": total_edges,
            "dry_run": bool(dry_run),
            "gem": gem_res,
            "agmarknet": agmark_res,
            "vahan": vahan_res,
        }
