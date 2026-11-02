"""
src/equity_option_cap.py — the stock-option sub-cap (owner rule, 2026-11-02)
============================================================================

THE RULE (owner, verbatim intent): single-stock options (RELIANCE, TCS,
ICICIBANK, HDFCBANK, INFY — `options_proposer.is_equity_option`) may never
consume more than EQUITY_OPTION_CAP_PCT (33%) of the active options budget.
When open stock-option margin plus a new entry would pass that ceiling, the
NEW stock-option entry is refused. Index options are not touched by this
module at all (they stay bounded only by the main bucket / cash gates). The
Shadow Learner is not touched either: a refused setup goes down the
proposer's existing learner path, exactly like an exposure-gate refusal.

WHY THE RULE EXISTS (the 11-02 market-vs-skill study, VM data): index
spreads earned ~+Rs.2.0L over 39 settled trades; stock options were mixed
(ICICIBANK 9 trades -Rs.2,827, INFY -Rs.8,627, HDFCBANK -Rs.2,432) and are
physically settled. The owner chose a collar, not a ban.

DEFINITIONS (one place, so the number can be audited):
  * consumed  = the margin of every ACTIVE primary lock (`margin_locks`,
                released_at IS NULL, not an `eqd:` darling lock) whose
                journal row is a stock option. For a debit spread that lock
                is its max loss (#145 M). The entry being judged is excluded
                (an approval re-judges its own proposal-time lock).
  * budget    = the active options budget:
                - multi-bucket ledger ON and the IDX_SPREADS bucket row
                  present -> the bucket's ceiling (BUCKET_CEILINGS_PCT, 25% of
                  firm equity) capped at the bucket's own equity: the most
                  the options book may deploy;
                - bucket row present, ledger OFF -> the bucket's equity
                  (starting capital + realized), the capital the M1 migration
                  assigned to options;
                - no bucket row -> PAPER_10L flat equity (pre-M1, the options
                  book WAS the account).
  * ceiling   = budget x EQUITY_OPTION_CAP_PCT / 100.
  * allowed   = consumed + this entry's margin <= ceiling. "Never more than
                33%" means the entry that WOULD cross the line is refused,
                not the one after it.

FAIL-CLOSED for stock options only: an unreadable ledger or a budget <= 0
refuses the stock-option entry with the reason named (a risk cap that
fails open is not a cap). Index options never reach the check, so a broken
read here can never stop the index book. Switch: config
`equity_option_cap_pct` (default 33; 0 or below = rule off).

Pure reads; the only write is one `account_events` row per refusal
(`equity_option_cap_refused`), the same audit trail every primary refusal
leaves. Paper money only.
"""
from __future__ import annotations

REFUSED_EVENT = "equity_option_cap_refused"
REASON_TAG = "stock-option sub-cap"          # proposal_ledger's REJECTED_EQUITY_CAP needle
DARLINGS_LOCK_PREFIX = "eqd:"


def cap_pct() -> float:
    try:
        from src.config import EQUITY_OPTION_CAP_PCT
        return float(EQUITY_OPTION_CAP_PCT)
    except Exception:
        return 33.0


def _is_equity_option(ticker) -> bool:
    from src.options_proposer import is_equity_option
    return is_equity_option(ticker)


def _journal_tickers(entries=None) -> dict:
    """{short_id: ticker} over the journal (or the rows passed in)."""
    if entries is None:
        from src import journal
        entries = journal.read_all()
    return {e.get("short_id"): e.get("ticker") for e in entries or [] if e.get("short_id")}


def consumed_rs(conn, entries=None, exclude_ref: str = None) -> float:
    """Active primary margin locked by stock-option entries (see module doc)."""
    tickers = _journal_tickers(entries)
    total = 0.0
    for ref, margin in conn.execute(
            "SELECT journal_ref, margin_rs FROM margin_locks WHERE released_at IS NULL").fetchall():
        if ref == exclude_ref or str(ref).startswith(DARLINGS_LOCK_PREFIX):
            continue
        if _is_equity_option(tickers.get(ref)):
            total += float(margin or 0.0)
    return round(total, 2)


def budget(conn) -> tuple[float | None, str]:
    """(active options budget in Rs, basis) — see module doc. None = unreadable."""
    from src import buckets, portfolio_manager as pm
    pid = f"{buckets.PRIMARY_ACCOUNT}/{buckets.IDX_FAMILY}"
    b = buckets.bucket(conn, pid)
    if b is not None:
        eq = buckets.equity(b)
        if buckets.enabled():
            st = buckets.ceiling_state(conn, pid)
            if st is not None:
                return round(min(eq, st["ceiling_rs"]), 2), (
                    f"IDX_SPREADS ceiling {st['ceiling_pct']:g}% of firm equity, capped at bucket equity")
        return round(eq, 2), "IDX_SPREADS bucket equity"
    return round(pm.equity(conn), 2), "PAPER_10L equity (no IDX_SPREADS bucket row)"


def state(conn, entries=None, exclude_ref: str = None) -> dict:
    """The cap's full reading, for cards / tests / the CLI."""
    pct = cap_pct()
    total, basis = budget(conn)
    used = consumed_rs(conn, entries, exclude_ref=exclude_ref)
    ceiling = None if total is None else round(max(0.0, total) * pct / 100.0, 2)
    return {"cap_pct": pct, "budget_rs": total, "budget_basis": basis,
            "consumed_rs": used, "ceiling_rs": ceiling,
            "headroom_rs": None if ceiling is None else round(ceiling - used, 2)}


def check(conn, ticker: str, margin_rs: float, journal_ref: str = None,
          entries=None, log: bool = True) -> tuple[bool, str | None]:
    """(allowed, reason) for OPENING `margin_rs` of a position on `ticker`.
    Index options and a disabled rule pass untouched. A stock option passes
    only while consumed + margin <= ceiling; a refusal is logged once to
    account_events (unless `log=False`)."""
    if not _is_equity_option(ticker):
        return True, None
    pct = cap_pct()
    if pct <= 0:
        return True, None
    try:
        st = state(conn, entries, exclude_ref=journal_ref)
    except Exception as exc:
        reason = f"{REASON_TAG}: cap state unreadable ({exc}) — stock-option entry refused (fails closed)"
        if log:
            _log(conn, journal_ref, reason)
        return False, reason
    if not st["budget_rs"] or st["budget_rs"] <= 0:
        reason = (f"{REASON_TAG}: options budget unreadable or zero ({st['budget_basis']}) — "
                  "stock-option entry refused (fails closed)")
        if log:
            _log(conn, journal_ref, reason)
        return False, reason
    margin = round(max(0.0, float(margin_rs or 0.0)), 2)
    if round(st["consumed_rs"] + margin, 2) <= st["ceiling_rs"]:
        return True, None
    reason = (f"{REASON_TAG}: open stock options Rs.{st['consumed_rs']:,.2f} + this "
              f"Rs.{margin:,.2f} would pass {st['cap_pct']:g}% of the options budget "
              f"(ceiling Rs.{st['ceiling_rs']:,.2f} of Rs.{st['budget_rs']:,.2f}, {st['budget_basis']}) "
              "— index options unaffected")
    if log:
        _log(conn, journal_ref, reason)
    return False, reason


def _log(conn, journal_ref, reason) -> None:
    try:
        from src import portfolio_manager as pm
        pm.log_event(conn, REFUSED_EVENT, f"entry {journal_ref or '?'} rejected ({reason})")
    except Exception:
        pass


def gate(ticker: str, margin_rs: float, journal_ref: str = None, conn=None,
         entries=None) -> tuple[bool, str | None]:
    """`check` on the real firm database (opened and closed here when no
    `conn` is passed). Index options return before any database is opened."""
    if not _is_equity_option(ticker) or cap_pct() <= 0:
        return True, None
    owns = conn is None
    try:
        if owns:
            from src import brain_map
            conn = brain_map.connect()
        return check(conn, ticker, margin_rs, journal_ref=journal_ref, entries=entries)
    except Exception as exc:
        return False, f"{REASON_TAG}: firm database unavailable ({exc}) — stock-option entry refused (fails closed)"
    finally:
        if owns and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":       # read-only: python3 -m src.equity_option_cap
    import json
    from src import brain_map
    c = brain_map.connect()
    try:
        print(json.dumps(state(c), indent=2))
    finally:
        c.close()
