import { EM_DASH, formatRupees, formatRupeesSigned } from "@/lib/desk-format";
import type { AccountTreasury } from "@/lib/api";

/** One row per open margin lock of this account (#182): ticket, symbol, structure, lock and open P&L. */
export function AccountTrades({ account }: { account: AccountTreasury }) {
  const rows = account.lock_breakdown;
  if (!rows) {
    return (
      <p className="mt-3 text-muted-foreground">
        {account.open_locks} open lock(s) — this bridge does not send per-trade rows yet.
      </p>
    );
  }
  if (rows.length === 0) {
    return <p className="mt-3 text-muted-foreground">No open trades.</p>;
  }
  const priced = rows.filter((r) => typeof r.unrealized_pnl === "number");
  const total = priced.reduce((sum, r) => sum + (r.unrealized_pnl ?? 0), 0);
  return (
    <div className="mt-3 overflow-x-auto">
      <table className="w-full min-w-[34rem] text-left">
        <thead>
          <tr className="border-b border-border text-[10px] tracking-[0.08em] text-muted-foreground uppercase">
            <th className="py-1 pr-3 font-normal">Ticket</th>
            <th className="py-1 pr-3 font-normal">Symbol</th>
            <th className="py-1 pr-3 font-normal">Strategy</th>
            <th className="py-1 pr-3 text-right font-normal">Margin locked</th>
            <th className="py-1 text-right font-normal">Unrealized P&amp;L</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const pnl = r.unrealized_pnl;
            return (
              <tr key={r.ref} className="border-b border-border/60 last:border-0">
                <td className="num py-1 pr-3" title={r.ticket_id ?? `journal ref ${r.ref}`}>
                  {r.ticket_id ?? <span className="text-muted-foreground">ref {r.ref}</span>}
                </td>
                <td className="py-1 pr-3">{r.ticker ?? EM_DASH}</td>
                <td className="py-1 pr-3">{r.strategy ?? EM_DASH}</td>
                <td
                  className={`num py-1 pr-3 text-right ${r.concentrated ? "font-semibold text-pnl-down" : ""}`}
                >
                  {formatRupees(r.margin_rs)}
                </td>
                <td
                  className={`num py-1 text-right ${
                    typeof pnl !== "number"
                      ? "text-muted-foreground"
                      : pnl >= 0
                        ? "text-pnl-up"
                        : "text-pnl-down"
                  }`}
                >
                  {typeof pnl === "number" ? formatRupeesSigned(pnl) : "unpriced"}
                </td>
              </tr>
            );
          })}
        </tbody>
        <tfoot>
          <tr className="text-muted-foreground">
            <td className="pt-2 pr-3" colSpan={3}>
              {rows.length} open trade(s)
              {priced.length < rows.length ? ` · ${rows.length - priced.length} unpriced` : ""}
            </td>
            <td className="num pt-2 pr-3 text-right">{formatRupees(account.locked_margin)}</td>
            <td className={`num pt-2 text-right ${total >= 0 ? "text-pnl-up" : "text-pnl-down"}`}>
              {priced.length ? formatRupeesSigned(total) : EM_DASH}
            </td>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}
