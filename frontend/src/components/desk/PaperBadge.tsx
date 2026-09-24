export function PaperBadge({ className = "" }: { className?: string }) {
  return (
    <span
      className={`num inline-flex items-center gap-2 rounded-sm border border-warn/50 bg-warn/10 px-2 py-1 text-[10px] font-semibold tracking-[0.12em] text-warn uppercase ${className}`}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-warn" aria-hidden />
      Paper trading — not real money
    </span>
  );
}
