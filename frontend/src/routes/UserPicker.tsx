import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { listUsers } from "../api/client";
import type { User } from "../types";
import { useUserStore } from "../stores/userStore";

export function UserPicker() {
  const navigate = useNavigate();
  const setStudentId = useUserStore((s) => s.setStudentId);
  const studentId = useUserStore((s) => s.studentId);
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (studentId) {
      navigate("/chat", { replace: true });
      return;
    }
    listUsers()
      .then(setUsers)
      .finally(() => setLoading(false));
  }, [navigate, studentId]);

  return (
    <div className="h-screen bg-bg text-text flex items-center justify-center p-6">
      <div className="w-full max-w-xl rounded-card border border-border bg-panel p-6 space-y-4">
        <h1 className="text-2xl font-semibold">Who is using Sokratic?</h1>
        {loading && <p className="text-muted">Loading users…</p>}
        {!loading && (
          <div className="grid grid-cols-1 gap-2">
            {users.map((u) => (
              <button
                key={u.id}
                className="rounded-card border border-border px-4 py-3 text-left hover:border-accent"
                onClick={() => {
                  setStudentId(u.id);
                  navigate("/chat");
                }}
              >
                {u.display_name}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
