import { useQuery } from "@tanstack/react-query";

import { StatusPill } from "./StatusPill";
import { freshnessQuery } from "@/lib/desk-queries";
import { ageLabel, freshnessState } from "@/lib/desk-format";
import type { Freshness } from "@/lib/api";

/** Simple page title with one plain-language "last updated" line. */
export function PageHeader({
  title,
  description,
  sources,
}: {
  title: string;
  description: string;
  sources: Array<keyof Freshness>;
}) {
  const { data, isPending } = useQuery(freshnessQuery);
  const primary = sources[0];
  const ts = data && primary ? data[primary] : null;
  const state = data ? freshnessState(ts) : "UNAVAILABLE";

  return (
    <div className="mb-6">
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      <p className="mt-1 text-sm text-muted-foreground">{description}</p>
      <div className="mt-2 flex items-center gap-2 text-xs text-muted-foreground">
        {isPending ? (
          "Checking…"
        ) : state === "FRESH" ? (
          <>Updated {ageLabel(ts)}</>
        ) : state === "STALE" ? (
          <>
            <StatusPill label="Old data" tone="warn" /> Last updated {ageLabel(ts)}
          </>
        ) : (
          <>
            <StatusPill label="No data" tone="neutral" /> Update time not available
          </>
        )}
      </div>
    </div>
  );
}
