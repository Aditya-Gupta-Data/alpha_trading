import { useQuery } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";

import { EmptyState, Panel } from "@/components/desk/Panel";
import { PageHeader } from "@/components/desk/PageHeader";
import { brainMapQuery } from "@/lib/desk-queries";

export const Route = createFileRoute("/_authenticated/brain-map")({
  head: () => ({
    meta: [
      { title: "Brain Map — Alpha Trading Prop Desk" },
      {
        name: "description",
        content: "The knowledge graph's causal edges — the system's learning loop.",
      },
      { name: "robots", content: "noindex, nofollow" },
      { property: "og:title", content: "Brain Map — Alpha Trading Prop Desk" },
      { property: "og:description", content: "The knowledge graph's causal edges." },
    ],
  }),
  component: BrainMapPage,
});

/**
 * Architect 2026-10-09: the Brain Map beside the money. The bridge renders
 * src/graph_viz.py's self-contained page live from the mirror's brain_map.db;
 * it is shown in a SANDBOXED iframe (scripts only — no same-origin access, no
 * network: the page carries its own inline JS and data).
 */
function BrainMapPage() {
  const { data, isPending, isError } = useQuery(brainMapQuery);
  const s = data?.stats ?? {};
  return (
    <>
      <PageHeader
        title="Brain Map"
        description="The knowledge graph's causal edges — what the system has learned, and from what"
        sources={["brain_map.db"]}
      />
      <Panel>
        {isError ? (
          <EmptyState label="Brain Map unavailable" />
        ) : isPending || !data ? (
          <EmptyState label="Loading the Brain Map…" />
        ) : (
          <>
            <p className="mb-2 text-xs text-muted-foreground">
              {s.nodes ?? 0} nodes · {s.edges_active ?? 0} active edges ({s.outcome_derived ?? 0}{" "}
              outcome-derived, {s.affinity ?? 0} smart-money affinity, {s.loss_permanent ?? 0}{" "}
              permanent loss lessons) · {s.edges_expired ?? 0} expired (hidden by default) ·{" "}
              {data.source}
            </p>
            <p className="mb-3 text-[11px] text-muted-foreground">
              Steel-blue = outcome-derived causal links (the only class that may move sizing,
              decision #38); gold = smart-money affinity; red core = a loss lesson that never
              decays. Drag nodes, scroll to zoom, search by name. Read-only.
            </p>
            <iframe
              title="Brain Map"
              srcDoc={data.html}
              sandbox="allow-scripts"
              className="h-[80vh] min-h-[600px] w-full rounded border border-border"
            />
          </>
        )}
      </Panel>
    </>
  );
}
