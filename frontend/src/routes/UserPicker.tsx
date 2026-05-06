import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listUsers, loginUser } from "../api/client";
import type { User } from "../types";
import { useUserStore } from "../stores/userStore";

export function UserPicker() {
  const navigate = useNavigate();
  const setAuth = useUserStore((s) => s.setAuth);
  const studentId = useUserStore((s) => s.studentId);
  const authToken = useUserStore((s) => s.authToken);
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [username, setUsername] = useState("grader");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (studentId && authToken) {
      navigate("/chat", { replace: true });
      return;
    }
    listUsers()
      .then((loaded) => {
        setUsers(loaded);
        if (loaded.length && !loaded.some((u) => u.id === username)) {
          setUsername(loaded[0].id);
        }
      })
      .catch(() => setUsers([]))
      .finally(() => setLoading(false));
  }, [navigate, studentId, authToken, username]);

  async function submitLogin(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setSubmitting(true);
    try {
      const resp = await loginUser(username, password);
      setAuth(resp.user.id, resp.token);
      navigate("/chat");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="h-screen bg-bg text-text flex items-center justify-center p-6">
      <form
        className="w-full max-w-sm rounded-card border border-border bg-panel p-6 space-y-4"
        onSubmit={submitLogin}
      >
        <h1 className="text-2xl font-semibold">Sign in</h1>
        {loading && <p className="text-muted">Loading users…</p>}
        {!loading && users.length === 0 && (
          <p className="text-sm text-muted">
            No demo users configured. Fill SOKRATIC_AUTH_USERS in .env and restart the backend.
          </p>
        )}
        {!loading && users.length > 0 && (
          <div className="space-y-3">
            <label className="block space-y-1">
              <span className="text-sm text-muted">User</span>
              <select
                className="w-full rounded-card border border-border bg-bg px-3 py-2 text-text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
              >
                {users.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block space-y-1">
              <span className="text-sm text-muted">Password</span>
              <input
                className="w-full rounded-card border border-border bg-bg px-3 py-2 text-text"
                type="password"
                value={password}
                autoComplete="current-password"
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>
            {error && <p className="text-sm text-red-400">{error}</p>}
            <button
              className="w-full rounded-card bg-accent px-4 py-2 font-medium text-white disabled:opacity-60"
              disabled={submitting || !password}
              type="submit"
            >
              {submitting ? "Signing in..." : "Continue"}
            </button>
          </div>
        )}
      </form>
    </div>
  );
}
