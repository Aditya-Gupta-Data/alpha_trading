/**
 * SINGLE DATA BOUNDARY.
 *
 * Every number shown in the UI comes from this file. Components must never
 * hard-code values — they call these functions only.
 *
 * When the real backend is ready:
 *   1. Set VITE_API_BASE_URL in the environment (empty placeholder for now).
 *   2. Mock data is opt-in: VITE_USE_MOCK=true (local design work only).
 *   3. Replace ONLY the body of each function below with the matching
 *      fetch(`${API_BASE_URL}/api/...`) call. Each function is one swappable
 *      unit — signatures and return shapes must stay identical.
 *
 * NEVER put API keys, tokens, broker credentials, server IPs or webhook URLs
 * in this file or anywhere else in the frontend.
 */

// Empty = same origin (the UI's own server proxies /api/* to the read-only
// bridge, so no host name is ever baked into the build).
export const API_BASE_URL: string = import.meta.env["VITE_API_BASE_URL"] ?? "";

// Mock data is OPT-IN (VITE_USE_MOCK=true, local design work only). It was
// opt-out until 2026-09-25, and the shipped desk showed the sample curve
// (Rs.10,49,780 on 2025-10-02) instead of the ledger for a day: a build that
// forgets a flag must fall back to REAL data, never to invented numbers.
export const USE_MOCK: boolean = import.meta.env["VITE_USE_MOCK"] === "true";

/* ------------------------------------------------------------------ types */

export type Strategy =
  | "Bear Put"
  | "Bull Call"
  | "Iron Condor"
  | "Iron Butterfly"
  | "Equity Long";

export interface AccountTreasury {
  account_id: string;
  starting_capital: number;
  realized_pnl: number;
  equity: number;
  peak_equity: number;
  /** Peak-to-now drawdown, percent (0 = at the peak). */
  drawdown_pct: number;
  /** Days since the account's measurement epoch (clean-sheet reset / birth). */
  days_elapsed: number | null;
  /** Equity vs starting capital, percent. */
  abs_return_pct: number | null;
  /** Annualised compound growth ((equity/start)^(365/days) − 1) × 100; null under one day. */
  cagr_pct: number | null;
  locked_margin: number;
  open_locks: number;
  available_cash: number;
  rejections?: number;
  /** PAPER_10L only (decision #116): equity at the ₹10L base the curve and CAGR start from. */
  base_equity?: number;
  base_ts?: string | null;
  /** Mark-to-market of open positions from the engine's snapshot; null = nothing priced. */
  unrealized_pnl: number | null;
  /** Realized equity + unrealized; null when nothing is priced. */
  net_equity: number | null;
  marked_positions: number;
  open_positions: number;
  /** When the engine published the marks (IST, tz-aware). */
  marks_as_of: string | null;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  /** Drawdown from the running peak at that point, percent. */
  drawdown_pct: number;
}

export interface Treasury {
  PAPER_10L: AccountTreasury;
  PAPER_2L: AccountTreasury & { rejections: number };
  /** The capital-rotation A/B arm (decision #115); absent until its first signal. */
  PAPER_2L_ROT?: AccountTreasury & { rejections: number };
  equity_curve: EquityPoint[];
  /** Compounding (return / CAGR) is measured from this date — the ₹10L base (#116). */
  base_epoch?: string;
  /** Pool moves drawn as chart markers (#117): resets and injections, oldest first. */
  capital_events?: CapitalEvent[];
}

export interface CapitalEvent {
  ts: string;
  kind: "clean_sheet" | "capital_injection";
  label: string;
  /** Tag drawn on the chart line (fits a phone): "+₹8L", "Reset → ₹2L". */
  short?: string;
  detail: string;
}

export interface OpenTrade {
  id: string;
  symbol: string;
  strategy: Strategy;
  direction: string;
  accounts: string;
  lots: number;
  entered: string;
  expiry: string;
  max_loss_rs: number;
  mtm_rs: number | null;
  capture_pct: number | null;
  ratchet_peak_pct: number | null;
  ratchet_lock_pct: number | null;
  ratchet: string;
  sizing: string;
}

export interface Outcome {
  settled: string;
  symbol: string;
  strategy: Strategy;
  resolution: string;
  pnl_rs: number;
  r_multiple: number | null;
  ticket: string;
}

export type ReconVerdict = "PARITY" | "MISMATCH" | "UNKNOWN";

export interface ReconRow {
  ts: string;
  verdict: ReconVerdict;
  broker_positions: number;
  book_rows: number;
  mismatches: string[];
}

export interface AuditEvent {
  ts: string;
  account: string;
  event_type: string;
  detail: string;
}

export interface Freshness {
  "brain_map.db": string | null;
  journal: string | null;
  market_snapshot: string | null;
  recon: string | null;
}

/* ------------------------------------------------------------- mock source */

const MOCK_LATENCY_MS = 220;

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), MOCK_LATENCY_MS));
}

/** Deterministic pseudo-random so mock figures stay stable between renders. */
function seeded(seed: number): () => number {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

const NOW = new Date("2026-09-24T15:30:00+05:30");

function isoMinutesAgo(minutes: number): string {
  return new Date(NOW.getTime() - minutes * 60_000).toISOString();
}

const PAPER_10L_EQUITY = 1_085_400;

function buildEquityCurve(): EquityPoint[] {
  const rand = seeded(20260924);
  const raw: number[] = [];
  let equity = 1_000_000;

  for (let i = 0; i < 60; i += 1) {
    equity += (rand() - 0.42) * 9_500;
    raw.push(equity);
  }

  // Anchor the series so its final point matches the reported net equity.
  const offset = PAPER_10L_EQUITY - (raw[raw.length - 1] ?? PAPER_10L_EQUITY);
  const points: EquityPoint[] = [];
  let peak = 0;

  raw.forEach((value, index) => {
    const anchored = value + offset;
    peak = Math.max(peak, anchored);
    points.push({
      ts: isoMinutesAgo((59 - index) * 8_700),
      equity: Math.round(anchored),
      drawdown_pct: Number((((peak - anchored) / peak) * 100).toFixed(2)),
    });
  });

  return points;
}

const EQUITY_CURVE = buildEquityCurve();
const PEAK_10L = Math.max(...EQUITY_CURVE.map((point) => point.equity));

const MOCK_TREASURY: Treasury = {
  PAPER_10L: {
    account_id: "PAPER_10L",
    starting_capital: 1_000_000,
    realized_pnl: PAPER_10L_EQUITY - 1_000_000,
    equity: PAPER_10L_EQUITY,
    peak_equity: PEAK_10L,
    drawdown_pct: Number((((PEAK_10L - PAPER_10L_EQUITY) / PEAK_10L) * 100).toFixed(2)),
    days_elapsed: 64,
    abs_return_pct: 8.54,
    cagr_pct: Number(((Math.pow(PAPER_10L_EQUITY / 1_000_000, 365 / 64) - 1) * 100).toFixed(2)),
    locked_margin: 412_500,
    open_locks: 6,
    available_cash: 672_900,
    unrealized_pnl: 21_450,
    net_equity: PAPER_10L_EQUITY + 21_450,
    marked_positions: 5,
    open_positions: 6,
    marks_as_of: NOW.toISOString(),
  },
  PAPER_2L: {
    account_id: "PAPER_2L",
    starting_capital: 200_000,
    realized_pnl: 3_150,
    equity: 203_150,
    peak_equity: 208_600,
    drawdown_pct: 2.61,
    days_elapsed: 5,
    abs_return_pct: 1.58,
    cagr_pct: null,
    locked_margin: 96_800,
    open_locks: 3,
    available_cash: 106_350,
    unrealized_pnl: -1_820,
    net_equity: 203_150 - 1_820,
    marked_positions: 3,
    open_positions: 3,
    marks_as_of: NOW.toISOString(),
    rejections: 4,
  },
  equity_curve: EQUITY_CURVE,
  base_epoch: "2026-08-07",
  capital_events: [
    { ts: EQUITY_CURVE[20]?.ts ?? NOW.toISOString(), kind: "clean_sheet", label: "Pool reset ₹10L → ₹2L", short: "Reset → ₹2L", detail: "mock" },
    { ts: EQUITY_CURVE[30]?.ts ?? NOW.toISOString(), kind: "capital_injection", label: "₹8L capital injection", short: "+₹8L", detail: "mock" },
  ],
};

const MOCK_OPEN_TRADES: OpenTrade[] = [
  {
    id: "OT-2411",
    symbol: "NIFTY 24SEP 25400/25300 PE",
    strategy: "Bear Put",
    direction: "Short delta",
    accounts: "PAPER_10L",
    lots: 4,
    entered: isoMinutesAgo(310),
    expiry: "2026-09-24",
    max_loss_rs: 18_000,
    mtm_rs: 7_420,
    capture_pct: 41.2,
    ratchet_peak_pct: 52.0,
    ratchet_lock_pct: 30.0,
    ratchet: "LOCKED @ 30%",
    sizing: "Risk 1.8% / 4 lots",
  },
  {
    id: "OT-2412",
    symbol: "BANKNIFTY 25SEP 54000/54200 CE",
    strategy: "Bull Call",
    direction: "Long delta",
    accounts: "PAPER_10L",
    lots: 3,
    entered: isoMinutesAgo(285),
    expiry: "2026-09-25",
    max_loss_rs: 22_500,
    mtm_rs: -4_980,
    capture_pct: -22.1,
    ratchet_peak_pct: 11.0,
    ratchet_lock_pct: null,
    ratchet: "ARMED",
    sizing: "Risk 2.2% / 3 lots",
  },
  {
    id: "OT-2413",
    symbol: "NIFTY 01OCT 25200/25000/25800/26000",
    strategy: "Iron Condor",
    direction: "Neutral",
    accounts: "PAPER_10L, PAPER_2L",
    lots: 2,
    entered: isoMinutesAgo(1_420),
    expiry: "2026-10-01",
    max_loss_rs: 15_000,
    mtm_rs: 5_210,
    capture_pct: 34.7,
    ratchet_peak_pct: 38.0,
    ratchet_lock_pct: 20.0,
    ratchet: "LOCKED @ 20%",
    sizing: "Risk 1.5% / 2 lots",
  },
  {
    id: "OT-2414",
    symbol: "BANKNIFTY 01OCT 54500 IB",
    strategy: "Iron Butterfly",
    direction: "Neutral",
    accounts: "PAPER_2L",
    lots: 1,
    entered: isoMinutesAgo(2_760),
    expiry: "2026-10-01",
    max_loss_rs: 8_200,
    mtm_rs: null,
    capture_pct: null,
    ratchet_peak_pct: null,
    ratchet_lock_pct: null,
    ratchet: "NO QUOTE",
    sizing: "Risk 4.1% / 1 lot",
  },
  {
    id: "OT-2415",
    symbol: "RELIANCE",
    strategy: "Equity Long",
    direction: "Long",
    accounts: "PAPER_10L",
    lots: 60,
    entered: isoMinutesAgo(5_880),
    expiry: "—",
    max_loss_rs: 26_400,
    mtm_rs: 12_960,
    capture_pct: 49.1,
    ratchet_peak_pct: 55.0,
    ratchet_lock_pct: 35.0,
    ratchet: "LOCKED @ 35%",
    sizing: "Risk 2.6% / 60 qty",
  },
  {
    id: "OT-2416",
    symbol: "HDFCBANK",
    strategy: "Equity Long",
    direction: "Long",
    accounts: "PAPER_2L",
    lots: 22,
    entered: isoMinutesAgo(4_320),
    expiry: "—",
    max_loss_rs: 9_460,
    mtm_rs: -1_210,
    capture_pct: -12.8,
    ratchet_peak_pct: 6.0,
    ratchet_lock_pct: null,
    ratchet: "ARMED",
    sizing: "Risk 4.7% / 22 qty",
  },
  {
    id: "OT-2417",
    symbol: "INFY 25SEP 1560/1520 PE",
    strategy: "Bear Put",
    direction: "Short delta",
    accounts: "PAPER_10L",
    lots: 5,
    entered: isoMinutesAgo(190),
    expiry: "2026-09-25",
    max_loss_rs: 14_000,
    mtm_rs: 2_310,
    capture_pct: 16.5,
    ratchet_peak_pct: 19.0,
    ratchet_lock_pct: null,
    ratchet: "ARMED",
    sizing: "Risk 1.4% / 5 lots",
  },
  {
    id: "OT-2418",
    symbol: "TATAMOTORS 25SEP 1080/1120 CE",
    strategy: "Bull Call",
    direction: "Long delta",
    accounts: "PAPER_2L",
    lots: 2,
    entered: isoMinutesAgo(150),
    expiry: "2026-09-25",
    max_loss_rs: 6_800,
    mtm_rs: 940,
    capture_pct: 13.8,
    ratchet_peak_pct: 15.0,
    ratchet_lock_pct: null,
    ratchet: "ARMED",
    sizing: "Risk 3.4% / 2 lots",
  },
  {
    id: "OT-2419",
    symbol: "NIFTY 08OCT 25600/25400 PE",
    strategy: "Bear Put",
    direction: "Short delta",
    accounts: "PAPER_10L",
    lots: 3,
    entered: isoMinutesAgo(1_105),
    expiry: "2026-10-08",
    max_loss_rs: 16_500,
    mtm_rs: -2_040,
    capture_pct: -12.4,
    ratchet_peak_pct: 4.0,
    ratchet_lock_pct: null,
    ratchet: "ARMED",
    sizing: "Risk 1.6% / 3 lots",
  },
];

const MOCK_OUTCOMES: Outcome[] = [
  {
    settled: isoMinutesAgo(95),
    symbol: "NIFTY 24SEP 25500/25400 PE",
    strategy: "Bear Put",
    resolution: "Ratchet lock hit",
    pnl_rs: 9_240,
    r_multiple: 0.62,
    ticket: "TK-9981",
  },
  {
    settled: isoMinutesAgo(260),
    symbol: "BANKNIFTY 24SEP 53800 IB",
    strategy: "Iron Butterfly",
    resolution: "Expired in range",
    pnl_rs: 6_100,
    r_multiple: 0.81,
    ticket: "TK-9978",
  },
  {
    settled: isoMinutesAgo(420),
    symbol: "SBIN 24SEP 860/880 CE",
    strategy: "Bull Call",
    resolution: "Stop breached",
    pnl_rs: -7_150,
    r_multiple: -1.0,
    ticket: "TK-9974",
  },
  {
    settled: isoMinutesAgo(1_380),
    symbol: "NIFTY 17SEP 25100/24900/25700/25900",
    strategy: "Iron Condor",
    resolution: "Closed on time stop",
    pnl_rs: 4_480,
    r_multiple: 0.36,
    ticket: "TK-9962",
  },
  {
    settled: isoMinutesAgo(2_820),
    symbol: "ICICIBANK",
    strategy: "Equity Long",
    resolution: "Trail exit",
    pnl_rs: 11_760,
    r_multiple: 1.42,
    ticket: "TK-9950",
  },
  {
    settled: isoMinutesAgo(4_240),
    symbol: "BANKNIFTY 10SEP 53200/53000 PE",
    strategy: "Bear Put",
    resolution: "Stop breached",
    pnl_rs: -5_320,
    r_multiple: -0.94,
    ticket: "TK-9931",
  },
  {
    settled: isoMinutesAgo(5_760),
    symbol: "LT 10SEP 3600/3680 CE",
    strategy: "Bull Call",
    resolution: "Target reached",
    pnl_rs: 8_020,
    r_multiple: 1.11,
    ticket: "TK-9917",
  },
  {
    settled: isoMinutesAgo(7_200),
    symbol: "WIPRO",
    strategy: "Equity Long",
    resolution: "Trail exit",
    pnl_rs: -2_180,
    r_multiple: -0.41,
    ticket: "TK-9902",
  },
];

const MOCK_RECON_HISTORY: ReconRow[] = [
  {
    ts: isoMinutesAgo(12),
    verdict: "PARITY",
    broker_positions: 9,
    book_rows: 9,
    mismatches: [],
  },
  {
    ts: isoMinutesAgo(72),
    verdict: "PARITY",
    broker_positions: 9,
    book_rows: 9,
    mismatches: [],
  },
  {
    ts: isoMinutesAgo(132),
    verdict: "MISMATCH",
    broker_positions: 8,
    book_rows: 9,
    mismatches: ["OT-2414 present in book, absent at broker"],
  },
  {
    ts: isoMinutesAgo(192),
    verdict: "UNKNOWN",
    broker_positions: 0,
    book_rows: 9,
    mismatches: ["Broker snapshot unavailable"],
  },
  {
    ts: isoMinutesAgo(252),
    verdict: "PARITY",
    broker_positions: 8,
    book_rows: 8,
    mismatches: [],
  },
  {
    ts: isoMinutesAgo(1_452),
    verdict: "PARITY",
    broker_positions: 7,
    book_rows: 7,
    mismatches: [],
  },
];

const MOCK_AUDIT: AuditEvent[] = [
  {
    ts: isoMinutesAgo(8),
    account: "PAPER_10L",
    event_type: "RATCHET_LOCK",
    detail: "OT-2411 lock raised to 30% of peak capture",
  },
  {
    ts: isoMinutesAgo(14),
    account: "SYSTEM",
    event_type: "RECON_RUN",
    detail: "Verdict PARITY — 9 broker positions vs 9 book rows",
  },
  {
    ts: isoMinutesAgo(46),
    account: "PAPER_2L",
    event_type: "ORDER_REJECTED",
    detail: "Margin shortfall on BANKNIFTY 01OCT butterfly leg 2",
  },
  {
    ts: isoMinutesAgo(88),
    account: "PAPER_10L",
    event_type: "POSITION_SETTLED",
    detail: "TK-9981 settled +₹9,240 (0.62R)",
  },
  {
    ts: isoMinutesAgo(150),
    account: "SYSTEM",
    event_type: "SNAPSHOT_GAP",
    detail: "Market snapshot late by 4m 12s",
  },
  {
    ts: isoMinutesAgo(196),
    account: "SYSTEM",
    event_type: "RECON_RUN",
    detail: "Verdict UNKNOWN — broker snapshot unavailable",
  },
  {
    ts: isoMinutesAgo(255),
    account: "PAPER_10L",
    event_type: "POSITION_OPENED",
    detail: "OT-2417 INFY bear put, 5 lots, risk 1.4%",
  },
  {
    ts: isoMinutesAgo(340),
    account: "PAPER_2L",
    event_type: "SIZING_CAP",
    detail: "Sizing capped at 1 lot by per-trade risk ceiling",
  },
  {
    ts: isoMinutesAgo(430),
    account: "SYSTEM",
    event_type: "DESK_START",
    detail: "Paper desk session opened for 24 Sep 2026",
  },
];

const MOCK_FRESHNESS: Freshness = {
  "brain_map.db": isoMinutesAgo(6),
  journal: isoMinutesAgo(9),
  market_snapshot: isoMinutesAgo(41),
  recon: isoMinutesAgo(12),
};

/* ------------------------------------------------------- swappable readers */

export async function getTreasury(): Promise<Treasury> {
  if (USE_MOCK) return delay(MOCK_TREASURY);
  return fetchJson<Treasury>("/api/treasury");
}

export async function getOpenTrades(): Promise<OpenTrade[]> {
  if (USE_MOCK) return delay(MOCK_OPEN_TRADES);
  return fetchJson<OpenTrade[]>("/api/open-trades");
}

export async function getRecentOutcomes(): Promise<Outcome[]> {
  if (USE_MOCK) return delay(MOCK_OUTCOMES);
  return fetchJson<Outcome[]>("/api/recent-outcomes");
}

export async function getLatestRecon(): Promise<ReconRow | null> {
  if (USE_MOCK) return delay(MOCK_RECON_HISTORY[0] ?? null);
  return fetchJson<ReconRow | null>("/api/recon/latest");
}

export async function getReconHistory(): Promise<ReconRow[]> {
  if (USE_MOCK) return delay(MOCK_RECON_HISTORY);
  return fetchJson<ReconRow[]>("/api/recon/history");
}

export async function getAuditEvents(): Promise<AuditEvent[]> {
  if (USE_MOCK) return delay(MOCK_AUDIT);
  return fetchJson<AuditEvent[]>("/api/audit");
}

export async function getFreshness(): Promise<Freshness> {
  if (USE_MOCK) return delay(MOCK_FRESHNESS);
  return fetchJson<Freshness>("/api/freshness");
}

/* ----------------------------------------------------------------- helpers */

async function fetchJson<T>(path: string): Promise<T> {
  const { readAccessKey, writeAccessKey } = await import("@/hooks/useAccessKey");
  const key = readAccessKey();
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "GET",
    credentials: "omit",
    headers: key ? { "X-Access-Key": key } : {},
  });
  if (response.status === 401 || response.status === 403) {
    // wrong or expired key: drop it and send the viewer back to the gate
    writeAccessKey(null);
    if (typeof window !== "undefined") window.location.assign("/login");
    throw new Error("Access key rejected");
  }
  if (!response.ok) {
    throw new Error(`Read failed (${response.status})`);
  }
  return (await response.json()) as T;
}
