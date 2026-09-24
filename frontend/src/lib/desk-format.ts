/** Formatting and data-freshness helpers. IST everywhere, en-IN rupees. */

const IST_TIME_ZONE = "Asia/Kolkata";

const MISSING = "—";

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const inrSigned = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
  signDisplay: "always",
});

export function formatRupees(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return inr.format(value);
}

export function formatRupeesSigned(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return inrSigned.format(value);
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return `${value.toFixed(digits)}%`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return new Intl.NumberFormat("en-IN").format(value);
}

export function formatR(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return MISSING;
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}R`;
}

export function formatIstDateTime(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  return `${new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date)} IST`;
}

export function formatIstTime(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatIstDate(iso: string | null | undefined): string {
  if (!iso || iso === MISSING) return iso === MISSING ? MISSING : MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(date);
}

export function formatIstDayMonth(iso: string | null | undefined): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: IST_TIME_ZONE,
    day: "2-digit",
    month: "short",
  }).format(date);
}

/* ------------------------------------------------------------- freshness */

export type FreshnessState = "FRESH" | "STALE" | "UNAVAILABLE";

const STALE_AFTER_MINUTES = 30;

/** IST minutes-of-day for the given instant. */
function istMinutesOfDay(date: Date): number {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: IST_TIME_ZONE,
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).formatToParts(date);
  const hour = Number(parts.find((p) => p.type === "hour")?.value ?? "0");
  const minute = Number(parts.find((p) => p.type === "minute")?.value ?? "0");
  return hour * 60 + minute;
}

function istWeekday(date: Date): string {
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: IST_TIME_ZONE,
    weekday: "short",
  }).format(date);
}

/** Market hours are 09:15–15:30 IST on weekdays. */
export function isMarketHours(now: Date = new Date()): boolean {
  const day = istWeekday(now);
  if (day === "Sat" || day === "Sun") return false;
  const minutes = istMinutesOfDay(now);
  return minutes >= 9 * 60 + 15 && minutes <= 15 * 60 + 30;
}

export function freshnessState(
  iso: string | null | undefined,
  now: Date = new Date(),
): FreshnessState {
  if (!iso) return "UNAVAILABLE";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "UNAVAILABLE";
  const ageMinutes = (now.getTime() - date.getTime()) / 60_000;
  if (isMarketHours(now) && ageMinutes > STALE_AFTER_MINUTES) return "STALE";
  return "FRESH";
}

export function ageLabel(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return MISSING;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return MISSING;
  const minutes = Math.max(0, Math.round((now.getTime() - date.getTime()) / 60_000));
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export const EM_DASH = MISSING;
