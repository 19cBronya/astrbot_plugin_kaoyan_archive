from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .provider_fallback import (
    ProviderFallbackExhausted,
    call_with_provider_fallback,
    format_provider_failures,
)
from .storage import ArchiveStore


ARCHIVE_PROMPT_VERSION = "archive-v3"
ARCHIVE_SYSTEM_PROMPT = r"""你是考研答疑归档器，只整理给定对话，不继续答题。
返回严格 JSON 对象，字段为 subject、title、knowledge_points、summary：
- subject 必须从允许科目中选择；
- title 用一句简洁中文概括题目；
- knowledge_points 是 1 至 8 个简洁的中文知识点字符串组成的数组；
- summary 使用 Markdown，依次整理题目、关键追问、解答结论和仍未解决点；
- 完整保留有意义的数学公式，行内公式使用 \(...\)，独立公式使用 \[...\]；按 JSON 规则转义反斜杠；
- 不得编造对话中没有的信息，不输出 JSON 之外的解释。"""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    question_uuid: str
    umo: str
    public_id: str
    subject: str
    title: str
    summary: str
    event_count: int
    warning: str = ""


class ArchiveService:
    def __init__(self, *, context, config, store: ArchiveStore, plugin_version: str):
        self.context = context
        self.config = config
        self.store = store
        self.plugin_version = plugin_version

    async def finalize(self, question_uuid: str) -> ArchiveResult:
        if not await self.store.claim_job(question_uuid):
            detail = await self.store.question_detail(question_uuid)
            if detail and detail.get("status") == "ARCHIVED":
                return self._result_from_row(detail)
            raise RuntimeError("archive job is not claimable")

        source = await self.store.question_source(question_uuid)
        if not source or not source.get("events"):
            raise ValueError("question interval has no effective events")

        subjects = self._subjects()
        transcript = self._transcript(source["events"])
        max_chars = self._cfg_int("max_archive_chars", 30000, minimum=1000, maximum=200000)
        warning = ""
        if len(transcript) > max_chars:
            transcript = self._head_tail(transcript, max_chars)
            warning = f"原区间过长，模型整理使用首尾 {max_chars} 字符；完整原文仍已保存"

        provider_id = ""
        model_id = "local-rules"
        archive = self._local_archive(transcript, subjects)
        if self._cfg_bool("enable_ai_archive", True):
            async def archive_with(candidate_id: str) -> tuple[dict[str, Any], str]:
                response = await self.context.llm_generate(
                    chat_provider_id=candidate_id,
                    system_prompt=ARCHIVE_SYSTEM_PROMPT,
                    prompt=json.dumps(
                        {"subjects": subjects, "conversation": transcript},
                        ensure_ascii=False,
                    ),
                )
                parsed = self._parse_response(response.completion_text)
                return (
                    self._validate_archive(parsed, subjects, transcript),
                    self._extract_model_id(response, candidate_id),
                )

            try:
                generated, provider_id, failures = await call_with_provider_fallback(
                    context=self.context,
                    config=self.config,
                    primary_key="archive_provider_id",
                    umo=source["umo"],
                    operation=archive_with,
                )
                archive, model_id = generated
                if failures:
                    warning = self._merge_warning(
                        warning,
                        f"模型已降级：{format_provider_failures(failures)}",
                    )
            except ProviderFallbackExhausted as exc:
                provider_id = "local"
                extra = f"所有整理模型均失败，已使用本地规则：{str(exc)[:700]}"
                warning = f"{warning}；{extra}".strip("；")

        prompt_hash = hashlib.sha256(ARCHIVE_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]
        row = await self.store.complete_question(
            question_uuid=question_uuid,
            subject=archive["subject"],
            title=archive["title"],
            summary=archive["summary"],
            knowledge_points=archive["knowledge_points"],
            provider_id=provider_id or "local",
            model_id=model_id,
            prompt_version=f"{ARCHIVE_PROMPT_VERSION}:{prompt_hash}",
            warning=warning,
        )
        return self._result_from_row(row)

    def _subjects(self) -> list[str]:
        raw = self.config.get("subjects", [])
        values = [str(item).strip() for item in raw] if isinstance(raw, list) else []
        values = [item for item in values if item]
        return values or ["数学", "英语", "政治", "数据结构", "计组", "操作系统", "计网", "408综合", "其他"]

    @staticmethod
    def _transcript(events: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for event in events:
            role = "用户" if event["direction"] == "user" else "助手"
            body = str(event.get("body_text") or "").strip()
            attachments = event.get("attachments") or []
            if attachments:
                names = "、".join(str(item.get("name") or "附件") for item in attachments)
                body = f"{body}\n[附件：{names}]".strip()
            if body:
                lines.append(f"{role}：{body}")
        return "\n\n".join(lines)

    @staticmethod
    def _head_tail(text: str, limit: int) -> str:
        half = max(limit // 2 - 40, 1)
        return f"{text[:half]}\n\n[中间内容因归档模型预算省略；原文已完整保存]\n\n{text[-half:]}"

    @staticmethod
    def _parse_response(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S)
        if fenced:
            cleaned = fenced.group(1)
        else:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start >= 0 and end > start:
                cleaned = cleaned[start : end + 1]
        value = json.loads(cleaned)
        if not isinstance(value, dict):
            raise ValueError("archive response is not an object")
        return value

    def _validate_archive(
        self,
        value: dict[str, Any],
        subjects: list[str],
        transcript: str,
    ) -> dict[str, Any]:
        local = self._local_archive(transcript, subjects)
        subject = str(value.get("subject") or "").strip()
        title = str(value.get("title") or "").strip()
        summary = str(value.get("summary") or "").strip()
        raw_points = value.get("knowledge_points")
        knowledge_points = (
            [str(item).strip()[:100] for item in raw_points if str(item).strip()][:8]
            if isinstance(raw_points, list)
            else []
        )
        return {
            "subject": subject if subject in subjects else local["subject"],
            "title": title[:200] or local["title"],
            "summary": summary or local["summary"],
            "knowledge_points": knowledge_points or local["knowledge_points"],
        }

    @staticmethod
    def _local_archive(transcript: str, subjects: list[str]) -> dict[str, Any]:
        lowered = transcript.lower()
        keyword_map = [
            ("操作系统", ("进程", "线程", "死锁", "分页", "虚拟内存", "操作系统")),
            ("计组", ("cpu", "cache", "流水线", "指令周期", "存储器", "补码", "计组")),
            ("计网", ("tcp", "udp", "ip地址", "子网", "拥塞", "路由", "计网")),
            ("数据结构", ("二叉树", "链表", "栈", "队列", "图算法", "排序", "数据结构")),
            ("数学", ("极限", "导数", "积分", "矩阵", "概率", "微分", "数学")),
            ("英语", ("英语", "阅读理解", "翻译", "作文", "单词", "语法")),
            ("政治", ("政治", "马原", "毛中特", "史纲", "思修")),
            ("408综合", ("408",)),
        ]
        subject = "其他" if "其他" in subjects else subjects[-1]
        knowledge_points: list[str] = []
        for candidate, keywords in keyword_map:
            if candidate in subjects and any(keyword in lowered for keyword in keywords):
                subject = candidate
                knowledge_points = [
                    keyword.upper() if keyword in {"cpu", "cache", "tcp", "udp"} else keyword
                    for keyword in keywords
                    if keyword in lowered and keyword not in {candidate.lower(), "408"}
                ][:8]
                break
        first_user = next(
            (
                line.removeprefix("用户：").strip()
                for line in transcript.splitlines()
                if line.startswith("用户：") and line.removeprefix("用户：").strip()
            ),
            "未命名题目",
        )
        title = re.sub(r"\s+", " ", first_user)[:60]
        summary = "## 对话归档\n\n" + (transcript or "（仅包含附件，暂无文本）")
        return {
            "subject": subject,
            "title": title,
            "summary": summary,
            "knowledge_points": knowledge_points or [subject],
        }

    @staticmethod
    def _extract_model_id(response: Any, provider_id: str) -> str:
        raw = getattr(response, "raw_completion", None)
        model = getattr(raw, "model", None) if raw is not None else None
        return str(model or provider_id or "unknown")

    @staticmethod
    def _result_from_row(row: dict[str, Any]) -> ArchiveResult:
        return ArchiveResult(
            question_uuid=row["uuid"],
            umo=row["umo"],
            public_id=row.get("public_id") or "",
            subject=row.get("subject") or "其他",
            title=row.get("title") or "未命名题目",
            summary=row.get("summary") or "",
            event_count=int(row.get("event_count") or 0),
            warning=row.get("analysis_warning") or "",
        )

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return value if isinstance(value, bool) else default

    def _cfg_int(self, key: str, default: int, *, minimum: int, maximum: int) -> int:
        try:
            return min(max(int(self.config.get(key, default)), minimum), maximum)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _merge_warning(*parts: str) -> str:
        return "；".join(part.strip("； ") for part in parts if part.strip("； "))
