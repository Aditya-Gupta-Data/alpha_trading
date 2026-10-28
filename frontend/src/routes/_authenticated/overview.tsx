import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { Actionables } from "@/components/desk/Actionables";
import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { PortfoliosPanel } from "@/components/desk/PortfoliosPanel";
import { StatusPill } from "@/components/desk/StatusPill";
import { latestReconQuery, treasuryQuery } from "@/lib/desk-queries";
import {
  EM_DASH,
  formatIstDate,
  formatIstDateTime,
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
  const headline =
    priced && typeof account.net_equity === "number" ? account.net_equity : account.equity;
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
          <p
            className={`num text-lg font-semibold ${!priced ? "" : (unreal ?? 0) >= 0 ? "text-pnl-up" : "text-pnl-down"}`}
          >
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
          <p
            className={`num text-lg font-semibold ${realizedUp ? "text-pnl-up" : "text-pnl-down"}`}
          >
            {formatRupeesSigned(account.realized_pnl)}
          </p>
          <p className="text-[11px] text-muted-foreground">
            realized equity {formatRupees(account.equity)}
          </p>
        </div>
        <div>
          <p className="text-xs text-muted-foreground">Drawdown from peak</p>
          <p
            className={`num text-lg font-semibold ${account.drawdown_pct > 0 ? "text-pnl-down" : ""}`}
          >
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

function ReconBanner({
  recon,
  isPending,
}: {
  recon: ReconRow | null | undefined;
  isPending: boolean;
}) {
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
      <StatusPill
        label={verdict === "PARITY" ? "OK" : verdict === "MISMATCH" ? "Problem" : "Unsure"}
        tone={tone}
      />
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
  // Pool moves (decision #117): a reset or an injection moves equity with no
  // trade behind it — drawn as a labelled dashed line, never smoothed away.
  const events = (treasury.data?.capital_events ?? [])
    .map((e) => ({ ...e, t: Date.parse(e.ts) }))
    .filter((e) => !Number.isNaN(e.t));

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
              {treasury.data.PAPER_2L_LIVE && <AccountCard account={treasury.data.PAPER_2L_LIVE} />}
              {treasury.data.PAPER_2L_LIVE_GEX && (
                <AccountCard account={treasury.data.PAPER_2L_LIVE_GEX} />
              )}
            </div>

            <Panel title="Compounding — PAPER_10L">
              {(() => {
                const a = treasury.data.PAPER_10L;
                const cagrTone =
                  a.cagr_pct === null ? "" : a.cagr_pct >= 0 ? "text-pnl-up" : "text-pnl-down";
                return (
                  <div className="mb-5 grid grid-cols-2 gap-6 sm:grid-cols-4">
                    <div>
                      <p className="text-xs text-muted-foreground">CAGR (annualised)</p>
                      <p className={`num text-2xl font-semibold ${cagrTone}`}>
                        {formatPct(a.cagr_pct)}
                      </p>
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
                      <p
                        className={`num text-2xl font-semibold ${(a.abs_return_pct ?? 0) >= 0 ? "text-pnl-up" : "text-pnl-down"}`}
                      >
                        {formatPct(a.abs_return_pct)}
                      </p>
                      <p className="text-[11px] text-muted-foreground">
                        on {formatRupees(a.base_equity ?? a.starting_capital)} contributed
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Days compounding</p>
                      <p className="num text-2xl font-semibold">
                        {a.days_elapsed === null ? EM_DASH : a.days_elapsed.toFixed(0)}
                      </p>
                      <p className="text-[11px] text-muted-foreground">
                        since {a.base_ts ? formatIstDate(a.base_ts) : "the ₹10L base"}
                      </p>
                    </div>
                    <div>
                      <p className="text-xs text-muted-foreground">Formula</p>
                      <p className="num text-sm font-semibold">(E/E₀)^(365/d) − 1</p>
                      <p className="text-[11px] text-muted-foreground">
                        realized equity, no unrealized marks
                      </p>
                    </div>
                  </div>
                );
              })()}
              {events.length > 0 && (
                <ul className="mt-3 space-y-1 text-xs">
                  {events.map((e) => (
                    <li key={`${e.kind}-${e.t}`} className="flex gap-2">
                      <span className="num shrink-0 text-muted-foreground">
                        {formatIstDate(e.ts)}
                      </span>
                      <span>
                        <span className="font-semibold">{e.short ?? e.label}</span> — {e.label}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {curve.length > 0 && (
                <p className="mt-2 text-[11px] text-muted-foreground">
                  The ₹ curve is the left axis of the portfolios chart below, with the benchmarks.
                  Capital moves above are not trading. Return and CAGR are measured from the ₹10L
                  base
                  {treasury.data.PAPER_10L.base_ts
                    ? ` (${formatIstDate(treasury.data.PAPER_10L.base_ts)})`
                    : ""}
                  .
                </p>
              )}
            </Panel>

            <PortfoliosPanel />

            <ReconBanner recon={recon.data} isPending={recon.isPending} />
          </>
        )}
      </div>
    </>
  );
}
