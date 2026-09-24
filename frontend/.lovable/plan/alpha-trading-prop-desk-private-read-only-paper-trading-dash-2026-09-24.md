# Alpha Trading — Prop Desk (private, read-only paper-trading dashboard)

A locked-down, invite-only dashboard that displays paper-trading data. Nothing in the app can place, change, or exit a trade — only viewing.

## Access

- Email + password sign-in only, no public sign-up. Accounts are created by you (the owner) from the backend user list.
- Password reset by email is supported, with a dedicated "set a new password" page.
- Every page except the sign-in page requires a session; visitors are sent to sign-in.
- Two roles: owner and viewer. Both see everything read-only; only owner sees Recon history and Audit Log.
- Sign-out button in the header, plus automatic sign-out after 12 hours of inactivity.

## Pages

1. **Overview** (first screen after sign-in) — cards for PAPER_10L and PAPER_2L (net equity, realized P&L, drawdown %, locked margin, open locks, available cash; rejections for 2L), an equity curve with drawdown, and a recon banner (green PARITY / red MISMATCH / amber UNKNOWN).
2. **Live Book** — open positions with symbol, strategy, direction, accounts, lots, entered, expiry, max loss, MTM, capture %, ratchet peak/lock. Sortable, filterable by strategy; switches to stacked cards on a phone.
3. **Recent Outcomes** — settled trades with time, symbol, strategy, resolution, P&L (green/red), R-multiple.
4. **Recon** (owner only) — verdict history table.
5. **Audit Log** (owner only) — newest-first event feed with a search box.

Shared on every page: a "PAPER TRADING — NOT REAL MONEY" badge and an "as of <timestamp>" line. Data older than 30 minutes during market hours (09:15–15:30 IST) shows amber STALE; missing data shows grey UNAVAILABLE. Numbers are never invented — missing values render as "—".

## Look and feel

Dark "institutional terminal" default: dense, calm, monospaced numerics, thin dividers. Light mode toggle. Green/red reserved for P&L, amber for warnings only. No imagery, no marketing page.

Money formatted en-IN (₹10,85,000), all times IST.

## Technical notes

- All data flows through a single `src/lib/api.ts` with a `USE_MOCK` flag and one async function per shape (`getTreasury`, `getOpenTrades`, `getRecentOutcomes`, `getLatestRecon`, `getReconHistory`, `getAuditEvents`, `getFreshness`) returning exactly the shapes in the brief. Components hold no hard-coded numbers. Each function is a single swappable unit, later pointed at `${VITE_API_BASE_URL}/api/...`.
- `VITE_API_BASE_URL` left empty as a marked placeholder. No keys, tokens, IPs, or webhook URLs anywhere.
- Lovable Cloud is enabled for auth only. A `profiles` table (own-row read under row-level security) holds display data; roles live in a separate `user_roles` table read through a security-definer check, which is the safe pattern — storing a role on the profile row invites privilege escalation.
- Protected pages sit under an authenticated layout; owner-only pages additionally check the owner role.
- React + TypeScript + Tailwind + shadcn/ui + Recharts. No analytics or trackers; fonts from Google Fonts only.

## Out of scope

Live broker/API wiring, any write or trade action, self-service sign-up.
