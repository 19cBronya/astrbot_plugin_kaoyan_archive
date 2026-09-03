from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

from kaoyan_archive.storage import ArchiveStore


def add_event(
    store: ArchiveStore,
    *,
    umo: str,
    direction: str,
    text: str,
    body_text: str | None = None,
    command: bool = False,
    boundary: bool = False,
) -> int:
    return asyncio.run(
        store.add_event(
            umo=umo,
            direction=direction,
            platform_message_id="",
            parent_event_id=None,
            sender_id="u" if direction == "user" else "bot",
            sender_name=direction,
            kind="boundary" if boundary else "command" if command else "question",
            text=text,
            body_text=text if body_text is None else body_text,
            components=[],
            raw={},
            is_command=command,
            is_boundary=boundary,
            boundary_rule="我问完了" if boundary else "",
            created_at=1,
            provider_id="",
            model_id="",
            prompt_version="",
        )
    )


def make_store(tmp_path: Path) -> ArchiveStore:
    store = ArchiveStore(tmp_path / "archive.sqlite3")
    asyncio.run(store.initialize())
    return store


def test_interval_excludes_commands_and_boundary(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:10001"
    user_id = add_event(store, umo=umo, direction="user", text="什么是进程？")
    assistant_id = add_event(store, umo=umo, direction="assistant", text="进程是资源分配单位。")
    command_id = add_event(
        store, umo=umo, direction="user", text="/status", body_text="", command=True
    )
    boundary_id = add_event(
        store,
        umo=umo,
        direction="user",
        text="我问完了",
        body_text="",
        boundary=True,
    )

    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question is not None
    assert question["status"] == "FINALIZING"
    assert question["event_count"] == 2

    source = asyncio.run(store.question_source(question["uuid"]))
    assert source is not None
    assert [event["id"] for event in source["events"]] == [user_id, assistant_id]

    detail = asyncio.run(store.question_detail(question["uuid"]))
    assert detail is not None
    assert [(event["id"], event["relation"]) for event in detail["events"]] == [
        (user_id, "primary"),
        (assistant_id, "answer"),
        (command_id, "excluded"),
        (boundary_id, "boundary"),
    ]


def test_intervals_do_not_cross_umo_or_previous_boundary(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo_a = "default:FriendMessage:a"
    umo_b = "default:FriendMessage:b"
    add_event(store, umo=umo_a, direction="user", text="A1")
    add_event(store, umo=umo_b, direction="user", text="B1")
    first_boundary = add_event(
        store, umo=umo_a, direction="user", text="问完了", body_text="", boundary=True
    )
    first = asyncio.run(
        store.create_question_interval(umo=umo_a, boundary_event_id=first_boundary)
    )
    add_event(store, umo=umo_a, direction="user", text="A2")
    second_boundary = add_event(
        store, umo=umo_a, direction="user", text="问完了", body_text="", boundary=True
    )
    second = asyncio.run(
        store.create_question_interval(umo=umo_a, boundary_event_id=second_boundary)
    )
    assert first and second
    first_source = asyncio.run(store.question_source(first["uuid"]))
    second_source = asyncio.run(store.question_source(second["uuid"]))
    assert [e["body_text"] for e in first_source["events"]] == ["A1"]
    assert [e["body_text"] for e in second_source["events"]] == ["A2"]


def test_subject_counter_is_transactional(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:counter"

    async def prepare(index: int) -> str:
        await store.add_event(
            umo=umo,
            direction="user",
            platform_message_id="",
            parent_event_id=None,
            sender_id="u",
            sender_name="u",
            kind="question",
            text=f"question-{index}",
            body_text=f"question-{index}",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=index,
            provider_id="",
            model_id="",
            prompt_version="",
        )
        boundary_id = await store.add_event(
            umo=umo,
            direction="user",
            platform_message_id="",
            parent_event_id=None,
            sender_id="u",
            sender_name="u",
            kind="boundary",
            text="我问完了",
            body_text="",
            components=[],
            raw={},
            is_command=False,
            is_boundary=True,
            boundary_rule="我问完了",
            created_at=index + 0.1,
            provider_id="",
            model_id="",
            prompt_version="",
        )
        question = await store.create_question_interval(
            umo=umo, boundary_event_id=boundary_id
        )
        assert question
        return question["uuid"]

    async def scenario() -> list[str]:
        question_ids = [await prepare(index) for index in range(1, 7)]
        rows = await asyncio.gather(
            *[
                store.complete_question(
                    question_uuid=question_id,
                    subject="数学",
                    title="title",
                    summary="summary",
                    provider_id="local",
                    model_id="local",
                    prompt_version="test",
                )
                for question_id in question_ids
            ]
        )
        return [row["public_id"] for row in rows]

    public_ids = asyncio.run(scenario())
    assert sorted(public_ids) == [f"数学{index:04d}" for index in range(1, 7)]


def test_raw_events_are_append_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    event_id = add_event(
        store,
        umo="default:FriendMessage:immutable",
        direction="user",
        text="original",
    )
    with sqlite3.connect(store.db_path) as db:
        try:
            db.execute("UPDATE events SET text='changed' WHERE id=?", (event_id,))
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("event update unexpectedly succeeded")


def test_schema_migrates_existing_questions_for_derived_archive_fields(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:migration"
    add_event(store, umo=umo, direction="user", text="什么是进程？")
    boundary_id = add_event(
        store,
        umo=umo,
        direction="user",
        text="我问完了",
        body_text="",
        boundary=True,
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question
    asyncio.run(store.claim_job(question["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="操作系统",
            title="进程概念",
            summary="## 对话归档\n\n进程是资源分配的基本单位。",
            provider_id="local",
            model_id="local",
            prompt_version="test",
        )
    )
    with sqlite3.connect(store.db_path) as db:
        db.execute("ALTER TABLE questions DROP COLUMN knowledge_points_json")
        db.execute("ALTER TABLE questions DROP COLUMN overview")
        db.execute("DELETE FROM schema_migrations WHERE version IN (2,3,4,5)")

    asyncio.run(store.initialize())
    detail = asyncio.run(store.question_detail(question["uuid"]))

    with sqlite3.connect(store.db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
        versions = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "knowledge_points_json" in columns
    assert "overview" in columns
    assert 5 in versions
    assert "question_revisions" in tables
    assert {"classification_jobs", "classification_revisions"} <= tables
    assert detail
    assert "资源分配" in detail["overview"]


def test_question_list_filters_and_searches_overview(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:filters"
    add_event(store, umo=umo, direction="user", text="页表如何完成地址转换？")
    boundary_id = add_event(
        store,
        umo=umo,
        direction="user",
        text="我问完了",
        body_text="",
        boundary=True,
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question
    asyncio.run(store.claim_job(question["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="操作系统",
            title="虚拟内存地址转换",
            overview="说明虚拟地址通过页表和 TLB 转换为物理地址的过程。",
            summary="地址转换过程总结",
            knowledge_points=["页表", "TLB"],
            provider_id="local",
            model_id="local",
            prompt_version="test",
        )
    )

    rows = asyncio.run(
        store.list_questions(
            umo=umo,
            subject="操作系统",
            status="ARCHIVED",
            search="物理地址",
            include_deleted=False,
            limit=20,
            offset=0,
        )
    )

    assert len(rows) == 1
    assert "页表和 TLB" in rows[0]["overview"]
    assert rows[0]["knowledge_points"] == ["页表", "TLB"]


def test_edit_archive_saves_append_only_revision_and_preserves_events(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:edit"
    event_id = add_event(store, umo=umo, direction="user", text="原始题目不可修改")
    boundary_id = add_event(
        store,
        umo=umo,
        direction="user",
        text="我问完了",
        body_text="",
        boundary=True,
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question
    asyncio.run(store.claim_job(question["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="数学",
            title="原标题",
            overview="原概览",
            summary="## 原总结",
            knowledge_points=["原知识点"],
            provider_id="local",
            model_id="local",
            prompt_version="test",
        )
    )

    updated = asyncio.run(
        store.update_question_archive(
            question_uuid=question["uuid"],
            subject="操作系统",
            title="新标题",
            overview="新概览",
            knowledge_points=["新知识点"],
            summary="## 新总结",
            editor="dashboard-user",
        )
    )
    detail = asyncio.run(store.question_detail(question["uuid"]))

    assert updated and updated["title"] == "新标题"
    assert detail and detail["subject"] == "操作系统"
    assert detail["overview"] == "新概览"
    assert detail["knowledge_points"] == ["新知识点"]
    assert detail["revision_count"] == 1
    assert next(event for event in detail["events"] if event["id"] == event_id)["text"] == "原始题目不可修改"

    asyncio.run(
        store.update_question_archive(
            question_uuid=question["uuid"],
            subject="操作系统",
            title="新标题",
            overview="新概览",
            knowledge_points=["新知识点"],
            summary="## 新总结",
            editor="dashboard-user",
        )
    )
    unchanged = asyncio.run(store.question_detail(question["uuid"]))
    assert unchanged and unchanged["revision_count"] == 1

    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute(
            "SELECT * FROM question_revisions WHERE question_uuid=?",
            (question["uuid"],),
        ).fetchone()
        assert revision is not None
        assert revision["subject"] == "数学"
        assert revision["title"] == "原标题"
        assert revision["summary"] == "## 原总结"
        try:
            db.execute(
                "UPDATE question_revisions SET title='tampered' WHERE id=?",
                (revision["id"],),
            )
        except sqlite3.IntegrityError as exc:
            assert "append-only" in str(exc)
        else:
            raise AssertionError("revision update unexpectedly succeeded")


def test_rearchive_replaces_derived_data_and_saves_old_revision(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:rearchive"
    add_event(store, umo=umo, direction="user", text="什么是极限？")
    boundary_id = add_event(
        store,
        umo=umo,
        direction="user",
        text="我问完了",
        body_text="",
        boundary=True,
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question
    asyncio.run(store.claim_job(question["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="数学",
            title="旧标题",
            overview="旧概览",
            summary="## 旧总结",
            knowledge_points=["旧知识点"],
            provider_id="local",
            model_id="local-rules",
            prompt_version="old",
        )
    )

    assert asyncio.run(store.rearchive_question_by_uuid(question["uuid"]))
    assert asyncio.run(store.claim_job(question["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="数学",
            title="新标题",
            overview="新概览",
            summary="## 新总结",
            knowledge_points=["极限"],
            provider_id="archive-provider",
            model_id="archive-model",
            prompt_version="new",
        )
    )
    detail = asyncio.run(store.question_detail(question["uuid"]))

    assert detail and detail["status"] == "ARCHIVED"
    assert detail["title"] == "新标题"
    assert detail["revision_count"] == 1
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        revision = db.execute(
            "SELECT * FROM question_revisions WHERE question_uuid=?",
            (question["uuid"],),
        ).fetchone()
    assert revision is not None
    assert revision["title"] == "旧标题"
    assert revision["summary"] == "## 旧总结"
    assert revision["editor"] == "automatic-rearchive"


def test_rearchive_rejects_deleted_or_running_question(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:rearchive-guard"
    add_event(store, umo=umo, direction="user", text="问题")
    boundary_id = add_event(
        store, umo=umo, direction="user", text="问完", body_text="", boundary=True
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question
    assert not asyncio.run(store.rearchive_question_by_uuid(question["uuid"]))


def test_failed_classification_is_excluded_until_manual_repair(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:classification-repair"
    user_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="user",
            platform_message_id="pending-user",
            parent_event_id=None,
            sender_id="u",
            sender_name="u",
            kind="pending_classification",
            text="解释一下死锁条件",
            body_text="",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="classifier-failed",
            created_at=1,
            provider_id="local",
            model_id="unclassified",
            prompt_version="classifier",
        )
    )
    assistant_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="pending-answer",
            parent_event_id=user_id,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="死锁有四个必要条件。",
            body_text="",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=2,
            provider_id="umo-provider",
            model_id="umo-model",
            prompt_version="runtime",
        )
    )
    asyncio.run(store.record_classification_failure(user_id, "provider unavailable"))
    boundary_id = add_event(
        store, umo=umo, direction="user", text="我问完了", body_text="", boundary=True
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question and question["status"] == "EMPTY"
    detail = asyncio.run(store.question_detail(question["uuid"]))
    assert detail
    assert [event["relation"] for event in detail["events"]] == [
        "excluded",
        "excluded",
        "boundary",
    ]

    affected = asyncio.run(
        store.complete_classification(
            event_id=user_id,
            kind="question",
            body_text="解释一下死锁条件",
            intent="dashboard-manual-question",
            confidence=1,
            provider_id="manual",
            model_id="dashboard",
            prompt_version="manual:v1",
            source="manual",
            editor="tester",
        )
    )
    assert affected == [question["uuid"]]
    source = asyncio.run(store.question_source(question["uuid"]))
    assert source and source["status"] == "FINALIZING"
    assert [(event["id"], event["body_text"]) for event in source["events"]] == [
        (user_id, "解释一下死锁条件"),
        (assistant_id, "死锁有四个必要条件。"),
    ]
    stats = asyncio.run(store.stats())
    assert stats["pending_classifications"] == 0

    with sqlite3.connect(store.db_path) as db:
        revision = db.execute(
            "SELECT kind,source,editor FROM classification_revisions WHERE event_id=?",
            (user_id,),
        ).fetchone()
    assert revision == ("question", "manual", "tester")
