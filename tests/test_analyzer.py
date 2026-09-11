from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from kaoyan_archive.analyzer import (
    CLASSIFIER_SYSTEM_PROMPT,
    MessageClassifier,
    MessageKind,
)


class FakeContext:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.provider_lookups = 0

    async def get_current_chat_provider_id(self, umo: str) -> str:
        self.provider_lookups += 1
        return "classifier-provider"

    async def llm_generate(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(
            completion_text=response,
            raw_completion=SimpleNamespace(model="classifier-model"),
        )


def classify(response: dict, text: str, *, has_attachment: bool = False):
    context = FakeContext([json.dumps(response, ensure_ascii=False)])
    classifier = MessageClassifier(context=context, config={})
    result = asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text=text,
            has_attachment=has_attachment,
        )
    )
    return result, context


def test_question_is_classified_by_llm() -> None:
    result, context = classify(
        {
            "kind": "question",
            "content": "为什么进程切换比线程切换慢？",
            "intent": "follow_up",
            "confidence": 0.97,
        },
        "为什么进程切换比线程切换慢？",
    )
    assert result.kind is MessageKind.QUESTION
    assert result.body_text == "为什么进程切换比线程切换慢？"
    assert result.provider_id == "classifier-provider"
    assert result.model_id == "classifier-model"
    assert result.prompt_version.startswith("message-classifier-v8:")
    assert len(context.calls) == 1
    assert context.calls[0]["system_prompt"] == CLASSIFIER_SYSTEM_PROMPT


def test_archive_can_keep_substantive_content() -> None:
    result, _ = classify(
        {
            "kind": "archive",
            "content": "最后补充：这里的复杂度是 O(n)。",
            "intent": "finish_and_archive",
            "confidence": 0.94,
        },
        "最后补充：这里的复杂度是 O(n)。我问完了",
    )
    assert result.kind is MessageKind.ARCHIVE
    assert result.body_text == "最后补充：这里的复杂度是 O(n)。"


def test_study_summary_request_is_not_accepted_as_archive_boundary() -> None:
    result, _ = classify(
        {
            "kind": "archive",
            "content": "",
            "intent": "summarize_current_question",
            "confidence": 0.97,
        },
        "帮我总结本题题干、思路，以及总结题目类型和同类方法",
        has_attachment=True,
    )

    assert result.kind is MessageKind.QUESTION
    assert result.body_text == "帮我总结本题题干、思路，以及总结题目类型和同类方法"
    assert result.intent == "study_summary_request"
    assert "corrected" in result.warning


def test_organize_this_question_alone_is_not_an_archive_boundary() -> None:
    result, _ = classify(
        {
            "kind": "archive",
            "content": "",
            "intent": "organize_current_question",
            "confidence": 0.98,
        },
        "整理一下这道题吧",
    )

    assert result.kind is MessageKind.QUESTION
    assert result.body_text == "整理一下这道题吧"
    assert result.intent == "study_summary_request"


def test_explicit_finish_with_summary_request_remains_archive() -> None:
    result, _ = classify(
        {
            "kind": "archive",
            "content": "",
            "intent": "finish_and_archive",
            "confidence": 0.99,
        },
        "我问完了，帮我总结本题题干和思路并整理入库",
        has_attachment=True,
    )

    assert result.kind is MessageKind.ARCHIVE


def test_explicit_finish_overrides_wrong_question_classification() -> None:
    result, _ = classify(
        {
            "kind": "question",
            "content": "ok我问完了整理一下吧",
            "intent": "study_summary_request",
            "confidence": 0.91,
        },
        "ok我问完了整理一下吧",
    )

    assert result.kind is MessageKind.ARCHIVE
    assert result.body_text == ""
    assert result.intent == "explicit_finish_boundary"
    assert "explicit finish semantics" in result.warning


def test_ok_then_organize_question_is_an_archive_boundary() -> None:
    result, _ = classify(
        {
            "kind": "question",
            "content": "ok了，整理一下这道题目的思路",
            "intent": "study_summary_request",
            "confidence": 0.88,
        },
        "ok了，整理一下这道题目的思路",
    )

    assert result.kind is MessageKind.ARCHIVE
    assert result.body_text == ""
    assert result.intent == "explicit_finish_boundary"


def test_equivalent_finish_words_then_organize_are_archive_boundaries() -> None:
    for text in (
        "好了，整理一下这道题目的思路",
        "行了，归纳一下本题",
    ):
        result, _ = classify(
            {
                "kind": "question",
                "content": text,
                "intent": "study_summary_request",
                "confidence": 0.85,
            },
            text,
        )
        assert result.kind is MessageKind.ARCHIVE
        assert result.body_text == ""
        assert result.intent == "explicit_finish_boundary"


def test_organize_question_thoughts_without_ok_remains_question() -> None:
    text = "整理一下这道题目的思路"
    result, _ = classify(
        {
            "kind": "archive",
            "content": "",
            "intent": "finish_and_archive",
            "confidence": 0.9,
        },
        text,
    )

    assert result.kind is MessageKind.QUESTION
    assert result.body_text == text


def test_negated_question_and_hypothetical_finish_phrases_do_not_force_archive() -> None:
    for text in (
        "我还没问完，继续讲",
        "我问完了吗？",
        "如果我说我问完了，你会怎么处理？",
        "比如我说ok了整理一下这道题，你会归档吗？",
    ):
        result, _ = classify(
            {
                "kind": "question",
                "content": text,
                "intent": "continue_question",
                "confidence": 0.99,
            },
            text,
        )
        assert result.kind is MessageKind.QUESTION


def test_negated_finish_with_organize_request_corrects_false_archive() -> None:
    text = "我还没问完，先整理一下这道题目前的思路"
    result, _ = classify(
        {
            "kind": "archive",
            "content": "",
            "intent": "finish_and_archive",
            "confidence": 0.92,
        },
        text,
    )

    assert result.kind is MessageKind.QUESTION
    assert result.body_text == text


def test_soft_instruction_is_excluded_from_question_body() -> None:
    result, _ = classify(
        {
            "kind": "instruction",
            "content": "模型不应保留这段",
            "intent": "query_history",
            "confidence": 0.99,
        },
        "帮我查询历史题目",
    )
    assert result.kind is MessageKind.INSTRUCTION
    assert result.body_text == ""
    assert result.intent == "query_history"


def test_unrelated_reminder_is_an_excluded_instruction() -> None:
    result, context = classify(
        {
            "kind": "instruction",
            "content": "",
            "intent": "unrelated_request",
            "confidence": 0.99,
        },
        "五小时后提醒我吃药",
    )

    assert result.kind is MessageKind.INSTRUCTION
    assert result.body_text == ""
    assert result.intent == "unrelated_request"
    assert "五小时后提醒我吃药" in context.calls[0]["system_prompt"]


def test_soft_cancel_marks_the_current_interval_invalid() -> None:
    result, _ = classify(
        {
            "kind": "cancel",
            "content": "这段不应进入正文",
            "intent": "cancel_current_interval",
            "confidence": 0.99,
        },
        "这段问错了，取消掉",
    )
    assert result.kind is MessageKind.CANCEL
    assert result.body_text == ""
    assert result.intent == "cancel_current_interval"


def test_invalid_classifier_output_waits_for_repair_without_polluting_question() -> None:
    context = FakeContext(["not-json"])
    classifier = MessageClassifier(context=context, config={})
    result = asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text="这条原文不能丢",
            has_attachment=False,
        )
    )
    assert result.kind is MessageKind.PENDING
    assert result.body_text == ""
    assert result.intent == "classifier-failed"
    assert "不会进入题目正文" in result.warning
    assert "JSONDecodeError" in result.warning


def test_empty_transport_event_does_not_spend_a_model_call() -> None:
    context = FakeContext([])
    classifier = MessageClassifier(context=context, config={})
    result = asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text="",
            has_attachment=False,
        )
    )
    assert result.kind is MessageKind.EMPTY
    assert context.calls == []


def test_configured_classifier_provider_is_used() -> None:
    context = FakeContext(
        ['{"kind":"question","content":"题目","intent":"new_question","confidence":1}']
    )
    classifier = MessageClassifier(
        context=context,
        config={"classification_provider_id": "cheap-classifier"},
    )
    asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text="题目",
            has_attachment=False,
        )
    )
    assert context.calls[0]["chat_provider_id"] == "cheap-classifier"
    assert context.provider_lookups == 0


def test_classifier_uses_backup_before_umo_provider() -> None:
    context = FakeContext(
        [
            RuntimeError("primary unavailable"),
            '{"kind":"question","content":"题目","intent":"new_question","confidence":1}',
        ]
    )
    classifier = MessageClassifier(
        context=context,
        config={
            "classification_provider_id": "primary-classifier",
            "fallback_provider_ids": ["backup-provider"],
        },
    )

    result = asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text="题目",
            has_attachment=False,
        )
    )

    assert result.provider_id == "backup-provider"
    assert [call["chat_provider_id"] for call in context.calls] == [
        "primary-classifier",
        "backup-provider",
    ]
    assert context.provider_lookups == 0
    assert "模型已降级" in result.warning
    assert "primary unavailable" in result.warning


def test_classifier_falls_back_to_umo_after_all_backup_failures() -> None:
    context = FakeContext(
        [
            RuntimeError("primary unavailable"),
            RuntimeError("backup one unavailable"),
            RuntimeError("backup two unavailable"),
            '{"kind":"archive","content":"","intent":"finish","confidence":1}',
        ]
    )
    classifier = MessageClassifier(
        context=context,
        config={
            "classification_provider_id": "primary-classifier",
            "fallback_provider_ids": ["backup-one", "backup-two"],
        },
    )

    result = asyncio.run(
        classifier.classify(
            umo="default:FriendMessage:10001",
            text="我问完了",
            has_attachment=False,
        )
    )

    assert result.kind is MessageKind.ARCHIVE
    assert result.provider_id == "classifier-provider"
    assert [call["chat_provider_id"] for call in context.calls] == [
        "primary-classifier",
        "backup-one",
        "backup-two",
        "classifier-provider",
    ]
    assert context.provider_lookups == 1
