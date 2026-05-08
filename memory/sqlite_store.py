"""
SQLite data layer for structured per-student state.

Three tables:

  students             — one row per user
  sessions             — one row per chat thread (lifecycle managed)
  subsection_mastery   — EWMA score per (student, subsection)

Each domain (anatomy, physics, …) gets its own SQLite file at
`data/student_state/sokratic_{retrieval_domain}.sqlite3` so a single
student can have independent progress per domain without any
WHERE-clause filtering.

Public surface (all writes auto-commit; one call = one logical write):

  ensure_student(student_id, *, display_name=None)
  start_session(thread_id, student_id, ...)
  update_session(thread_id, **fields)
  get_session(thread_id) / list_sessions(student_id, ...)
  upsert_subsection_mastery(student_id, subsection_path, fresh_score, ...)
  get_subsection_mastery / list_subsection_mastery
  student_stats(student_id)
  mastery_tree(student_id, topic_index)
  close()
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

REPO = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Per-domain SQLite DB files — physical isolation guarantees zero cross-domain
# contamination, mirroring how mem0 already isolates per domain via its own
# Qdrant collection (cfg.domain.memory_collection). One DB file per domain
# means: same student_id can have independent progress across anatomy /
# physics / future domains; no domain column needed on any table; per-domain
# dump / restore / reset is one file operation.
def default_db_path(domain: str) -> Path:
    """Resolve the canonical SQLite path for a domain.

 `domain` should match `cfg.domain.retrieval_domain` (e.g.
 "openstax_anatomy", "physics"). Falls back to "default" only if a caller
 explicitly opts out by passing domain="default" — production callers
 should always pass an explicit domain.
"""
    return REPO / "data" / "student_state" / f"sokratic_{domain}.sqlite3"

# Mastery tier mapping (+ — score → categorical tier).
# Read at rollup time + when projecting subsection mastery into a session.
TIER_THRESHOLDS = {
    "proficient":   0.75,    # >= 0.75
    "developing":   0.50,    # >= 0.50 and < 0.75
    "needs_review": 0.0,     # >= 0.0 and < 0.50
}
TIER_TO_SCORE = {
    "proficient":   0.85,    # midpoint of [0.75, 1.0)
    "developing":   0.625,   # midpoint of [0.50, 0.75)
    "needs_review": 0.25,    # midpoint of [0.0, 0.50)
    "not_assessed": None,
}

# Color thresholds (UI rendering — duplicated here so backend can serve
# pre-colored payloads without the frontend recomputing).
COLOR_GREEN_MIN = 0.75
COLOR_YELLOW_MIN = 0.50

# Status enum (— no abandoned_mid_session; that's derived).
SESSION_STATUSES = {
    "in_progress",
    "completed",
    "ended_off_domain",
    "ended_by_student",
    "ended_turn_limit",
    "abandoned_no_lock",
}

def utc_now() -> str:
    """ISO-8601 UTC timestamp (seconds resolution; matches sqlite datetime)."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def score_to_tier(score: Optional[float]) -> str:
    """Map an EWMA / aggregated score to a categorical tier ."""
    if score is None:
        return "not_assessed"
    if score >= TIER_THRESHOLDS["proficient"]:
        return "proficient"
    if score >= TIER_THRESHOLDS["developing"]:
        return "developing"
    return "needs_review"

def score_to_color(score: Optional[float]) -> str:
    """Map a score to UI color band ."""
    if score is None:
        return "grey"
    if score >= COLOR_GREEN_MIN:
        return "green"
    if score >= COLOR_YELLOW_MIN:
        return "yellow"
    return "red"

# ─────────────────────────────────────────────────────────────────────────────
# SQLiteStore
# ─────────────────────────────────────────────────────────────────────────────

class SQLiteStore:
    """Thread-safe wrapper around a single SQLite database file.

 Connection strategy: one connection per thread (via threading.local).
 Migrations are applied lazily on first use.
"""

    # Per-DB-path cache so we don't re-run migrations on the same file in
    # the same process. Keyed by absolute path string. A separate DB file
    # (different domain, test fixture, etc.) gets its own migration pass.
    _migrations_applied_paths: set[str] = set()
    _migrations_lock = threading.Lock()

    def __init__(
        self,
        domain: Optional[str] = None,
        *,
        db_path: Optional[Path | str] = None,
    ):
        """Open a per-domain SQLite store.

 Resolution order:
 1. Explicit `db_path` (test isolation, override).
 2. `default_db_path(domain)` if `domain` is given.
 3. `default_db_path(cfg.domain.retrieval_domain)` (production path).
 4. Raise — refuse to open a domain-blind DB.
"""
        if db_path is not None:
            self.db_path = Path(db_path)
        else:
            if domain is None:
                # Resolve from active config — keeps callers terse in
                # production (`SQLiteStore` "just works" against the active
                # domain) without ever risking a shared cross-domain file.
                from config import cfg as _cfg
                domain = _cfg.domain.retrieval_domain
            if not domain:
                raise ValueError(
                    "SQLiteStore requires a non-empty domain (got "
                    f"{domain!r}); pass domain= or db_path= explicitly."
                )
            self.db_path = default_db_path(domain)
        self.domain = domain
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._tls = threading.local()
        self._ensure_migrations()

    # ── Connection management ──────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._tls, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA journal_mode = WAL")  # better concurrent reads
            self._tls.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._tls, "conn", None)
        if c is not None:
            c.close()
            self._tls.conn = None

    def _ensure_migrations(self) -> None:
        """Apply any unrun migrations against this instance's DB file.

 Cached per absolute DB path so repeated SQLiteStore instances on the
 same file in the same process pay no extra cost, while different
 files (different domains, test fixtures) each get their own pass.
"""
        path_key = str(self.db_path.resolve())
        with type(self)._migrations_lock:
            if path_key in type(self)._migrations_applied_paths:
                return
            conn = self._conn()
            try:
                cur = conn.execute("SELECT version FROM schema_version")
                applied: set[int] = {row["version"] for row in cur.fetchall()}
            except sqlite3.OperationalError:
                applied = set()

            for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                # Filename like '001_initial_schema.sql'
                version = int(migration_file.name.split("_", 1)[0])
                if version in applied:
                    continue
                conn.executescript(migration_file.read_text())
                conn.commit()
            type(self)._migrations_applied_paths.add(path_key)

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
        return dict(row) if row else None

    # ── Students ───────────────────────────────────────────────────────────

    def ensure_student(self, student_id: str, *, display_name: Optional[str] = None) -> dict:
        """Insert student row if absent. Returns the row."""
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO students(student_id, created_at, display_name) "
            "VALUES (?, ?, ?)",
            (student_id, utc_now(), display_name),
        )
        conn.commit()
        if display_name is not None:
            conn.execute(
                "UPDATE students SET display_name = ? WHERE student_id = ?",
                (display_name, student_id),
            )
            conn.commit()
        cur = conn.execute("SELECT * FROM students WHERE student_id = ?", (student_id,))
        return self._row_to_dict(cur.fetchone())  # type: ignore[return-value]

    def get_student(self, student_id: str) -> dict | None:
        cur = self._conn().execute("SELECT * FROM students WHERE student_id = ?", (student_id,))
        return self._row_to_dict(cur.fetchone())

    # ── Curriculum lookups (post-migration 003) ─────────────────────────────
    #
    # Path strings entering the sqlite_store API (from runtime locked_topic,
    # from API filter params, from mem0 metadata) get resolved to integer
    # subsection_id here. Outbound rows JOIN back to chapters/sections/
    # subsections to emit a canonical "Chapter N: ... > ... > ..." path
    # string so callers and the frontend keep their existing field names.
    #
    # The prefix-tolerant matching (with or without "Chapter N: " prefix) is
    # captured once here; downstream code no longer worries about it.

    @staticmethod
    def _strip_chapter_prefix(path: str) -> str:
        import re as _re
        return _re.sub(r"^Chapter \d+:\s*", "", path) if isinstance(path, str) else path

    def resolve_subsection_id(self, path: Optional[str]) -> Optional[int]:
        """Map a path string to subsection_id. Returns None on miss.

  Accepts either of these formats (both produced by code in the wild):
      "Chapter 24: Metabolism and Nutrition > Carbohydrate ... > Oxidative ..."
      "Metabolism and Nutrition > Carbohydrate ... > Oxidative ..."
  """
        if not path or not isinstance(path, str):
            return None
        stripped = self._strip_chapter_prefix(path)
        parts = [p.strip() for p in stripped.split(" > ")]
        if len(parts) != 3:
            return None
        ch_title, sec_title, sub_title = parts
        cur = self._conn().execute(
            """
            SELECT sub.subsection_id
            FROM subsections sub
            JOIN sections s ON s.section_id = sub.section_id
            JOIN chapters c ON c.chapter_id = s.chapter_id
            WHERE c.title = ? AND s.title = ? AND sub.title = ?
            """,
            (ch_title, sec_title, sub_title),
        )
        row = cur.fetchone()
        return int(row["subsection_id"]) if row else None

    def subsection_path_for_id(self, subsection_id: Optional[int]) -> Optional[str]:
        """Return canonical "Chapter N: ... > ... > ..." path or None."""
        if subsection_id is None:
            return None
        cur = self._conn().execute(
            """
            SELECT c.chapter_num, c.title AS chapter_title,
                   s.title AS section_title,
                   sub.title AS subsection_title
            FROM subsections sub
            JOIN sections s ON s.section_id = sub.section_id
            JOIN chapters c ON c.chapter_id = s.chapter_id
            WHERE sub.subsection_id = ?
            """,
            (subsection_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return (
            f"Chapter {row['chapter_num']}: {row['chapter_title']} > "
            f"{row['section_title']} > {row['subsection_title']}"
        )

    # ── Sessions ───────────────────────────────────────────────────────────

    def start_session(
        self,
        thread_id: str,
        student_id: str,
        *,
        image_path: Optional[str] = None,
        image_context: Optional[dict] = None,
    ) -> dict:
        """Insert a fresh in_progress session row at rapport_node entry.

  No locked_subsection at start time — that gets set later via update_session
  once the dean confirms what the student wants to study.
  """
        self.ensure_student(student_id)
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO sessions(
                thread_id, student_id, started_at, status,
                image_path, image_context
            ) VALUES (?, ?, ?, 'in_progress', ?, ?)
            """,
            (
                thread_id,
                student_id,
                utc_now(),
                image_path,
                json.dumps(image_context) if image_context is not None else None,
            ),
        )
        conn.commit()
        return self.get_session(thread_id)  # type: ignore[return-value]

    # Whitelist of update-able columns (defense against typos / injection).
    # Path-string columns (`locked_topic_path`, `locked_subsection_path`,
    # `message_log_path`) are gone post-migration 003; callers may still pass
    # those names and update_session resolves them to the FK / drops them.
    _UPDATEABLE_SESSION_COLS = {
        "ended_at",
        "locked_subsection_id",
        "locked_question",
        "locked_answer",
        "full_answer",
        "reach_status",
        "mastery_tier",
        "core_mastery_tier",
        "clinical_mastery_tier",
        "core_score",
        "clinical_score",
        "hint_level_final",
        "turn_count",
        "status",
        "key_takeaways",
        "image_path",
        "image_context",
    }

    def update_session(self, thread_id: str, **fields: Any) -> dict | None:
        """Partial update of a session row.

  Every kwarg passed is written, including explicit None. To skip a column,
  don't pass that kwarg — unambiguous semantics avoid silent drops on typos.

  Backward-compatible aliases (callers don't need to know about the FK):
      locked_subsection_path  → resolves to locked_subsection_id (FK)
      locked_topic_path       → ignored (the FK supersedes the redundant
                                full path; left as kwarg so old callers
                                don't crash)
      message_log_path        → ignored (chats now live in `messages`)
  """
        if not fields:
            return self.get_session(thread_id)

        # Resolve / drop legacy path kwargs.
        if "locked_subsection_path" in fields:
            path = fields.pop("locked_subsection_path")
            fields["locked_subsection_id"] = self.resolve_subsection_id(path)
        fields.pop("locked_topic_path", None)
        fields.pop("message_log_path", None)

        if not fields:
            return self.get_session(thread_id)

        sets: list[str] = []
        vals: list[Any] = []
        for col, value in fields.items():
            if col not in self._UPDATEABLE_SESSION_COLS:
                raise ValueError(f"Unknown session column: {col!r}")
            if col == "status":
                if value is None:
                    raise ValueError("status is NOT NULL — cannot be set to None")
                if value not in SESSION_STATUSES:
                    raise ValueError(
                        f"Invalid status {value!r}; valid: {sorted(SESSION_STATUSES)}"
                    )
            elif value is not None:
                if col in ("key_takeaways", "image_context") and isinstance(value, (dict, list)):
                    value = json.dumps(value)
                elif col == "reach_status" and isinstance(value, bool):
                    value = 1 if value else 0
            sets.append(f"{col} = ?")
            vals.append(value)

        vals.append(thread_id)
        conn = self._conn()
        conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE thread_id = ?", vals)
        conn.commit()
        return self.get_session(thread_id)

    def end_session(
        self,
        thread_id: str,
        *,
        status: str,
        ended_at: Optional[str] = None,
        **other_fields: Any,
    ) -> dict | None:
        """Convenience for the memory_update_node's session-end UPDATE .

 Sets `ended_at` (default: now) + status + any other passed fields
 in one round trip.
"""
        if status not in SESSION_STATUSES:
            raise ValueError(f"Invalid status {status!r}")
        return self.update_session(
            thread_id,
            ended_at=ended_at or utc_now(),
            status=status,
            **other_fields,
        )

    def get_session(self, thread_id: str) -> dict | None:
        """Return the session row joined with curriculum so callers see a
  `locked_subsection_path` / `locked_topic_path` field shaped like the
  pre-migration store. The FK column `locked_subsection_id` is also
  emitted for new code that wants the integer key.
  """
        cur = self._conn().execute(
            """
            SELECT
                sess.*,
                CASE
                  WHEN sub.subsection_id IS NULL THEN NULL
                  ELSE 'Chapter ' || c.chapter_num || ': ' || c.title ||
                       ' > ' || s.title || ' > ' || sub.title
                END AS locked_subsection_path
            FROM sessions sess
            LEFT JOIN subsections sub ON sub.subsection_id = sess.locked_subsection_id
            LEFT JOIN sections    s   ON s.section_id    = sub.section_id
            LEFT JOIN chapters    c   ON c.chapter_id    = s.chapter_id
            WHERE sess.thread_id = ?
            """,
            (thread_id,),
        )
        row = self._row_to_dict(cur.fetchone())
        if row is None:
            return None
        # Backward-compat alias: legacy callers expect locked_topic_path too.
        row["locked_topic_path"] = row.get("locked_subsection_path")
        for col in ("key_takeaways", "image_context"):
            if row.get(col):
                try:
                    row[col] = json.loads(row[col])
                except (TypeError, json.JSONDecodeError):
                    pass
        return row

    def list_sessions(
        self,
        student_id: str,
        *,
        limit: int = 20,
        status: Optional[str | Iterable[str]] = None,
        completed_only: bool = False,
        subsection_path: Optional[str] = None,
    ) -> list[dict]:
        """List sessions newest-first.

 `completed_only=True` filters on `ended_at IS NOT NULL` (downstream pattern: 'completed sessions only').
 `status` can be a single value or iterable of values to OR together.
 `subsection_path` : filter to sessions whose locked_subsection_path
 matches. Used by the My Mastery → Subsection inline session list.
"""
        # JOIN curriculum so the returned rows carry locked_subsection_path
        # for backward compatibility (frontend MasterySessionRow expects it).
        sql = (
            "SELECT sess.*, "
            "  CASE WHEN sub.subsection_id IS NULL THEN NULL "
            "       ELSE 'Chapter ' || c.chapter_num || ': ' || c.title || "
            "            ' > ' || s.title || ' > ' || sub.title "
            "  END AS locked_subsection_path "
            "FROM sessions sess "
            "LEFT JOIN subsections sub ON sub.subsection_id = sess.locked_subsection_id "
            "LEFT JOIN sections    s   ON s.section_id    = sub.section_id "
            "LEFT JOIN chapters    c   ON c.chapter_id    = s.chapter_id "
            "WHERE sess.student_id = ?"
        )
        params: list[Any] = [student_id]

        if completed_only:
            sql += " AND sess.ended_at IS NOT NULL"

        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND sess.status IN ({placeholders})"
            params.extend(statuses)

        if subsection_path is not None and subsection_path:
            # Resolve the path (with or without "Chapter N: " prefix) to its
            # FK and filter by integer id. Cleaner and faster than the prefix
            # tolerance dance the old mem0 schema had to do.
            sub_id = self.resolve_subsection_id(subsection_path)
            if sub_id is None:
                # Unknown subsection — nothing matches; short-circuit.
                return []
            sql += " AND sess.locked_subsection_id = ?"
            params.append(sub_id)

        sql += " ORDER BY sess.started_at DESC LIMIT ?"
        params.append(limit)

        cur = self._conn().execute(sql, params)
        out: list[dict] = []
        for row in cur.fetchall():
            d = dict(row)
            d["locked_topic_path"] = d.get("locked_subsection_path")
            for col in ("key_takeaways", "image_context"):
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except (TypeError, json.JSONDecodeError):
                        pass
            out.append(d)
        return out

    # ── Subsection mastery (EWMA ) ───────────────────────────────────

    def upsert_subsection_mastery(
        self,
        student_id: str,
        subsection_path: str,
        fresh_score: float,
        outcome: str,
        *,
        session_at: Optional[str] = None,
        alpha: float = 0.7,
    ) -> dict:
        """Apply EWMA blend : new = alpha * fresh + (1 - alpha) * prior.

 First touch (no prior row) inserts with new = fresh.

 bumped alpha 0.6 → 0.7 so
 the current session contributes more. Reduces mastery dilution
 from non-reach sessions that we still save (per user decision
 to keep saving tutoring_cap / hints_exhausted for the mem0 +
 SQLite signal value).
"""
        if outcome not in {"reached", "partial", "not_reached"}:
            raise ValueError(f"Invalid outcome {outcome!r}")

        sub_id = self.resolve_subsection_id(subsection_path)
        if sub_id is None:
            raise ValueError(
                f"Unknown subsection — cannot resolve {subsection_path!r} "
                "to subsection_id. Was the curriculum seeded?"
            )

        conn = self._conn()
        prior = self.get_subsection_mastery(student_id, subsection_path)
        ts = session_at or utc_now()
        if prior is None:
            new_score = float(fresh_score)
            attempt = 1
            conn.execute(
                """
                INSERT INTO subsection_mastery(
                    student_id, subsection_id, ewma_score, last_outcome,
                    last_session_at, attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (student_id, sub_id, new_score, outcome, ts, attempt),
            )
        else:
            new_score = alpha * float(fresh_score) + (1.0 - alpha) * float(prior["ewma_score"])
            attempt = int(prior["attempt_count"]) + 1
            conn.execute(
                """
                UPDATE subsection_mastery
                SET ewma_score = ?, last_outcome = ?, last_session_at = ?,
                    attempt_count = ?
                WHERE student_id = ? AND subsection_id = ?
                """,
                (new_score, outcome, ts, attempt, student_id, sub_id),
            )
        conn.commit()
        return self.get_subsection_mastery(student_id, subsection_path)  # type: ignore[return-value]

    def get_subsection_mastery(self, student_id: str, subsection_path: str) -> dict | None:
        sub_id = self.resolve_subsection_id(subsection_path)
        if sub_id is None:
            return None
        cur = self._conn().execute(
            """
            SELECT m.*,
                   'Chapter ' || c.chapter_num || ': ' || c.title ||
                   ' > ' || s.title || ' > ' || sub.title AS subsection_path
            FROM subsection_mastery m
            JOIN subsections sub ON sub.subsection_id = m.subsection_id
            JOIN sections    s   ON s.section_id    = sub.section_id
            JOIN chapters    c   ON c.chapter_id    = s.chapter_id
            WHERE m.student_id = ? AND m.subsection_id = ?
            """,
            (student_id, sub_id),
        )
        return self._row_to_dict(cur.fetchone())

    def list_subsection_mastery(self, student_id: str) -> list[dict]:
        cur = self._conn().execute(
            """
            SELECT m.*,
                   'Chapter ' || c.chapter_num || ': ' || c.title ||
                   ' > ' || s.title || ' > ' || sub.title AS subsection_path
            FROM subsection_mastery m
            JOIN subsections sub ON sub.subsection_id = m.subsection_id
            JOIN sections    s   ON s.section_id    = sub.section_id
            JOIN chapters    c   ON c.chapter_id    = s.chapter_id
            WHERE m.student_id = ?
            ORDER BY m.ewma_score ASC
            """,
            (student_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    # ── Observations (replaces mem0 — narrative misconception/style atoms) ─

    def add_observation(
        self,
        *,
        observation_id: str,
        student_id: str,
        thread_id: Optional[str],
        category: str,
        text: str,
        subsection_path: Optional[str] = None,
        evidence: Optional[str] = None,
        session_at: Optional[str] = None,
        created_at: Optional[str] = None,
        hash_value: Optional[str] = None,
    ) -> bool:
        """Insert one observation row. Returns True on insert, False on
  dedup-skip or error.

  Resolves `subsection_path` (with or without "Chapter N: " prefix) to
  `subsection_id`. NULL is allowed — cross-topic style cues don't need
  to attach to a specific subsection.

  Dedup: if (student_id, hash) collides with an existing row, the
  insert silently no-ops (matches the previous mem0 hash-dedup
  semantics).
  """
        if hash_value is None and text:
            import hashlib
            normalized = " ".join(text.lower().split())
            hash_value = hashlib.md5(normalized.encode("utf-8")).hexdigest()
        created_at = created_at or utc_now()
        sub_id = self.resolve_subsection_id(subsection_path)

        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO observations("
                "observation_id, student_id, thread_id, subsection_id, category, "
                "text, evidence, hash, created_at, session_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation_id, student_id, thread_id, sub_id, category,
                    text, evidence, hash_value, created_at, session_at,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            # Most likely the hash dedup constraint. Retry with NULL thread_id
            # in case a legacy session FK miss is at play.
            conn.rollback()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO observations("
                    "observation_id, student_id, thread_id, subsection_id, category, "
                    "text, evidence, hash, created_at, session_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        observation_id, student_id, None, sub_id, category,
                        text, evidence, hash_value, created_at, session_at,
                    ),
                )
                conn.commit()
                cur = conn.execute(
                    "SELECT 1 FROM observations WHERE observation_id = ?",
                    (observation_id,),
                )
                return cur.fetchone() is not None
            except sqlite3.IntegrityError:
                conn.rollback()
                return False

    def list_observations(
        self,
        student_id: str,
        *,
        category: Optional[str | Iterable[str]] = None,
        subsection_path: Optional[str] = None,
        limit: int = 50,
        order_by_recency: bool = True,
    ) -> list[dict]:
        """List observations for a student. Returns rows with curriculum
  fields denormalized (subsection_path, section_path, chapter_num) so
  callers don't have to JOIN themselves."""
        sql = (
            "SELECT o.*, "
            "  CASE WHEN o.subsection_id IS NULL THEN NULL "
            "       ELSE 'Chapter ' || c.chapter_num || ': ' || c.title || "
            "            ' > ' || s.title || ' > ' || sub.title "
            "  END AS subsection_path, "
            "  CASE WHEN o.subsection_id IS NULL THEN NULL "
            "       ELSE 'Chapter ' || c.chapter_num || ': ' || c.title || "
            "            ' > ' || s.title "
            "  END AS section_path, "
            "  c.chapter_num AS chapter_num "
            "FROM observations o "
            "LEFT JOIN subsections sub ON sub.subsection_id = o.subsection_id "
            "LEFT JOIN sections    s   ON s.section_id    = sub.section_id "
            "LEFT JOIN chapters    c   ON c.chapter_id    = s.chapter_id "
            "WHERE o.student_id = ?"
        )
        params: list[Any] = [student_id]

        if category is not None:
            if isinstance(category, str):
                cats = [category]
            else:
                cats = list(category)
            placeholders = ",".join("?" for _ in cats)
            sql += f" AND o.category IN ({placeholders})"
            params.extend(cats)

        if subsection_path:
            sub_id = self.resolve_subsection_id(subsection_path)
            if sub_id is None:
                return []
            sql += " AND o.subsection_id = ?"
            params.append(sub_id)

        if order_by_recency:
            sql += " ORDER BY o.created_at DESC"
        sql += " LIMIT ?"
        params.append(limit)

        cur = self._conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def delete_observations(self, student_id: str) -> int:
        """Delete every observation for one student (forget-me path).

  Returns the count of rows deleted.
  """
        conn = self._conn()
        cur = conn.execute(
            "DELETE FROM observations WHERE student_id = ?",
            (student_id,),
        )
        conn.commit()
        return cur.rowcount or 0

    # ── Messages (chat persistence) ────────────────────────────────────────

    def record_messages(
        self,
        thread_id: str,
        messages: list[dict],
    ) -> int:
        """Persist the full message list of a session.

  Called once at memory_update_node. Each list element is expected to
  have at least `role` and `content`; optional `metadata` dict is
  serialized as JSON. `seq` is assigned by list position (0-indexed).

  Idempotent at the (thread_id, seq) level — re-running on the same
  thread won't produce duplicates because of the UNIQUE constraint.
  Returns the number of rows actually inserted.
  """
        if not messages:
            return 0
        conn = self._conn()
        inserted = 0
        for seq, m in enumerate(messages):
            role = m.get("role") or "?"
            content = m.get("content") or ""
            metadata = m.get("metadata")
            md_json = json.dumps(metadata) if isinstance(metadata, (dict, list)) else None
            ts = m.get("created_at") or utc_now()
            try:
                conn.execute(
                    "INSERT INTO messages("
                    "thread_id, seq, role, content, created_at, metadata"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (thread_id, seq, role, content, ts, md_json),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                # Already persisted (re-run safety) — skip.
                continue
        conn.commit()
        return inserted

    def list_messages(self, thread_id: str) -> list[dict]:
        """Return all messages for one session, in seq order."""
        cur = self._conn().execute(
            "SELECT * FROM messages WHERE thread_id = ? ORDER BY seq",
            (thread_id,),
        )
        out: list[dict] = []
        for row in cur.fetchall():
            d = dict(row)
            if d.get("metadata"):
                try:
                    d["metadata"] = json.loads(d["metadata"])
                except (TypeError, json.JSONDecodeError):
                    pass
            out.append(d)
        return out

    # ── Aggregated stats (powers returning-student opener ) ─────────

    def student_stats(
        self,
        student_id: str,
        *,
        abandoned_grace_hours: int = 1,
    ) -> dict:
        """Return derived counters for the student.

 Per :
 * total_sessions — every row
 * completed_sessions — ended_at IS NOT NULL AND status='completed'
 * unfinished_count — ended_at IS NULL OR status IN
 ('abandoned_no_lock','ended_off_domain','ended_turn_limit')
 * abandoned_mid_session — derived with 1-hour grace:
 status='in_progress' AND ended_at IS NULL
 AND started_at < (now - grace)
 * by_tier — count grouped by mastery_tier
 (proficient / developing / needs_review / not_assessed / null)
"""
        conn = self._conn()

        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE student_id = ?", (student_id,)
        )
        total = cur.fetchone()["n"]

        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions "
            "WHERE student_id = ? AND ended_at IS NOT NULL AND status='completed'",
            (student_id,),
        )
        completed = cur.fetchone()["n"]

        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions "
            "WHERE student_id = ? AND ("
            "  ended_at IS NULL "
            "  OR status IN ('abandoned_no_lock','ended_off_domain','ended_turn_limit')"
            ")",
            (student_id,),
        )
        unfinished = cur.fetchone()["n"]

        # Abandoned-mid-session — derived (1h grace by default)
        grace_cutoff = (
            datetime.utcnow() - timedelta(hours=abandoned_grace_hours)
        ).replace(microsecond=0).isoformat() + "Z"
        cur = conn.execute(
            "SELECT COUNT(*) AS n FROM sessions "
            "WHERE student_id = ? AND status='in_progress' AND ended_at IS NULL "
            "AND started_at < ?",
            (student_id, grace_cutoff),
        )
        abandoned = cur.fetchone()["n"]

        cur = conn.execute(
            "SELECT mastery_tier, COUNT(*) AS n FROM sessions "
            "WHERE student_id = ? AND ended_at IS NOT NULL "
            "GROUP BY mastery_tier",
            (student_id,),
        )
        by_tier: dict[str, int] = {
            "proficient": 0,
            "developing": 0,
            "needs_review": 0,
            "not_assessed": 0,
        }
        for row in cur.fetchall():
            t = row["mastery_tier"] or "not_assessed"
            by_tier[t] = by_tier.get(t, 0) + row["n"]

        return {
            "total_sessions": total,
            "completed_sessions": completed,
            "unfinished_count": unfinished,
            "abandoned_mid_session": abandoned,
            "by_tier": by_tier,
            "strong_count": by_tier.get("proficient", 0),
            "weak_count": by_tier.get("needs_review", 0),
        }

    # ── Mastery tree rollup ─────────────────────────────────────────

    def mastery_tree(self, student_id: str, topic_index: Optional[list[dict]] = None) -> dict:
        """Roll subsection_mastery up to section / chapter level.

  Curriculum is loaded from the SQL chapters/sections/subsections tables
  — `topic_index` is no longer required (kept as an optional positional
  arg only so old callers that still pass it don't break).

  Returns the same nested shape as before:
      {"chapters": [{
          chapter, chapter_num, score, color, tier, touched, total,
          sections: [{
              section, score, color, tier, touched, total,
              subsections: [{
                  subsection, display_label, path, score, color, tier,
                  outcome, last_session_at, attempt_count
              }]
          }]
      }]}
  """
        # Pull the full curriculum + each row's mastery (if any) in one
        # LEFT JOIN — single query feeds the whole tree.
        cur = self._conn().execute(
            """
            SELECT
                c.chapter_id, c.chapter_num, c.title AS chapter_title,
                s.section_id, s.title AS section_title, s.section_order,
                sub.subsection_id, sub.title AS subsection_title,
                sub.display_label, sub.subsection_order,
                m.ewma_score, m.last_outcome, m.last_session_at,
                m.attempt_count
            FROM chapters c
            JOIN sections s ON s.chapter_id = c.chapter_id
            JOIN subsections sub ON sub.section_id = s.section_id
            LEFT JOIN subsection_mastery m
                ON m.subsection_id = sub.subsection_id AND m.student_id = ?
            ORDER BY c.chapter_num, s.section_order, sub.subsection_order
            """,
            (student_id,),
        )

        chapters: dict[int, dict] = {}
        for row in cur.fetchall():
            ch_id = row["chapter_id"]
            ch_num = row["chapter_num"]
            ch_title = row["chapter_title"]
            sec_title = row["section_title"]
            sub_title = row["subsection_title"]

            ch_node = chapters.setdefault(
                ch_id,
                {"chapter": ch_title, "chapter_num": ch_num, "sections": {}},
            )
            sec_node = ch_node["sections"].setdefault(
                row["section_id"],
                {"section": sec_title, "subsections": []},
            )
            score = row["ewma_score"]
            sec_node["subsections"].append({
                "subsection": sub_title,
                "display_label": row["display_label"] or sub_title,
                "path": f"{ch_title} > {sec_title} > {sub_title}",
                "score": score,
                "color": score_to_color(score),
                "tier": score_to_tier(score),
                "outcome": row["last_outcome"],
                "last_session_at": row["last_session_at"],
                "attempt_count": int(row["attempt_count"] or 0),
            })

        # Roll up sections + chapters (mean of TOUCHED children only)
        out_chapters = []
        for ch_id in sorted(chapters.keys(), key=lambda x: chapters[x].get("chapter_num") or 999):
            ch_node = chapters[ch_id]
            section_rolls = []
            # ch_node["sections"] is keyed by section_id; preserve textbook
            # ordering by sorting by the section's first subsection's
            # subsection_order isn't necessary — section_order was the
            # ORDER BY in the source query, so dict insertion order matches.
            for sec_node in ch_node["sections"].values():
                touched_subs = [s for s in sec_node["subsections"] if s["score"] is not None]
                sec_score = (
                    sum(s["score"] for s in touched_subs) / len(touched_subs)
                    if touched_subs
                    else None
                )
                section_rolls.append(
                    {
                        "section": sec_node["section"],
                        "score": sec_score,
                        "color": score_to_color(sec_score),
                        "tier": score_to_tier(sec_score),
                        "touched": len(touched_subs),
                        "total": len(sec_node["subsections"]),
                        "subsections": sec_node["subsections"],
                    }
                )

            touched_secs = [s for s in section_rolls if s["score"] is not None]
            ch_score = (
                sum(s["score"] for s in touched_secs) / len(touched_secs)
                if touched_secs
                else None
            )
            ch_touched_subs = sum(s["touched"] for s in section_rolls)
            ch_total_subs = sum(s["total"] for s in section_rolls)
            out_chapters.append(
                {
                    "chapter": ch_node["chapter"],
                    "chapter_num": ch_node["chapter_num"],
                    "score": ch_score,
                    "color": score_to_color(ch_score),
                    "tier": score_to_tier(ch_score),
                    "touched": ch_touched_subs,
                    "total": ch_total_subs,
                    "sections": section_rolls,
                }
            )

        return {"chapters": out_chapters}

def normalize_subsection_path(
    path: str,
    chapter_lookup: Optional[dict[int, str]] = None,
) -> str:
    """Convert a runtime path string to the canonical format.

 Accepts both legacy (`Ch20|Section|Subsection`) and canonical
 (`<full chapter title> > Section > Subsection`) input. Returns
 canonical form. Idempotent on already-canonical input.

 For legacy input the chapter shorthand is resolved via
 `chapter_lookup` (build via `load_chapter_title_lookup` once per
 process). Returns the input unchanged if the chapter number can't
 be resolved — caller decides how to handle.
"""
    if " > " in path and "|" not in path:
        return path  # already canonical
    if "|" not in path:
        return path  # unrecognized shape; pass through

    parts = path.split("|", 2)
    if len(parts) != 3:
        return path
    head, section, subsection = parts
    if not (head.startswith("Ch") and head[2:].isdigit()):
        return path
    ch_num = int(head[2:])

    if chapter_lookup is None:
        chapter_lookup = load_chapter_title_lookup()
    full_title = chapter_lookup.get(ch_num)
    if not full_title:
        return path
    return f"{full_title} > {section} > {subsection}"

_CHAPTER_LOOKUP_CACHE: Optional[dict[int, str]] = None

def load_chapter_title_lookup(structure_path: Optional[Path] = None) -> dict[int, str]:
    """Return {chapter_num: full_chapter_title}, cached at module level.

  Source of truth post-migration 003: the SQL `chapters` table. The
  `structure_path` argument is preserved on the signature for tests
  that want to inject a different JSON, but production reads from SQL.
  """
    global _CHAPTER_LOOKUP_CACHE
    if structure_path is None and _CHAPTER_LOOKUP_CACHE is not None:
        return _CHAPTER_LOOKUP_CACHE

    if structure_path is None:
        # SQL path — single SELECT, no flat-file dependency.
        try:
            store = SQLiteStore()
            cur = store._conn().execute(
                "SELECT chapter_num, title FROM chapters ORDER BY chapter_num"
            )
            out = {int(r["chapter_num"]): str(r["title"]) for r in cur.fetchall()}
            _CHAPTER_LOOKUP_CACHE = out
            return out
        except Exception:
            return {}

    # Test fallback: load from a JSON file the caller pointed us at.
    if not structure_path.exists():
        return {}
    try:
        structure = json.loads(structure_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for key in (structure or {}).keys():
        if not key.startswith("Chapter "):
            continue
        try:
            after = key[len("Chapter "):]
            num_str, title = after.split(":", 1)
            out[int(num_str.strip())] = title.strip()
        except (ValueError, IndexError):
            continue
    return out
