import { createFileRoute, Outlet, redirect } from "@tanstack/react-router";

import { DeskShell } from "@/components/desk/DeskShell";
import { readAccessKey } from "@/hooks/useAccessKey";

export const Route = createFileRoute("/_authenticated")({
  ssr: false,
  beforeLoad: () => {
    if (!readAccessKey()) throw redirect({ to: "/login" });
  },
  component: AuthenticatedLayout,
});

function AuthenticatedLayout() {
  return (
    <DeskShell>
      <Outlet />
    </DeskShell>
  );
}
