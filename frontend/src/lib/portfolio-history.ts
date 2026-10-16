/**
 * The four-portfolio graph's pure helpers (2026-10-09). The bridge sends every
 * point as % return on the capital contributed at the time; these window it
 * and lay it out for recharts. No fetching here.
 */
import type { PortfolioPoint } from "./api";

export const PORTFOLIOS = ["PAPER_10L", "PAPER_2L", "PAPER_2L_ROT", "PAPER_2L_LIVE"] as const;

/** Owner's choice 2026-10-09: human names on the portfolio graph only. */
export const PORTFOLIO_NAMES: Record<string, string> = {
  PAPER_10L: "Model Portfolio",
  PAPER_2L: "Small-Account Test",
  PAPER_2L_ROT: "Capital-Rotation Test",
  PAPER_2L_LIVE: "Live-Quote Test",
};

export const PORTFOLIO_STROKES: Record<string, string> = {
  PAPER_10L: "#4c78a8",
  PAPER_2L: "#f58518",
  PAPER_2L_ROT: "#54a24b",
  PAPER_2L_LIVE: "#e45756",
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

/** recharts rows: one per point, {t, <account>: pct}; lines join with connectNulls. */
export function toChartRows(points: PortfolioPoint[]): Array<Record<string, number>> {
  return points
    .filter((p) => p.pct !== null)
    .map((p) => ({ t: Date.parse(p.ts), [p.account]: p.pct as number }))
    .sort((a, b) => a.t - b.t);
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
