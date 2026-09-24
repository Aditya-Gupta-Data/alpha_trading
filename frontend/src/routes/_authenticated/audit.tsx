import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useMemo, useState } from "react";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { OwnerOnly } from "@/components/desk/OwnerOnly";
import { auditQuery } from "@/lib/desk-queries";
import { formatIstDateTime } from "@/lib/desk-format";

export const Route = createFileRoute("/_authenticated/audit")({
  head: () => ({
    meta: [
      { title: "Audit Log — Alpha Trading Prop Desk" },
      { name: "description", content: "Newest-first event feed for the automated paper desk." },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Audit Log — Alpha Trading Prop Desk" },
      {
        property: "og:description",
        content: "Newest-first event feed for the automated paper desk.",
      },
    ],
  }),
  component: AuditPage,
});

function AuditPage() {
  const { data, isPending, isError } = useQuery(auditQuery);
  const [search, setSearch] = useState("");

  const rows = useMemo(() => {
    const term = search.trim().toLowerCase();
    const sorted = [...(data ?? [])].sort(
      (a, b) => new Date(b.ts).getTime() - new Date(a.ts).getTime(),
    );
    if (!term) return sorted;
    return sorted.filter((row) =>
      `${row.account} ${row.event_type} ${row.detail}`.toLowerCase().includes(term),
    );
  }, [data, search]);

  return (
    <OwnerOnly>
      <PageHeader
        title="Audit Log"
        description="Newest events first"
        sources={["journal", "brain_map.db"]}
      />

      <Panel
        title={`Events (${rows.length})`}
        right={
          <input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search events…"
            className="num w-48 rounded-sm border border-input bg-background px-2 py-1 text-[11px] outline-none focus:border-ring"
          />
        }
      >
        {isError ? (
          <EmptyState label="Audit log unavailable" />
        ) : isPending ? (
          <EmptyState label="Loading events…" />
        ) : rows.length === 0 ? (
          <EmptyState label="No matching events" />
        ) : (
          <ul className="divide-y divide-border">
            {rows.map((row) => (
              <li key={`${row.ts}-${row.event_type}`} className="py-2.5">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="num text-[11px] text-muted-foreground">
                    {formatIstDateTime(row.ts)}
                  </span>
                  <span className="num text-[10px] tracking-[0.1em] text-primary uppercase">
                    {row.event_type}
                  </span>
                  <span className="num text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
                    {row.account}
                  </span>
                </div>
                <p className="num mt-1 text-xs">{row.detail}</p>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </OwnerOnly>
  );
}
