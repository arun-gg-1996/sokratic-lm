import { API_BASE } from "./client";

export function wsUrl(threadId: string): string {
  const base = API_BASE.replace(/^http/, "ws");
  return `${base}/ws/chat/${threadId}`;
}
