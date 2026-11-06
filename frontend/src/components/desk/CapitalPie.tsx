import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip, type TooltipProps } from "recharts";

import { formatPct, formatRupees } from "@/lib/desk-format";
import type { AccountTreasury } from "@/lib/api";

type Slice = {
  key: string;
  name: string;
  value: number;
  pct: number | null;
  kind: "free" | "lock" | "hot";
};

const FILL: Record<Slice["kind"], string> = {
  free: "var(--color-pnl-up)",
  lock: "var(--color-chart-5)",
  hot: "var(--color-pnl-down)",
};

/** Free cash vs every open lock (#182): a lock above the threshold share of equity is drawn red. */
function capitalSlices(account: AccountTreasury): Slice[] {
  const equity = account.equity;
  const locks = account.lock_breakdown ?? [];
  const free = Math.max(0, account.available_cash);
  const slices: Slice[] = [
    {
      key: "free",
      name: "Free cash",
      value: free,
      pct: equity > 0 ? (free / equity) * 100 : null,
      kind: "free",
    },
  ];
  if (locks.length === 0 && account.locked_margin > 0) {
    slices.push({
      key: "locked",
      name: `Locked in ${account.open_locks} trade(s)`,
      value: account.locked_margin,
      pct: equity > 0 ? (account.locked_margin / equity) * 100 : null,
      kind: "lock",
    });
  }
  for (const l of locks) {
    slices.push({
      key: l.ref,
      name: l.label,
      value: l.margin_rs,
      pct: l.pct_of_equity,
      kind: l.concentrated ? "hot" : "lock",
    });
  }
  return slices.filter((s) => s.value > 0);
}

function SliceTooltip({ active, payload }: TooltipProps<number, string>) {
  if (!active || !payload?.length) return null;
  const s = payload[0]?.payload as Slice | undefined;
  if (!s) return null;
  return (
    <div className="rounded border border-border bg-background px-2 py-1 text-xs shadow">
      <p className="font-semibold">{s.name}</p>
      <p className="num">
        {formatRupees(s.value)}
        {s.pct !== null ? ` · ${formatPct(s.pct, 1)} of equity` : ""}
      </p>
    </div>
  );
}

export function CapitalPie({ account }: { account: AccountTreasury }) {
  const slices = capitalSlices(account);
  const limit = account.concentration_threshold_pct ?? 20;
  const hot = (account.lock_breakdown ?? []).filter((l) => l.concentrated);
  const freePct =
    account.equity > 0 ? (Math.max(0, account.available_cash) / account.equity) * 100 : null;
  const lockedPct = account.equity > 0 ? (account.locked_margin / account.equity) * 100 : null;
  return (
    <div className="mt-5 grid items-center gap-4 sm:grid-cols-[9rem_1fr]">
      <div className="relative mx-auto h-36 w-36">
        {slices.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie
                data={slices}
                dataKey="value"
                nameKey="name"
                innerRadius="58%"
                outerRadius="100%"
                startAngle={90}
                endAngle={-270}
                stroke="var(--color-background)"
                strokeWidth={1}
                isAnimationActive={false}
              >
                {slices.map((s) => (
                  <Cell key={s.key} fill={FILL[s.kind]} />
                ))}
              </Pie>
              <Tooltip content={<SliceTooltip />} />
            </PieChart>
          </ResponsiveContainer>
        ) : null}
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="num text-sm font-semibold">{formatPct(freePct, 0)}</span>
          <span className="text-[10px] text-muted-foreground">free</span>
        </div>
      </div>
      <div className="space-y-1 text-xs">
        <p className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: FILL.free }} />
          Free cash{" "}
          <span className="num font-semibold">
            {formatRupees(Math.max(0, account.available_cash))}
          </span>
        </p>
        <p className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: FILL.lock }} />
          Locked margin{" "}
          <span className="num font-semibold">{formatRupees(account.locked_margin)}</span>
          <span className="text-muted-foreground">
            {lockedPct !== null ? `${formatPct(lockedPct, 1)} of equity · ` : ""}
            {account.open_locks} trade(s)
          </span>
        </p>
        {account.available_cash < 0 && (
          <p className="text-pnl-down">
            Locks exceed equity by {formatRupees(-account.available_cash)}.
          </p>
        )}
        {hot.length > 0 ? (
          <div className="mt-2 rounded border border-pnl-down/40 bg-pnl-down/10 p-2 text-pnl-down">
            <p className="font-semibold">
              {hot.length} trade{hot.length > 1 ? "s" : ""} over {formatPct(limit, 0)} of equity
            </p>
            <ul className="mt-1 space-y-0.5">
              {hot.map((l) => (
                <li key={l.ref} className="num">
                  {l.label} — {formatRupees(l.margin_rs)} ({formatPct(l.pct_of_equity, 1)})
                </li>
              ))}
            </ul>
          </div>
        ) : (
          account.lock_breakdown && (
            <p className="text-muted-foreground">
              No single trade over {formatPct(limit, 0)} of equity
              {typeof account.largest_lock_pct === "number" && account.largest_lock_pct > 0
                ? ` (largest ${formatPct(account.largest_lock_pct, 1)})`
                : ""}
              .
            </p>
          )
        )}
      </div>
    </div>
  );
}
