import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { SeriesDetail, SeriesSummary } from "@/types";
import { getSeries, listSeries } from "@/lib/api";

const PROCESSING = (s: { status: string }) => s.status === "processing";

/** Library list. Auto-refetches every 2.5s while any series is processing,
 *  and stops once everything settles. `refresh` invalidates the cache. */
export function useSeriesList() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["series"],
    queryFn: listSeries,
    refetchInterval: (q) =>
      (q.state.data as SeriesSummary[] | undefined)?.some(PROCESSING) ? 2500 : false,
  });
  return {
    series: query.data ?? [],
    loading: query.isLoading,
    error: query.error ? (query.error as Error).message : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["series"] }),
  };
}

/** Single series detail. Auto-refetches every 2s while any of its volumes are
 *  processing or queued. */
export function useSeries(id: string | undefined) {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["series", id],
    queryFn: () => getSeries(id as string),
    enabled: !!id,
    refetchInterval: (q) =>
      (q.state.data as SeriesDetail | undefined)?.volumes.some(
        (v) => v.status === "processing" || v.status === "queued"
      )
        ? 2000
        : false,
  });
  return {
    series: query.data ?? null,
    loading: query.isLoading,
    error: query.error ? (query.error as Error).message : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["series", id] }),
  };
}
