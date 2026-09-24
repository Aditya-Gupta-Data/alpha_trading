import { Link } from "@tanstack/react-router";
import { LogOut, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { PaperBadge } from "./PaperBadge";
import { useSignOut } from "@/hooks/useAccessKey";

const THEME_KEY = "desk:theme-v2";

function useTheme() {
  const [theme, setTheme] = useState<"dark" | "light">("light");

  useEffect(() => {
    const stored = window.localStorage.getItem(THEME_KEY);
    if (stored === "light" || stored === "dark") setTheme(stored);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    window.localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  return { theme, toggle: () => setTheme((t) => (t === "dark" ? "light" : "dark")) };
}

const NAV = [
  { to: "/overview", label: "Overview" },
  { to: "/live-book", label: "Live Book" },
  { to: "/outcomes", label: "Outcomes" },
  { to: "/recon", label: "Recon" },
  { to: "/audit", label: "Audit Log" },
] as const;

export function DeskShell({ children }: { children: React.ReactNode }) {
  const { theme, toggle } = useTheme();
  const signOut = useSignOut();
  const nav = NAV;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-20 border-b border-border bg-sidebar/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3">
          <div className="flex min-w-0 flex-1 items-center gap-3">
            <span className="num text-sm font-semibold tracking-[0.14em] text-primary uppercase">
              Alpha Trading
            </span>
            <span className="hidden text-xs tracking-[0.18em] text-muted-foreground uppercase sm:inline">
              Prop Desk
            </span>
          </div>
          <PaperBadge className="order-3 sm:order-none" />
          <div className="flex items-center gap-2">
            <span className="num hidden text-xs text-muted-foreground md:inline">
              read-only · key holder
            </span>
            <button
              type="button"
              onClick={toggle}
              aria-label="Toggle colour mode"
              className="rounded-sm border border-border p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              {theme === "dark" ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </button>
            <button
              type="button"
              onClick={() => void signOut()}
              className="num inline-flex items-center gap-1.5 rounded-sm border border-border px-2 py-1.5 text-[11px] font-semibold tracking-[0.08em] text-muted-foreground uppercase transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              <LogOut className="h-3.5 w-3.5" /> Lock
            </button>
          </div>
        </div>
        <nav className="mx-auto flex max-w-[1400px] gap-1 overflow-x-auto px-3 pb-2">
          {nav.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="num rounded-sm px-3 py-1.5 text-[11px] font-semibold tracking-[0.1em] text-muted-foreground uppercase whitespace-nowrap transition-colors hover:bg-accent hover:text-accent-foreground"
              activeProps={{ className: "bg-accent text-accent-foreground" }}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-[1400px] px-4 py-5">{children}</main>
      <footer className="mx-auto max-w-[1400px] px-4 pb-8 text-[11px] text-muted-foreground">
        Read-only monitoring window. This dashboard cannot place, modify or exit any position.
      </footer>
    </div>
  );
}
