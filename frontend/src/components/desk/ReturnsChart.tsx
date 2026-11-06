import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PortfolioTooltip, type TooltipLine } from "@/components/desk/PortfoliosPanel";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { equityHistoryQuery, treasuryQuery } from "@/lib/desk-queries";
import { formatIstDate } from "@/lib/desk-format";
import {
  PORTFOLIOS,
  PORTFOLIO_NAMES,
  PORTFOLIO_STROKES,
  benchKey,
  benchmarkSeries,
  formatTimeTick,
  hasLine,
  inWindow,
  pctKey,
  seriesByAccount,
  timeTicks,
  toPctRows,
  windowStart,
  type SeriesPoint,
  type WindowChoice,
} from "@/lib/portfolio-history";

const CHOICES: WindowChoice[] = ["1W", "1M", "All time"];
const NIFTY_STROKE = "var(--color-muted-foreground)";

/** Every book's % return on one axis, with Nifty 50 dashed (#183). The detailed ₹ chart lives on Portfolios. */
export function ReturnsChart() {
  const { data, isPending, isError } = useQuery(equityHistoryQuery);
  const treasury = useQuery(treasuryQuery);
  const bench = treasury.data?.benchmarks;
  const [win, setWin] = useState<WindowChoice>("All time");

  const start = windowStart(win);
  const net = useMemo(() => inWindow(data?.net ?? [], start), [data, start]);
  const useNet = hasLine(net);
  const pts = useMemo(
    () => (useNet ? net : inWindow(data?.realized ?? [], start)),
    [useNet, net, data, start],
  );
  const series = useMemo(() => seriesByAccount(pts), [pts]);
  // Nifty from the books' first point, rebased to 0% there: the index's own move over the same span.
  const nifty = useMemo(() => {
    const out = new Map<string, SeriesPoint[]>();
    const from = Math.min(...[...series.values()].map((s) => s[0]?.t ?? Infinity));
    const raw = benchmarkSeries(bench?.series?.nifty50 ?? [], bench?.base ?? 0, start).filter(
      (p) => p.t >= from && p.inr !== null,
    );
    const base = raw[0]?.inr;
    if (base)
      out.set(
        "nifty50",
        raw.map((p) => ({ t: p.t, inr: null, pct: ((p.inr ?? base) / base - 1) * 100 })),
      );
    return out;
  }, [bench, start, series]);
  const rows = useMemo(() => toPctRows(series, nifty), [series, nifty]);
  const first = rows[0]?.["t"] ?? 0;
  const last = rows[rows.length - 1]?.["t"] ?? 0;
  const ticks = useMemo(() => timeTicks(first, last), [first, last]);
  const accounts = PORTFOLIOS.filter((a) => series.has(a));

  const lines: TooltipLine[] = [
    ...accounts.map((a) => ({
      key: a,
      label: PORTFOLIO_NAMES[a] ?? a,
      stroke: PORTFOLIO_STROKES[a] ?? "var(--color-chart-1)",
      series: series.get(a),
    })),
    ...(nifty.has("nifty50")
      ? [{ key: "nifty50", label: "Nifty 50", stroke: NIFTY_STROKE, series: nifty.get("nifty50") }]
      : []),
  ];

  return (
    <Panel
      title="Returns"
      right={
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={win}
          onValueChange={(v) => v && setWin(v as WindowChoice)}
          aria-label="Timeframe"
        >
          {CHOICES.map((w) => (
            <ToggleGroupItem key={w} value={w} className="h-7 px-2 text-xs">
              {w}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      }
    >
      {isError ? (
        <EmptyState label="Return history unavailable" />
      ) : isPending ? (
        <EmptyState label="Loading…" />
      ) : rows.length === 0 ? (
        <EmptyState label="No points in this window yet" />
      ) : (
        <div className="h-[300px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
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
                domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                tickFormatter={(value: number) => `${value.toFixed(0)}%`}
                width={40}
              />
              <Tooltip
                content={<PortfolioTooltip lines={lines} />}
                cursor={{ stroke: "var(--color-muted-foreground)", strokeDasharray: "3 3" }}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <ReferenceLine y={0} stroke="var(--color-border)" />
              {nifty.has("nifty50") && (
                <Line
                  dataKey={benchKey("nifty50")}
                  name="Nifty 50"
                  connectNulls
                  stroke={NIFTY_STROKE}
                  strokeWidth={1.5}
                  strokeDasharray="5 3"
                  dot={false}
                  isAnimationActive={false}
                />
              )}
              {accounts.map((a) => (
                <Line
                  key={a}
                  type={useNet ? "linear" : "stepAfter"}
                  dataKey={pctKey(a)}
                  name={PORTFOLIO_NAMES[a] ?? a}
                  connectNulls
                  stroke={PORTFOLIO_STROKES[a]}
                  strokeWidth={2}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <p className="mt-2 text-[11px] text-muted-foreground">
        % return of each book since it opened,{" "}
        {useNet ? "counting open positions at their marks" : "settled trades only"}.
        {nifty.has("nifty50") && first
          ? ` Nifty 50 (dashed) is the index's own move from ${formatIstDate(new Date(first).toISOString())}.`
          : ""}{" "}
        Per-account detail is on the Portfolios tab.
      </p>
    </Panel>
  );
}
