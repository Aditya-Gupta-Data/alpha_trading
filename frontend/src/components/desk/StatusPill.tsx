import type { FreshnessState } from "@/lib/desk-format";

const TONE = {
  ok: "border-pnl-up/40 bg-pnl-up/10 text-pnl-up",
  warn: "border-warn/50 bg-warn/10 text-warn",
  bad: "border-pnl-down/40 bg-pnl-down/10 text-pnl-down",
  neutral: "border-border bg-muted text-muted-foreground",
} as const;

export type PillTone = keyof typeof TONE;

export function StatusPill({
  label,
  tone = "neutral",
  className = "",
}: {
  label: string;
  tone?: PillTone;
  className?: string;
}) {
  return (
    <span
      className={`num inline-flex items-center rounded-sm border px-2 py-0.5 text-[10px] font-semibold tracking-[0.1em] uppercase ${TONE[tone]} ${className}`}
    >
      {label}
    </span>
  );
}

export function freshnessTone(state: FreshnessState): PillTone {
  if (state === "FRESH") return "ok";
  if (state === "STALE") return "warn";
  return "neutral";
}
