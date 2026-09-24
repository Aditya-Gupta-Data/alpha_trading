import { queryOptions } from "@tanstack/react-query";

import {
  getAuditEvents,
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
