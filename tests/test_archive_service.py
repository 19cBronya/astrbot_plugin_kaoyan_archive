from __future__ import annotations

import asyncio
from pathlib import Path

from kaoyan_archive.archive_service import ArchiveService
from kaoyan_archive.storage import ArchiveStore


class NoLLMContext:
    async def get_current_chat_provider_id(self, umo: str) -> str:
        raise AssertionError("LLM should not be used when enable_ai_archive=false")


async def build_question(store: ArchiveStore) -> str:
    umo = "default:FriendMessage:archive"
    for direction, text in (
        ("user", "操作系统中进程和线程有什么区别？"),
        ("assistant", "进程是资源分配单位，线程是调度单位。"),
    ):
        await store.add_event(
            umo=umo,
            direction=direction,
            platform_message_id="",
            parent_event_id=None,
            sender_id=direction,
            sender_name=direction,
            kind="question" if direction == "user" else "assistant",
            text=text,
            body_text=text,
            components=[],
            raw={},
            is_command=False,
            is_boundary=False,
            boundary_rule="",
            created_at=1,
            provider_id="",
            model_id="",
            prompt_version="",
        )
    boundary_id = await store.add_event(
        umo=umo,
        direction="user",
        platform_message_id="",
        parent_event_id=None,
        sender_id="user",
        sender_name="user",
        kind="boundary",
        text="我问完了",
        body_text="",
        components=[],
        raw={},
        is_command=False,
        is_boundary=True,
        boundary_rule="我问完了",
        created_at=2,
        provider_id="",
        model_id="",
        prompt_version="",
    )
    question = await store.create_question_interval(
        umo=umo, boundary_event_id=boundary_id
    )
    assert question
    return question["uuid"]


def test_local_archive_assigns_subject_and_id(tmp_path: Path) -> None:
    async def scenario():
        store = ArchiveStore(tmp_path / "archive.sqlite3")
        await store.initialize()
        question_uuid = await build_question(store)
        config = {
            "enable_ai_archive": False,
            "subjects": ["数学", "英语", "政治", "数据结构", "计组", "操作系统", "计网", "408综合", "其他"],
            "max_archive_chars": 30000,
        }
        service = ArchiveService(
            context=NoLLMContext(),
            config=config,
            store=store,
            plugin_version="test",
        )
        return await service.finalize(question_uuid)

    result = asyncio.run(scenario())
    assert result.public_id == "操作系统0001"
    assert result.subject == "操作系统"
    assert "进程和线程" in result.title
    assert "资源分配单位" in result.summary
