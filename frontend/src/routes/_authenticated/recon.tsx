import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { StatusPill } from "@/components/desk/StatusPill";
import { OwnerOnly } from "@/components/desk/OwnerOnly";
import { reconHistoryQuery } from "@/lib/desk-queries";
import { formatIstDateTime, formatNumber } from "@/lib/desk-format";
import type { ReconVerdict } from "@/lib/api";

export const Route = createFileRoute("/_authenticated/recon")({
  head: () => ({
    meta: [
      { title: "Recon — Alpha Trading Prop Desk" },
      { name: "description", content: "Reconciliation verdict history for the paper desk." },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Recon — Alpha Trading Prop Desk" },
      {
        property: "og:description",
        content: "Reconciliation verdict history for the paper desk.",
      },
    ],
  }),
  component: ReconPage,
});

function verdictTone(verdict: ReconVerdict) {
  if (verdict === "PARITY") return "ok" as const;
  if (verdict === "MISMATCH") return "bad" as const;
  return "warn" as const;
}

function ReconPage() {
  const { data, isPending, isError } = useQuery(reconHistoryQuery);
  const rows = data ?? [];

  return (
    <OwnerOnly>
      <PageHeader
        title="Recon"
        description="Broker vs book reconciliation history"
        sources={["recon", "brain_map.db"]}
      />

      <Panel title={`Runs (${rows.length})`}>
        {isError ? (
          <EmptyState label="Recon history unavailable" />
        ) : isPending ? (
          <EmptyState label="Loading recon history…" />
        ) : rows.length === 0 ? (
          <EmptyState label="No recon runs recorded" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-left">
              <thead>
                <tr className="border-b border-border">
                  {["Timestamp", "Verdict", "Broker positions", "Book rows", "Mismatches"].map(
                    (label, index) => (
                      <th
                        key={label}
                        className={`num px-2 py-2 text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase ${
                          index === 2 || index === 3 ? "text-right" : ""
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
                  <tr key={row.ts} className="border-b border-border/60 last:border-0">
                    <td className="num px-2 py-2 text-xs text-muted-foreground">
                      {formatIstDateTime(row.ts)}
                    </td>
                    <td className="px-2 py-2">
                      <StatusPill label={row.verdict} tone={verdictTone(row.verdict)} />
                    </td>
                    <td className="num px-2 py-2 text-right text-xs">
                      {formatNumber(row.broker_positions)}
                    </td>
                    <td className="num px-2 py-2 text-right text-xs">
                      {formatNumber(row.book_rows)}
                    </td>
                    <td className="num px-2 py-2 text-xs text-muted-foreground">
                      {row.mismatches.length === 0 ? "—" : row.mismatches.join("; ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </OwnerOnly>
  );
}
