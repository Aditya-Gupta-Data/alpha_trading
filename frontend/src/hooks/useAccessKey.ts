/**
 * The desk's access gate (replaces Supabase auth, 2026-09-24): one shared
 * access key, typed once on the "Enter access key" screen and kept in
 * localStorage. The read-only API bridge checks it on every request
 * (X-Access-Key); a 401 clears it and returns the viewer to the gate.
 * No accounts, no roles, no password fields.
 */
import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useState } from "react";

export const ACCESS_KEY_STORAGE = "desk:access-key";

export function readAccessKey(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(ACCESS_KEY_STORAGE);
    return value && value.trim() ? value.trim() : null;
  } catch {
    return null;
  }
}

export function writeAccessKey(value: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (value && value.trim()) window.localStorage.setItem(ACCESS_KEY_STORAGE, value.trim());
    else window.localStorage.removeItem(ACCESS_KEY_STORAGE);
  } catch {
    /* storage blocked (private mode) — the gate simply asks again next time */
  }
}

export function useAccessKey() {
  const [key, setKey] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setKey(readAccessKey());
    setReady(true);
  }, []);

  const save = useCallback((value: string) => {
    writeAccessKey(value);
    setKey(value.trim() || null);
  }, []);

  const clear = useCallback(() => {
    writeAccessKey(null);
    setKey(null);
  }, []);

  return { key, ready, save, clear };
}

export function useSignOut() {
  const navigate = useNavigate();
  return useCallback(() => {
    writeAccessKey(null);
    void navigate({ to: "/login", replace: true });
  }, [navigate]);
}
