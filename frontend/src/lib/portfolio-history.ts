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
