import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { outcomesQuery } from "@/lib/desk-queries";
import { formatIstDateTime, formatR, formatRupeesSigned } from "@/lib/desk-format";

export const Route = createFileRoute("/_authenticated/outcomes")({
  head: () => ({
    meta: [
      { title: "Recent Outcomes — Alpha Trading Prop Desk" },
      { name: "description", content: "Settled paper trades with resolution, P&L and R-multiple." },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Recent Outcomes — Alpha Trading Prop Desk" },
      {
        property: "og:description",
        content: "Settled paper trades with resolution, P&L and R-multiple.",
      },
    ],
  }),
  component: OutcomesPage,
});

function OutcomesPage() {
  const { data, isPending, isError } = useQuery(outcomesQuery);
  const rows = data ?? [];

  return (
    <>
      <PageHeader
        title="Recent Outcomes"
        description="Settled paper trades"
        sources={["journal", "brain_map.db"]}
      />

      <Panel title={`Settled (${rows.length})`}>
        {isError ? (
          <EmptyState label="Outcomes unavailable" />
        ) : isPending ? (
          <EmptyState label="Loading outcomes…" />
        ) : rows.length === 0 ? (
          <EmptyState label="No settled trades" />
        ) : (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-border">
                    {["Settled", "Symbol", "Strategy", "Resolution", "P&L", "R-multiple", "Ticket"].map(
                      (label, index) => (
                        <th
                          key={label}
                          className={`num px-2 py-2 text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase ${
                            index === 4 || index === 5 ? "text-right" : ""
                          }`}
                        >
                          {label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.ticket} className="border-b border-border/60 last:border-0">
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {formatIstDateTime(row.settled)}
                      </td>
                      <td className="num px-2 py-2 text-xs font-medium">{row.symbol}</td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">{row.strategy}</td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">
                        {row.resolution}
                      </td>
                      <td
                        className={`num px-2 py-2 text-right text-xs font-semibold ${
                          row.pnl_rs >= 0 ? "text-pnl-up" : "text-pnl-down"
                        }`}
                      >
                        {formatRupeesSigned(row.pnl_rs)}
                      </td>
                      <td
                        className={`num px-2 py-2 text-right text-xs ${
                          (row.r_multiple ?? 0) >= 0 ? "text-pnl-up" : "text-pnl-down"
                        }`}
                      >
                        {formatR(row.r_multiple)}
                      </td>
                      <td className="num px-2 py-2 text-xs text-muted-foreground">{row.ticket}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="space-y-3 md:hidden">
              {rows.map((row) => (
                <article key={row.ticket} className="rounded-sm border border-border p-3">
                  <div className="flex items-start justify-between gap-3">
                    <div>
                      <p className="num text-xs font-semibold">{row.symbol}</p>
                      <p className="num text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
                        {row.strategy}
                      </p>
                    </div>
                    <p
                      className={`num text-sm font-semibold ${
                        row.pnl_rs >= 0 ? "text-pnl-up" : "text-pnl-down"
                      }`}
                    >
                      {formatRupeesSigned(row.pnl_rs)}
                    </p>
                  </div>
                  <p className="num mt-2 text-xs text-muted-foreground">{row.resolution}</p>
                  <p className="num mt-1 text-[11px] text-muted-foreground">
                    {formatIstDateTime(row.settled)} · {formatR(row.r_multiple)} · {row.ticket}
                  </p>
                </article>
              ))}
            </div>
          </>
        )}
      </Panel>
    </>
  );
}
