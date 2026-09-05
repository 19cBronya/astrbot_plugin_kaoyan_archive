from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .attachments import CapturedAttachment
from .utils import canonical_json, utc_timestamp


SCHEMA_VERSION = 6


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
                    overview TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS question_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_uuid TEXT NOT NULL REFERENCES questions(uuid),
                    revision INTEGER NOT NULL,
                    subject TEXT NOT NULL,
                    title TEXT NOT NULL,
                    overview TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    knowledge_points_json TEXT NOT NULL DEFAULT '[]',
                    editor TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(question_uuid, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_question_revisions_question
                    ON question_revisions(question_uuid, revision DESC);
                CREATE TRIGGER IF NOT EXISTS question_revisions_no_update
                BEFORE UPDATE ON question_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'question revisions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS question_revisions_no_delete
                BEFORE DELETE ON question_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'question revisions are append-only');
                END;

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
                    rerun_requested INTEGER NOT NULL DEFAULT 0
                        CHECK (rerun_requested IN (0, 1)),
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS classification_jobs (
                    event_id INTEGER PRIMARY KEY REFERENCES events(id),
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_classification_jobs_status
                    ON classification_jobs(status, updated_at);

                CREATE TABLE IF NOT EXISTS classification_revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES events(id),
                    revision INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    body_text TEXT NOT NULL DEFAULT '',
                    intent TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0,
                    provider_id TEXT NOT NULL DEFAULT '',
                    model_id TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL,
                    editor TEXT NOT NULL DEFAULT '',
                    warning TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    UNIQUE(event_id, revision)
                );
                CREATE INDEX IF NOT EXISTS idx_classification_revisions_event
                    ON classification_revisions(event_id, revision DESC);
                CREATE TRIGGER IF NOT EXISTS classification_revisions_no_update
                BEFORE UPDATE ON classification_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'classification revisions are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS classification_revisions_no_delete
                BEFORE DELETE ON classification_revisions
                BEGIN
                    SELECT RAISE(ABORT, 'classification revisions are append-only');
                END;

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
            overview_added = "overview" not in question_columns
            if overview_added:
                db.execute(
                    "ALTER TABLE questions ADD COLUMN overview TEXT NOT NULL DEFAULT ''"
                )
                old_questions = db.execute(
                    "SELECT uuid,title,summary FROM questions WHERE status='ARCHIVED'"
                ).fetchall()
                for question in old_questions:
                    db.execute(
                        "UPDATE questions SET overview=? WHERE uuid=?",
                        (
                            self._overview_from_text(
                                str(question["summary"] or ""),
                                str(question["title"] or ""),
                            ),
                            question["uuid"],
                        ),
                    )
            archive_job_columns = {
                str(row["name"])
                for row in db.execute("PRAGMA table_info(archive_jobs)").fetchall()
            }
            if "rerun_requested" not in archive_job_columns:
                db.execute(
                    "ALTER TABLE archive_jobs ADD COLUMN rerun_requested "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (rerun_requested IN (0, 1))"
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

    async def record_classification_failure(self, event_id: int, error: str) -> None:
        self._record_classification_failure_sync(event_id, error)

    async def recover_classification_jobs(self) -> int:
        return self._recover_classification_jobs_sync()

    def _recover_classification_jobs_sync(self) -> int:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                UPDATE classification_jobs
                SET status='FAILED',error='插件重启，上一次分类重试已中断',updated_at=?
                WHERE status='RUNNING'
                """,
                (utc_timestamp(),),
            )
            return int(cursor.rowcount)

    def _record_classification_failure_sync(self, event_id: int, error: str) -> None:
        with self._lock, self._connect() as db:
            now = utc_timestamp()
            db.execute(
                """
                INSERT INTO classification_jobs(event_id,status,attempts,error,created_at,updated_at)
                VALUES(?, 'FAILED', 1, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    status='FAILED',error=excluded.error,updated_at=excluded.updated_at
                """,
                (event_id, error[:4000], now, now),
            )
            self._audit(
                db,
                "classification_failed",
                "event",
                str(event_id),
                {"error": error[:1000]},
            )

    async def list_pending_classifications(
        self, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._list_pending_classifications_sync(limit)

    def _list_pending_classifications_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT e.id AS event_id,e.umo,e.text,e.created_at,e.platform_message_id,
                       cj.status,cj.attempts,cj.error,cj.updated_at,
                       COUNT(ea.sha256) AS attachment_count
                FROM classification_jobs cj
                JOIN events e ON e.id=cj.event_id
                LEFT JOIN event_attachments ea ON ea.event_id=e.id
                WHERE cj.status <> 'DONE'
                GROUP BY e.id
                ORDER BY e.id DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            return [dict(row) for row in rows]

    async def list_unarchived_messages(
        self, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        return self._list_unarchived_messages_sync(limit)

    async def list_messages(
        self,
        *,
        umo: str = "",
        direction: str = "",
        ownership: str = "",
        search: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._list_messages_sync(
            umo=umo,
            direction=direction,
            ownership=ownership,
            search=search,
            limit=limit,
            offset=offset,
        )

    def _list_messages_sync(
        self,
        *,
        umo: str,
        direction: str,
        ownership: str,
        search: str,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if umo:
            clauses.append("e.umo=?")
            params.append(umo)
        if direction == "control":
            clauses.append("(e.is_command=1 OR e.is_boundary=1)")
        elif direction in {"user", "assistant"}:
            clauses.append("e.direction=?")
            clauses.append("e.is_command=0 AND e.is_boundary=0")
            params.append(direction)
        if ownership == "assigned":
            clauses.append(
                "EXISTS(SELECT 1 FROM question_events qe WHERE qe.event_id=e.id "
                "AND qe.relation IN ('primary','supplement','answer'))"
            )
        elif ownership == "unarchived":
            clauses.append(
                "NOT EXISTS(SELECT 1 FROM question_events qe WHERE qe.event_id=e.id "
                "AND qe.relation IN ('primary','supplement','answer'))"
            )
            clauses.append("e.is_command=0 AND e.is_boundary=0")
        elif ownership == "excluded":
            clauses.append(
                "EXISTS(SELECT 1 FROM question_events qe WHERE qe.event_id=e.id "
                "AND qe.relation='excluded')"
            )
        elif ownership == "pending":
            clauses.append(
                "EXISTS(SELECT 1 FROM classification_jobs cj WHERE cj.event_id=e.id "
                "AND cj.status<>'DONE')"
            )
        if search:
            pattern = f"%{search[:100]}%"
            clauses.append(
                "(e.text LIKE ? OR e.body_text LIKE ? OR e.platform_message_id LIKE ? "
                "OR e.sender_name LIKE ?)"
            )
            params.extend([pattern, pattern, pattern, pattern])

        where = " AND ".join(clauses)
        safe_limit = min(max(int(limit), 1), 200)
        safe_offset = max(int(offset), 0)
        with self._lock, self._connect() as db:
            total = int(
                db.execute(
                    f"SELECT COUNT(*) AS count FROM events e WHERE {where}", params
                ).fetchone()["count"]
            )
            rows = db.execute(
                f"""
                SELECT e.*,
                       COALESCE((
                           SELECT cr.kind FROM classification_revisions cr
                           WHERE cr.event_id=e.id
                           ORDER BY cr.revision DESC LIMIT 1
                       ), e.kind) AS effective_kind,
                       COALESCE((
                           SELECT cj.status FROM classification_jobs cj
                           WHERE cj.event_id=e.id
                       ), '') AS classification_status,
                       COALESCE((
                           SELECT cj.error FROM classification_jobs cj
                           WHERE cj.event_id=e.id
                       ), '') AS classification_error,
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
                FROM events e
                WHERE {where}
                ORDER BY e.id DESC LIMIT ? OFFSET ?
                """,
                [*params, safe_limit, safe_offset],
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = self._event_dict(row)
                memberships = db.execute(
                    """
                    SELECT q.uuid AS question_uuid,q.public_id,q.title,q.status,
                           q.deleted_at,qe.relation,qe.ordinal
                    FROM question_events qe
                    JOIN questions q ON q.uuid=qe.question_uuid
                    WHERE qe.event_id=?
                    ORDER BY q.boundary_event_id DESC,qe.ordinal
                    """,
                    (row["id"],),
                ).fetchall()
                item["memberships"] = [dict(membership) for membership in memberships]
                item["movable"], item["move_blocked_reason"] = self._message_mobility(
                    db, row
                )
                items.append(item)
            return {
                "items": items,
                "total": total,
                "limit": safe_limit,
                "offset": safe_offset,
            }

    @staticmethod
    def _message_mobility(
        db: sqlite3.Connection, row: sqlite3.Row
    ) -> tuple[bool, str]:
        if bool(row["is_boundary"]):
            return False, "结束边界只读"
        if bool(row["is_command"]):
            return False, "框架指令只读"
        if row["direction"] == "user":
            return True, ""
        if row["direction"] != "assistant" or not row["parent_event_id"]:
            return False, "无法确定所属对话轮次"
        parent = db.execute(
            "SELECT direction,is_command,is_boundary FROM events WHERE id=?",
            (row["parent_event_id"],),
        ).fetchone()
        if not parent or parent["direction"] != "user":
            return False, "无法确定所属用户消息"
        if parent["is_boundary"] or parent["is_command"]:
            return False, "所属用户消息是只读控制消息"
        return True, ""

    async def reassign_message_turns(
        self,
        *,
        event_ids: list[int],
        question_uuid: str | None,
        editor: str,
    ) -> dict[str, Any]:
        return self._reassign_message_turns_sync(
            event_ids=event_ids,
            question_uuid=question_uuid,
            editor=editor,
        )

    def _reassign_message_turns_sync(
        self,
        *,
        event_ids: list[int],
        question_uuid: str | None,
        editor: str,
    ) -> dict[str, Any]:
        selected_ids = sorted({int(event_id) for event_id in event_ids})
        if not selected_ids:
            raise ValueError("no messages selected")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = None
            if question_uuid:
                target = db.execute(
                    """
                    SELECT * FROM questions
                    WHERE uuid=? AND status IN ('ARCHIVED','FINALIZE_FAILED','ABANDONED')
                      AND deleted_at IS NULL
                    """,
                    (question_uuid,),
                ).fetchone()
                if not target:
                    db.rollback()
                    raise ValueError("target question is not available")

            placeholders = ",".join("?" for _ in selected_ids)
            selected = db.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders}) ORDER BY id",
                selected_ids,
            ).fetchall()
            if len(selected) != len(selected_ids):
                db.rollback()
                raise ValueError("one or more messages do not exist")

            expanded_ids: set[int] = set()
            for row in selected:
                movable, reason = self._message_mobility(db, row)
                if not movable:
                    db.rollback()
                    raise ValueError(reason)
                if row["direction"] == "user":
                    parent_id = int(row["id"])
                else:
                    parent_id = int(row["parent_event_id"])
                expanded_ids.add(parent_id)
                expanded_ids.update(
                    int(child["id"])
                    for child in db.execute(
                        """
                        SELECT id FROM events
                        WHERE parent_event_id=? AND direction='assistant'
                        ORDER BY id
                        """,
                        (parent_id,),
                    ).fetchall()
                )

            expanded = sorted(expanded_ids)
            expanded_placeholders = ",".join("?" for _ in expanded)
            rows = db.execute(
                f"SELECT * FROM events WHERE id IN ({expanded_placeholders}) ORDER BY id",
                expanded,
            ).fetchall()
            if target and any(str(row["umo"]) != str(target["umo"]) for row in rows):
                db.rollback()
                raise ValueError("messages and target question must use the same UMO")

            previous = db.execute(
                f"""
                SELECT question_uuid,event_id,relation,ordinal
                FROM question_events
                WHERE event_id IN ({expanded_placeholders})
                  AND relation IN ('primary','supplement','answer')
                ORDER BY question_uuid,ordinal
                """,
                expanded,
            ).fetchall()
            affected = {str(row["question_uuid"]) for row in previous}
            target_relations = {
                int(row["event_id"]): str(row["relation"])
                for row in previous
                if question_uuid and row["question_uuid"] == question_uuid
            }

            for old in previous:
                db.execute(
                    """
                    DELETE FROM question_events
                    WHERE question_uuid=? AND event_id=? AND relation=?
                    """,
                    (old["question_uuid"], old["event_id"], old["relation"]),
                )
                if old["question_uuid"] != question_uuid:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO question_events(
                            question_uuid,event_id,relation,ordinal
                        ) VALUES(?,?,'excluded',?)
                        """,
                        (old["question_uuid"], old["event_id"], old["ordinal"]),
                    )

            if question_uuid:
                affected.add(question_uuid)
                ordinal = int(
                    db.execute(
                        """
                        SELECT COALESCE(MAX(ordinal), -1) + 1 AS value
                        FROM question_events WHERE question_uuid=?
                        """,
                        (question_uuid,),
                    ).fetchone()["value"]
                )
                for row in rows:
                    event_id = int(row["id"])
                    db.execute(
                        "DELETE FROM question_events WHERE question_uuid=? AND event_id=? AND relation='excluded'",
                        (question_uuid, event_id),
                    )
                    relation = (
                        target_relations.get(event_id, "supplement")
                        if row["direction"] == "user"
                        else "answer"
                    )
                    if relation not in {"primary", "supplement", "answer"}:
                        relation = "supplement" if row["direction"] == "user" else "answer"
                    db.execute(
                        """
                        INSERT INTO question_events(question_uuid,event_id,relation,ordinal)
                        VALUES(?,?,?,?)
                        """,
                        (question_uuid, event_id, relation, ordinal),
                    )
                    ordinal += 1

            queued: list[str] = []
            abandoned: list[str] = []
            for affected_uuid in sorted(affected):
                active_count = int(
                    db.execute(
                        """
                        SELECT COUNT(*) AS count FROM question_events
                        WHERE question_uuid=?
                          AND relation IN ('primary','supplement','answer')
                        """,
                        (affected_uuid,),
                    ).fetchone()["count"]
                )
                db.execute(
                    "UPDATE questions SET event_count=? WHERE uuid=?",
                    (active_count, affected_uuid),
                )
                affected_question = db.execute(
                    "SELECT deleted_at FROM questions WHERE uuid=?",
                    (affected_uuid,),
                ).fetchone()
                if not affected_question or affected_question["deleted_at"] is not None:
                    continue
                if active_count == 0:
                    db.execute(
                        "UPDATE questions SET status='ABANDONED',error='' WHERE uuid=?",
                        (affected_uuid,),
                    )
                    db.execute(
                        """
                        UPDATE archive_jobs
                        SET status='DONE',rerun_requested=0,error='',updated_at=?
                        WHERE question_uuid=?
                        """,
                        (utc_timestamp(), affected_uuid),
                    )
                    abandoned.append(affected_uuid)
                else:
                    self._queue_after_relation_change(db, affected_uuid)
                    queued.append(affected_uuid)

            action = "assign_message_turns" if question_uuid else "unarchive_message_turns"
            self._audit(
                db,
                action,
                "event_batch",
                ",".join(str(event_id) for event_id in selected_ids),
                {
                    "selected_event_ids": selected_ids,
                    "expanded_event_ids": expanded,
                    "question_uuid": question_uuid or "",
                    "affected_questions": sorted(affected),
                    "editor": editor[:100],
                },
            )
            db.commit()
            return {
                "selected_event_count": len(selected_ids),
                "event_count": len(expanded),
                "event_ids": expanded,
                "question_uuid": question_uuid or "",
                "affected_questions": sorted(affected),
                "queued_questions": queued,
                "abandoned_questions": abandoned,
            }

    def _list_unarchived_messages_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                """
                SELECT e.id AS event_id,e.umo,e.text,e.created_at,e.platform_message_id,
                       COUNT(DISTINCT ea.sha256) AS attachment_count,
                       COALESCE((
                           SELECT json_group_array(json_object(
                               'sha256', uea.sha256,
                               'name', uea.original_name,
                               'type', uea.component_type,
                               'size', ua.size,
                               'mime_type', ua.mime_type,
                               'stored_path', ua.stored_path
                           ))
                           FROM event_attachments uea
                           JOIN attachments ua ON ua.sha256=uea.sha256
                           WHERE uea.event_id=e.id
                       ), '[]') AS attachments_json
                FROM events e
                LEFT JOIN event_attachments ea ON ea.event_id=e.id
                WHERE e.direction='user'
                  AND COALESCE((
                      SELECT cr.kind FROM classification_revisions cr
                      WHERE cr.event_id=e.id
                      ORDER BY cr.revision DESC LIMIT 1
                  ), e.kind)='question'
                  AND NOT EXISTS(
                      SELECT 1 FROM question_events qe
                      WHERE qe.event_id=e.id
                        AND qe.relation IN ('primary','supplement')
                  )
                GROUP BY e.id
                ORDER BY e.id DESC LIMIT ?
                """,
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                item = self._event_dict(row)
                answers = db.execute(
                    """
                    SELECT e.id,e.text,e.created_at,
                           COUNT(DISTINCT ea.sha256) AS attachment_count,
                           COALESCE((
                               SELECT json_group_array(json_object(
                                   'sha256', aea.sha256,
                                   'name', aea.original_name,
                                   'type', aea.component_type,
                                   'size', aa.size,
                                   'mime_type', aa.mime_type,
                                   'stored_path', aa.stored_path
                               ))
                               FROM event_attachments aea
                               JOIN attachments aa ON aa.sha256=aea.sha256
                               WHERE aea.event_id=e.id
                           ), '[]') AS attachments_json
                    FROM events e
                    LEFT JOIN event_attachments ea ON ea.event_id=e.id
                    WHERE e.parent_event_id=? AND e.direction='assistant'
                    GROUP BY e.id ORDER BY e.id
                    """,
                    (row["event_id"],),
                ).fetchall()
                item["answers"] = [self._event_dict(answer) for answer in answers]
                result.append(item)
            return result

    async def attach_unarchived_messages(
        self,
        *,
        question_uuid: str,
        event_ids: list[int],
        editor: str,
    ) -> dict[str, Any]:
        return self._attach_unarchived_messages_sync(
            question_uuid=question_uuid,
            event_ids=event_ids,
            editor=editor,
        )

    def _attach_unarchived_messages_sync(
        self,
        *,
        question_uuid: str,
        event_ids: list[int],
        editor: str,
    ) -> dict[str, Any]:
        unique_ids = sorted({int(event_id) for event_id in event_ids})
        if not unique_ids:
            raise ValueError("no messages selected")
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            question = db.execute(
                """
                SELECT * FROM questions
                WHERE uuid=? AND status='ARCHIVED' AND deleted_at IS NULL
                """,
                (question_uuid,),
            ).fetchone()
            if not question:
                db.rollback()
                raise ValueError("target question is not available")
            placeholders = ",".join("?" for _ in unique_ids)
            rows = db.execute(
                f"""
                SELECT e.*,
                       COALESCE((
                           SELECT cr.kind FROM classification_revisions cr
                           WHERE cr.event_id=e.id
                           ORDER BY cr.revision DESC LIMIT 1
                       ), e.kind) AS effective_kind
                FROM events e
                WHERE e.id IN ({placeholders}) AND e.direction='user'
                ORDER BY e.id
                """,
                unique_ids,
            ).fetchall()
            if len(rows) != len(unique_ids):
                db.rollback()
                raise ValueError("one or more messages do not exist")
            if any(str(row["umo"]) != str(question["umo"]) for row in rows):
                db.rollback()
                raise ValueError("messages and target question must use the same UMO")
            if any(str(row["effective_kind"]) != "question" for row in rows):
                db.rollback()
                raise ValueError("only classified question messages can be attached")
            already_archived = db.execute(
                f"""
                SELECT qe.event_id FROM question_events qe
                WHERE qe.event_id IN ({placeholders})
                  AND qe.relation IN ('primary','supplement')
                LIMIT 1
                """,
                unique_ids,
            ).fetchone()
            if already_archived:
                db.rollback()
                raise ValueError("one or more messages are already archived")

            ordinal = int(
                db.execute(
                    """
                    SELECT COALESCE(MAX(ordinal), -1) + 1 AS value
                    FROM question_events WHERE question_uuid=?
                    """,
                    (question_uuid,),
                ).fetchone()["value"]
            )
            attached_ids: list[int] = []
            for row in rows:
                event_id = int(row["id"])
                db.execute(
                    """
                    DELETE FROM question_events
                    WHERE question_uuid=? AND event_id=? AND relation='excluded'
                    """,
                    (question_uuid, event_id),
                )
                db.execute(
                    """
                    INSERT INTO question_events(question_uuid,event_id,relation,ordinal)
                    VALUES(?,?,'supplement',?)
                    """,
                    (question_uuid, event_id, ordinal),
                )
                attached_ids.append(event_id)
                ordinal += 1
                answers = db.execute(
                    """
                    SELECT id FROM events
                    WHERE parent_event_id=? AND direction='assistant'
                    ORDER BY id
                    """,
                    (event_id,),
                ).fetchall()
                for answer in answers:
                    answer_id = int(answer["id"])
                    db.execute(
                        """
                        DELETE FROM question_events
                        WHERE question_uuid=? AND event_id=? AND relation='excluded'
                        """,
                        (question_uuid, answer_id),
                    )
                    db.execute(
                        """
                        INSERT OR IGNORE INTO question_events(
                            question_uuid,event_id,relation,ordinal
                        ) VALUES(?,?,'answer',?)
                        """,
                        (question_uuid, answer_id, ordinal),
                    )
                    attached_ids.append(answer_id)
                    ordinal += 1
            db.execute(
                """
                UPDATE questions SET event_count=(
                    SELECT COUNT(*) FROM question_events
                    WHERE question_uuid=?
                      AND relation IN ('primary','supplement','answer')
                ) WHERE uuid=?
                """,
                (question_uuid, question_uuid),
            )
            self._retry_row(db, question_uuid, audit_action="attach_supplement")
            self._audit(
                db,
                "attach_unarchived_messages",
                "question",
                question_uuid,
                {
                    "event_ids": unique_ids,
                    "attached_event_ids": attached_ids,
                    "editor": editor[:100],
                },
            )
            db.commit()
            return {
                "question_uuid": question_uuid,
                "message_count": len(unique_ids),
                "event_count": len(attached_ids),
            }

    async def reconcile_late_answers(
        self, assistant_event_id: int | None = None
    ) -> list[str]:
        return self._reconcile_late_answers_sync(assistant_event_id)

    def _reconcile_late_answers_sync(
        self, assistant_event_id: int | None
    ) -> list[str]:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            params: tuple[Any, ...] = ()
            event_filter = ""
            if assistant_event_id is not None:
                event_filter = "AND assistant.id=?"
                params = (int(assistant_event_id),)
            answers = db.execute(
                f"""
                SELECT assistant.id,assistant.parent_event_id
                FROM events assistant
                WHERE assistant.direction='assistant'
                  AND assistant.parent_event_id IS NOT NULL
                  {event_filter}
                  AND EXISTS(
                      SELECT 1 FROM question_events parent_qe
                      JOIN questions parent_q ON parent_q.uuid=parent_qe.question_uuid
                      WHERE parent_qe.event_id=assistant.parent_event_id
                        AND parent_qe.relation IN ('primary','supplement')
                        AND parent_q.deleted_at IS NULL
                  )
                ORDER BY assistant.id
                """,
                params,
            ).fetchall()
            changed_questions: set[str] = set()
            for answer in answers:
                answer_id = int(answer["id"])
                parent_id = int(answer["parent_event_id"])
                target_rows = db.execute(
                    """
                    SELECT DISTINCT q.uuid
                    FROM question_events qe
                    JOIN questions q ON q.uuid=qe.question_uuid
                    WHERE qe.event_id=?
                      AND qe.relation IN ('primary','supplement')
                      AND q.deleted_at IS NULL
                    """,
                    (parent_id,),
                ).fetchall()
                targets = {str(row["uuid"]) for row in target_rows}
                if not targets:
                    continue
                current_rows = db.execute(
                    """
                    SELECT question_uuid,ordinal FROM question_events
                    WHERE event_id=? AND relation='answer'
                    """,
                    (answer_id,),
                ).fetchall()
                current = {
                    str(row["question_uuid"]): int(row["ordinal"])
                    for row in current_rows
                }
                removed_from: list[str] = []
                linked_to: list[str] = []
                for question_uuid, ordinal in current.items():
                    if question_uuid in targets:
                        continue
                    db.execute(
                        """
                        DELETE FROM question_events
                        WHERE question_uuid=? AND event_id=? AND relation='answer'
                        """,
                        (question_uuid, answer_id),
                    )
                    db.execute(
                        """
                        INSERT OR IGNORE INTO question_events(
                            question_uuid,event_id,relation,ordinal
                        ) VALUES(?,?,'excluded',?)
                        """,
                        (question_uuid, answer_id, ordinal),
                    )
                    changed_questions.add(question_uuid)
                    removed_from.append(question_uuid)
                for question_uuid in targets:
                    if question_uuid in current:
                        continue
                    existing = db.execute(
                        """
                        SELECT ordinal FROM question_events
                        WHERE question_uuid=? AND event_id=? AND relation='excluded'
                        """,
                        (question_uuid, answer_id),
                    ).fetchone()
                    if existing:
                        ordinal = int(existing["ordinal"])
                        db.execute(
                            """
                            DELETE FROM question_events
                            WHERE question_uuid=? AND event_id=? AND relation='excluded'
                            """,
                            (question_uuid, answer_id),
                        )
                    else:
                        ordinal = int(
                            db.execute(
                                """
                                SELECT COALESCE(MAX(ordinal), -1) + 1 AS value
                                FROM question_events WHERE question_uuid=?
                                """,
                                (question_uuid,),
                            ).fetchone()["value"]
                        )
                    db.execute(
                        """
                        INSERT INTO question_events(
                            question_uuid,event_id,relation,ordinal
                        ) VALUES(?,?,'answer',?)
                        """,
                        (question_uuid, answer_id, ordinal),
                    )
                    changed_questions.add(question_uuid)
                    linked_to.append(question_uuid)
                if removed_from or linked_to:
                    self._audit(
                        db,
                        "reconcile_late_answer",
                        "event",
                        str(answer_id),
                        {
                            "parent_event_id": parent_id,
                            "linked_to": linked_to,
                            "removed_from": removed_from,
                        },
                    )
            for question_uuid in sorted(changed_questions):
                db.execute(
                    """
                    UPDATE questions SET event_count=(
                        SELECT COUNT(*) FROM question_events
                        WHERE question_uuid=?
                          AND relation IN ('primary','supplement','answer')
                    ) WHERE uuid=?
                    """,
                    (question_uuid, question_uuid),
                )
                self._queue_after_relation_change(db, question_uuid)
            db.commit()
            return sorted(changed_questions)

    def _queue_after_relation_change(
        self, db: sqlite3.Connection, question_uuid: str
    ) -> None:
        row = db.execute(
            """
            SELECT q.status,aj.status AS job_status
            FROM questions q
            LEFT JOIN archive_jobs aj ON aj.question_uuid=q.uuid
            WHERE q.uuid=? AND q.deleted_at IS NULL
            """,
            (question_uuid,),
        ).fetchone()
        if not row:
            return
        if row["status"] in {"ARCHIVED", "FINALIZE_FAILED", "ABANDONED"}:
            self._retry_row(db, question_uuid, audit_action="relation_change_rearchive")
        elif row["status"] == "FINALIZING" and row["job_status"] == "RUNNING":
            db.execute(
                """
                UPDATE archive_jobs SET rerun_requested=1,updated_at=?
                WHERE question_uuid=?
                """,
                (utc_timestamp(), question_uuid),
            )
        elif row["status"] == "FINALIZING" and row["job_status"] != "PENDING":
            self._retry_row(db, question_uuid, audit_action="late_answer_rearchive")

    async def classification_event(self, event_id: int) -> dict[str, Any] | None:
        return self._classification_event_sync(event_id)

    def _classification_event_sync(self, event_id: int) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT e.*,cj.status,cj.attempts,cj.error,
                       EXISTS(SELECT 1 FROM event_attachments ea WHERE ea.event_id=e.id)
                           AS has_attachment
                FROM classification_jobs cj JOIN events e ON e.id=cj.event_id
                WHERE e.id=? AND e.direction='user' AND cj.status <> 'DONE'
                """,
                (event_id,),
            ).fetchone()
            return dict(row) if row else None

    async def claim_classification(self, event_id: int) -> bool:
        return self._claim_classification_sync(event_id)

    def _claim_classification_sync(self, event_id: int) -> bool:
        with self._lock, self._connect() as db:
            cursor = db.execute(
                """
                UPDATE classification_jobs
                SET status='RUNNING',attempts=attempts+1,error='',updated_at=?
                WHERE event_id=? AND status IN ('PENDING','FAILED')
                """,
                (utc_timestamp(), event_id),
            )
            return cursor.rowcount == 1

    async def fail_classification(self, event_id: int, error: str) -> None:
        self._fail_classification_sync(event_id, error)

    def _fail_classification_sync(self, event_id: int, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE classification_jobs SET status='FAILED',error=?,updated_at=?
                WHERE event_id=?
                """,
                (error[:4000], utc_timestamp(), event_id),
            )
            self._audit(
                db,
                "classification_retry_failed",
                "event",
                str(event_id),
                {"error": error[:1000]},
            )

    async def complete_classification(
        self,
        *,
        event_id: int,
        kind: str,
        body_text: str,
        intent: str,
        confidence: float,
        provider_id: str,
        model_id: str,
        prompt_version: str,
        source: str,
        editor: str,
        warning: str = "",
    ) -> list[str]:
        return self._complete_classification_sync(
            event_id=event_id,
            kind=kind,
            body_text=body_text,
            intent=intent,
            confidence=confidence,
            provider_id=provider_id,
            model_id=model_id,
            prompt_version=prompt_version,
            source=source,
            editor=editor,
            warning=warning,
        )

    def _complete_classification_sync(self, **values: Any) -> list[str]:
        kind = str(values["kind"])
        if kind not in {"question", "instruction", "archive"}:
            raise ValueError("unsupported classification kind")
        event_id = int(values["event_id"])
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            event = db.execute(
                "SELECT * FROM events WHERE id=? AND direction='user'", (event_id,)
            ).fetchone()
            job = db.execute(
                "SELECT * FROM classification_jobs WHERE event_id=?", (event_id,)
            ).fetchone()
            if not event or not job or job["status"] == "DONE":
                db.rollback()
                raise ValueError("classification item is not pending")
            revision = int(
                db.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) + 1 AS value
                    FROM classification_revisions WHERE event_id=?
                    """,
                    (event_id,),
                ).fetchone()["value"]
            )
            body_text = str(values.get("body_text") or "")
            if kind == "question":
                body_text = str(event["text"] or body_text)
            elif kind == "instruction":
                body_text = ""
            now = utc_timestamp()
            db.execute(
                """
                INSERT INTO classification_revisions(
                    event_id,revision,kind,body_text,intent,confidence,provider_id,
                    model_id,prompt_version,source,editor,warning,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    revision,
                    kind,
                    body_text,
                    str(values.get("intent") or kind)[:80],
                    min(max(float(values.get("confidence") or 0), 0), 1),
                    str(values.get("provider_id") or "")[:200],
                    str(values.get("model_id") or "")[:200],
                    str(values.get("prompt_version") or "")[:200],
                    str(values.get("source") or "manual")[:40],
                    str(values.get("editor") or "")[:100],
                    str(values.get("warning") or "")[:1000],
                    now,
                ),
            )
            db.execute(
                "UPDATE classification_jobs SET status='DONE',error='',updated_at=? WHERE event_id=?",
                (now, event_id),
            )
            affected = self._reconcile_classification_relations(db, event, kind)
            self._audit(
                db,
                "classification_repaired",
                "event",
                str(event_id),
                {
                    "revision": revision,
                    "kind": kind,
                    "source": str(values.get("source") or "manual")[:40],
                    "affected_questions": affected,
                },
            )
            db.commit()
            return affected

    def _reconcile_classification_relations(
        self, db: sqlite3.Connection, event: sqlite3.Row, kind: str
    ) -> list[str]:
        event_id = int(event["id"])
        child_ids = [
            int(row["id"])
            for row in db.execute(
                "SELECT id FROM events WHERE parent_event_id=? AND direction='assistant'",
                (event_id,),
            ).fetchall()
        ]
        ids = [event_id, *child_ids]
        placeholders = ",".join("?" for _ in ids)
        question_rows = db.execute(
            f"SELECT DISTINCT question_uuid FROM question_events WHERE event_id IN ({placeholders})",
            ids,
        ).fetchall()
        changed_questions: list[str] = []
        for item in question_rows:
            question_uuid = str(item["question_uuid"])
            changed = False
            target_relation = "primary" if kind == "question" else "excluded"
            current = db.execute(
                "SELECT relation,ordinal FROM question_events WHERE question_uuid=? AND event_id=?",
                (question_uuid, event_id),
            ).fetchone()
            if current and current["relation"] not in {"boundary", target_relation}:
                db.execute(
                    "DELETE FROM question_events WHERE question_uuid=? AND event_id=?",
                    (question_uuid, event_id),
                )
                db.execute(
                    "INSERT INTO question_events(question_uuid,event_id,relation,ordinal) VALUES(?,?,?,?)",
                    (question_uuid, event_id, target_relation, current["ordinal"]),
                )
                changed = True
            child_relation = "answer" if kind == "question" else "excluded"
            for child_id in child_ids:
                child = db.execute(
                    "SELECT relation,ordinal FROM question_events WHERE question_uuid=? AND event_id=?",
                    (question_uuid, child_id),
                ).fetchone()
                if child and child["relation"] != child_relation:
                    db.execute(
                        "DELETE FROM question_events WHERE question_uuid=? AND event_id=?",
                        (question_uuid, child_id),
                    )
                    db.execute(
                        "INSERT INTO question_events(question_uuid,event_id,relation,ordinal) VALUES(?,?,?,?)",
                        (question_uuid, child_id, child_relation, child["ordinal"]),
                    )
                    changed = True
            if not changed:
                continue
            db.execute(
                """
                UPDATE questions SET event_count=(
                    SELECT COUNT(*) FROM question_events
                    WHERE question_uuid=?
                      AND relation IN ('primary','supplement','answer')
                ) WHERE uuid=?
                """,
                (question_uuid, question_uuid),
            )
            status = db.execute(
                "SELECT status FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()["status"]
            if status in {"ARCHIVED", "FINALIZE_FAILED"} or (
                status == "EMPTY" and kind == "question"
            ):
                self._retry_row(
                    db,
                    question_uuid,
                    audit_action="classification_repair_rearchive",
                )
                changed_questions.append(question_uuid)
        return changed_questions

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

    async def attachment_metadata(self, sha256: str) -> dict[str, Any] | None:
        return self._attachment_metadata_sync(sha256)

    def _attachment_metadata_sync(self, sha256: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT sha256,size,mime_type,stored_path
                FROM attachments WHERE sha256=?
                """,
                (sha256,),
            ).fetchone()
            return dict(row) if row else None

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
                       EXISTS(SELECT 1 FROM event_attachments ea WHERE ea.event_id=e.id) AS has_attachment,
                       EXISTS(
                           SELECT 1 FROM question_events qe
                           WHERE qe.event_id=e.id
                             AND qe.relation IN ('primary','supplement','answer')
                       ) AS already_archived
                FROM events e
                WHERE e.umo=? AND e.id>? AND e.id<=?
                ORDER BY e.id
                """,
                (umo, start_id, boundary_event_id),
            ).fetchall()
            revisions = self._latest_classifications(db, rows)
            rows_by_id = {int(row["id"]): row for row in rows}
            user_kinds: dict[int, str] = {}
            for row in rows:
                if row["direction"] != "user":
                    continue
                revision = revisions.get(int(row["id"]))
                user_kinds[int(row["id"])] = str(
                    revision["kind"] if revision else row["kind"]
                )

            effective: list[sqlite3.Row] = []
            for row in rows:
                row_id = int(row["id"])
                body_text = str(row["body_text"] or "")
                if row["direction"] == "user":
                    revision = revisions.get(row_id)
                    kind = user_kinds.get(row_id, str(row["kind"]))
                    if revision:
                        body_text = str(revision["body_text"] or "")
                    include = not row["already_archived"] and kind == "question" and (
                        body_text.strip() or row["has_attachment"]
                    )
                    # An archive boundary may contain a final verbatim supplement.
                    if kind == "archive" and body_text.strip():
                        include = True
                elif row["direction"] == "assistant":
                    parent_id = int(row["parent_event_id"] or 0)
                    parent_kind = user_kinds.get(parent_id)
                    parent = rows_by_id.get(parent_id)
                    include = (
                        parent_kind == "question"
                        and not (parent and parent["already_archived"])
                        and bool(str(row["text"] or "").strip() or row["has_attachment"])
                    ) if parent_kind else (
                        not row["already_archived"]
                        and not row["is_command"]
                        and bool(body_text.strip() or row["has_attachment"])
                        and not row["is_boundary"]
                    )
                    if include and not body_text.strip():
                        body_text = str(row["text"] or "")
                else:
                    include = False
                if include:
                    effective.append(row)
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
                       (
                           SELECT cr.kind FROM classification_revisions cr
                           WHERE cr.event_id=e.id
                           ORDER BY cr.revision DESC LIMIT 1
                       ) AS repaired_kind,
                       (
                           SELECT cr.body_text FROM classification_revisions cr
                           WHERE cr.event_id=e.id
                           ORDER BY cr.revision DESC LIMIT 1
                       ) AS repaired_body_text,
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
                WHERE qe.question_uuid=?
                  AND qe.relation IN ('primary','supplement','answer')
                ORDER BY qe.ordinal
                """,
                (question_uuid,),
            ).fetchall()
            result = dict(question)
            result["events"] = [self._source_event_dict(row) for row in events]
            return result

    @staticmethod
    def _latest_classifications(
        db: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> dict[int, sqlite3.Row]:
        event_ids = [int(row["id"]) for row in rows if row["direction"] == "user"]
        if not event_ids:
            return {}
        placeholders = ",".join("?" for _ in event_ids)
        revisions = db.execute(
            f"""
            SELECT cr.* FROM classification_revisions cr
            JOIN (
                SELECT event_id,MAX(revision) AS revision
                FROM classification_revisions
                WHERE event_id IN ({placeholders})
                GROUP BY event_id
            ) latest ON latest.event_id=cr.event_id AND latest.revision=cr.revision
            """,
            event_ids,
        ).fetchall()
        return {int(row["event_id"]): row for row in revisions}

    @classmethod
    def _source_event_dict(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = cls._event_dict(row)
        repaired_kind = result.pop("repaired_kind", None)
        repaired_body = result.pop("repaired_body_text", None)
        if repaired_kind:
            result["kind"] = repaired_kind
            result["body_text"] = repaired_body or ""
        elif result.get("relation") in {"answer", "supplement"} and not result.get("body_text"):
            result["body_text"] = result.get("text") or ""
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
        overview: str = "",
        warning: str = "",
    ) -> dict[str, Any]:
        return self._complete_question_sync(
            question_uuid,
            subject,
            title,
            overview,
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
        overview: str,
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
            active_count = int(
                db.execute(
                    """
                    SELECT COUNT(*) AS count FROM question_events
                    WHERE question_uuid=?
                      AND relation IN ('primary','supplement','answer')
                    """,
                    (question_uuid,),
                ).fetchone()["count"]
            )
            if active_count == 0 or question["status"] == "ABANDONED":
                now = utc_timestamp()
                db.execute(
                    "UPDATE questions SET status='ABANDONED',event_count=0,error='' WHERE uuid=?",
                    (question_uuid,),
                )
                db.execute(
                    """
                    UPDATE archive_jobs
                    SET status='DONE',rerun_requested=0,error='',updated_at=?
                    WHERE question_uuid=?
                    """,
                    (now, question_uuid),
                )
                self._audit(
                    db,
                    "archive_discarded_empty",
                    "question",
                    question_uuid,
                    {},
                )
                db.commit()
                return dict(
                    db.execute(
                        "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
                    ).fetchone()
                )
            job = db.execute(
                "SELECT rerun_requested FROM archive_jobs WHERE question_uuid=?",
                (question_uuid,),
            ).fetchone()
            rerun_requested = bool(job and job["rerun_requested"])
            final_status = "FINALIZING" if rerun_requested else "ARCHIVED"
            final_job_status = "PENDING" if rerun_requested else "DONE"
            public_id = question["public_id"]
            is_rearchive = bool(public_id)
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
            cleaned_overview = re.sub(r"\s+", " ", overview).strip()[:300]
            if not cleaned_overview:
                cleaned_overview = self._overview_from_text(summary, title)
            cleaned_title = title.strip()[:200]
            cleaned_summary = summary.strip()
            cleaned_points_json = canonical_json(
                [
                    str(item).strip()[:100]
                    for item in knowledge_points
                    if str(item).strip()
                ][:20]
            )
            if is_rearchive and self._archive_values_changed(
                question,
                subject=subject,
                title=cleaned_title,
                overview=cleaned_overview,
                summary=cleaned_summary,
                knowledge_points_json=cleaned_points_json,
            ):
                self._insert_revision(
                    db,
                    question,
                    editor="automatic-rearchive",
                    created_at=now,
                )
            db.execute(
                """
                UPDATE questions
                SET public_id=?, subject=?, title=?, overview=?, summary=?,
                    knowledge_points_json=?, status=?,
                    provider_id=?, model_id=?, prompt_version=?, analysis_warning=?,
                    error='', archived_at=?
                WHERE uuid=?
                """,
                (
                    public_id,
                    subject,
                    cleaned_title,
                    cleaned_overview,
                    cleaned_summary,
                    cleaned_points_json,
                    final_status,
                    provider_id,
                    model_id,
                    prompt_version,
                    warning[:1000],
                    now,
                    question_uuid,
                ),
            )
            db.execute(
                """
                UPDATE archive_jobs
                SET status=?,rerun_requested=0,error='',updated_at=?
                WHERE question_uuid=?
                """,
                (final_job_status, now, question_uuid),
            )
            self._audit(
                db,
                "archive",
                "question",
                question_uuid,
                {
                    "public_id": public_id,
                    "subject": subject,
                    "warning": warning,
                    "rerun_requested": rerun_requested,
                },
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
            cursor = db.execute(
                """
                UPDATE questions SET status='FINALIZE_FAILED',error=?
                WHERE uuid=? AND status<>'ABANDONED'
                """,
                (error[:4000], question_uuid),
            )
            if cursor.rowcount == 0:
                return
            db.execute(
                """
                UPDATE archive_jobs
                SET status='FAILED',rerun_requested=0,error=?,updated_at=?
                WHERE question_uuid=?
                """,
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

    async def archive_job_pending(self, question_uuid: str) -> bool:
        return self._archive_job_pending_sync(question_uuid)

    def _archive_job_pending_sync(self, question_uuid: str) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                """
                SELECT 1 FROM archive_jobs
                WHERE question_uuid=? AND status='PENDING'
                """,
                (question_uuid,),
            ).fetchone()
            return bool(row)

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

    async def rearchive_question_by_uuid(self, question_uuid: str) -> bool:
        return self._rearchive_question_by_uuid_sync(question_uuid)

    def _rearchive_question_by_uuid_sync(self, question_uuid: str) -> bool:
        """Queue a failed or completed archive again without touching raw events."""
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT status,deleted_at FROM questions WHERE uuid=?",
                (question_uuid,),
            ).fetchone()
            if (
                not row
                or row["deleted_at"] is not None
                or row["status"] not in {"ARCHIVED", "FINALIZE_FAILED"}
            ):
                return False
            self._retry_row(db, question_uuid, audit_action="rearchive")
            return True

    def _retry_row(
        self,
        db: sqlite3.Connection,
        question_uuid: str,
        *,
        audit_action: str = "retry",
    ) -> None:
        now = utc_timestamp()
        db.execute(
            "UPDATE questions SET status='FINALIZING',error='' WHERE uuid=?",
            (question_uuid,),
        )
        db.execute(
            """
            INSERT INTO archive_jobs(question_uuid,status,created_at,updated_at)
            VALUES(?, 'PENDING', ?, ?)
            ON CONFLICT(question_uuid) DO UPDATE SET
                status='PENDING',rerun_requested=0,error='',updated_at=excluded.updated_at
            """,
            (question_uuid, now, now),
        )
        self._audit(db, audit_action, "question", question_uuid, {})

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
            classification_where = "AND e.umo=?" if umo else ""
            pending_classifications = db.execute(
                f"""
                SELECT COUNT(*) AS c FROM classification_jobs cj
                JOIN events e ON e.id=cj.event_id
                WHERE cj.status <> 'DONE' {classification_where}
                """,
                params,
            ).fetchone()["c"]
            unarchived_where = "AND e.umo=?" if umo else ""
            unarchived_messages = db.execute(
                f"""
                SELECT COUNT(*) AS c FROM events e
                WHERE e.direction='user' {unarchived_where}
                  AND COALESCE((
                      SELECT cr.kind FROM classification_revisions cr
                      WHERE cr.event_id=e.id
                      ORDER BY cr.revision DESC LIMIT 1
                  ), e.kind)='question'
                  AND NOT EXISTS(
                      SELECT 1 FROM question_events qe
                      WHERE qe.event_id=e.id
                        AND qe.relation IN ('primary','supplement')
                  )
                """,
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
                "pending_classifications": int(pending_classifications),
                "unarchived_messages": int(unarchived_messages),
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
                "(public_id LIKE ? OR title LIKE ? OR overview LIKE ? OR summary LIKE ? "
                "OR knowledge_points_json LIKE ?)"
            )
            pattern = f"%{search[:100]}%"
            params.extend([pattern, pattern, pattern, pattern, pattern])
        if not include_deleted:
            clauses.append("deleted_at IS NULL")
        params.extend([limit, offset])
        with self._lock, self._connect() as db:
            rows = db.execute(
                f"""
                SELECT uuid,umo,public_id,subject,title,overview,status,event_count,
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
            result["revision_count"] = int(
                db.execute(
                    "SELECT COUNT(*) AS count FROM question_revisions WHERE question_uuid=?",
                    (question_uuid,),
                ).fetchone()["count"]
            )
            return result

    async def update_question_archive(
        self,
        *,
        question_uuid: str,
        subject: str,
        title: str,
        overview: str,
        knowledge_points: list[str],
        summary: str,
        editor: str,
    ) -> dict[str, Any] | None:
        return self._update_question_archive_sync(
            question_uuid,
            subject,
            title,
            overview,
            knowledge_points,
            summary,
            editor,
        )

    def _update_question_archive_sync(
        self,
        question_uuid: str,
        subject: str,
        title: str,
        overview: str,
        knowledge_points: list[str],
        summary: str,
        editor: str,
    ) -> dict[str, Any] | None:
        cleaned_subject = self._safe_subject(subject)
        cleaned_title = re.sub(r"\s+", " ", title).strip()[:200]
        cleaned_summary = summary.strip()[:200000]
        if not cleaned_title or not cleaned_summary:
            raise ValueError("title and summary are required")
        cleaned_overview = re.sub(r"\s+", " ", overview).strip()[:300]
        if not cleaned_overview:
            cleaned_overview = self._overview_from_text(cleaned_summary, cleaned_title)
        cleaned_points = [
            str(item).strip()[:100]
            for item in knowledge_points
            if str(item).strip()
        ][:20]
        cleaned_points_json = canonical_json(cleaned_points)

        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            if (
                not current
                or current["status"] != "ARCHIVED"
                or current["deleted_at"] is not None
            ):
                db.rollback()
                return None
            if (
                current["subject"] == cleaned_subject
                and current["title"] == cleaned_title
                and current["overview"] == cleaned_overview
                and current["summary"] == cleaned_summary
                and current["knowledge_points_json"] == cleaned_points_json
            ):
                db.rollback()
                return self._question_dict(current)
            now = utc_timestamp()
            revision = self._insert_revision(
                db,
                current,
                editor=editor,
                created_at=now,
            )
            db.execute(
                """
                UPDATE questions
                SET subject=?,title=?,overview=?,summary=?,knowledge_points_json=?
                WHERE uuid=?
                """,
                (
                    cleaned_subject,
                    cleaned_title,
                    cleaned_overview,
                    cleaned_summary,
                    cleaned_points_json,
                    question_uuid,
                ),
            )
            self._audit(
                db,
                "edit_archive",
                "question",
                question_uuid,
                {
                    "revision": revision,
                    "editor": editor[:100],
                    "subject": cleaned_subject,
                },
            )
            db.commit()
            updated = db.execute(
                "SELECT * FROM questions WHERE uuid=?", (question_uuid,)
            ).fetchone()
            return self._question_dict(updated)

    @staticmethod
    def _archive_values_changed(
        current: sqlite3.Row,
        *,
        subject: str,
        title: str,
        overview: str,
        summary: str,
        knowledge_points_json: str,
    ) -> bool:
        return any(
            (
                current["subject"] != subject,
                current["title"] != title,
                current["overview"] != overview,
                current["summary"] != summary,
                current["knowledge_points_json"] != knowledge_points_json,
            )
        )

    @staticmethod
    def _insert_revision(
        db: sqlite3.Connection,
        current: sqlite3.Row,
        *,
        editor: str,
        created_at: float,
    ) -> int:
        question_uuid = str(current["uuid"])
        revision = int(
            db.execute(
                """
                SELECT COALESCE(MAX(revision), 0) + 1 AS value
                FROM question_revisions WHERE question_uuid=?
                """,
                (question_uuid,),
            ).fetchone()["value"]
        )
        db.execute(
            """
            INSERT INTO question_revisions(
                question_uuid,revision,subject,title,overview,summary,
                knowledge_points_json,editor,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                question_uuid,
                revision,
                current["subject"],
                current["title"],
                current["overview"],
                current["summary"],
                current["knowledge_points_json"],
                editor[:100],
                created_at,
            ),
        )
        return revision

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
    def _overview_from_text(summary: str, title: str) -> str:
        cleaned = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", str(summary or ""))
        cleaned = re.sub(r"(?m)^\s*[-*+]\s+", "", cleaned)
        cleaned = re.sub(r"[`*_>]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        cleaned = re.sub(r"^对话归档\s*", "", cleaned)
        return (cleaned or str(title or "暂无概览").strip())[:240]

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
