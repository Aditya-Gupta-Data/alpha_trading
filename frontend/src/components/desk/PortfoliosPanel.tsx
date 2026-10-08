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
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { equityHistoryQuery } from "@/lib/desk-queries";
import { formatIstDate, formatIstDateTime, formatIstDayMonth, formatPct } from "@/lib/desk-format";
import {
  PORTFOLIOS,
  PORTFOLIO_NAMES,
  PORTFOLIO_STROKES,
  WINDOWS,
  hasLine,
  inWindow,
  toChartRows,
  windowStart,
  type WindowChoice,
} from "@/lib/portfolio-history";

type Basis = "net" | "realized";

/**
 * The four paper portfolios on ONE % axis (owner request 2026-10-09) — a
 * ₹10L and three ₹2L books are only comparable as % return on the capital
 * contributed at the time. Realized = a step at every settlement (full
 * history); True net = realized + open marks, recorded every 15 min on the VM
 * from 9 Oct 2026.
 */
export function PortfoliosPanel() {
  const { data, isPending, isError } = useQuery(equityHistoryQuery);
  const [win, setWin] = useState<WindowChoice>("All time");
  const [basisChoice, setBasisChoice] = useState<Basis | null>(null);

  const start = windowStart(win);
  const netPts = useMemo(() => inWindow(data?.net ?? [], start), [data, start]);
  const basis: Basis = basisChoice ?? (hasLine(netPts) ? "net" : "realized");
  const pts = useMemo(
    () => (basis === "net" ? netPts : inWindow(data?.realized ?? [], start)),
    [basis, netPts, data, start],
  );
  const rows = useMemo(() => toChartRows(pts), [pts]);
  const accounts = PORTFOLIOS.filter((a) => pts.some((p) => p.account === a));
  const events =
    basis === "realized"
      ? (data?.capital_events ?? []).filter((e) => start === null || Date.parse(e.ts) >= start)
      : [];

  return (
    <Panel title="All four portfolios — % return on contributed capital">
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
        <div className="h-[300px] w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke="var(--color-border)" vertical={false} />
              <XAxis
                dataKey="t"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                tickFormatter={(value: number) => formatIstDayMonth(new Date(value).toISOString())}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                minTickGap={64}
              />
              <YAxis
                domain={["auto", "auto"]}
                tick={{ fontSize: 10, fill: "var(--color-muted-foreground)" }}
                stroke="var(--color-border)"
                tickFormatter={(value: number) => `${value.toFixed(0)}%`}
                width={48}
              />
              <Tooltip
                contentStyle={{
                  background: "var(--color-popover)",
                  border: "1px solid var(--color-border)",
                  borderRadius: 4,
                  fontSize: 12,
                }}
                labelFormatter={(value) => formatIstDateTime(new Date(Number(value)).toISOString())}
                formatter={(value: number, name) => [formatPct(value), String(name)]}
              />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              {events.map((e) => (
                <ReferenceLine
                  key={`${e.kind}-${e.ts}`}
                  x={Date.parse(e.ts)}
                  stroke="var(--color-muted-foreground)"
                  strokeDasharray="4 3"
                />
              ))}
              {accounts.map((a) => (
                <Line
                  key={a}
                  type={basis === "realized" ? "stepAfter" : "linear"}
                  dataKey={a}
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
        Model Portfolio = PAPER_10L · Small-Account Test = PAPER_2L · Capital-Rotation Test =
        PAPER_2L_ROT · Live-Quote Test = PAPER_2L_LIVE. The Model Portfolio&apos;s % is on the
        capital contributed at the time (₹10L, ₹2L from the 21 Jul clean sheet, ₹10L again from the
        7 Aug injection
        {events.length > 0 ? " — dashed lines" : ""}); the ₹2L books start at 0% on the day each
        opened.
        {(data?.capital_events ?? []).length > 0 && basis === "realized" && start === null
          ? ` Capital moves: ${(data?.capital_events ?? []).map((e) => `${e.short ?? e.label} (${formatIstDate(e.ts)})`).join(", ")}.`
          : ""}
      </p>
    </Panel>
  );
}
