import { create } from "zustand";

/**
 * Active domain (anatomy = "ot", physics = "physics").
 *
 * Routes API + WebSocket calls to the right backend. The deployment
 * runs two backend processes on the same VM:
 *
 *   anatomy backend  → bound to /api/* and /ws/* (port 8000)
 *   physics backend  → bound to /api/physics/* and /ws/physics/* (port 8001)
 *
 * Both backends share SOKRATIC_AUTH_USERS + SOKRATIC_AUTH_SECRET so the
 * JWT issued by either is accepted by both. Login + listUsers always
 * route to the default (anatomy) backend; everything session-bound
 * routes to the picked domain.
 *
 * The choice is persisted to localStorage so reload preserves it. New
 * logins keep the prior domain by default.
 */

export type DomainId = "ot" | "physics";

interface DomainState {
  domain: DomainId;
  setDomain: (d: DomainId) => void;
}

const DOMAIN_KEY = "sokratic_domain";

function readDomain(): DomainId {
  if (typeof window === "undefined") return "ot";
  const v = localStorage.getItem(DOMAIN_KEY);
  return v === "physics" ? "physics" : "ot";
}

export const useDomainStore = create<DomainState>((set) => ({
  domain: readDomain(),
  setDomain: (d) => {
    if (typeof window !== "undefined") {
      localStorage.setItem(DOMAIN_KEY, d);
    }
    set({ domain: d });
  },
}));

/**
 * Path-prefix the active domain's backend listens on at the nginx
 * level. Anatomy is the default ("" — no prefix). Physics gets
 * "/physics" so /api/physics/foo routes to the physics backend.
 */
export function domainPathPrefix(): string {
  return useDomainStore.getState().domain === "physics" ? "/physics" : "";
}

export const DOMAIN_LABELS: Record<DomainId, string> = {
  ot: "Anatomy",
  physics: "Physics",
};
