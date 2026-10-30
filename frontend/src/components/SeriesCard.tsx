import { Link } from "react-router-dom";
import { BookText, Library } from "lucide-react";
import type { SeriesSummary } from "@/types";
import { Card } from "@/components/ui/card";
import StatusBadge from "./StatusBadge";

export default function SeriesCard({ series }: { series: SeriesSummary }) {
  return (
    <Link to={`/series/${series.id}`} className="group">
      <Card className="overflow-hidden p-0 transition-colors hover:border-primary/50">
        <div className="relative aspect-[2/3] w-full overflow-hidden bg-muted">
          {series.poster_url ? (
            <img
              src={series.poster_url}
              alt={series.title}
              className="size-full object-cover transition-transform duration-300 group-hover:scale-105"
              draggable={false}
            />
          ) : (
            <div className="flex size-full items-center justify-center">
              <BookText className="size-12 text-muted-foreground/40" />
            </div>
          )}
          <div className="absolute left-2 top-2">
            <StatusBadge status={series.status} />
          </div>
        </div>
        <div className="space-y-1 p-3">
          <h3 className="line-clamp-2 text-sm font-medium leading-snug" title={series.title}>
            {series.title}
          </h3>
          <p className="flex items-center gap-1 text-xs text-muted-foreground">
            <Library className="size-3" />
            {series.volume_count} {series.volume_count === 1 ? "volume" : "volumes"} · {series.page_count} {series.page_count === 1 ? "page" : "pages"}
          </p>
        </div>
      </Card>
    </Link>
  );
}
