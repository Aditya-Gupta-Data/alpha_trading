import type { ReactNode } from "react";

/** Roles are gone (single shared access key, 2026-09-24): every page is
 *  visible to whoever holds the key. Kept as a pass-through so the pages
 *  that wrapped themselves in it need no edit. */
export function OwnerOnly({ children }: { children: ReactNode }) {
  return <>{children}</>;
}
