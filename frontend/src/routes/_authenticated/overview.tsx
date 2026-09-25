import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import {
  CartesianGrid,
  Legend,
  Line,
  ComposedChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Actionables } from "@/components/desk/Actionables";
import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { StatusPill } from "@/components/desk/StatusPill";
import { latestReconQuery, treasuryQuery } from "@/lib/desk-queries";
import {
  EM_DASH,
  formatIstDate,
  formatIstDateTime,
  formatIstDayMonth,
  formatNumber,
  formatPct,
  formatRupees,
  formatRupeesSigned,
} from "@/lib/desk-format";
import type { AccountTreasury, ReconRow } from "@/lib/api";

export const Route = createFileRoute("/_authenticated/overview")({
  head: () => ({
    meta: [
      { title: "Overview — Alpha Trading Prop Desk" },
      {
        name: "description",
        content: "Paper account equity, realised P&L, drawdown and reconciliation status.",
      },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Overview — Alpha Trading Prop Desk" },
      {
        property: "og:description",
        content: "Paper account equity, realised P&L, drawdown and reconciliation status.",
      },
    ],
  }),
  component: OverviewPage,
});

function Metric({ label, value, tone }: { label: string; value: string; tone?: "up" | "down" }) {
  return (
    <div className="border-t border-border pt-2">
      <p className="num text-[10px] tracking-[0.1em] text-muted-foreground uppercase">{label}</p>
      <p
        className={`num mt-0.5 text-sm font-semibold ${
          tone === "up" ? "text-pnl-up" : tone === "down" ? "text-pnl-down" : ""
        }`}
      >
        {value}
      </p>
    </div>
  );
}

function AccountCard({ account }: { account: AccountTreasury }) {
  const realizedUp = account.realized_pnl >= 0;
  const unreal = account.unrealized_pnl;
  // an older bridge (before #116) sends no MTM fields: treat as unpriced
  const priced = typeof unreal === "number";
  const headline = priced && typeof account.net_equity === "number" ? account.net_equity : account.equity;
  const hasCoverage = typeof account.open_positions === "number";
  return (
    <Panel>
      <p className="text-sm text-muted-foreground">{account.account_id}</p>
      <p className="num mt-1 text-3xl font-semibold">{formatRupees(headline)}</p>
      <p className="text-xs text-muted-foreground">
        {priced
          ? "True net equity — realized + open positions at the engine's last marks"
          : "Realized equity — no open position has a live mark"}
      </p>
      <div className="mt-5 grid grid-cols-2 gap-6 sm:grid-cols-3">
        <div>
          <p className="text-xs text-muted-foreground">Unrealized P&amp;L</p>
          <p className={`num text-lg font-semibold ${!priced ? "" : (unreal ?? 0) >= 0 ? "text-pnl-up" : "text-pnl-down"}`}>
            {priced ? formatRupeesSigned(unreal) : EM_DASH}
          </p>
          {hasCoverage && (
            <p className="text-[11px] text-muted-foreground">
              priced on {account.marked_positions} of {account.open_positions} open
              {account.marks_as_of ? ` · ${formatIstDateTime(account.marks_as_of)}` : ""}
            </p>
          )}
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Realized P&amp;L</p>
          <p className={`num text-lg font-semibold ${realizedUp ? "text-pnl-up" : "text-pnl-down"}`}>
            {formatRupeesSigned(account.realized_pnl)}
          </p>
          <p className="text-[11px] text-muted-foreground">
            realized equity {formatRupees(account.equity)}
          </p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Drawdown from peak</p>
          <p className={`num text-lg font-semibold ${account.drawdown_pct > 0 ? "text-pnl-down" : ""}`}>
            {formatPct(account.drawdown_pct)}
          </p>
          <p className="text-[11px] text-muted-foreground">realized, from the peak</p>
        </div>
      </div>
      <details className="mt-5 text-xs">
        <summary className="cursor-pointer text-muted-foreground">More details</summary>
        <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
          <Metric label="Started with" value={formatRupees(account.starting_capital)} />
          <Metric label="Locked margin" value={formatRupees(account.locked_margin)} />
          <Metric label="Open locks" value={formatNumber(account.open_locks)} />
          <Metric label="Available cash" value={formatRupees(account.available_cash)} />
          {account.rejections !== undefined && (
            <Metric label="Rejections" value={formatNumber(account.rejections)} />
          )}
        </div>
      </details>
    </Panel>
  );
}

function ReconBanner({ recon, isPending }: { recon: ReconRow | null | undefined; isPending: boolean }) {
  if (isPending) return null;
  const verdict = recon?.verdict ?? "UNKNOWN";
  const text =
    verdict === "PARITY"
      ? "Broker records match our records."
      : verdict === "MISMATCH"
        ? "Broker records do not match our records."
        : "Could not confirm broker records.";
  const tone = verdict === "PARITY" ? "ok" : verdict === "MISMATCH" ? "bad" : "warn";
  return (
    <div className="flex items-center gap-3 text-sm text-muted-foreground">
      <StatusPill label={verdict === "PARITY" ? "OK" : verdict === "MISMATCH" ? "Problem" : "Unsure"} tone={tone} />
      {text}
      {recon && <span className="text-xs">· {formatIstDateTime(recon.ts)}</span>}
    </div>
  );
}

function OverviewPage() {
  const treasury = useQuery(treasuryQuery);
  const recon = useQuery(latestReconQuery);
  // X axis on real time (decision #116): each point carries its epoch ms so
  // a two-week gap is wider than seven settlements in one morning.
  const curve = (treasury.data?.equity_curve ?? [])
    .map((p) => ({ ...p, t: Date.parse(p.ts) }))
    .filter((p) => !Number.isNaN(p.t));

  return (
    <>
      <PageHeader
        title="Overview"
        description="How much money each practice account has, and whether anything needs a look."
        sources={["journal", "market_snapshot", "recon"]}
      />

      <div className="space-y-8">
        <Actionables />

        {treasury.isError ? (
          <Panel>
            <EmptyState label="Account data unavailable" />
          </Panel>
        ) : treasury.isPending ? (
          <Panel>
            <EmptyState label="Loading…" />
          </Panel>
        ) : (
          <>
            <div className="grid gap-6 lg:grid-cols-2">
              <AccountCard account={treasury.data.PAPER_10L} />
              <AccountCard account={treasury.data.PAPER_2L} />
              {treasury.data.PAPER_2L_ROT && <AccountCard account={treasury.data.PAPER_2L_ROT} />}
            </div>

            <Panel title="Compounding — PAPER_10L">
              {(() => {
                const a = treasury.data.PAPER_10L;
                const cagrTone = a.cagr_pct === null ? "" : a.cagr_pct >= 0 ? "text-pnl-up" : "text-pnl-down";
                return (
                  <div className="mb-5 grid grid-cols-2 gap-6 sm:grid-cols-4">
                    <div>
                      <p className="text-xs text-muted-foreground">CAGR (annualised)</p>
                      <p className={`num text-2xl font-semibold ${cagrTone}`}>{formatPct(a.cagr_pct)}</p>
                      <p className="text-[11px] text-muted-foreground">
                        {a.days_elapsed === null
                          ? "epoch unknown"
                          : a.days_elapsed < 30
                            ? `from only ${a.days_elapsed.toFixed(0)} days — noisy`
                            : `from ${a.days_elapsed.toFixed(0)} days of live trading`}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Return since the ₹10L base</p>
                      <p className={`num text-2xl font-semibold ${(a.abs_return_pct ?? 0) >= 0 ? "text-pnl-up" : "text-pnl-down"}`}>
                        {formatPct(a.abs_return_pct)}
                      </p>
                      <p className="text-[11px] text-muted-foreground">on the {formatRupees(a.base_equity ?? a.starting_capital)} base</p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Days compounding</p>
                      <p className="num text-2xl font-semibold">{a.days_elapsed === null ? EM_DASH : a.days_elapsed.toFixed(0)}</p>
                      <p className="text-[11px] text-muted-foreground">
                        since {a.base_ts ? formatIstDate(a.base_ts) : "the ₹10L base"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Formula</p>
                      <p className="num text-sm font-semibold">(E/E₀)^(365/d) − 1</p>
                      <p className="text-[11px] text-muted-foreground">realized equity, no unrealized marks</p>
                    </div>
                  </div>
                );
              })()}
              {curve.length === 0 ? (
                <EmptyState label="No history yet" />
              ) : (
                <div className="h-[240px] w-full">
                  <ResponsiveContainer width="100%" height="100%">
                    <ComposedChart data={curve} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
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
                        tickFormatter={(value: number) => formatRupees(value)}
                        width={88}
                      />
                      <Tooltip
                        contentStyle={{
                          background: "var(--color-popover)",
                          border: "1px solid var(--color-border)",
                          borderRadius: 4,
                          fontSize: 12,
                        }}
                        labelFormatter={(value) => formatIstDateTime(new Date(Number(value)).toISOString())}
                        formatter={(value: number, name) =>
                          name === "Drawdown" ? [formatPct(value), name] : [formatRupees(value), name]
                        }
                      />
                      <Legend wrapperStyle={{ fontSize: 12 }} />
                      <Line
                        type="monotone"
                        dataKey="equity"
                        name="Equity"
                        stroke="var(--color-chart-1)"
                        strokeWidth={2}
                        dot={false}
                      />
                    </ComposedChart>
                  </ResponsiveContainer>
                </div>
              )}
            </Panel>

            <ReconBanner recon={recon.data} isPending={recon.isPending} />
          </>
        )}
      </div>
    </>
  );
}
