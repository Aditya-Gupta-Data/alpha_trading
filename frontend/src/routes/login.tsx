import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";

import { PaperBadge } from "@/components/desk/PaperBadge";
import { useAccessKey } from "@/hooks/useAccessKey";

export const Route = createFileRoute("/login")({
  ssr: false,
  head: () => ({
    meta: [
      { title: "Access key — Alpha Trading Prop Desk" },
      { name: "description", content: "Private access gate for the Alpha Trading paper-trading desk." },
      { name: "robots", content: "noindex, nofollow" },
    ],
  }),
  component: AccessKeyPage,
});

function AccessKeyPage() {
  const navigate = useNavigate();
  const { key, ready, save } = useAccessKey();
  const [value, setValue] = useState("");

  useEffect(() => {
    if (ready && key) void navigate({ to: "/overview", replace: true });
  }, [ready, key, navigate]);

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    save(value);
    void navigate({ to: "/overview", replace: true });
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-6 text-center">
          <p className="num text-sm font-semibold tracking-[0.16em] text-primary uppercase">
            Alpha Trading
          </p>
          <p className="num mt-1 text-[11px] tracking-[0.2em] text-muted-foreground uppercase">
            Prop Desk · Private
          </p>
          <div className="mt-4 flex justify-center">
            <PaperBadge />
          </div>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4 rounded-md border border-border bg-card p-5">
          <div className="space-y-1.5">
            <label htmlFor="access-key" className="num text-[11px] tracking-[0.1em] uppercase">
              Enter access key
            </label>
            <input
              id="access-key"
              type="text"
              autoComplete="off"
              spellCheck={false}
              required
              value={value}
              onChange={(e) => setValue(e.target.value)}
              className="num w-full rounded-sm border border-input bg-background px-3 py-2 text-sm outline-none focus:border-ring"
            />
          </div>
          <button
            type="submit"
            className="num w-full rounded-sm bg-primary px-3 py-2 text-[12px] font-semibold tracking-[0.1em] text-primary-foreground uppercase transition-opacity hover:opacity-90"
          >
            Open the desk
          </button>
        </form>

        <p className="mt-4 text-center text-[11px] text-muted-foreground">
          One shared key, issued by the desk owner. Read-only: nothing here can trade.
        </p>
      </div>
    </div>
  );
}
