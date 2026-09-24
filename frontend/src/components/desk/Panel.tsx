import type { ReactNode } from "react";

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title?: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-border bg-card shadow-sm text-card-foreground ${className}`}
    >
      {(title || right) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-5 py-3">
          {title && (
            <h2 className="num text-[11px] font-semibold tracking-[0.12em] text-muted-foreground uppercase">
              {title}
            </h2>
          )}
          {right}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function EmptyState({ label }: { label: string }) {
  return (
    <p className="num py-8 text-center text-xs tracking-[0.08em] text-muted-foreground uppercase">
      {label}
    </p>
  );
}
