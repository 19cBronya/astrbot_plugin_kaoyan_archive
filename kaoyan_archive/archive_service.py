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


ARCHIVE_PROMPT_VERSION = "archive-v7"
OVERVIEW_MAX_CHARS = 300
OVERVIEW_PROBLEM_MAX_CHARS = 80
OVERVIEW_APPROACH_MAX_CHARS = 90
OVERVIEW_FOCUS_MAX_CHARS = 60
ARCHIVE_SYSTEM_PROMPT = r"""你是考研答疑归档器，只整理给定对话，不继续答题。
返回严格 JSON 对象，字段为 subject、title、overview、knowledge_points、summary：
- subject 必须从允许科目中选择；
- title 用一句简洁中文概括题目；
- overview 是供题库快速浏览的简洁概览，不是题目总结；必须是 JSON 对象而不是字符串，并且必须同时包含 problem、approach、focus 三个非空字符串；不得省略、合并或用同一句话重复填充这三项，三项合计尽量控制在 200 字以内：
  - problem：用一句话忠实概括原题的关键已知条件和所求内容，不逐句照抄题目，不写解答结论，不超过 80 字；
  - approach：用一句话概括大致解题思路，只说明主要方法和关键步骤，不展开推导或罗列细节，不超过 90 字；
  - focus：用简洁短语提炼最重要的条件、公式、易错点或结论，不重复知识点列表，不超过 60 字；
  - 三项中的每个公式片段都必须用 $...$ 包围；即使对话信息不足，也要依据已有原文分别填写，不能只返回其中一部分；
- knowledge_points 是 1 至 8 个简洁的中文知识点字符串组成的数组；其中出现公式时必须用 $...$ 包围；
- summary 使用 Markdown，依次整理题目、关键追问、解答结论和仍未解决点；
- 完整保留有意义的数学公式，所有字段的行内公式使用 $...$，summary 中的独立公式使用 $$...$$；
- 不得编造对话中没有的信息，不输出 JSON 之外的解释。
overview 的格式示例：{"problem":"已知……，求……","approach":"先……，再……，最后……","focus":"适用条件、关键公式、易错符号"}。"""


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    question_uuid: str
    umo: str
    public_id: str
    subject: str
    title: str
    overview: str
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
        is_rearchive = bool(source.get("public_id"))

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
                if is_rearchive:
                    raise RuntimeError(
                        "重新归档的所有模型均失败，原归档内容已保留："
                        f"{str(exc)[:700]}"
                    ) from exc
                provider_id = "local"
                extra = f"所有整理模型均失败，已使用本地规则：{str(exc)[:700]}"
                warning = f"{warning}；{extra}".strip("；")

        prompt_hash = hashlib.sha256(ARCHIVE_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:16]
        row = await self.store.complete_question(
            question_uuid=question_uuid,
            subject=archive["subject"],
            title=archive["title"],
            overview=archive["overview"],
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
        overview = self._normalized_overview(
            value.get("overview"),
            transcript=transcript,
            local_overview=local["overview"],
        )
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
            "overview": overview,
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
        messages = ArchiveService._transcript_messages(transcript)
        user_messages = [body for role, body in messages if role == "用户" and body]
        assistant_messages = [
            body for role, body in messages if role == "助手" and body
        ]
        first_user = user_messages[0] if user_messages else "未命名题目"
        title = re.sub(r"\s+", " ", first_user)[:60]
        problem = ArchiveService._clean_overview_part(
            "；".join(user_messages) or first_user,
            OVERVIEW_PROBLEM_MAX_CHARS,
        )
        approach = ArchiveService._clean_overview_part(
            "；".join(assistant_messages)
            or "对话中尚无完整解答，需要先核对题设条件，再选择方法并验证结论。",
            OVERVIEW_APPROACH_MAX_CHARS,
        )
        meaningful_points = [point for point in knowledge_points if point != subject]
        focus_source = "、".join(meaningful_points[:6]) or f"{subject}题设条件、关键方法与结果校验"
        focus = ArchiveService._clean_overview_part(
            focus_source,
            OVERVIEW_FOCUS_MAX_CHARS,
        )
        overview = ArchiveService._format_overview(problem, approach, focus)
        summary = "## 对话归档\n\n" + (transcript or "（仅包含附件，暂无文本）")
        return {
            "subject": subject,
            "title": title,
            "overview": overview,
            "summary": summary,
            "knowledge_points": knowledge_points or [subject],
        }

    @staticmethod
    def _transcript_messages(transcript: str) -> list[tuple[str, str]]:
        pattern = re.compile(
            r"(?:^|\n\n)(用户|助手)：(.*?)(?=\n\n(?:用户|助手)：|\Z)",
            re.S,
        )
        return [
            (match.group(1), match.group(2).strip())
            for match in pattern.finditer(transcript)
            if match.group(2).strip()
        ]

    @staticmethod
    def _clean_overview_part(value: Any, limit: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ；;")
        if len(text) <= limit:
            return text
        shortened = text[: limit - 1].rstrip(" ，。；;")
        if shortened.count("$") % 2:
            shortened = shortened[: shortened.rfind("$")].rstrip(" ，。；;")
        return f"{shortened}…" if shortened else text[:limit]

    @staticmethod
    def _split_overview(value: Any) -> dict[str, str]:
        if isinstance(value, dict):
            return {
                "problem": str(value.get("problem") or "").strip(),
                "approach": str(value.get("approach") or "").strip(),
                "focus": str(value.get("focus") or "").strip(),
            }
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return {"problem": "", "approach": "", "focus": ""}
        match = re.fullmatch(
            r"原题\s*[:：]\s*(.*?)\s*[；;]\s*思路\s*[:：]\s*(.*?)\s*[；;]\s*重点\s*[:：]\s*(.*)",
            text,
        )
        if match:
            return {
                "problem": match.group(1).strip(),
                "approach": match.group(2).strip(),
                "focus": match.group(3).strip(),
            }
        # 兼容 archive-v5 及不完全遵循 JSON 结构的模型：旧式单句概览通常描述解题方向。
        return {"problem": "", "approach": text, "focus": ""}

    @staticmethod
    def _format_overview(problem: str, approach: str, focus: str) -> str:
        return f"原题：{problem}；思路：{approach}；重点：{focus}"[:OVERVIEW_MAX_CHARS]

    @classmethod
    def _normalized_overview(
        cls,
        value: Any,
        *,
        transcript: str,
        local_overview: str,
    ) -> str:
        generated = cls._split_overview(value)
        local = cls._split_overview(local_overview)
        problem = cls._clean_overview_part(
            generated["problem"] or local["problem"],
            OVERVIEW_PROBLEM_MAX_CHARS,
        )
        approach = cls._clean_overview_part(
            generated["approach"] or local["approach"],
            OVERVIEW_APPROACH_MAX_CHARS,
        )
        focus = cls._clean_overview_part(
            generated["focus"] or local["focus"],
            OVERVIEW_FOCUS_MAX_CHARS,
        )
        if not problem or not approach or not focus:
            fallback = cls._local_archive(transcript, ["其他"])["overview"]
            fallback_parts = cls._split_overview(fallback)
            problem = problem or fallback_parts["problem"]
            approach = approach or fallback_parts["approach"]
            focus = focus or fallback_parts["focus"]
        return cls._format_overview(problem, approach, focus)

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
            overview=row.get("overview") or "",
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
