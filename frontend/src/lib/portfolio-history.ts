/**
 * The four-portfolio graph's pure helpers (2026-10-09). The bridge sends every
 * point as % return on the capital contributed at the time; these window it
 * and lay it out for recharts. No fetching here.
 */
import type { PortfolioPoint } from "./api";

export const PORTFOLIOS = [
  "PAPER_10L",
  "PAPER_2L",
  "PAPER_2L_ROT",
  "PAPER_2L_LIVE",
  "PAPER_2L_LIVE_GEX",
] as const;

/** Owner's choice 2026-10-09: human names on the portfolio graph only. */
export const PORTFOLIO_NAMES: Record<string, string> = {
  PAPER_10L: "Model Portfolio",
  PAPER_2L: "Small-Account Test",
  PAPER_2L_ROT: "Capital-Rotation Test",
  PAPER_2L_LIVE: "Live-Quote Test",
  PAPER_2L_LIVE_GEX: "Live-Quote GEX Test",
};

export const PORTFOLIO_STROKES: Record<string, string> = {
  PAPER_10L: "#4c78a8",
  PAPER_2L: "#f58518",
  PAPER_2L_ROT: "#54a24b",
  PAPER_2L_LIVE: "#e45756",
  PAPER_2L_LIVE_GEX: "#b279a2",
};

export const WINDOWS = ["Today", "1W", "1M", "YTD", "All time"] as const;
export type WindowChoice = (typeof WINDOWS)[number];

const IST_MS = 5.5 * 3600_000;

/** The window's first instant (epoch ms), in IST calendar terms; null = all time. */
export function windowStart(choice: WindowChoice, now: Date = new Date()): number | null {
  const t = now.getTime();
  if (choice === "1W") return t - 7 * 86_400_000;
  if (choice === "1M") return t - 30 * 86_400_000;
  const ist = new Date(t + IST_MS);
  if (choice === "Today")
    return Date.UTC(ist.getUTCFullYear(), ist.getUTCMonth(), ist.getUTCDate()) - IST_MS;
  if (choice === "YTD") return Date.UTC(ist.getUTCFullYear(), 0, 1) - IST_MS;
  return null;
}

/** Points inside the window, each line CARRIED IN: its last point before the
 * window is restated at the window's start, so a line that did not move still
 * shows where it stands. */
export function inWindow(points: PortfolioPoint[], start: number | null): PortfolioPoint[] {
  if (start === null) return points;
  const before = new Map<string, PortfolioPoint>();
  const inside: PortfolioPoint[] = [];
  for (const p of points) {
    if (Date.parse(p.ts) < start) before.set(p.account, p);
    else inside.push(p);
  }
  const carried = [...before.values()].map((p) => ({ ...p, ts: new Date(start).toISOString() }));
  return [...carried, ...inside];
}

/** The book drawn in ₹ on the left axis; every other book is drawn in % on the right (2026-10-28). */
export const INR_ACCOUNT = "PAPER_10L";

export const inrKey = (account: string) => `${account}_inr`;
export const pctKey = (account: string) => `${account}_pct`;
export const benchKey = (key: string) => `bench_${key}`;

/** One line's points, time-sorted — what the tooltip reads its as-of values from. */
export type SeriesPoint = { t: number; inr: number | null; pct: number | null };

const finite = (v: number | null | undefined): number | null =>
  typeof v === "number" && Number.isFinite(v) ? v : null;

/** Each account's points as a sorted series; points with an unparseable time are dropped. */
export function seriesByAccount(points: PortfolioPoint[]): Map<string, SeriesPoint[]> {
  const out = new Map<string, SeriesPoint[]>();
  for (const p of points) {
    const t = Date.parse(p.ts);
    if (Number.isNaN(t)) continue;
    const list = out.get(p.account) ?? [];
    list.push({ t, inr: finite(p.equity), pct: finite(p.pct) });
    out.set(p.account, list);
  }
  for (const list of out.values()) list.sort((a, b) => a.t - b.t);
  return out;
}

/** A ₹ benchmark (worth of `base` invested on the epoch) as a series; % is against `base`. */
export function benchmarkSeries(
  points: { ts: string; value: number }[],
  base: number,
  start: number | null,
): SeriesPoint[] {
  return points
    .map((p) => {
      const inr = finite(p.value);
      return {
        t: Date.parse(p.ts),
        inr,
        pct: inr !== null && base > 0 ? (inr / base - 1) * 100 : null,
      };
    })
    .filter((p) => !Number.isNaN(p.t) && (start === null || p.t >= start))
    .sort((a, b) => a.t - b.t);
}

/**
 * The last point at or before `t` (binary search) — the line's value at a hovered
 * instant it has no point of its own at. Null when the line starts after `t`.
 */
export function asOf(series: SeriesPoint[] | undefined, t: number): SeriesPoint | null {
  const head = series?.[0];
  if (!series || !head || !Number.isFinite(t) || t < head.t) return null;
  let lo = 0;
  let hi = series.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if ((series[mid]?.t ?? Infinity) <= t) lo = mid;
    else hi = mid - 1;
  }
  return series[lo] ?? null;
}

/**
 * recharts rows for the dual-axis chart: one per point, carrying only that
 * point's own keys — INR_ACCOUNT in ₹ (`<acct>_inr`), every other book in %
 * (`<acct>_pct`), each shown benchmark in ₹ (`bench_<key>`). Sparse by design:
 * lines join with connectNulls, and the tooltip fills the gaps with asOf.
 */
export function toDualRows(
  accounts: Map<string, SeriesPoint[]>,
  benchmarks: Map<string, SeriesPoint[]> = new Map(),
): Array<Record<string, number>> {
  const rows: Array<Record<string, number>> = [];
  for (const [account, list] of accounts) {
    const key = account === INR_ACCOUNT ? inrKey(account) : pctKey(account);
    for (const p of list) {
      const v = account === INR_ACCOUNT ? p.inr : p.pct;
      if (v !== null) rows.push({ t: p.t, [key]: v });
    }
  }
  for (const [key, list] of benchmarks) {
    for (const p of list) if (p.inr !== null) rows.push({ t: p.t, [benchKey(key)]: p.inr });
  }
  return rows.sort((a, b) => (a["t"] ?? 0) - (b["t"] ?? 0));
}

/** True when some account has at least two points — a line, not a dot. */
export function hasLine(points: PortfolioPoint[]): boolean {
  const n = new Map<string, number>();
  for (const p of points) n.set(p.account, (n.get(p.account) ?? 0) + 1);
  return [...n.values()].some((v) => v >= 2);
}

const HOUR_MS = 3600_000;
const DAY_MS = 86_400_000;
const DAY_STEPS = [1, 2, 3, 7, 14, 21, 28];
const MONTH_STEPS = [1, 2, 3, 6, 12];

/** IST midnight on or before `t` (epoch ms). */
function istMidnight(t: number): number {
  return Math.floor((t + IST_MS) / DAY_MS) * DAY_MS - IST_MS;
}

/**
 * Time-axis ticks on IST calendar boundaries (2026-10-16: recharts' numeric
 * ticks landed at arbitrary instants, so neighbouring ticks repeated the same
 * "13 Oct" label). Under two days: whole hours; up to ~4 months: IST midnights
 * every k days; beyond: the 1st of every k months. At most `maxTicks`.
 */
export function timeTicks(min: number, max: number, maxTicks = 7): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [min];
  const span = max - min;
  const out: number[] = [];
  if (span <= 2 * DAY_MS) {
    const stepH = [1, 2, 3, 4, 6, 12].find((h) => span / (h * HOUR_MS) <= maxTicks) ?? 12;
    const step = stepH * HOUR_MS;
    for (let t = Math.ceil((min + IST_MS) / step) * step - IST_MS; t <= max; t += step) out.push(t);
    return out;
  }
  if (span <= 120 * DAY_MS) {
    const k = DAY_STEPS.find((d) => span / (d * DAY_MS) <= maxTicks) ?? 28;
    let t = istMidnight(min);
    if (t < min) t += DAY_MS;
    for (; t <= max; t += k * DAY_MS) out.push(t);
    return out;
  }
  const months = span / (30 * DAY_MS);
  const k = MONTH_STEPS.find((m) => months / m <= maxTicks) ?? 12;
  const start = new Date(min + IST_MS);
  for (let idx = start.getUTCFullYear() * 12 + start.getUTCMonth() + 1; ; idx += 1) {
    const t = Date.UTC(Math.floor(idx / 12), idx % 12, 1) - IST_MS;
    if (t > max) break;
    if (idx % k === 0) out.push(t);
  }
  return out;
}

/** The tick label for a span: "10:00" under two days, "13 Oct" up to ~4 months, "Oct '26" beyond. */
export function formatTimeTick(value: number, span: number): string {
  const d = new Date(value);
  if (span <= 2 * DAY_MS) {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).format(d);
  }
  if (span <= 120 * DAY_MS) {
    return new Intl.DateTimeFormat("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "numeric",
      month: "short",
    }).format(d);
  }
  const parts = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    month: "short",
    year: "2-digit",
  }).formatToParts(d);
  const month = parts.find((p) => p.type === "month")?.value ?? "";
  const year = parts.find((p) => p.type === "year")?.value ?? "";
  return `${month} '${year}`;
}
