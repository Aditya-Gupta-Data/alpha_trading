# MANUAL OFFLINE TOOL — the Streamlit showcase (decision #111). Run:
#   streamlit run src/dashboard/app.py
"""
Agentic AI in Financial Systems — a read-only window on the paper desk:
treasury, live book with the profit ratchet, broker reconciliation, and the
autonomous event log. Every number comes from `src/dashboard/data.py`
(SQLite read-only, JSON ledgers); nothing here can place, size or settle.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from src.dashboard import data as d  # noqa: E402

st.set_page_config(page_title="Alpha Desk — Agentic AI", page_icon="🧭", layout="wide",
                   initial_sidebar_state="collapsed")
st.markdown("""
<style>
  .block-container {padding-top: 1.2rem; max-width: 1400px;}
  div[data-testid="stMetric"] {background: #11161f; border: 1px solid #1f2733; border-radius: 10px;
                               padding: 12px 16px;}
  div[data-testid="stMetricLabel"] {color: #8b95a7; font-size: 0.8rem; letter-spacing: .04em;}
  .parity {background: #0f2a1c; border: 1px solid #1f7a4a; color: #7ee2a8; padding: 14px 18px;
           border-radius: 10px; font-size: 1.05rem;}
  .mismatch {background: #2a1010; border: 1px solid #a33; color: #ff9b9b; padding: 14px 18px;
             border-radius: 10px; font-size: 1.05rem;}
  .unknown {background: #24200f; border: 1px solid #8a7a1f; color: #f1d97a; padding: 14px 18px;
            border-radius: 10px;}
  .foot {color: #6b7484; font-size: 0.78rem;}
</style>
""", unsafe_allow_html=True)


def rs(x):
    """Rupees the Indian way: lakhs above ₹1L, so a metric card never truncates."""
    if x is None:
        return "—"
    x = float(x)
    if abs(x) >= 1e7:
        return f"₹{x / 1e7:.2f} Cr"
    if abs(x) >= 1e5:
        return f"₹{x / 1e5:.2f} L"
    return f"₹{x:,.0f}"


def pct(x):
    return "—" if x is None else f"{x:.2f}%"


# Access gate (decision #112): when DASHBOARD_KEY is set in the environment
# (the always-on box), the page shows nothing until the viewer supplies it.
# Compared in constant time; the key never appears in the page or the repo.
import hmac  # noqa: E402
import os  # noqa: E402

_KEY = os.environ.get("DASHBOARD_KEY")
if _KEY:
    if not st.session_state.get("_authed"):
        st.title("🧭 Alpha Desk")
        given = st.text_input("Access key", type="password")
        if given and hmac.compare_digest(given, _KEY):
            st.session_state["_authed"] = True
            st.rerun()
        elif given:
            st.error("wrong key")
        st.stop()

fresh = d.freshness()
st.title("🧭 Alpha Desk — Agentic AI in Financial Systems")
st.caption("Paper money. Read-only. Every entry, size, exit and reconciliation below was decided by the "
           "engine under its numbered decision log — no order path exists. "
           f"brain_map as of {fresh.get('brain_map.db') or 'n/a'} · journal {fresh.get('journal') or 'n/a'} · "
           f"snapshot {fresh.get('market_snapshot') or 'n/a'} · recon {fresh.get('recon') or 'n/a'}")

# Auto-refresh (decision #112): the body re-runs every 5 minutes on its own,
# so a browser left open on the always-on box follows the ledger mirror
# (which the box pulls every 15 minutes) without anyone touching it.
@st.fragment(run_every="5m")
def _body():
  tab_t, tab_l, tab_r, tab_a = st.tabs(["🏦 Treasury", "📈 Live Book & Ratchets", "🔍 Compliance & Recon",
                                         "📜 Event Audit Log"])

  # ------------------------------------------------------------- treasury
  with tab_t:
      T = d.treasury()
      if T.get("error"):
          st.error(f"Treasury unavailable: {T['error']}")
      else:
          for acct, title in (("PAPER_10L", "Primary account — ₹10L paper pool"),
                              ("PAPER_2L", "Shadow account — ₹2L stress test (sized independently)"),
                              ("PAPER_2L_ROT", "Rotation arm — ₹2L with capital rotation / eviction (#115)")):
              a = T.get(acct)
              st.subheader(title)
              if not a:
                  st.info("no rows for this account yet")
                  continue
              u = a.get("unrealized_pnl")
              cover = f"priced on {a.get('marked_positions', 0)} of {a.get('open_positions', 0)} open"
              c1, c2, c3 = st.columns(3)
              c1.metric("True Net Equity", rs(a["net_equity"]) if a.get("net_equity") is not None else "—",
                        "realized + unrealized" if u is not None else "no live marks", delta_color="off")
              # delta carries the sign so Streamlit colours it green / red
              c2.metric("Unrealized P&L", f"{u:+,.0f}" if u is not None else "—",
                        (f"{u:+,.0f} · {cover}" if u is not None else cover),
                        delta_color="normal" if u is not None else "off")
              c3.metric("Realized P&L", rs(a["realized_pnl"]), f"realized equity {rs(a['equity'])}",
                        delta_color="off")
              c4, c5, c6 = st.columns(3)
              c4.metric("Active Drawdown", pct(a["drawdown_pct"]), f"peak {rs(a['peak_equity'])}", delta_color="inverse")
              c5.metric("Margin locked", rs(a["locked_margin"]), f"{a['open_locks']} open lock(s)")
              c6.metric("Liquid cash", rs(a["available_cash"]),
                        (f"{a['rejections']} refusal(s)" if a.get("rejections") is not None else None))
              if a.get("marks_as_of"):
                  st.caption(f"Marks: the engine's snapshot as of {a['marks_as_of']} — never a fresh quote.")
          curve = T.get("equity_curve") or []
          if curve:
              import pandas as pd
              st.subheader(f"Equity curve — PAPER_10L, realized, from the ₹10L base ({T.get('curve_epoch', '')})")
              df = pd.DataFrame({"time": pd.to_datetime([c["ts"] for c in curve]),
                                 "equity": [c["equity"] for c in curve]}).set_index("time")
              st.line_chart(df, height=220)   # datetime index = true time spacing
              st.caption("Before 7 Aug the pool was reset to ₹2L (21 Jul) and topped up by ₹8L (7 Aug) — "
                         "capital moves, not trading, so the view starts at the base (decision #116).")
          st.caption("Sizing is fixed-fractional per account (decision #106): the same structure is sized on "
                     "each account's own equity, so the ₹2L book refuses what the ₹10L book takes.")

  # ----------------------------------------------------------- live book
  with tab_l:
      rows = d.open_trades()
      st.subheader(f"Open positions — {len(rows)}")
      if rows:
          st.dataframe([{"Symbol": r["symbol"], "Strategy": r["strategy"], "Dir": r["direction"],
                         "Accounts": r["accounts"], "Lots/Qty": r["lots"], "Entered": r["entered"],
                         "Expiry": r["expiry"] or "—", "Max loss ₹": r["max_loss_rs"],
                         "MTM ₹": r["mtm_rs"], "Capture %": r["capture_pct"],
                         "Ratchet peak %": r["ratchet_peak_pct"], "Ratchet lock %": r["ratchet_lock_pct"],
                         "Exit rule": r["ratchet"], "Sizing note": r["sizing"] or ""}
                        for r in rows], width="stretch", hide_index=True)
      else:
          st.info("no open positions in the journal / equity ledger")
      st.caption("Directional spreads ride the asymmetric profit ratchet (decision #110): armed at 40% of max "
                 "profit → breakeven lock; 60→30, 80→50, 90→70; exit when capture falls below the lock. "
                 "Condors keep a static 65% take. Equity darlings trail 3×ATR(14) (decision #107). "
                 "MTM is the engine's own last snapshot — this page never quotes the broker.")
      outs = d.recent_outcomes()
      if outs:
          st.subheader("Recent settlements")
          st.dataframe([{"Settled": o["settled"], "Symbol": o["symbol"], "Strategy": o["strategy"],
                         "Resolution": o["resolution"], "P&L ₹": o["pnl_rs"], "R": o["r_multiple"],
                         "OMS ticket": o["ticket"] or "—"} for o in outs],
                       width="stretch", hide_index=True)

  # ------------------------------------------------------------- recon
  with tab_r:
      st.subheader("Broker ↔ AI book reconciliation (read-only recon engine, decision #94)")
      R = d.latest_recon()
      if R is None:
          st.markdown('<div class="unknown">No recon run recorded yet on this machine '
                      '(logs/recon.jsonl absent). Run: python3 -m src.execution.recon_engine</div>',
                      unsafe_allow_html=True)
      else:
          v = str(R.get("verdict") or "unknown").upper()
          css = {"PARITY": "parity", "MISMATCH": "mismatch"}.get(v, "unknown")
          head = {"PARITY": "✅ PARITY — every broker row is explained by the AI's book",
                  "MISMATCH": f"🔴 RECON MISMATCH — {len(R.get('mismatches') or [])} broker row(s) the book cannot explain",
                  }.get(v, "⚠️ UNKNOWN — the broker was not read; no parity claim is made")
          st.markdown(f'<div class="{css}"><b>{head}</b><br>as of {R.get("ts")} · broker positions '
                      f'{R.get("broker_positions")} · holdings {R.get("broker_holdings")} · paper book rows '
                      f'{R.get("book_rows")}</div>', unsafe_allow_html=True)
          f = R.get("funds") or {}
          if f:
              c1, c2, c3 = st.columns(3)
              c1.metric("Broker funds available", rs(f.get("available")))
              c2.metric("Utilised", rs(f.get("utilised")))
              c3.metric("Collateral", rs(f.get("collateral")))
          if R.get("mismatches"):
              st.error("Mismatches")
              st.dataframe(R["mismatches"], width="stretch", hide_index=True)
          if R.get("errors"):
              st.warning("\n".join(str(e) for e in R["errors"]))
          with st.expander(f"Paper-only rows ({len(R.get('paper_only') or [])}) — expected absent at the broker while Rule 7 holds"):
              st.dataframe([{"Account": p.get("account"), "Ref": p.get("ref"), "Underlying": p.get("underlying") or "—",
                             "Margin ₹": p.get("margin_rs"), "Source": p.get("source")}
                            for p in R.get("paper_only") or []], width="stretch", hide_index=True)
          hist = d.recon_history()
          if len(hist) > 1:
              st.subheader("Recon history")
              st.dataframe(hist, width="stretch", hide_index=True)
      st.caption("The recon engine GETs /positions, /holdings and /fundlimit only; a build-time test fails if any "
                 "write verb or order path appears in it. A failed read is 'unknown', never parity.")

  # ---------------------------------------------------------- audit log
  with tab_a:
      st.subheader("Autonomous event log — account + shadow-account events, newest first")
      ev = d.audit_events()
      st.dataframe([{"When": e.get("ts"), "Account": e.get("account"), "Event": e.get("event_type"),
                     "Detail": e.get("detail")} for e in ev], width="stretch", hide_index=True, height=560)
      st.caption("Entries lock margin, releases settle P&L, halts latch on drawdown, treasury rotations move "
                 "the equity budget, sizing refusals name their reason. Ratchet arm/step events live on the "
                 "journal rows (Live Book tab); exits are OMS tickets (Recent settlements).")


_body()

st.markdown('<p class="foot">Alpha Desk · paper only · decisions #1–#111 in DECISIONS.md · this page writes nothing.</p>',
            unsafe_allow_html=True)
