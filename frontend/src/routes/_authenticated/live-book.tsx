import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { StatusPill } from "@/components/desk/StatusPill";
import { openTradesQuery } from "@/lib/desk-queries";
import {
  EM_DASH,
  formatIstDate,
  formatIstDateTime,
  formatNumber,
  formatPct,
  formatRupees,
  formatRupeesSigned,
} from "@/lib/desk-format";
import type { OpenTrade } from "@/lib/api";

export const Route = createFileRoute("/_authenticated/live-book")({
  head: () => ({
    meta: [
      { title: "Live Book — Alpha Trading Prop Desk" },
      { name: "description", content: "Open paper positions with MTM, capture and ratchet state." },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Live Book — Alpha Trading Prop Desk" },
      {
        property: "og:description",
        content: "Open paper positions with MTM, capture and ratchet state.",
      },
    ],
  }),
  component: LiveBookPage,
});

type SortKey = "symbol" | "strategy" | "entered" | "max_loss_rs" | "mtm_rs" | "capture_pct";

const COLUMNS: Array<{ key: SortKey | null; label: string; numeric?: boolean }> = [
  { key: "symbol", label: "Symbol" },
  { key: "strategy", label: "Strategy" },
  { key: null, label: "Direction" },
  { key: null, label: "Accounts" },
  { key: null, label: "Lots", numeric: true },
  { key: "entered", label: "Entered" },
  { key: null, label: "Expiry" },
  { key: "max_loss_rs", label: "Max loss", numeric: true },
  { key: "mtm_rs", label: "MTM", numeric: true },
  { key: "capture_pct", label: "Capture", numeric: true },
  { key: null, label: "Ratchet" },
];

function pnlClass(value: number | null) {
  if (value === null) return "text-muted-foreground";
  return value >= 0 ? "text-pnl-up" : "text-pnl-down";
}

function ratchetLine(trade: OpenTrade) {
  const peak = trade.ratchet_peak_pct === null ? EM_DASH : formatPct(trade.ratchet_peak_pct, 1);
  const lock = trade.ratchet_lock_pct === null ? EM_DASH : formatPct(trade.ratchet_lock_pct, 1);
  return `peak ${peak} / lock ${lock}`;
}

function LiveBookPage() {
  const { data, isPending, isError } = useQuery(openTradesQuery);
  const [strategy, setStrategy] = useState<string>("ALL");
  const [sortKey, setSortKey] = useState<SortKey>("entered");
  const [asc, setAsc] = useState(false);

  const strategies = useMemo(() => {
    const set = new Set((data ?? []).map((t) => t.strategy));
    return ["ALL", ...Array.from(set)];
  }, [data]);

  const rows = useMemo(() => {
    const filtered = (data ?? []).filter((t) => strategy === "ALL" || t.strategy === strategy);
    return [...filtered].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      if (typeof av === "number" && typeof bv === "number") return asc ? av - bv : bv - av;
      return asc
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av));
    });
  }, [data, strategy, sortKey, asc]);

  function toggleSort(key: SortKey | null) {
    if (!key) return;
    if (key === sortKey) setAsc((prev) => !prev);
    else {
      setSortKey(key);
      setAsc(false);
    }
  }

  return (
    <>
      <PageHeader
        title="Live Book"
        description="Open paper positions"
        sources={["market_snapshot", "brain_map.db"]}
      />

      <Panel
        title={`Open positions (${rows.length})`}
        right={
          <label className="num flex items-center gap-2 text-[11px] text-muted-foreground uppercase">
            Strategy
            <select
              value={strategy}
              onChange={(e) => setStrategy(e.target.value)}
              className="num rounded-sm border border-input bg-background px-2 py-1 text-[11px] text-foreground"
            >
              {strategies.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
        }
      >
        {isError ? (
          <EmptyState label="Live book unavailable" />
        ) : isPending ? (
          <EmptyState label="Loading positions…" />
        ) : rows.length === 0 ? (
          <EmptyState label="No open positions" />
        ) : (
          <>
            {/* Desktop table */}
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-border">
                    {COLUMNS.map((col) => (
                      <th
                        key={col.label}
                        onClick={() => toggleSort(col.key)}
                        className={`num px-2 py-2 text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase ${
                          col.numeric ? "text-right" : ""
                        } ${col.key ? "cursor-pointer select-none hover:text-foreground" : ""}`}
                      >
                        {col.label}
                        {col.key === sortKey ? (asc ? " ▲" : " ▼") : ""}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((trade) => (
                    <tr key={trade.id} className="border-b border-border/60 last:border-0">
                      <td className="num px-2 py-2 text-xs font-medium">{trade.symbol}</td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {trade.strategy}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {trade.direction}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {trade.accounts}
                      </td>
                      <td className="num px-2 py-2 text-right text-xs">
                        {formatNumber(trade.lots)}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {formatIstDateTime(trade.entered)}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {trade.expiry === "—" ? EM_DASH : formatIstDate(trade.expiry)}
                      </td>
                      <td className="num px-2 py-2 text-right text-xs">
                        {formatRupees(trade.max_loss_rs)}
                      </td>
                      <td className={`num px-2 py-2 text-right text-xs ${pnlClass(trade.mtm_rs)}`}>
                        {trade.mtm_rs === null ? EM_DASH : formatRupeesSigned(trade.mtm_rs)}
                      </td>
                      <td
                        className={`num px-2 py-2 text-right text-xs ${pnlClass(trade.capture_pct)}`}
                      >
                        {formatPct(trade.capture_pct, 1)}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        <span className="block">{trade.ratchet}</span>
                        <span className="block text-[10px]">{ratchetLine(trade)}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* Mobile cards */}
            <div className="space-y-3 md:hidden">
              {rows.map((trade) => (
                <article key={trade.id} className="rounded-sm border border-border p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="num text-xs font-semibold">{trade.symbol}</p>
                      <p className="num text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
                        {trade.strategy} · {trade.direction}
                      </p>
                    </div>
                    <StatusPill
                      label={trade.mtm_rs === null ? "NO MTM" : trade.mtm_rs >= 0 ? "UP" : "DOWN"}
                      tone={
                        trade.mtm_rs === null ? "neutral" : trade.mtm_rs >= 0 ? "ok" : "bad"
                      }
                    />
                  </div>
                  <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2">
                    {[
                      ["Accounts", trade.accounts],
                      ["Lots", formatNumber(trade.lots)],
                      ["MTM", trade.mtm_rs === null ? EM_DASH : formatRupeesSigned(trade.mtm_rs)],
                      ["Capture", formatPct(trade.capture_pct, 1)],
                      ["Max loss", formatRupees(trade.max_loss_rs)],
                      ["Expiry", trade.expiry === "—" ? EM_DASH : formatIstDate(trade.expiry)],
                      ["Entered", formatIstDateTime(trade.entered)],
                      ["Ratchet", `${trade.ratchet} · ${ratchetLine(trade)}`],
                    ].map(([label, value]) => (
                      <div key={label}>
                        <dt className="num text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
                          {label}
                        </dt>
                        <dd className="num text-xs">{value}</dd>
                      </div>
                    ))}
                  </dl>
                </article>
              ))}
            </div>
          </>
        )}
      </Panel>
    </>
  );
}
