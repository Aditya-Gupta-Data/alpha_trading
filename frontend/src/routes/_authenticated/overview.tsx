import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { ReturnsChart } from "@/components/desk/ReturnsChart";
import { marketViewQuery } from "@/lib/desk-queries";
import { EM_DASH, formatIstDateTime } from "@/lib/desk-format";
import type { MarketHighlight, MarketView } from "@/lib/api";

export const Route = createFileRoute("/_authenticated/overview")({
  head: () => ({
    meta: [
      { title: "Overview — Alpha Trading Prop Desk" },
      { name: "description", content: "How the desk reads the market right now." },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Overview — Alpha Trading Prop Desk" },
      { property: "og:description", content: "How the desk reads the market right now." },
    ],
  }),
  component: OverviewPage,
});

const VIEW_TONE: Record<string, string> = {
  bullish: "text-pnl-up",
  bearish: "text-pnl-down",
};

const DOT: Record<MarketHighlight["tone"], string> = {
  up: "bg-pnl-up",
  down: "bg-pnl-down",
  warn: "bg-warn",
  neutral: "bg-muted-foreground",
};

function day(iso: string | null | undefined): string {
  if (!iso) return EM_DASH;
  const d = new Date(`${iso.slice(0, 10)}T12:00:00+05:30`);
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleDateString("en-IN", { day: "numeric", month: "short", timeZone: "Asia/Kolkata" });
}

const crore = (v: number | null | undefined) =>
  typeof v === "number"
    ? `${v >= 0 ? "+" : "−"}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: 0 })} cr`
    : EM_DASH;

const level = (v: number | null | undefined) =>
  typeof v === "number" ? v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) : EM_DASH;

function Tile({
  label,
  value,
  tone = "",
  children,
}: {
  label: string;
  value: string;
  tone?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className={`num mt-0.5 text-2xl font-semibold ${tone}`}>{value}</p>
      <div className="mt-1 space-y-0.5 text-[11px] text-muted-foreground">{children}</div>
    </div>
  );
}

function MarketRead({ view }: { view: MarketView }) {
  const names = view.engine.names;
  const bull = names.filter((n) => n.view === "bullish").length;
  const bear = names.filter((n) => n.view === "bearish").length;
  const lean =
    names.length === 0 ? null : bull > bear ? "Bullish" : bear > bull ? "Bearish" : "Mixed";
  const nifty = view.gamma?.indices["NIFTY 50"];
  const spot = view.spots["NIFTY 50"];
  const vix = view.vix.intraday ?? view.vix.close;
  const vixBand = view.vix.intraday !== null ? view.vix.intraday_band : view.vix.close_band;
  const flows = view.flows;
  return (
    <Panel
      title="How the desk reads the market"
      right={
        view.spots_as_of ? (
          <span className="num text-[11px] text-muted-foreground">
            prices {formatIstDateTime(view.spots_as_of)}
          </span>
        ) : undefined
      }
    >
      <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Desk's read today"
          value={lean ?? EM_DASH}
          tone={lean === "Bullish" ? "text-pnl-up" : lean === "Bearish" ? "text-pnl-down" : ""}
        >
          {names.length > 0 ? (
            <>
              <p>
                {Math.max(bull, bear)} of {names.length} names it proposed trades on ·{" "}
                {day(view.engine.date)}
              </p>
              <p className="flex flex-wrap gap-x-2">
                {names.map((n) => (
                  <span key={n.ticker} className={VIEW_TONE[n.view ?? ""] ?? ""}>
                    {n.ticker.replace(/\.NS$/, "")}
                  </span>
                ))}
              </p>
            </>
          ) : (
            <p>No proposals on record yet</p>
          )}
        </Tile>
        <Tile
          label="India VIX"
          value={typeof vix === "number" ? vix.toFixed(2) : EM_DASH}
          tone={vixBand === "high" ? "text-pnl-down" : ""}
        >
          <p>{vixBand ? `${vixBand} band (high is 16 and above)` : "band unknown"}</p>
          {view.vix.close !== null && view.vix.intraday !== null && (
            <p>
              {day(view.vix.close_date)} close {view.vix.close.toFixed(2)}
            </p>
          )}
        </Tile>
        <Tile
          label="Foreign investors (FII), net"
          value={crore(flows?.fii_net_cr)}
          tone={flows ? (flows.fii_net_cr >= 0 ? "text-pnl-up" : "text-pnl-down") : ""}
        >
          {flows ? (
            <>
              <p>
                {day(flows.date)} · domestic funds (DII) {crore(flows.dii_net_cr)}
              </p>
              {flows.fii_run_days >= 2 && (
                <p>
                  {flows.fii_net_cr >= 0 ? "buying" : "selling"} {flows.fii_run_days} sessions
                  running
                </p>
              )}
            </>
          ) : (
            <p>No flow data on record</p>
          )}
        </Tile>
        <Tile label="NIFTY 50" value={level(spot ?? nifty?.spot)}>
          {nifty ? (
            <>
              <p>
                dealers{" "}
                {nifty.regime_at_spot === "positive"
                  ? "long gamma (damping)"
                  : nifty.regime_at_spot === "negative"
                    ? "short gamma (amplifying)"
                    : "gamma unknown"}{" "}
                · {day(view.gamma?.date)}
              </p>
              {nifty.call_wall !== null && nifty.put_wall !== null && (
                <p>
                  call wall {level(nifty.call_wall)} · put wall {level(nifty.put_wall)}
                </p>
              )}
            </>
          ) : (
            <p>No dealer-gamma reading on record</p>
          )}
        </Tile>
      </div>
    </Panel>
  );
}

function WorthKnowing({ items }: { items: MarketHighlight[] }) {
  return (
    <Panel title="Worth knowing">
      {items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          Nothing out of the ordinary in today's data.
        </p>
      ) : (
        <ul className="space-y-3 text-sm">
          {items.map((h) => (
            <li key={h.text} className="flex gap-3">
              <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOT[h.tone]}`} />
              <span>{h.text}</span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function OverviewPage() {
  const market = useQuery(marketViewQuery);
  return (
    <>
      <PageHeader
        title="Overview"
        description="How the desk reads the market right now, and how its books have done."
        sources={["journal", "brain_map.db"]}
      />
      <div className="space-y-8">
        {market.isError ? (
          <Panel>
            <EmptyState label="Market read unavailable" />
          </Panel>
        ) : market.isPending ? (
          <Panel>
            <EmptyState label="Loading…" />
          </Panel>
        ) : (
          <>
            <MarketRead view={market.data} />
            <WorthKnowing items={market.data.highlights} />
          </>
        )}
        <ReturnsChart />
      </div>
    </>
  );
}
