import { API_BASE } from "./client";
import { useUserStore } from "../stores/userStore";
import { domainPathPrefix } from "../stores/domainStore";

export function wsUrl(threadId: string): string {
  const token = useUserStore.getState().authToken || "";
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";

  // Dev: route by port (no nginx).
  // Prod: route by URL prefix (anatomy: /ws/..., physics: /physics/ws/...).
  if (import.meta.env.DEV && !import.meta.env.VITE_API_BASE) {
    const physicsBase = (import.meta.env.VITE_PHYSICS_API_BASE || "http://localhost:8001")
      .replace(/^http/, "ws").replace(/\/$/, "");
    const anatomyBase = (import.meta.env.VITE_ANATOMY_API_BASE || "http://localhost:8000")
      .replace(/^http/, "ws").replace(/\/$/, "");
    const base = domainPathPrefix() === "/physics" ? physicsBase : anatomyBase;
    return `${base}/ws/chat/${threadId}${qs}`;
  }

  const base = API_BASE.replace(/^http/, "ws");
  return `${base}${domainPathPrefix()}/ws/chat/${threadId}${qs}`;
}
