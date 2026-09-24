import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";

import { useAccessKey } from "@/hooks/useAccessKey";

export const Route = createFileRoute("/")({
  ssr: false,
  head: () => ({
    meta: [
      { title: "Alpha Trading — Prop Desk" },
      {
        name: "description",
        content: "Private read-only monitoring desk for an automated paper-trading system on Indian markets.",
      },
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
  component: Landing,
});

function Landing() {
  const navigate = useNavigate();
  const { key, ready } = useAccessKey();

  useEffect(() => {
    if (!ready) return;
    void navigate({ to: key ? "/overview" : "/login", replace: true });
  }, [ready, key, navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-background">
      <p className="num text-[11px] tracking-[0.2em] text-muted-foreground uppercase">
        Alpha Trading · Prop Desk
      </p>
    </div>
  );
}
