from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

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
        db.execute("DELETE FROM schema_migrations WHERE version IN (2,3,4,5,6)")

    asyncio.run(store.initialize())
    detail = asyncio.run(store.question_detail(question["uuid"]))

    with sqlite3.connect(store.db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(questions)")}
        archive_job_columns = {
            row[1] for row in db.execute("PRAGMA table_info(archive_jobs)")
        }
        versions = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        tables = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "knowledge_points_json" in columns
    assert "overview" in columns
    assert "rerun_requested" in archive_job_columns
    assert 6 in versions
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


def test_unarchived_follow_up_can_be_attached_to_existing_question(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:follow-up"
    original_id = add_event(
        store, umo=umo, direction="user", text="什么是进程？"
    )
    first_boundary = add_event(
        store, umo=umo, direction="user", text="我问完了", body_text="", boundary=True
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=first_boundary)
    )
    assert question
    asyncio.run(store.claim_job(question["uuid"]))
    archived = asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="操作系统",
            title="进程概念",
            overview="进程的基本概念。",
            summary="## 进程概念\n\n进程是资源分配的基本单位。",
            knowledge_points=["进程"],
            provider_id="archive-provider",
            model_id="archive-model",
            prompt_version="test",
        )
    )
    follow_up_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="user",
            platform_message_id="follow-up-user",
            parent_event_id=None,
            sender_id="u",
            sender_name="u",
            kind="question",
            text="那线程和进程有什么区别？",
            body_text="那线程和进程有什么区别？",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=2,
            provider_id="classifier",
            model_id="classifier-model",
            prompt_version="classifier",
        )
    )
    answer_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="follow-up-answer",
            parent_event_id=follow_up_id,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="线程共享进程资源。",
            body_text="线程共享进程资源。",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=3,
            provider_id="umo-provider",
            model_id="umo-model",
            prompt_version="runtime",
        )
    )

    pending = asyncio.run(store.list_unarchived_messages())
    assert len(pending) == 1
    assert pending[0]["event_id"] == follow_up_id
    assert pending[0]["answers"][0]["id"] == answer_id
    assert asyncio.run(store.stats())["unarchived_messages"] == 1

    attached = asyncio.run(
        store.attach_unarchived_messages(
            question_uuid=archived["uuid"],
            event_ids=[follow_up_id],
            editor="tester",
        )
    )
    assert attached["message_count"] == 1
    assert attached["event_count"] == 2
    assert asyncio.run(store.stats())["unarchived_messages"] == 0
    source = asyncio.run(store.question_source(archived["uuid"]))
    assert source and source["status"] == "FINALIZING"
    assert [(event["id"], event["relation"]) for event in source["events"]] == [
        (original_id, "primary"),
        (follow_up_id, "supplement"),
        (answer_id, "answer"),
    ]

    second_boundary = add_event(
        store, umo=umo, direction="user", text="我又问完了", body_text="", boundary=True
    )
    duplicate = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=second_boundary)
    )
    assert duplicate and duplicate["status"] == "EMPTY"


def test_late_answer_is_moved_from_wrong_interval_to_its_parent_question(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:late-answer-recovery"
    parent_id = add_event(store, umo=umo, direction="user", text="第一题追问")
    first_boundary = add_event(
        store, umo=umo, direction="user", text="我问完了", body_text="", boundary=True
    )
    first = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=first_boundary)
    )
    assert first
    asyncio.run(store.claim_job(first["uuid"]))
    first = asyncio.run(
        store.complete_question(
            question_uuid=first["uuid"],
            subject="数学",
            title="第一题",
            summary="第一题旧总结",
            provider_id="archive-provider",
            model_id="archive-model",
            prompt_version="test",
        )
    )
    late_answer_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="late-answer",
            parent_event_id=parent_id,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="这是迟到的第一题回答。",
            body_text="这是迟到的第一题回答。",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=3,
            provider_id="umo-provider",
            model_id="umo-model",
            prompt_version="runtime",
        )
    )
    add_event(store, umo=umo, direction="user", text="第二题")
    second_boundary = add_event(
        store, umo=umo, direction="user", text="又问完了", body_text="", boundary=True
    )
    second = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=second_boundary)
    )
    assert second
    second_before = asyncio.run(store.question_source(second["uuid"]))
    assert second_before
    assert late_answer_id in [event["id"] for event in second_before["events"]]

    affected = asyncio.run(store.reconcile_late_answers())

    assert affected == sorted([first["uuid"], second["uuid"]])
    first_source = asyncio.run(store.question_source(first["uuid"]))
    second_source = asyncio.run(store.question_source(second["uuid"]))
    assert first_source and second_source
    assert late_answer_id in [event["id"] for event in first_source["events"]]
    assert late_answer_id not in [event["id"] for event in second_source["events"]]
    second_detail = asyncio.run(store.question_detail(second["uuid"]))
    assert second_detail
    late_in_second = next(
        event for event in second_detail["events"] if event["id"] == late_answer_id
    )
    assert late_in_second["relation"] == "excluded"
    assert asyncio.run(store.reconcile_late_answers()) == []


def test_late_answer_during_running_archive_requests_a_second_pass(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:late-answer-rerun"
    parent_id = add_event(store, umo=umo, direction="user", text="需要慢回答的问题")
    boundary_id = add_event(
        store, umo=umo, direction="user", text="我问完了", body_text="", boundary=True
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question and asyncio.run(store.claim_job(question["uuid"]))
    late_answer_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="answer-during-archive",
            parent_event_id=parent_id,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="终于返回的完整回答。",
            body_text="终于返回的完整回答。",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=3,
            provider_id="umo-provider",
            model_id="umo-model",
            prompt_version="runtime",
        )
    )

    affected = asyncio.run(store.reconcile_late_answers(late_answer_id))
    stale_completion = asyncio.run(
        store.complete_question(
            question_uuid=question["uuid"],
            subject="数学",
            title="第一次整理",
            summary="第一次整理尚未看到迟到回答。",
            provider_id="archive-provider",
            model_id="archive-model",
            prompt_version="first-pass",
        )
    )

    assert affected == [question["uuid"]]
    assert stale_completion["status"] == "FINALIZING"
    assert asyncio.run(store.archive_job_pending(question["uuid"])) is True
    assert asyncio.run(store.claim_job(question["uuid"])) is True
    source = asyncio.run(store.question_source(question["uuid"]))
    assert source
    assert late_answer_id in [event["id"] for event in source["events"]]


def test_all_messages_show_current_and_historical_question_relations(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:all-messages"
    user_id = add_event(store, umo=umo, direction="user", text="解释一下极限")
    answer_id = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="answer-1",
            parent_event_id=user_id,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="先判断函数趋近方向。",
            body_text="先判断函数趋近方向。",
            components=[],
            raw={"kept": True},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=2,
            provider_id="umo-provider",
            model_id="umo-model",
            prompt_version="runtime",
        )
    )
    boundary_id = add_event(
        store, umo=umo, direction="user", text="我问完了", body_text="", boundary=True
    )
    question = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=boundary_id)
    )
    assert question

    result = asyncio.run(store.list_messages(umo=umo, limit=20))

    assert result["total"] == 3
    by_id = {item["id"]: item for item in result["items"]}
    assert by_id[user_id]["movable"] is True
    assert by_id[answer_id]["movable"] is True
    assert by_id[boundary_id]["movable"] is False
    assert by_id[boundary_id]["move_blocked_reason"] == "结束边界只读"
    assert by_id[user_id]["memberships"][0]["relation"] == "primary"
    assert by_id[answer_id]["memberships"][0]["relation"] == "answer"
    assert by_id[boundary_id]["memberships"][0]["relation"] == "boundary"

    assigned = asyncio.run(
        store.list_messages(umo=umo, ownership="assigned", limit=20)
    )
    assert {item["id"] for item in assigned["items"]} == {user_id, answer_id}
    searched = asyncio.run(store.list_messages(umo=umo, search="趋近方向", limit=20))
    assert [item["id"] for item in searched["items"]] == [answer_id]


def test_reassigning_one_message_moves_the_complete_turn_and_changes_sources(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:reassign"

    first_user = add_event(store, umo=umo, direction="user", text="第一题")
    first_answer = asyncio.run(
        store.add_event(
            umo=umo,
            direction="assistant",
            platform_message_id="first-answer",
            parent_event_id=first_user,
            sender_id="bot",
            sender_name="bot",
            kind="assistant",
            text="第一题答案",
            body_text="第一题答案",
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=2,
            provider_id="",
            model_id="",
            prompt_version="",
        )
    )
    first_boundary = add_event(
        store, umo=umo, direction="user", text="问完了", body_text="", boundary=True
    )
    first = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=first_boundary)
    )
    assert first and asyncio.run(store.claim_job(first["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=first["uuid"],
            subject="数学",
            title="第一题",
            summary="第一题总结",
            provider_id="test",
            model_id="test",
            prompt_version="test",
        )
    )

    second_user = add_event(store, umo=umo, direction="user", text="第二题")
    second_boundary = add_event(
        store, umo=umo, direction="user", text="又问完了", body_text="", boundary=True
    )
    second = asyncio.run(
        store.create_question_interval(umo=umo, boundary_event_id=second_boundary)
    )
    assert second and asyncio.run(store.claim_job(second["uuid"]))
    asyncio.run(
        store.complete_question(
            question_uuid=second["uuid"],
            subject="数学",
            title="第二题",
            summary="第二题总结",
            provider_id="test",
            model_id="test",
            prompt_version="test",
        )
    )

    moved = asyncio.run(
        store.reassign_message_turns(
            event_ids=[first_answer],
            question_uuid=second["uuid"],
            editor="tester",
        )
    )

    assert moved["event_ids"] == [first_user, first_answer]
    assert moved["abandoned_questions"] == [first["uuid"]]
    assert moved["queued_questions"] == [second["uuid"]]
    first_source = asyncio.run(store.question_source(first["uuid"]))
    second_source = asyncio.run(store.question_source(second["uuid"]))
    assert first_source and first_source["status"] == "ABANDONED"
    assert first_source["events"] == []
    assert second_source
    assert [(item["id"], item["relation"]) for item in second_source["events"]] == [
        (second_user, "primary"),
        (first_user, "supplement"),
        (first_answer, "answer"),
    ]
    first_detail = asyncio.run(store.question_detail(first["uuid"]))
    assert first_detail
    assert {
        item["id"]: item["relation"] for item in first_detail["events"]
    }[first_user] == "excluded"

    unassigned = asyncio.run(
        store.reassign_message_turns(
            event_ids=[first_user], question_uuid=None, editor="tester"
        )
    )
    assert unassigned["event_ids"] == [first_user, first_answer]
    second_source = asyncio.run(store.question_source(second["uuid"]))
    assert second_source
    assert [item["id"] for item in second_source["events"]] == [second_user]
    unarchived = asyncio.run(
        store.list_messages(
            umo=umo, direction="user", ownership="unarchived", limit=20
        )
    )
    assert [item["id"] for item in unarchived["items"]] == [first_user]


def test_control_messages_cannot_be_reassigned(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    umo = "default:FriendMessage:readonly-control"
    boundary_id = add_event(
        store, umo=umo, direction="user", text="问完了", body_text="", boundary=True
    )

    with pytest.raises(ValueError, match="结束边界只读"):
        asyncio.run(
            store.reassign_message_turns(
                event_ids=[boundary_id], question_uuid=None, editor="tester"
            )
        )
