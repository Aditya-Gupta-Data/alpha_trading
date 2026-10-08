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
PORTFOLIO_COLOURS = {"PAPER_10L": "#4c78a8", "PAPER_2L": "#f58518", "PAPER_2L_ROT": "#54a24b",
                     "PAPER_2L_LIVE": "#e45756"}
# owner's choice 2026-10-09: human names on the portfolio graph only (the rest
# of the page keeps the account codes)
PORTFOLIO_NAMES = {"PAPER_10L": "Model Portfolio", "PAPER_2L": "Small-Account Test",
                   "PAPER_2L_ROT": "Capital-Rotation Test", "PAPER_2L_LIVE": "Live-Quote Test"}


def _all_portfolios_chart():
    """The four paper portfolios on ONE axis (owner request 2026-10-09), as %
    return on each one's contributed capital — ₹10L and ₹2L books are only
    comparable that way. Realized = a step at every settlement (full history);
    True net = realized + open positions' marks, recorded every 15 min on the
    VM from 2026-10-09 (src/equity_history.py)."""
    import altair as alt
    import pandas as pd
    H = d.equity_history()
    st.subheader("All four portfolios — % return on contributed capital")
    if H.get("error"):
        st.info(f"Equity history unavailable: {H['error']}")
        return
    c1, c2 = st.columns([3, 2])
    window = c1.radio("Timeframe", d.EQUITY_WINDOWS, index=len(d.EQUITY_WINDOWS) - 1, horizontal=True,
                      key="equity_window")
    start = d.window_start(window)
    net = d.in_window(H.get("net") or [], start)
    bases = ["True net equity (every 15 min)", "Realized (settled trades)"]
    from collections import Counter
    has_line = any(n >= 2 for n in Counter(p["account"] for p in net).values())
    basis = c2.radio("Basis", bases, index=0 if has_line else 1, horizontal=True, key="equity_basis",
                     help="True net includes the open positions' marks; it has been recorded since 9 Oct 2026. "
                          "Realized moves only when a trade settles, but goes back to July.")
    pts = net if basis == bases[0] else d.in_window(d.extend_to_now(H.get("realized") or []), start)
    pts = [p for p in pts if p.get("pct") is not None]
    if not pts:
        st.caption("No points in this window yet." if basis == bases[1] else
                   "No true-net-equity points in this window yet — they are recorded every 15 min on the VM "
                   "during the session (from 9 Oct 2026). Switch to Realized for the full history.")
        return
    df = pd.DataFrame({"time": pd.to_datetime([p["ts"] for p in pts]),
                       "return_pct": [p["pct"] for p in pts], "equity": [p["equity"] for p in pts],
                       "portfolio": [PORTFOLIO_NAMES.get(p["account"], p["account"]) for p in pts],
                       "account": [p["account"] for p in pts]})
    accounts = [a for a in d.EQUITY_ACCOUNTS if a in set(df["account"])]
    chart = alt.Chart(df).mark_line(interpolate="step-after" if basis == bases[1] else "linear",
                                    point=len(df) < 60).encode(
        x=alt.X("time:T", title=None),
        y=alt.Y("return_pct:Q", title="% return", scale=alt.Scale(zero=False)),
        color=alt.Color("portfolio:N", scale=alt.Scale(domain=[PORTFOLIO_NAMES.get(a, a) for a in accounts],
                                                       range=[PORTFOLIO_COLOURS[a] for a in accounts]),
                        legend=alt.Legend(orient="bottom", title=None, columns=2, labelLimit=0)),
        tooltip=["portfolio:N", alt.Tooltip("account:N", title="account"),
                 alt.Tooltip("time:T", format="%d %b %Y %H:%M"),
                 alt.Tooltip("return_pct:Q", format="+.2f", title="% return"),
                 alt.Tooltip("equity:Q", format=",.0f", title="₹ equity")])
    layers = [chart]
    ev = [e for e in (H.get("capital_events") or []) if basis == bases[1] and (start is None or e["ts"] >= start)]
    if ev:
        edf = pd.DataFrame({"time": pd.to_datetime([e["ts"] for e in ev]), "label": [e["label"] for e in ev]})
        layers.append(alt.Chart(edf).mark_rule(strokeDash=[4, 3], color="#22a06b").encode(
            x="time:T", tooltip=["label:N"]))
    st.altair_chart(alt.layer(*layers).properties(height=300), use_container_width=True)
    st.caption("The Model Portfolio's (PAPER_10L) % is measured on the capital contributed at the time: ₹10L until the 21 Jul clean "
               "sheet, ₹2L from it, ₹10L again from the 7 Aug ₹8L injection (dashed markers). The ₹2L books "
               "start at 0% on the day each was opened (Small-Account Test 21 Sep, Capital-Rotation Test 29 Sep, "
               "Live-Quote Test 30 Sep). Model Portfolio = PAPER_10L, Small-Account Test = PAPER_2L, "
               "Capital-Rotation Test = PAPER_2L_ROT, Live-Quote Test = PAPER_2L_LIVE.")
    for n in H.get("notes") or []:
        if basis == bases[1] or "true-net" not in n:
            st.caption(n)


@st.cache_data(show_spinner=False)
def _brain_map_cached(db_mtime):
    # keyed on the mirror's mtime: the page's 5-min refresh re-sends the SAME
    # html (the iframe and its layout stay put) until a new mirror lands
    return d.brain_map_html()


def _brain_map_tab():
    """Architect 2026-10-09: the Brain Map — the system's learning loop and
    causal edges — beside the money, rendered from the same mirror."""
    import streamlit.components.v1 as components
    try:
        mtime = d.DB_PATH.stat().st_mtime
    except OSError:
        mtime = None
    B = _brain_map_cached(mtime)
    st.subheader("Brain Map — the knowledge graph's causal edges")
    if B.get("error"):
        st.info(f"Brain Map unavailable: {B['error']}")
        return
    s = B.get("stats") or {}
    if s:
        st.caption(f"{s.get('nodes', 0)} nodes · {s.get('edges_active', 0)} active edges "
                   f"({s.get('outcome_derived', 0)} outcome-derived, {s.get('affinity', 0)} smart-money affinity, "
                   f"{s.get('loss_permanent', 0)} permanent loss lessons) · {s.get('edges_expired', 0)} expired "
                   f"(hidden by default) · {B.get('source')}")
    else:
        st.caption(B.get("source") or "")
    st.caption("Steel-blue = outcome-derived causal links (the only class that may move sizing, decision #38); "
               "gold = smart-money affinity; red core = a loss lesson that never decays. Drag to move nodes, "
               "scroll to zoom. Read-only.")
    # (a page loaded inside the hidden tab re-lays itself out when shown: data._relayout_once)
    components.html(B["html"], height=820, scrolling=True)


@st.fragment(run_every="5m")
def _body():
  tab_t, tab_l, tab_r, tab_a, tab_b = st.tabs(["🏦 Treasury", "📈 Live Book & Ratchets", "🔍 Compliance & Recon",
                                                "📜 Event Audit Log", "🧠 Brain Map"])
  with tab_b:
      _brain_map_tab()

  # ------------------------------------------------------------- treasury
  with tab_t:
      T = d.treasury()
      if T.get("error"):
          st.error(f"Treasury unavailable: {T['error']}")
      else:
          for acct, title in (("PAPER_10L", "Primary account — ₹10L paper pool"),
                              ("PAPER_2L", "Shadow account — ₹2L stress test (sized independently)"),
                              ("PAPER_2L_ROT", "Rotation arm — ₹2L with capital rotation / eviction (#115)"),
                              ("PAPER_2L_LIVE", "Live-quote arm — ₹2L marked and exited on crossed bid/ask (#120)")):
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
              if acct in d.LIVE_ACCOUNTS:
                  # audit F02: this arm prices itself; its mark can sit unchanged
                  # for days while it abstains or holds — say how old it is.
                  # Judged as of when this copy of the data was taken (the box
                  # reads a 15-min mirror), and the warning names no cause: the
                  # page cannot tell an abstaining arm from a stopped loop.
                  checked = a.get("mark_stale_as_of") or "n/a"
                  if a.get("marks_as_of"):
                      st.caption(f"Marks: this arm's own crossed bid/ask marks, oldest as of {a['marks_as_of']} "
                                 f"(checked against the data captured at {checked}).")
                  if a.get("mark_stale"):
                      limit = a.get("mark_stale_after_s")
                      limit = f"{limit / 60:.0f} minutes" if limit else "two quote intervals"
                      st.warning(f"Stale mark — in the data captured at {checked}, an open position's mark "
                                 f"(oldest {a.get('marks_as_of') or 'never marked'}) or the chain quotes under it "
                                 f"was more than {limit} of market time old. The unrealized figure is that older "
                                 "mark, not a current price.")
              elif a.get("marks_as_of"):
                  st.caption(f"Marks: the engine's snapshot as of {a['marks_as_of']} — never a fresh quote.")
          curve = T.get("equity_curve") or []
          if curve:
              import altair as alt
              import pandas as pd
              st.subheader("Equity curve — PAPER_10L, realized, full history")
              df = pd.DataFrame({"time": pd.to_datetime([c["ts"] for c in curve]),
                                 "equity": [c["equity"] for c in curve]})
              line = alt.Chart(df).mark_line().encode(       # temporal axis = true time spacing
                  x=alt.X("time:T", title=None), y=alt.Y("equity:Q", title="₹", scale=alt.Scale(zero=False)),
                  tooltip=[alt.Tooltip("time:T", format="%d %b %Y %H:%M"), alt.Tooltip("equity:Q", format=",.0f")])
              layers = [line]
              # decision #119: optional passive lines, off by default
              B = T.get("benchmarks") or {}
              if B.get("series"):
                  k1, k2, k3 = st.columns(3)
                  picks = {"nifty50": k1.checkbox("Nifty 50", value=False, help=B["sources"].get("nifty50")),
                           "gold": k2.checkbox("Gold (GOLDBEES)", value=False, help=B["sources"].get("gold")),
                           "fd_7pct": k3.checkbox("FD 7% p.a.", value=False, help=B["sources"].get("fd_7pct"))}
                  colours = {"nifty50": "#e0a13a", "gold": "#c9b037", "fd_7pct": "#7f8fa6"}
                  labels = {"nifty50": "Nifty 50", "gold": "Gold (GOLDBEES)", "fd_7pct": "FD 7%"}
                  for key, on in picks.items():
                      pts = B["series"].get(key) or []
                      if not on:
                          continue
                      if not pts:
                          st.caption(f"{labels[key]}: {B.get('notes', {}).get(key, 'no data')}")
                          continue
                      bdf = pd.DataFrame({"time": pd.to_datetime([p["ts"] for p in pts]),
                                          "value": [p["value"] for p in pts], "line": labels[key]})
                      layers.append(alt.Chart(bdf).mark_line(strokeDash=[5, 3], color=colours[key]).encode(
                          x="time:T", y="value:Q",
                          tooltip=["line:N", alt.Tooltip("time:T", format="%d %b %Y"),
                                   alt.Tooltip("value:Q", format=",.0f")]))
                  st.caption(f"Benchmarks start at ₹{B.get('base', 0):,.0f} (contributed capital) on "
                             f"{B.get('epoch', '')}; index / ETF closes from the local lakes, FD synthetic — "
                             "no quote is fetched for this page.")
              ev = T.get("capital_events") or []
              if ev:
                  lo, hi = float(df["equity"].min()), float(df["equity"].max())
                  edf = pd.DataFrame({"time": pd.to_datetime([e["ts"] for e in ev]),
                                      "label": [f"{e['label']} ({pd.to_datetime(e['ts']):%d %b})" for e in ev],
                                      # staggered heights so neighbouring labels never overlap
                                      "y": [lo + (hi - lo) * (0.72 - 0.22 * (i % 3)) for i in range(len(ev))]})
                  layers += [alt.Chart(edf).mark_rule(strokeDash=[4, 3], color="#22a06b").encode(
                                 x="time:T", tooltip=["label:N"]),
                             alt.Chart(edf).mark_text(align="left", dx=5, fontSize=11, color="#9aa4b2").encode(
                                 x="time:T", y="y:Q", text="label:N")]
              st.altair_chart(alt.layer(*layers).properties(height=260), use_container_width=True)
              st.caption("Dashed lines are capital moves (decision #117) — the 21 Jul reset to ₹2L and the "
                         "7 Aug ₹8L injection moved equity with no trade behind them. Return and CAGR are "
                         f"measured from the ₹10L base ({T.get('base_epoch', '')}, decision #116).")
          _all_portfolios_chart()
          st.caption("Sizing is fixed-fractional per account (decision #106): the same structure is sized on "
                     "each account's own equity, so the ₹2L book refuses what the ₹10L book takes.")

  # ----------------------------------------------------------- live book
  with tab_l:
      rows = d.open_trades()
      st.subheader(f"Open positions — {len(rows)}")
      if rows:
          # audit F10: a PAPER_2L_LIVE row carries the arm's OWN crossed mark,
          # its quotes' time and the Fix D stale flag (the primary's rows are
          # priced on the engine snapshot, so those two cells read "—")
          st.dataframe([{"Symbol": r["symbol"], "Strategy": r["strategy"], "Dir": r["direction"],
                         "Accounts": r["accounts"], "Lots/Qty": r["lots"], "Entered": r["entered"],
                         "Expiry": r["expiry"] or "—", "Max loss ₹": r["max_loss_rs"],
                         "MTM ₹": r["mtm_rs"], "Capture %": r["capture_pct"],
                         "Ratchet peak %": r["ratchet_peak_pct"], "Ratchet lock %": r["ratchet_lock_pct"],
                         "Exit rule": r["ratchet"],
                         "Mark as of": r.get("last_mark_ts") or "—",
                         "Mark stale": ("STALE" if r.get("mark_stale") else
                                        "unknown" if r.get("account") in d.LIVE_ACCOUNTS
                                        and r.get("mark_stale") is None else "—"),
                         "Note": r.get("note") or r["sizing"] or ""}
                        for r in rows], width="stretch", hide_index=True)
      else:
          st.info("no open positions in the journal / equity ledger / live arm's book")
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
