import { API_BASE } from "./client";
import { useUserStore } from "../stores/userStore";

export function wsUrl(threadId: string): string {
  const base = API_BASE.replace(/^http/, "ws");
  const token = useUserStore.getState().authToken || "";
  const qs = token ? `?token=${encodeURIComponent(token)}` : "";
  return `${base}/ws/chat/${threadId}${qs}`;
}
