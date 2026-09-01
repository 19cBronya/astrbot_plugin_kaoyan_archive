from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .attachments import CapturedAttachment
from .utils import canonical_json, utc_timestamp


SCHEMA_VERSION = 2


class ArchiveStore:
    """SQLite repository. Raw events are append-only; derived archive rows are mutable."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()

    async def initialize(self) -> None:
        self._initialize_sync()

    async def close(self) -> None:
        return None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_uuid TEXT NOT NULL UNIQUE,
                    umo TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    platform_message_id TEXT NOT NULL DEFAULT '',
                    parent_event_id INTEGER REFERENCES events(id),
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    body_text TEXT NOT NULL DEFAULT '',
                    components_json TEXT NOT NULL DEFAULT '[]',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    is_command INTEGER NOT NULL DEFAULT 0 CHECK (is_command IN (0, 1)),
                    is_boundary INTEGER NOT NULL DEFAULT 0 CHECK (is_boundary IN (0, 1)),
                    boundary_rule TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    inserted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_umo_id ON events(umo, id);
                CREATE INDEX IF NOT EXISTS idx_events_parent ON events(parent_event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS uq_events_platform_message
                    ON events(umo, direction, platform_message_id)
                    WHERE platform_message_id <> '';

                CREATE TRIGGER IF NOT EXISTS events_no_update
                BEFORE UPDATE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS events_no_delete
                BEFORE DELETE ON events
                BEGIN
                    SELECT RAISE(ABORT, 'events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS attachments (
                    sha256 TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mime_type TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_attachments (
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    sha256 TEXT NOT NULL REFERENCES attachments(sha256),
                    original_name TEXT NOT NULL,
                    component_type TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(event_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS questions (
                    uuid TEXT PRIMARY KEY,
                    umo TEXT NOT NULL,
                    public_id TEXT UNIQUE,
                    subject TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    knowledge_points_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL,
                    start_event_id INTEGER,
                    boundary_event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
                    event_count INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    analysis_warning TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    archived_at REAL,
                    deleted_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_questions_umo_boundary
                    ON questions(umo, boundary_event_id);
                CREATE INDEX IF NOT EXISTS idx_questions_public_id ON questions(public_id);

                CREATE TABLE IF NOT EXISTS question_events (
                    question_uuid TEXT NOT NULL REFERENCES questions(uuid),
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    relation TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(question_uuid, event_id, relation)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_event_primary_question
                    ON question_events(event_id)
                    WHERE relation = 'primary';

                CREATE TABLE IF NOT EXISTS subject_counters (
                    subject TEXT PRIMARY KEY,
                    next_value INTEGER NOT NULL CHECK (next_value > 0)
                );

                CREATE TABLE IF NOT EXISTS archive_jobs (
                    question_uuid TEXT PRIMARY KEY REFERENCES questions(uuid),
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TRIGGER IF NOT EXISTS audit_no_update
                BEFORE UPDATE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete
                BEFORE DELETE ON audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'audit log is append-only');
                END;
                """
            )
            question_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(questions)").fetchall()
            }
            if "knowledge_points_json" not in question_columns:
                db.execute(
                    "ALTER TABLE questions ADD COLUMN knowledge_points_json "
                    "TEXT NOT NULL DEFAULT '[]'"
                )
            db.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(?, ?)",
                (SCHEMA_VERSION, utc_timestamp()),
            )
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass

    async def add_event(self, **values: Any) -> int:
        return self._add_event_sync(values)

    def _add_event_sync(self, values: dict[str, Any]) -> int:
        platform_message_id = str(values.get("platform_message_id") or "")
        with self._lock, self._connect() as db:
            if platform_message_id:
                existing = db.execute(
                    """
                    SELECT id FROM events
                    WHERE umo=? AND direction=? AND platform_message_id=?
                    """,
                    (values["umo"], values["direction"], platform_message_id),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            cursor = db.execute(
                """
                INSERT INTO events(
                    event_uuid, umo, direction, platform_message_id, parent_event_id,
                    sender_id, sender_name, kind, text, body_text, components_json,
                    raw_json, is_command, is_boundary, boundary_rule, created_at,
                    provider_id, model_id, prompt_version, inserted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    values["umo"],
                    values["direction"],
                    platform_message_id,
                    values.get("parent_event_id"),
                    values.get("sender_id", ""),
                    values.get("sender_name", ""),
                    values.get("kind", "question"),
                    values.get("text", ""),
                    values.get("body_text", ""),
                    canonical_json(values.get("components", [])),
                    canonical_json(values.get("raw", {})),
                    int(bool(values.get("is_command"))),
                    int(bool(values.get("is_boundary"))),
                    values.get("boundary_rule", ""),
                    float(values.get("created_at") or utc_timestamp()),
                    values.get("provider_id", ""),
                    values.get("model_id", ""),
                    values.get("prompt_version", ""),
                    utc_timestamp(),
                ),
            )
            return int(cursor.lastrowid)

    async def mark_boundary(self, event_id: int, rule: str) -> int:
        """Append a boundary marker event; raw events themselves remain immutable."""
        return self._mark_boundary_sync(event_id, rule)

    def _mark_boundary_sync(self, event_id: int, rule: str) -> int:
        with self._lock, self._connect() as db:
            source = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not source:
                raise ValueError("event not found")
            if source["is_boundary"]:
                return int(source["id"])
            cursor = db.execute(
                """
                INSERT INTO events(
                    event_uuid, umo, direction, platform_message_id, parent_event_id,
                    sender_id, sender_name, kind, text, body_text, components_json,
                    raw_json, is_command, is_boundary, boundary_rule, created_at,
                    provider_id, model_id, prompt_version, inserted_at
                ) VALUES (?, ?, 'control', '', ?, ?, ?, 'boundary', ?, '', '[]', '{}', 1, 1, ?, ?, '', '', '', ?)
                """,
                (
                    uuid.uuid4().hex,
                    source["umo"],
                    event_id,
                    source["sender_id"],
                    source["sender_name"],
                    source["text"],
                    rule,
                    utc_timestamp(),
                    utc_timestamp(),
                ),
            )
            boundary_id = int(cursor.lastrowid)
            db.execute(
                "INSERT INTO audit_log(action,target_type,target_id,detail_json,created_at) VALUES('mark_boundary','event',?,?,?)",
                (str(event_id), canonical_json({"boundary_event_id": boundary_id}), utc_timestamp()),
            )
            return boundary_id

    async def link_attachment(
        self,
        event_id: int,
        attachment: CapturedAttachment,
    ) -> None:
        self._link_attachment_sync(event_id, attachment)

    def _link_attachment_sync(
        self,
        event_id: int,
        attachment: CapturedAttachment,
    ) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO attachments(sha256,size,mime_type,stored_path,created_at)
                VALUES(?,?,?,?,?)
                """,
                (
                    attachment.sha256,
                    attachment.size,
                    attachment.mime_type,
                    attachment.stored_path,
                    utc_timestamp(),
                ),
            )
            ordinal = db.execute(
                "SELECT COALESCE(MAX(ordinal), -1) + 1 AS value FROM event_attachments WHERE event_id=?",
                (event_id,),
            ).fetchone()["value"]
            db.execute(
                """
                INSERT OR IGNORE INTO event_attachments(
                    event_id,sha256,original_name,component_type,ordinal
                ) VALUES(?,?,?,?,?)
                """,
                (
                    event_id,
                    attachment.sha256,
                    attachment.original_name,
                    attachment.component_type,
                    int(ordinal),
                ),
            )

    async def create_question_interval(
        self,
        *,
        umo: str,
        boundary_event_id: int,
    ) -> dict[str, Any] | None:
        return self._create_question_interval_sync(umo, boundary_event_id)

    def _create_question_interval_sync(
        self,
        umo: str,
        boundary_event_id: int,
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM questions WHERE boundary_event_id=?",
                (boundary_event_id,),
            ).fetchone()
            if existing:
                db.commit()
                return dict(existing)
            boundary = db.execute(
                "SELECT * FROM events WHERE id=? AND umo=?",
                (boundary_event_id, umo),
            ).fetchone()
            if not boundary:
                db.rollback()
                return None
            previous = db.execute(
                """
                SELECT MAX(boundary_event_id) AS boundary_id
                FROM questions WHERE umo=? AND boundary_event_id < ?
                """,
                (umo, boundary_event_id),
            ).fetchone()["boundary_id"]
            start_id = int(previous or 0)
            rows = db.execute(
                """
                SELECT e.*,
                       EXISTS(SELECT 1 FROM event_attachments ea WHERE ea.event_id=e.id) AS has_attachment
                FROM events e
                WHERE e.umo=? AND e.id>? AND e.id<=?
                ORDER BY e.id
                """,
                (umo, start_id, boundary_event_id),
            ).fetchall()
            effective = [
                row
                for row in rows
                if not row["is_command"]
                and (row["body_text"].strip() or row["has_attachment"])
                and not (row["direction"] == "assistant" and row["is_boundary"])
            ]
            question_uuid = str(uuid.uuid4())
            status = "FINALIZING" if effective else "EMPTY"
            now = utc_timestamp()
            db.execute(
                """
                INSERT INTO questions(
                    uuid,umo,status,start_event_id,boundary_event_id,event_count,
                    created_at,archived_at
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    question_uuid,
                    umo,
                    status,
                    start_id or None,
                    boundary_event_id,
                    len(effective),
                    now,
                    now if status == "EMPTY" else None,
                ),
            )
            ordinal = 0
            effective_ids = {int(row["id"]) for row in effective}
            for row in effective:
                relation = "primary" if row["direction"] == "user" else "answer"
                db.execute(
                    "INSERT INTO question_events(question_uuid,event_id,relation,ordinal) VALUES(?,?,?,?)",
                    (question_uuid, row["id"], relation, ordinal),
                )
                ordinal += 1
            for row in rows:
                row_id = int(row["id"])
                if row_id == boundary_event_id or row_id in effective_ids:
                    continue
                db.execute(
                    "INSERT INTO question_events(question_uuid,event_id,relation,ordinal) VALUES(?,?, 'excluded', ?)",
                    (question_uuid, row_id, ordinal),
                )
                ordinal += 1
            db.execute(
                "INSERT OR IGNORE INTO question_events(question_uuid,event_id,relation,ordinal) VALUES(?,?, 'boundary', ?)",
                (question_uuid, boundary_event_id, ordinal),
            )
            if status == "FINALIZING":
                db.execute(
                    """
                    INSERT INTO archive_jobs(question_uuid,status,created_at,updated_at)
                    VALUES(?, 'PENDING', ?, ?)
                    """,
                    (question_uuid, now, now),
                )
            self._audit(
                db,
                "create_interval",
                "question",
                question_uuid,
                {"start_event_id": start_id, "boundary_event_id": boundary_event_id},
            )
            db.commit()
            return dict(
                db.execute("SELECT * FROM questions WHERE uuid=?", (question_uuid,)).fetchone()
            )

    async def question_source(self, question_uuid: str) -> dict[str, Any] | None:
        return self._question_source_sync(question_uuid)

    def _question_source_sync(self, question_uuid: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            question = db.execute(
                "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            if not question:
                return None
            events = db.execute(
                """
                SELECT e.*, qe.relation, qe.ordinal,
                       COALESCE((
                           SELECT json_group_array(json_object(
                               'sha256', ea.sha256,
                               'name', ea.original_name,
                               'type', ea.component_type,
                               'size', a.size,
                               'mime_type', a.mime_type,
                               'stored_path', a.stored_path
                           ))
                           FROM event_attachments ea
                           JOIN attachments a ON a.sha256=ea.sha256
                           WHERE ea.event_id=e.id
                       ), '[]') AS attachments_json
                FROM question_events qe
                JOIN events e ON e.id=qe.event_id
                WHERE qe.question_uuid=? AND qe.relation IN ('primary','answer')
                ORDER BY qe.ordinal
                """,
                (question_uuid,),
            ).fetchall()
            result = dict(question)
            result["events"] = [self._event_dict(row) for row in events]
            return result

    async def claim_job(self, question_uuid: str) -> bool:
        return self._claim_job_sync(question_uuid)

    def _claim_job_sync(self, question_uuid: str) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                UPDATE archive_jobs
                SET status='RUNNING', attempts=attempts+1, error='', updated_at=?
                WHERE question_uuid=? AND status IN ('PENDING','FAILED')
                """,
                (utc_timestamp(), question_uuid),
            )
            return cursor.rowcount == 1

    async def complete_question(
        self,
        *,
        question_uuid: str,
        subject: str,
        title: str,
        summary: str,
        knowledge_points: list[str] | None = None,
        provider_id: str,
        model_id: str,
        prompt_version: str,
        warning: str = "",
    ) -> dict[str, Any]:
        return self._complete_question_sync(
            question_uuid,
            subject,
            title,
            summary,
            knowledge_points or [],
            provider_id,
            model_id,
            prompt_version,
            warning,
        )

    def _complete_question_sync(
        self,
        question_uuid: str,
        subject: str,
        title: str,
        summary: str,
        knowledge_points: list[str],
        provider_id: str,
        model_id: str,
        prompt_version: str,
        warning: str,
    ) -> dict[str, Any]:
        subject = self._safe_subject(subject)
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            question = db.execute(
                "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            if not question:
                db.rollback()
                raise ValueError("question not found")
            public_id = question["public_id"]
            if not public_id:
                counter = db.execute(
                    "SELECT next_value FROM subject_counters WHERE subject=?",
                    (subject,),
                ).fetchone()
                value = int(counter["next_value"]) if counter else 1
                if counter:
                    db.execute(
                        "UPDATE subject_counters SET next_value=? WHERE subject=?",
                        (value + 1, subject),
                    )
                else:
                    db.execute(
                        "INSERT INTO subject_counters(subject,next_value) VALUES(?,?)",
                        (subject, value + 1),
                    )
                public_id = f"{subject}{value:04d}"
            now = utc_timestamp()
            db.execute(
                """
                UPDATE questions
                SET public_id=?, subject=?, title=?, summary=?,
                    knowledge_points_json=?, status='ARCHIVED',
                    provider_id=?, model_id=?, prompt_version=?, analysis_warning=?,
                    error='', archived_at=?
                WHERE uuid=?
                """,
                (
                    public_id,
                    subject,
                    title.strip()[:200],
                    summary.strip(),
                    canonical_json(
                        [str(item).strip()[:100] for item in knowledge_points if str(item).strip()][:20]
                    ),
                    provider_id,
                    model_id,
                    prompt_version,
                    warning[:1000],
                    now,
                    question_uuid,
                ),
            )
            db.execute(
                "UPDATE archive_jobs SET status='DONE',error='',updated_at=? WHERE question_uuid=?",
                (now, question_uuid),
            )
            self._audit(
                db,
                "archive",
                "question",
                question_uuid,
                {"public_id": public_id, "subject": subject, "warning": warning},
            )
            db.commit()
            return dict(
                db.execute("SELECT * FROM questions WHERE uuid=?", (question_uuid,)).fetchone()
            )

    async def fail_question(self, question_uuid: str, error: str) -> None:
        self._fail_question_sync(question_uuid, error)

    def _fail_question_sync(self, question_uuid: str, error: str) -> None:
        with self._lock, self._connect() as db:
            now = utc_timestamp()
            db.execute(
                "UPDATE questions SET status='FINALIZE_FAILED',error=? WHERE uuid=?",
                (error[:4000], question_uuid),
            )
            db.execute(
                "UPDATE archive_jobs SET status='FAILED',error=?,updated_at=? WHERE question_uuid=?",
                (error[:4000], now, question_uuid),
            )
            self._audit(db, "archive_failed", "question", question_uuid, {"error": error[:1000]})

    async def recover_pending_jobs(self) -> list[str]:
        return self._recover_pending_jobs_sync()

    def _recover_pending_jobs_sync(self) -> list[str]:
        with self._lock, self._connect() as db:
            db.execute(
                "UPDATE archive_jobs SET status='PENDING',updated_at=? WHERE status='RUNNING'",
                (utc_timestamp(),),
            )
            return [
                row["question_uuid"]
                for row in db.execute(
                    "SELECT question_uuid FROM archive_jobs WHERE status='PENDING'"
                ).fetchall()
            ]

    async def retry_question(
        self, *, umo: str, public_id: str | None
    ) -> dict[str, Any] | None:
        return self._retry_question_sync(umo, public_id)

    def _retry_question_sync(
        self, umo: str, public_id: str | None
    ) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            if public_id:
                row = db.execute(
                    "SELECT * FROM questions WHERE umo=? AND public_id=?",
                    (umo, public_id),
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT * FROM questions WHERE umo=? AND status='FINALIZE_FAILED'
                    ORDER BY boundary_event_id DESC LIMIT 1
                    """,
                    (umo,),
                ).fetchone()
            if not row:
                return None
            self._retry_row(db, row["uuid"])
            return dict(db.execute("SELECT * FROM questions WHERE uuid=?", (row["uuid"],)).fetchone())

    async def retry_question_by_uuid(self, question_uuid: str) -> bool:
        return self._retry_question_by_uuid_sync(question_uuid)

    def _retry_question_by_uuid_sync(self, question_uuid: str) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT uuid FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            if not row:
                return False
            self._retry_row(db, question_uuid)
            return True

    def _retry_row(self, db: sqlite3.Connection, question_uuid: str) -> None:
        now = utc_timestamp()
        db.execute(
            "UPDATE questions SET status='FINALIZING',error='' WHERE uuid=?",
            (question_uuid,),
        )
        db.execute(
            """
            INSERT INTO archive_jobs(question_uuid,status,created_at,updated_at)
            VALUES(?, 'PENDING', ?, ?)
            ON CONFLICT(question_uuid) DO UPDATE SET status='PENDING',error='',updated_at=excluded.updated_at
            """,
            (question_uuid, now, now),
        )
        self._audit(db, "retry", "question", question_uuid, {})

    async def latest_question(self, umo: str) -> dict[str, Any] | None:
        return self._latest_question_sync(umo)

    def _latest_question_sync(self, umo: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT * FROM questions
                WHERE umo=? AND status='ARCHIVED' AND deleted_at IS NULL
                ORDER BY boundary_event_id DESC LIMIT 1
                """,
                (umo,),
            ).fetchone()
            return dict(row) if row else None

    async def stats(self, umo: str = "") -> dict[str, Any]:
        return self._stats_sync(umo)

    def _stats_sync(self, umo: str) -> dict[str, Any]:
        where = "WHERE umo=?" if umo else ""
        params = (umo,) if umo else ()
        with self._lock, self._connect() as db:
            events = db.execute(f"SELECT COUNT(*) AS c FROM events {where}", params).fetchone()["c"]
            q_where = f"{where} {'AND' if where else 'WHERE'} status <> 'EMPTY'"
            questions = db.execute(
                f"SELECT COUNT(*) AS c FROM questions {q_where}", params
            ).fetchone()["c"]
            finalizing = db.execute(
                f"SELECT COUNT(*) AS c FROM questions {where} {'AND' if where else 'WHERE'} status='FINALIZING'",
                params,
            ).fetchone()["c"]
            failed = db.execute(
                f"SELECT COUNT(*) AS c FROM questions {where} {'AND' if where else 'WHERE'} status='FINALIZE_FAILED'",
                params,
            ).fetchone()["c"]
            umo_count = db.execute(
                "SELECT COUNT(DISTINCT umo) AS c FROM events"
            ).fetchone()["c"]
            umo_rows = db.execute(
                """
                SELECT DISTINCT umo FROM questions
                WHERE status <> 'EMPTY'
                ORDER BY umo
                """
            ).fetchall()
            subject_rows = db.execute(
                """
                SELECT subject, COUNT(*) AS count FROM questions
                WHERE status='ARCHIVED' AND deleted_at IS NULL
                GROUP BY subject ORDER BY count DESC, subject
                """
            ).fetchall()
            return {
                "events": int(events),
                "questions": int(questions),
                "finalizing": int(finalizing),
                "failed": int(failed),
                "umos": int(umo_count),
                "umo_values": [str(row["umo"]) for row in umo_rows],
                "subjects": [dict(row) for row in subject_rows],
            }

    async def list_questions(
        self,
        *,
        umo: str,
        subject: str,
        search: str,
        include_deleted: bool,
        limit: int,
        offset: int,
        status: str = "",
    ) -> list[dict[str, Any]]:
        return self._list_questions_sync(
            umo,
            subject,
            status,
            search,
            include_deleted,
            limit,
            offset,
        )

    def _list_questions_sync(
        self,
        umo: str,
        subject: str,
        status: str,
        search: str,
        include_deleted: bool,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        clauses = ["status <> 'EMPTY'"]
        params: list[Any] = []
        if umo:
            clauses.append("umo=?")
            params.append(umo)
        if subject:
            clauses.append("subject=?")
            params.append(subject)
        if status:
            clauses.append("status=?")
            params.append(status)
        if search:
            clauses.append(
                "(public_id LIKE ? OR title LIKE ? OR summary LIKE ? "
                "OR knowledge_points_json LIKE ?)"
            )
            pattern = f"%{search[:100]}%"
            params.extend([pattern, pattern, pattern, pattern])
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        params.extend([limit, offset])
        with self._lock, self._connect() as db:
            rows = db.execute(
                f"""
                SELECT uuid,umo,public_id,subject,title,status,event_count,
                       knowledge_points_json,
                       analysis_warning,error,created_at,archived_at,deleted_at
                FROM questions WHERE {' AND '.join(clauses)}
                ORDER BY boundary_event_id DESC LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
            return [self._question_dict(row) for row in rows]

    async def question_detail(self, question_uuid: str) -> dict[str, Any] | None:
        return self._question_detail_sync(question_uuid)

    def _question_detail_sync(self, question_uuid: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            question = db.execute(
                "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            if not question:
                return None
            events = db.execute(
                """
                SELECT e.*, GROUP_CONCAT(qe.relation, ',') AS relation,
                       MIN(qe.ordinal) AS ordinal,
                       COALESCE((
                           SELECT json_group_array(json_object(
                               'sha256', ea.sha256,
                               'name', ea.original_name,
                               'type', ea.component_type,
                               'size', a.size,
                               'mime_type', a.mime_type,
                               'stored_path', a.stored_path
                           ))
                           FROM event_attachments ea
                           JOIN attachments a ON a.sha256=ea.sha256
                           WHERE ea.event_id=e.id
                       ), '[]') AS attachments_json
                FROM question_events qe JOIN events e ON e.id=qe.event_id
                WHERE qe.question_uuid=?
                GROUP BY e.id ORDER BY MIN(qe.ordinal), e.id
                """,
                (question_uuid,),
            ).fetchall()
            result = self._question_dict(question)
            result["events"] = [self._event_dict(row) for row in events]
            return result

    async def soft_delete_question(self, question_uuid: str, deleted: bool) -> bool:
        return self._soft_delete_question_sync(question_uuid, deleted)

    def _soft_delete_question_sync(self, question_uuid: str, deleted: bool) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE questions SET deleted_at=? WHERE uuid=?",
                (utc_timestamp() if deleted else None, question_uuid),
            )
            if cursor.rowcount:
                self._audit(
                    db,
                    "soft_delete" if deleted else "restore",
                    "question",
                    question_uuid,
                    {},
                )
            return cursor.rowcount == 1

    @staticmethod
    def _question_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        raw = result.pop("knowledge_points_json", "[]")
        try:
            points = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            points = []
        result["knowledge_points"] = (
            [str(item) for item in points if str(item).strip()]
            if isinstance(points, list)
            else []
        )
        return result

    @staticmethod
    def _safe_subject(subject: str) -> str:
        cleaned = "".join(
            char for char in str(subject).strip() if char.isalnum() or "\u4e00" <= char <= "\u9fff"
        )[:20]
        return cleaned or "其他"

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for key in ("components_json", "raw_json", "attachments_json"):
            if key in result:
                try:
                    result[key.removesuffix("_json")] = json.loads(result.pop(key))
                except (json.JSONDecodeError, TypeError):
                    result[key.removesuffix("_json")] = [] if key != "raw_json" else {}
        return result

    @staticmethod
    def _audit(
        db: sqlite3.Connection,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict[str, Any],
    ) -> None:
        db.execute(
            """
            INSERT INTO audit_log(action,target_type,target_id,detail_json,created_at)
            VALUES(?,?,?,?,?)
            """,
            (action, target_type, target_id, canonical_json(detail), utc_timestamp()),
        )
