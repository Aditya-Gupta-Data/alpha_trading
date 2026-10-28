import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  type TooltipProps,
} from "recharts";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { equityHistoryQuery, treasuryQuery } from "@/lib/desk-queries";
import {
  formatIstDate,
  formatIstDateTime,
  formatIstDayMonth,
  formatIstTime,
  formatPct,
  formatRupees,
} from "@/lib/desk-format";
import type { BenchmarkKey } from "@/lib/api";
import {
  INR_ACCOUNT,
  PORTFOLIOS,
  PORTFOLIO_NAMES,
  PORTFOLIO_STROKES,
  WINDOWS,
  asOf,
  benchKey,
  benchmarkSeries,
  formatTimeTick,
  hasLine,
  inWindow,
  inrKey,
  pctKey,
  seriesByAccount,
  timeTicks,
  toDualRows,
  windowStart,
  type SeriesPoint,
  type WindowChoice,
} from "@/lib/portfolio-history";

type Basis = "net" | "realized";

// The Compounding chart's optional passive lines (decision #119), now on the ₹ axis here.
const BENCHMARKS: { key: BenchmarkKey; label: string; stroke: string }[] = [
  { key: "nifty50", label: "Nifty 50", stroke: "var(--color-chart-3)" },
  { key: "gold", label: "Gold (GOLDBEES)", stroke: "var(--color-chart-4)" },
  { key: "fd_7pct", label: "FD 7% p.a.", stroke: "var(--color-chart-5)" },
];

/** One drawn line, as the tooltip needs it: its whole series, so it can answer for any hovered instant. */
export type TooltipLine = {
  key: string;
  label: string;
  stroke: string;
  series: SeriesPoint[] | undefined;
};

type PortfolioTooltipProps = TooltipProps<number, string> & { lines: TooltipLine[] };

/**
 * Every line's ₹ and % at the hovered instant (2026-10-28). recharts' own
 * payload holds only the series that have a point in the hovered row, and the
 * rows are sparse (each point carries one line's key), so the default tooltip
 * dropped every other book. This one reads each line's last point at or before
 * the hovered time; a line that has not started yet reads "not started", a
 * missing ₹ or % value reads "—", and a carried value says when it is from.
 */
export function PortfolioTooltip({ active, label, lines }: PortfolioTooltipProps) {
  const t = Number(label);
  if (!active || !Number.isFinite(t)) return null;
  return (
    <div className="rounded border border-border bg-popover px-3 py-2 text-xs shadow-sm">
      <p className="mb-1 font-semibold">{formatIstDateTime(new Date(t).toISOString())}</p>
      <table className="num">
        <tbody>
          {lines.map((line) => {
            const p = asOf(line.series, t);
            return (
              <tr key={line.key}>
                <td className="pr-2">
                  <span
                    className="mr-1 inline-block h-2 w-2 rounded-full"
                    style={{ background: line.stroke }}
                  />
                  {line.label}
                </td>
                {p === null ? (
                  <td colSpan={3} className="text-muted-foreground">
                    not started
                  </td>
                ) : (
                  <>
                    <td className="pr-2 text-right">{formatRupees(p.inr)}</td>
                    <td className="pr-2 text-right">{formatPct(p.pct)}</td>
                    <td className="text-muted-foreground">{carriedFrom(p.t, t)}</td>
                  </>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const IST_DAY = (t: number) => Math.floor((t + 5.5 * 3600_000) / 86_400_000);

/** "" for the hovered point itself; else when the carried value is from (time today, date before). */
function carriedFrom(pointT: number, hoveredT: number): string {
  if (pointT === hoveredT) return "";
  const iso = new Date(pointT).toISOString();
  return `as of ${IST_DAY(pointT) === IST_DAY(hoveredT) ? formatIstTime(iso) : formatIstDayMonth(iso)}`;
}

/**
 * All paper portfolios on ONE dual-axis chart (2026-10-28): the Model Portfolio
 * (PAPER_10L) in ₹ on the left axis — its compounding — and every ₹2L book in %
 * return on contributed capital on the right, with the optional passive
 * benchmarks in ₹ beside the Model Portfolio. Realized = a step at every
 * settlement (full history); True net = realized + open marks, recorded every
 * 15 min on the VM from 9 Oct 2026.
 */
export function PortfoliosPanel() {
  const { data, isPending, isError } = useQuery(equityHistoryQuery);
  const treasury = useQuery(treasuryQuery);
  const bench = treasury.data?.benchmarks;
  const [win, setWin] = useState<WindowChoice>("All time");
  const [basisChoice, setBasisChoice] = useState<Basis | null>(null);
  const [shown, setShown] = useState<BenchmarkKey[]>([]);

  const start = windowStart(win);
  const netPts = useMemo(() => inWindow(data?.net ?? [], start), [data, start]);
  const basis: Basis = basisChoice ?? (hasLine(netPts) ? "net" : "realized");
  const pts = useMemo(
    () => (basis === "net" ? netPts : inWindow(data?.realized ?? [], start)),
    [basis, netPts, data, start],
  );
  const series = useMemo(() => seriesByAccount(pts), [pts]);
  const benchSeries = useMemo(() => {
    const out = new Map<string, SeriesPoint[]>();
    for (const key of shown) {
      const s = benchmarkSeries(bench?.series?.[key] ?? [], bench?.base ?? 0, start);
      if (s.length > 0) out.set(key, s);
    }
    return out;
  }, [shown, bench, start]);
  const rows = useMemo(() => toDualRows(series, benchSeries), [series, benchSeries]);
  const first = rows[0]?.["t"] ?? 0;
  const last = rows[rows.length - 1]?.["t"] ?? 0;
  const ticks = useMemo(() => timeTicks(first, last), [first, last]);
  const accounts = PORTFOLIOS.filter((a) => series.has(a));
  const pctAccounts = accounts.filter((a) => a !== INR_ACCOUNT);
  const hasInr = accounts.includes(INR_ACCOUNT) || benchSeries.size > 0;
  const events =
    basis === "realized"
      ? (data?.capital_events ?? []).filter((e) => start === null || Date.parse(e.ts) >= start)
      : [];

  const tooltipLines: TooltipLine[] = [
    ...accounts.map((a) => ({
      key: a,
      label: PORTFOLIO_NAMES[a] ?? a,
      stroke: PORTFOLIO_STROKES[a] ?? "var(--color-chart-1)",
      series: series.get(a),
    })),
    ...BENCHMARKS.filter((b) => benchSeries.has(b.key)).map((b) => ({
      key: benchKey(b.key),
      label: b.label,
      stroke: b.stroke,
      series: benchSeries.get(b.key),
    })),
  ];

  return (
    <Panel title="All portfolios — Model Portfolio in ₹ (left), ₹2L books in % (right)">
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2">
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={win}
          onValueChange={(v) => v && setWin(v as WindowChoice)}
          aria-label="Timeframe"
        >
          {WINDOWS.map((w) => (
            <ToggleGroupItem key={w} value={w} className="h-7 px-2 text-xs">
              {w}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={basis}
          onValueChange={(v) => v && setBasisChoice(v as Basis)}
          aria-label="Basis"
        >
          <ToggleGroupItem
            value="net"
            className="h-7 px-2 text-xs"
            title="Realized + open positions' marks, recorded every 15 min from 9 Oct 2026"
          >
            True net equity
          </ToggleGroupItem>
          <ToggleGroupItem
            value="realized"
            className="h-7 px-2 text-xs"
            title="Moves only when a trade settles; full history back to July"
          >
            Realized
          </ToggleGroupItem>
        </ToggleGroup>
        {bench && (
          <ToggleGroup
            type="multiple"
            size="sm"
            variant="outline"
            value={shown}
            onValueChange={(v) => setShown(v as BenchmarkKey[])}
            aria-label="Benchmark lines"
          >
            {BENCHMARKS.map((b) => (
              <ToggleGroupItem
                key={b.key}
                value={b.key}
                disabled={(bench.series?.[b.key] ?? []).length === 0}
                title={bench.notes?.[b.key] ?? bench.sources?.[b.key]}
                className="h-7 px-2 text-xs"
              >
                <span
                  className="mr-1 inline-block h-2 w-2 rounded-full"
                  style={{ background: b.stroke }}
                />
                {b.label}
              </ToggleGroupItem>
            ))}
          </ToggleGroup>
        )}
      </div>
      {isError ? (
        <EmptyState label="Portfolio history unavailable" />
      ) : isPending ? (
        <EmptyState label="Loading portfolio history…" />
      ) : rows.length === 0 ? (
        <EmptyState
          label={
            basis === "net"
              ? "No true-net-equity points in this window yet — recorded every 15 min during the session from 9 Oct 2026. Switch to Realized for the full history."
              : "No points in this window yet"
          }
        />
      ) : (
        <div className="h-[320px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={rows} margin={{ top: 6, right: 0, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--color-border)" vertical={false} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                ticks={ticks}
                interval={0}
                tickFormatter={(value: number) => formatTimeTick(value, last - first)}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                minTickGap={8}
              />
              <YAxis
                yAxisId="inr"
                orientation="left"
                hide={!hasInr}
                domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                tickFormatter={(value: number) => formatRupees(value)}
                width={84}
              />
              <YAxis
                yAxisId="pct"
                orientation="right"
                hide={pctAccounts.length === 0}
                domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                tickFormatter={(value: number) => `${value.toFixed(0)}%`}
                width={44}
              />
              <Tooltip
                content={<PortfolioTooltip lines={tooltipLines} />}
                cursor={{ stroke: "var(--color-muted-foreground)", strokeDasharray: "3 3" }}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {pctAccounts.length > 0 && (
                <ReferenceLine yAxisId="pct" y={0} stroke="var(--color-border)" />
              )}
              {events.map((e) => (
                <ReferenceLine
                  key={`${e.kind}-${e.ts}`}
                  yAxisId="inr"
                  x={Date.parse(e.ts)}
                  stroke="var(--color-muted-foreground)"
                  strokeDasharray="4 3"
                />
              ))}
              {BENCHMARKS.filter((b) => benchSeries.has(b.key)).map((b) => (
                <Line
                  key={b.key}
                  yAxisId="inr"
                  type="linear"
                  dataKey={benchKey(b.key)}
                  name={`${b.label} (₹)`}
                  connectNulls
                  stroke={b.stroke}
                  strokeWidth={1.5}
                  strokeDasharray="5 3"
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
              {accounts.map((a) => (
                <Line
                  key={a}
                  yAxisId={a === INR_ACCOUNT ? "inr" : "pct"}
                  type={basis === "realized" ? "stepAfter" : "linear"}
                  dataKey={a === INR_ACCOUNT ? inrKey(a) : pctKey(a)}
                  name={`${PORTFOLIO_NAMES[a] ?? a} (${a === INR_ACCOUNT ? "₹" : "%"})`}
                  connectNulls
                  stroke={PORTFOLIO_STROKES[a]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      )}
      <p className="mt-2 text-[11px] text-muted-foreground">
        Left axis: Model Portfolio (PAPER_10L) equity in ₹ — its compounding. Right axis:
        Small-Account Test (PAPER_2L), Capital-Rotation Test (PAPER_2L_ROT), Live-Quote Test
        (PAPER_2L_LIVE) and Live-Quote GEX Test (PAPER_2L_LIVE_GEX) as % return on contributed
        capital, each from 0% on the day it opened. Hover shows every line&apos;s ₹ and % at that
        moment; a value carried from an earlier point says when it is from.
        {bench && shown.length > 0
          ? ` Benchmarks: ${formatRupees(bench.base)} invested on ${formatIstDate(`${bench.epoch}T15:30:00+05:30`)}.`
          : ""}
        {events.length > 0 ? " Dashed vertical lines are capital moves, not trading." : ""}
      </p>
    </Panel>
  );
}
