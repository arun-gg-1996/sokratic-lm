import { API_BASE } from "./client";
import { useUserStore } from "../stores/userStore";
import { domainPathPrefix } from "../stores/domainStore";

export function wsUrl(threadId: string): string {
  const base = API_BASE.replace(/^http/, "ws");
  const token = useUserStore.getState().authToken || "";
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  // Domain prefix mirrors the HTTP API: anatomy gets /ws/chat/<tid>,
  // physics gets /physics/ws/chat/<tid>. nginx routes the prefix to
  // the right backend and strips it before proxying.
  return `${base}${domainPathPrefix()}/ws/chat/${threadId}${qs}`;
}
