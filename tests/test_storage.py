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
