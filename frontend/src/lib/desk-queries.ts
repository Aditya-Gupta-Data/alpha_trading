import { queryOptions } from "@tanstack/react-query";

import {
  getAuditEvents,
  getBrainMap,
  getEquityHistory,
  getFreshness,
  getLatestRecon,
  getOpenTrades,
  getRecentOutcomes,
  getReconHistory,
  getTreasury,
} from "./api";

const REFETCH_MS = 60_000;

export const treasuryQuery = queryOptions({
  queryKey: ["treasury"],
  queryFn: getTreasury,
  refetchInterval: REFETCH_MS,
});

export const openTradesQuery = queryOptions({
  queryKey: ["open-trades"],
  queryFn: getOpenTrades,
  refetchInterval: REFETCH_MS,
});

export const outcomesQuery = queryOptions({
  queryKey: ["recent-outcomes"],
  queryFn: getRecentOutcomes,
  refetchInterval: REFETCH_MS,
});

export const latestReconQuery = queryOptions({
  queryKey: ["recon", "latest"],
  queryFn: getLatestRecon,
  refetchInterval: REFETCH_MS,
});

export const reconHistoryQuery = queryOptions({
  queryKey: ["recon", "history"],
  queryFn: getReconHistory,
  refetchInterval: REFETCH_MS,
});

export const auditQuery = queryOptions({
  queryKey: ["audit"],
  queryFn: getAuditEvents,
  refetchInterval: REFETCH_MS,
});

export const freshnessQuery = queryOptions({
  queryKey: ["freshness"],
  queryFn: getFreshness,
  refetchInterval: REFETCH_MS,
});

export const equityHistoryQuery = queryOptions({
  queryKey: ["equity-history"],
  queryFn: getEquityHistory,
  refetchInterval: REFETCH_MS,
});

// The map re-lays itself out whenever its HTML changes, so it is re-read only
// on the mirror's own cadence (15 min), never every minute.
export const brainMapQuery = queryOptions({
  queryKey: ["brain-map"],
  queryFn: getBrainMap,
  refetchInterval: 15 * 60_000,
  staleTime: 15 * 60_000,
});
