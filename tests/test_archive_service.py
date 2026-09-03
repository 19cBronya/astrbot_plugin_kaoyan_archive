from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from kaoyan_archive.archive_service import (
    ARCHIVE_PROMPT_VERSION,
    ARCHIVE_SYSTEM_PROMPT,
    ArchiveService,
)
from kaoyan_archive.storage import ArchiveStore


class NoLLMContext:
    async def get_current_chat_provider_id(self, umo: str) -> str:
        raise AssertionError("LLM should not be used when enable_ai_archive=false")


class FallbackLLMContext:
    def __init__(self) -> None:
        self.calls = []
        self.provider_lookups = 0

    async def get_current_chat_provider_id(self, umo: str) -> str:
        self.provider_lookups += 1
        return "umo-provider"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["chat_provider_id"] == "primary-archive":
            raise RuntimeError("primary archive provider unavailable")
        return SimpleNamespace(
            completion_text=json.dumps(
                {
                    "subject": "操作系统",
                    "title": "备用模型整理成功",
                    "overview": "概括进程状态与线程调度的核心区别。",
                    "knowledge_points": ["进程状态", "线程调度"],
                    "summary": "## 备用整理结果",
                },
                ensure_ascii=False,
            ),
            raw_completion=SimpleNamespace(model="backup-model"),
        )


class AllFailLLMContext:
    def __init__(self) -> None:
        self.calls = []

    async def get_current_chat_provider_id(self, umo: str) -> str:
        return "umo-provider"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        raise RuntimeError(f"{kwargs['chat_provider_id']} unavailable")


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


def test_archive_prompt_preserves_renderable_formula_delimiters() -> None:
    assert ARCHIVE_PROMPT_VERSION == "archive-v4"
    assert "overview" in ARCHIVE_SYSTEM_PROMPT
    assert r"\(...\)" in ARCHIVE_SYSTEM_PROMPT
    assert r"\[...\]" in ARCHIVE_SYSTEM_PROMPT
    assert "完整保留" in ARCHIVE_SYSTEM_PROMPT


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
    assert "题目主要讨论" in result.overview
    assert "资源分配单位" in result.summary


def test_archive_uses_backup_before_umo_provider(tmp_path: Path) -> None:
    async def scenario():
        store = ArchiveStore(tmp_path / "archive.sqlite3")
        await store.initialize()
        question_uuid = await build_question(store)
        context = FallbackLLMContext()
        service = ArchiveService(
            context=context,
            config={
                "enable_ai_archive": True,
                "archive_provider_id": "primary-archive",
                "fallback_provider_id": "backup-provider",
                "subjects": ["操作系统", "其他"],
                "max_archive_chars": 30000,
            },
            store=store,
            plugin_version="test",
        )
        result = await service.finalize(question_uuid)
        detail = await store.question_detail(question_uuid)
        return result, detail, context

    result, detail, context = asyncio.run(scenario())

    assert result.title == "备用模型整理成功"
    assert result.overview == "概括进程状态与线程调度的核心区别。"
    assert detail["overview"] == result.overview
    assert detail["provider_id"] == "backup-provider"
    assert detail["model_id"] == "backup-model"
    assert detail["knowledge_points"] == ["进程状态", "线程调度"]
    assert "模型已降级" in result.warning
    assert [call["chat_provider_id"] for call in context.calls] == [
        "primary-archive",
        "backup-provider",
    ]
    assert context.provider_lookups == 0


def test_archive_uses_local_rules_when_all_providers_fail(tmp_path: Path) -> None:
    async def scenario():
        store = ArchiveStore(tmp_path / "archive.sqlite3")
        await store.initialize()
        question_uuid = await build_question(store)
        context = AllFailLLMContext()
        service = ArchiveService(
            context=context,
            config={
                "enable_ai_archive": True,
                "archive_provider_id": "primary-archive",
                "fallback_provider_id": "backup-provider",
                "subjects": ["操作系统", "其他"],
                "max_archive_chars": 30000,
            },
            store=store,
            plugin_version="test",
        )
        result = await service.finalize(question_uuid)
        detail = await store.question_detail(question_uuid)
        return result, detail, context

    result, detail, context = asyncio.run(scenario())

    assert result.public_id == "操作系统0001"
    assert "进程和线程" in result.title
    assert detail["status"] == "ARCHIVED"
    assert detail["provider_id"] == "local"
    assert detail["model_id"] == "local-rules"
    assert detail["overview"]
    assert {"进程", "线程"}.issubset(detail["knowledge_points"])
    assert "所有整理模型均失败" in result.warning
    assert [call["chat_provider_id"] for call in context.calls] == [
        "primary-archive",
        "backup-provider",
        "umo-provider",
    ]
