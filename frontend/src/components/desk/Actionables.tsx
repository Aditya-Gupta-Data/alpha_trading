import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { Panel } from "@/components/desk/Panel";
import { StatusPill } from "@/components/desk/StatusPill";
import {
  freshnessQuery,
  latestReconQuery,
  openTradesQuery,
  treasuryQuery,
} from "@/lib/desk-queries";
import { ageLabel, formatNumber, formatRupeesSigned, freshnessState } from "@/lib/desk-format";

type Severity = "bad" | "warn";
interface Item {
  key: string;
  severity: Severity;
  title: string;
  detail: string;
  to: "/overview" | "/live-book" | "/outcomes";
}

function todayIst(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
}

/** Read-only list of things that need the desk's attention. Derived only from reported data. */
export function Actionables() {
  const treasury = useQuery(treasuryQuery);
  const trades = useQuery(openTradesQuery);
  const recon = useQuery(latestReconQuery);
  const fresh = useQuery(freshnessQuery);

  const loading = treasury.isPending || trades.isPending || recon.isPending || fresh.isPending;
  const items: Item[] = [];

  if (recon.data?.verdict === "MISMATCH") {
    items.push({
      key: "recon",
      severity: "bad",
      title: "Reconciliation mismatch",
      detail: recon.data.mismatches.join("; ") || "Broker and book disagree",
      to: "/overview",
    });
  } else if (recon.data?.verdict === "UNKNOWN" || (!recon.isPending && !recon.data)) {
    items.push({ key: "recon", severity: "warn", title: "Reconciliation unknown", detail: "No confirmed parity", to: "/overview" });
  }

  if (fresh.data) {
    for (const [source, ts] of Object.entries(fresh.data)) {
      const state = freshnessState(ts);
      if (state !== "FRESH") {
        items.push({
          key: `fresh-${source}`,
          severity: "warn",
          title: `${source} ${state.toLowerCase()}`,
          detail: ts ? `Last update ${ageLabel(ts)}` : "No timestamp reported",
          to: "/overview",
        });
      }
    }
  }

  const today = todayIst();
  for (const t of trades.data ?? []) {
    if (t.expiry === today) {
      items.push({ key: `exp-${t.id}`, severity: "warn", title: `Expires today · ${t.symbol}`, detail: `${t.strategy} · ${t.accounts}`, to: "/live-book" });
    }
    if (t.mtm_rs === null) {
      items.push({ key: `nq-${t.id}`, severity: "warn", title: `No quote · ${t.symbol}`, detail: "MTM unavailable", to: "/live-book" });
    } else if (t.mtm_rs < 0 && t.max_loss_rs > 0 && -t.mtm_rs / t.max_loss_rs >= 0.2) {
      items.push({
        key: `loss-${t.id}`,
        severity: "bad",
        title: `Losing position · ${t.symbol}`,
        detail: `MTM ${formatRupeesSigned(t.mtm_rs)} (${Math.round((-t.mtm_rs / t.max_loss_rs) * 100)}% of max loss)`,
        to: "/live-book",
      });
    }
  }

  const rej = treasury.data?.PAPER_2L.rejections ?? 0;
  if (rej > 0) {
    items.push({ key: "rej", severity: "warn", title: `PAPER_2L rejections: ${formatNumber(rej)}`, detail: "Orders rejected by sizing / margin", to: "/overview" });
  }

  items.sort((a, b) => (a.severity === b.severity ? 0 : a.severity === "bad" ? -1 : 1));

  const shown = items.slice(0, 4);

  return (
    <Panel title="Needs attention">
      {loading ? (
        <p className="text-sm text-muted-foreground">Checking…</p>
      ) : items.length === 0 ? (
        <div className="flex items-center gap-3 text-sm text-muted-foreground">
          <StatusPill label="All good" tone="ok" /> Nothing needs your attention right now.
        </div>
      ) : (
        <ul className="space-y-2">
          {shown.map((item) => (
            <li key={item.key}>
              <Link to={item.to} className="flex items-center gap-3 rounded-md py-1 text-sm hover:bg-muted/40">
                <StatusPill label={item.severity === "bad" ? "Urgent" : "Check"} tone={item.severity} />
                <span>{item.title}</span>
              </Link>
            </li>
          ))}
          {items.length > shown.length && (
            <li className="text-xs text-muted-foreground">+ {items.length - shown.length} more smaller items</li>
          )}
        </ul>
      )}
    </Panel>
  );
}
