"""
memory/sqlite_store.py
──────────────────────
Raw-sqlite3 data layer for the post-paper architecture (L1, L2, L21 in
docs/AUDIT_2026-05-02.md). Owns:

  - students
  - sessions (one row per chat thread, lifecycle managed per L21)
  - subsection_mastery (EWMA score per student × subsection)

mem0 is reduced to misconception + learning_style only (per L1). Anything
structured / countable / aggregatable lives here.

Public entry points
-------------------
SQLiteStore(db_path)                        constructor; runs migrations on first use
  .ensure_student(student_id, *, display_name=None) → student row
  .start_session(thread_id, student_id, *, message_log_path=None,
                 image_path=None, image_context=None) → inserts in_progress row
  .update_session(thread_id, **fields)      partial update; only non-None fields applied
  .get_session(thread_id) → dict | None
  .list_sessions(student_id, *, limit=20, status=None) → list[dict]
  .upsert_subsection_mastery(student_id, subsection_path, fresh_score, outcome,
                             session_at=None, alpha=0.6) → dict
  .get_subsection_mastery(student_id, subsection_path) → dict | None
  .list_subsection_mastery(student_id) → list[dict]
  .student_stats(student_id) → dict (counts: total / completed / unfinished /
                                     strong / developing / needs_review /
                                     not_assessed; abandoned-mid-session derived
                                     with 1-hour grace window per L21)
  .mastery_tree(student_id, topic_index) → nested rollup per L3
  .close()

All write methods are committing — no explicit transactions exposed; one DAO
call = one logical write. The connection is created per-thread (sqlite3 has a
check_same_thread default of True).
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
DEFAULT_DB_PATH = REPO / "data" / "student_state" / "sokratic.sqlite3"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Mastery tier mapping (per L3 + L68 — score → categorical tier).
# Read at L3 rollup time + when projecting subsection mastery into a session.
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

# Color thresholds per L3 (UI rendering — duplicated here so backend can serve
# pre-colored payloads without the frontend recomputing).
COLOR_GREEN_MIN = 0.75
COLOR_YELLOW_MIN = 0.50

# Status enum (per L21 — no abandoned_mid_session; that's derived).
SESSION_STATUSES = {
    "in_progress",
    "completed",
    "ended_off_domain",
    "ended_by_student",
    "ended_turn_limit",
    "abandoned_no_lock",
}


def utc_now() -> str:
    """ISO-8601 UTC timestamp (seconds resolution; matches sqlite datetime())."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def score_to_tier(score: Optional[float]) -> str:
    """Map an EWMA / aggregated score to a categorical tier per L3."""
    if score is None:
        return "not_assessed"
    if score >= TIER_THRESHOLDS["proficient"]:
        return "proficient"
    if score >= TIER_THRESHOLDS["developing"]:
        return "developing"
    return "needs_review"


def score_to_color(score: Optional[float]) -> str:
    """Map a score to UI color band per L3."""
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

    _migrations_applied = False
    _migrations_lock = threading.Lock()

    def __init__(self, db_path: Optional[Path | str] = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
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
        # Lazy + thread-safe; run once per process per db_path.
        with type(self)._migrations_lock:
            if type(self)._migrations_applied:
                # Even if the class flag is set, a brand-new db file may need
                # migrations. Check schema_version to be safe.
                pass
            conn = self._conn()
            applied: set[int] = set()
            try:
                cur = conn.execute("SELECT version FROM schema_version")
                applied = {row["version"] for row in cur.fetchall()}
            except sqlite3.OperationalError:
                applied = set()

            for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                # Filename like '001_initial_schema.sql'
                version = int(migration_file.name.split("_", 1)[0])
                if version in applied:
                    continue
                sql = migration_file.read_text()
                conn.executescript(sql)
                conn.commit()
            type(self)._migrations_applied = True

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

    # ── Sessions ───────────────────────────────────────────────────────────

    def start_session(
        self,
        thread_id: str,
        student_id: str,
        *,
        message_log_path: Optional[str] = None,
        image_path: Optional[str] = None,
        image_context: Optional[dict] = None,
    ) -> dict:
        """Insert a fresh in_progress session row at rapport_node entry per L21."""
        self.ensure_student(student_id)
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO sessions(
                thread_id, student_id, started_at, status,
                message_log_path, image_path, image_context
            ) VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
            """,
            (
                thread_id,
                student_id,
                utc_now(),
                message_log_path,
                image_path,
                json.dumps(image_context) if image_context is not None else None,
            ),
        )
        conn.commit()
        return self.get_session(thread_id)  # type: ignore[return-value]

    # Whitelist of update-able columns (defense against typos / injection).
    _UPDATEABLE_SESSION_COLS = {
        "ended_at",
        "locked_topic_path",
        "locked_subsection_path",
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
        "message_log_path",
        "image_path",
        "image_context",
    }

    def update_session(self, thread_id: str, **fields: Any) -> dict | None:
        """Partial update of a session row. Only non-None fields are written.

        Special handling:
          * `key_takeaways` and `image_context` accept dicts; serialized as JSON.
          * `reach_status` accepts bool; stored as INTEGER 0/1.
          * `status` validated against the L21 enum.
        """
        if not fields:
            return self.get_session(thread_id)

        sets: list[str] = []
        vals: list[Any] = []
        for col, value in fields.items():
            if col not in self._UPDATEABLE_SESSION_COLS:
                raise ValueError(f"Unknown session column: {col!r}")
            if value is None:
                # Explicit None means "set to NULL" — but treat skip-if-None as
                # the default for partial updates. Caller can set status=None
                # only deliberately and it's harmless (status has NOT NULL +
                # default).
                continue
            if col == "status" and value not in SESSION_STATUSES:
                raise ValueError(
                    f"Invalid status {value!r}; valid: {sorted(SESSION_STATUSES)}"
                )
            if col == "key_takeaways" and isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif col == "image_context" and isinstance(value, (dict, list)):
                value = json.dumps(value)
            elif col == "reach_status" and isinstance(value, bool):
                value = 1 if value else 0
            sets.append(f"{col} = ?")
            vals.append(value)

        if not sets:
            return self.get_session(thread_id)

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
        """Convenience for the memory_update_node's session-end UPDATE per L21.

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
        cur = self._conn().execute("SELECT * FROM sessions WHERE thread_id = ?", (thread_id,))
        row = self._row_to_dict(cur.fetchone())
        if row is None:
            return None
        # Rehydrate JSON-encoded fields for caller convenience.
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
    ) -> list[dict]:
        """List sessions newest-first.

        `completed_only=True` filters on `ended_at IS NOT NULL` (per L21
        downstream pattern: 'completed sessions only').
        `status` can be a single value or iterable of values to OR together.
        """
        sql = "SELECT * FROM sessions WHERE student_id = ?"
        params: list[Any] = [student_id]

        if completed_only:
            sql += " AND ended_at IS NOT NULL"

        if status is not None:
            if isinstance(status, str):
                statuses = [status]
            else:
                statuses = list(status)
            placeholders = ",".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)

        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        cur = self._conn().execute(sql, params)
        out: list[dict] = []
        for row in cur.fetchall():
            d = dict(row)
            for col in ("key_takeaways", "image_context"):
                if d.get(col):
                    try:
                        d[col] = json.loads(d[col])
                    except (TypeError, json.JSONDecodeError):
                        pass
            out.append(d)
        return out

    # ── Subsection mastery (EWMA per L3) ───────────────────────────────────

    def upsert_subsection_mastery(
        self,
        student_id: str,
        subsection_path: str,
        fresh_score: float,
        outcome: str,
        *,
        session_at: Optional[str] = None,
        alpha: float = 0.6,
    ) -> dict:
        """Apply EWMA blend per L3: new = alpha * fresh + (1 - alpha) * prior.

        First touch (no prior row) inserts with new = fresh.
        """
        if outcome not in {"reached", "partial", "not_reached"}:
            raise ValueError(f"Invalid outcome {outcome!r}")

        conn = self._conn()
        prior = self.get_subsection_mastery(student_id, subsection_path)
        ts = session_at or utc_now()
        if prior is None:
            new_score = float(fresh_score)
            attempt = 1
            conn.execute(
                """
                INSERT INTO subsection_mastery(
                    student_id, subsection_path, ewma_score, last_outcome,
                    last_session_at, attempt_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (student_id, subsection_path, new_score, outcome, ts, attempt),
            )
        else:
            new_score = alpha * float(fresh_score) + (1.0 - alpha) * float(prior["ewma_score"])
            attempt = int(prior["attempt_count"]) + 1
            conn.execute(
                """
                UPDATE subsection_mastery
                SET ewma_score = ?, last_outcome = ?, last_session_at = ?,
                    attempt_count = ?
                WHERE student_id = ? AND subsection_path = ?
                """,
                (new_score, outcome, ts, attempt, student_id, subsection_path),
            )
        conn.commit()
        return self.get_subsection_mastery(student_id, subsection_path)  # type: ignore[return-value]

    def get_subsection_mastery(self, student_id: str, subsection_path: str) -> dict | None:
        cur = self._conn().execute(
            "SELECT * FROM subsection_mastery WHERE student_id = ? AND subsection_path = ?",
            (student_id, subsection_path),
        )
        return self._row_to_dict(cur.fetchone())

    def list_subsection_mastery(self, student_id: str) -> list[dict]:
        cur = self._conn().execute(
            "SELECT * FROM subsection_mastery WHERE student_id = ? ORDER BY ewma_score ASC",
            (student_id,),
        )
        return [dict(row) for row in cur.fetchall()]

    # ── Aggregated stats (powers returning-student opener per L17) ─────────

    def student_stats(
        self,
        student_id: str,
        *,
        abandoned_grace_hours: int = 1,
    ) -> dict:
        """Return derived counters for the student.

        Per L21:
          * total_sessions      — every row
          * completed_sessions  — ended_at IS NOT NULL AND status='completed'
          * unfinished_count    — ended_at IS NULL OR status IN
                                   ('abandoned_no_lock','ended_off_domain','ended_turn_limit')
          * abandoned_mid_session — derived with 1-hour grace:
              status='in_progress' AND ended_at IS NULL
              AND started_at < (now - grace)
          * by_tier             — count grouped by mastery_tier
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

        # Abandoned-mid-session — derived per L21 (1h grace by default)
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

    # ── Mastery tree rollup per L3 ─────────────────────────────────────────

    def mastery_tree(self, student_id: str, topic_index: list[dict]) -> dict:
        """Roll subsection_mastery up to section / chapter level per L3.

        `topic_index` is the list loaded from data/topic_index.json. Each entry
        is expected to have at least:
          * chapter / chapter_title
          * section / section_title
          * subsection / subsection_title
          * (optional) display_label, chapter_num

        Returns a 3-level nested dict:
          {
            "chapters": [
              {
                "chapter":      str,
                "chapter_num":  int,
                "score":        float | None,
                "color":        "green"/"yellow"/"red"/"grey",
                "tier":         "proficient"/.../"not_assessed",
                "touched":      int,    # subsection count with mastery row
                "total":        int,    # subsection count in topic_index
                "sections": [
                  { "section": str, "score": ..., "color": ..., "tier": ...,
                    "touched": int, "total": int,
                    "subsections": [
                      {"subsection": str, "display_label": str,
                       "score": float|None, "color": ..., "tier": ...,
                       "outcome": str|None, "last_session_at": str|None}
                    ]
                  }, ...
                ]
              }, ...
            ]
          }
        """
        # Index subsection mastery by canonical path
        masteries = {
            row["subsection_path"]: row
            for row in self.list_subsection_mastery(student_id)
        }

        # Group topic_index by chapter -> section -> subsection
        chapters: dict[str, dict] = {}
        for entry in topic_index:
            ch = entry.get("chapter") or entry.get("chapter_title") or ""
            sec = entry.get("section") or entry.get("section_title") or ""
            sub = entry.get("subsection") or entry.get("subsection_title") or ""
            if not (ch and sec and sub):
                continue
            ch_num = entry.get("chapter_num")

            ch_node = chapters.setdefault(
                ch,
                {"chapter": ch, "chapter_num": ch_num, "sections": {}},
            )
            sec_node = ch_node["sections"].setdefault(sec, {"section": sec, "subsections": []})
            path = f"{ch} > {sec} > {sub}"
            mastery_row = masteries.get(path)
            score = mastery_row["ewma_score"] if mastery_row else None
            sec_node["subsections"].append(
                {
                    "subsection": sub,
                    "display_label": entry.get("display_label") or sub,
                    "path": path,
                    "score": score,
                    "color": score_to_color(score),
                    "tier": score_to_tier(score),
                    "outcome": mastery_row["last_outcome"] if mastery_row else None,
                    "last_session_at": mastery_row["last_session_at"] if mastery_row else None,
                    "attempt_count": mastery_row["attempt_count"] if mastery_row else 0,
                }
            )

        # Roll up sections + chapters per L3 (mean of TOUCHED children only)
        out_chapters = []
        for ch_name in sorted(chapters.keys(), key=lambda x: chapters[x].get("chapter_num") or 999):
            ch_node = chapters[ch_name]
            section_rolls = []
            for sec_name in sorted(ch_node["sections"].keys()):
                sec_node = ch_node["sections"][sec_name]
                touched_subs = [s for s in sec_node["subsections"] if s["score"] is not None]
                sec_score = (
                    sum(s["score"] for s in touched_subs) / len(touched_subs)
                    if touched_subs
                    else None
                )
                section_rolls.append(
                    {
                        "section": sec_name,
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
