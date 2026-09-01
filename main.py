from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Record, Video
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.message.components import Plain
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .kaoyan_archive.analyzer import AnalysisResult, MessageAnalyzer, MessageKind
from .kaoyan_archive.archive_service import ArchiveResult, ArchiveService
from .kaoyan_archive.attachments import AttachmentStore
from .kaoyan_archive.storage import ArchiveStore
from .kaoyan_archive.utils import json_safe, utc_timestamp


PLUGIN_NAME = "astrbot_plugin_kaoyan_archive"
PLUGIN_VERSION = "0.1.1"


@register(
    PLUGIN_NAME,
    "19cBronya",
    "仅在指定 UMO 中旁路记录、分析并按题目区间归档考研答疑对话",
    PLUGIN_VERSION,
)
class KaoyanArchivePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.store = ArchiveStore(self.data_dir / "archive.sqlite3")
        self.attachment_store = AttachmentStore(
            self.data_dir / "attachments",
            max_file_bytes=self._cfg_int("max_attachment_mb", 20) * 1024 * 1024,
        )
        self.analyzer = MessageAnalyzer()
        self.archive_service = ArchiveService(
            context=context,
            config=config,
            store=self.store,
            plugin_version=PLUGIN_VERSION,
        )
        self._tasks: set[asyncio.Task[Any]] = set()
        self._register_web_apis()

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            logger.warning("无法设置插件数据目录权限: %s", self.data_dir)
        await self.store.initialize()
        await self._recover_pending_jobs()
        logger.info(
            "%s 已加载；白名单 UMO 数量=%d",
            PLUGIN_NAME,
            len(self._umo_whitelist()),
        )

    async def terminate(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.store.close()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def capture_private_message(self, event: AstrMessageEvent):
        """旁路记录白名单私聊；不停止事件、不替换结果、不控制默认 LLM。"""
        if not self._should_process(event):
            return

        analysis = self.analyzer.analyze(
            event.message_str or "",
            end_phrases=self._cfg_list("end_phrases"),
            command_prefixes=self._cfg_list("command_prefixes"),
            control_phrases=self._cfg_list("control_phrases"),
            has_attachment=bool(event.get_messages()),
        )
        event_id = await self._persist_user_event(event, analysis)
        event.set_extra(f"{PLUGIN_NAME}:event_id", event_id)
        event.set_extra(f"{PLUGIN_NAME}:analysis", analysis.as_dict())

        if analysis.kind is MessageKind.BOUNDARY:
            question = await self.store.create_question_interval(
                umo=event.unified_msg_origin,
                boundary_event_id=event_id,
            )
            if question and question["status"] == "FINALIZING":
                self._schedule_archive(question["uuid"], notify=True)

    @filter.on_agent_done(priority=-100)
    async def capture_agent_response(self, event, run_context, resp) -> None:
        if not self._should_process(event):
            return
        parent_event_id = event.get_extra(f"{PLUGIN_NAME}:event_id")
        if not parent_event_id:
            return
        analysis_data = event.get_extra(f"{PLUGIN_NAME}:analysis", {}) or {}
        excluded = bool(
            analysis_data.get("kind")
            in {MessageKind.COMMAND.value, MessageKind.BOUNDARY.value}
        )
        completion = getattr(resp, "completion_text", "") or ""
        provider_id = await self._safe_current_provider(event.unified_msg_origin)
        model_id = self._extract_model_id(resp, provider_id)
        await self.store.add_event(
            umo=event.unified_msg_origin,
            direction="assistant",
            platform_message_id=str(getattr(resp, "id", "") or ""),
            parent_event_id=int(parent_event_id),
            sender_id=str(event.get_self_id() or "bot"),
            sender_name="bot",
            kind="assistant",
            text=completion,
            body_text="" if excluded else completion,
            components=[],
            raw={"response_id": getattr(resp, "id", None)},
            is_command=excluded and analysis_data.get("kind") == MessageKind.COMMAND.value,
            is_boundary=excluded and analysis_data.get("kind") == MessageKind.BOUNDARY.value,
            boundary_rule=analysis_data.get("matched_rule", ""),
            created_at=utc_timestamp(),
            provider_id=provider_id,
            model_id=model_id,
            prompt_version="astrbot-runtime",
        )

    @filter.command_group("kaoyan")
    def kaoyan_commands(self):
        """考研归档调试与恢复命令。"""
        pass

    @kaoyan_commands.command("status")
    async def command_status(self, event: AstrMessageEvent):
        if not self._should_process(event):
            return
        stats = await self.store.stats(umo=event.unified_msg_origin)
        yield event.plain_result(
            "考研归档状态："
            f"事件 {stats['events']} 条，题目 {stats['questions']} 道，"
            f"处理中 {stats['finalizing']}，失败 {stats['failed']}。"
        )

    @kaoyan_commands.command("archive")
    async def command_archive(self, event: AstrMessageEvent):
        if not self._should_process(event):
            return
        event_id = event.get_extra(f"{PLUGIN_NAME}:event_id")
        if not event_id:
            analysis = AnalysisResult(
                kind=MessageKind.COMMAND,
                body_text="",
                matched_rule="/kaoyan archive",
            )
            event_id = await self._persist_user_event(event, analysis)
        boundary_event_id = await self.store.mark_boundary(
            int(event_id), "/kaoyan archive"
        )
        question = await self.store.create_question_interval(
            umo=event.unified_msg_origin,
            boundary_event_id=boundary_event_id,
        )
        if not question or question["status"] == "EMPTY":
            yield event.plain_result("当前区间没有可归档的题目内容。")
            return
        self._schedule_archive(question["uuid"], notify=True)
        yield event.plain_result("已提交归档任务。")

    @kaoyan_commands.command("retry")
    async def command_retry(self, event: AstrMessageEvent, public_id: str = ""):
        if not self._should_process(event):
            return
        question = await self.store.retry_question(
            umo=event.unified_msg_origin,
            public_id=public_id or None,
        )
        if not question:
            yield event.plain_result("没有找到可重试的归档任务。")
            return
        self._schedule_archive(question["uuid"], notify=True)
        yield event.plain_result("已重新提交归档任务。")

    @kaoyan_commands.command("latest")
    async def command_latest(self, event: AstrMessageEvent):
        if not self._should_process(event):
            return
        question = await self.store.latest_question(event.unified_msg_origin)
        if not question:
            yield event.plain_result("尚无已归档题目。")
            return
        yield event.plain_result(self._question_notice(question))

    async def _persist_user_event(
        self,
        event: AstrMessageEvent,
        analysis: AnalysisResult,
    ) -> int:
        components = []
        for component in event.get_messages():
            try:
                components.append(component.model_dump(mode="json"))
            except Exception:
                components.append(json_safe(component))

        message_obj = event.message_obj
        event_id = await self.store.add_event(
            umo=event.unified_msg_origin,
            direction="user",
            platform_message_id=str(getattr(message_obj, "message_id", "") or ""),
            parent_event_id=None,
            sender_id=str(event.get_sender_id() or ""),
            sender_name=event.get_sender_name() or "",
            kind=analysis.kind.value,
            text=event.message_str or "",
            body_text=analysis.body_text,
            components=components,
            raw=json_safe(getattr(message_obj, "raw_message", None)),
            is_command=analysis.kind is MessageKind.COMMAND,
            is_boundary=analysis.kind is MessageKind.BOUNDARY,
            boundary_rule=analysis.matched_rule,
            created_at=float(getattr(message_obj, "timestamp", 0) or utc_timestamp()),
            provider_id="",
            model_id="",
            prompt_version="",
        )
        await self._capture_attachments(event, event_id)
        return event_id

    async def _capture_attachments(self, event: AstrMessageEvent, event_id: int) -> None:
        for component in event.get_messages():
            if not isinstance(component, (Image, File, Record, Video)):
                continue
            try:
                if isinstance(component, File):
                    source = await component.get_file()
                    original_name = component.name or "file"
                else:
                    source = await component.convert_to_file_path()
                    original_name = Path(source).name if source else component.type.value
                if not source:
                    continue
                captured = await self.attachment_store.capture(
                    source=source,
                    original_name=original_name,
                    component_type=str(component.type.value),
                )
                await self.store.link_attachment(event_id, captured)
            except Exception as exc:
                logger.warning("附件归档失败 event_id=%s: %s", event_id, exc)

    async def _recover_pending_jobs(self) -> None:
        for question_uuid in await self.store.recover_pending_jobs():
            self._schedule_archive(question_uuid, notify=False)

    def _schedule_archive(self, question_uuid: str, notify: bool) -> None:
        task_name = f"kaoyan-archive:{question_uuid}"
        if any(task.get_name() == task_name and not task.done() for task in self._tasks):
            return
        task = asyncio.create_task(
            self._run_archive(question_uuid, notify=notify),
            name=task_name,
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_archive(self, question_uuid: str, notify: bool) -> None:
        try:
            result = await self.archive_service.finalize(question_uuid)
            if notify and self._cfg_bool("send_archive_notice", True):
                await self.context.send_message(
                    result.umo,
                    MessageChain([Plain(self._archive_result_notice(result))]),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("归档任务失败 question=%s", question_uuid)
            await self.store.fail_question(question_uuid, str(exc))

    def _register_web_apis(self) -> None:
        routes = [
            ("/stats", self.web_stats, ["GET"], "归档总览"),
            ("/config", self.web_config, ["GET", "POST"], "归档配置"),
            ("/questions", self.web_questions, ["GET"], "题目列表"),
            ("/questions/<question_uuid>", self.web_question, ["GET"], "题目详情"),
            ("/questions/<question_uuid>/action", self.web_question_action, ["POST"], "题目操作"),
        ]
        for suffix, handler, methods, desc in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}{suffix}", handler, methods, desc
            )

    @staticmethod
    def _require_dashboard_user():
        if not request.username:
            return error_response("unauthorized", status_code=401)
        return None

    async def web_stats(self):
        if response := self._require_dashboard_user():
            return response
        return json_response(await self.store.stats())

    async def web_config(self):
        if response := self._require_dashboard_user():
            return response
        if request.method == "GET":
            return json_response(
                {
                    "enabled": self._cfg_bool("enabled", True),
                    "umo_whitelist": self._umo_whitelist(),
                    "subjects": self._cfg_list("subjects"),
                    "end_phrases": self._cfg_list("end_phrases"),
                }
            )
        payload = await request.json(default={})
        whitelist = payload.get("umo_whitelist")
        if not isinstance(whitelist, list) or not all(
            isinstance(item, str) for item in whitelist
        ):
            return error_response("umo_whitelist must be a string list", status_code=400)
        cleaned = sorted({item.strip() for item in whitelist if item.strip()})
        if any(len(item) > 256 or item.count(":") < 2 for item in cleaned):
            return error_response("invalid UMO", status_code=400)
        self.config["umo_whitelist"] = cleaned
        self.config.save_config()
        return json_response({"saved": True, "umo_whitelist": cleaned})

    async def web_questions(self):
        if response := self._require_dashboard_user():
            return response
        limit = min(max(request.query.get("limit", 50, type=int), 1), 200)
        offset = max(request.query.get("offset", 0, type=int), 0)
        questions = await self.store.list_questions(
            umo=request.query.get("umo", ""),
            subject=request.query.get("subject", ""),
            search=request.query.get("search", ""),
            include_deleted=request.query.get("include_deleted", "0") == "1",
            limit=limit,
            offset=offset,
        )
        return json_response({"items": questions, "limit": limit, "offset": offset})

    async def web_question(self, question_uuid: str):
        if response := self._require_dashboard_user():
            return response
        detail = await self.store.question_detail(question_uuid)
        if not detail:
            return error_response("question not found", status_code=404)
        return json_response(detail)

    async def web_question_action(self, question_uuid: str):
        if response := self._require_dashboard_user():
            return response
        payload = await request.json(default={})
        action = str(payload.get("action", ""))
        if action == "delete":
            changed = await self.store.soft_delete_question(question_uuid, True)
        elif action == "restore":
            changed = await self.store.soft_delete_question(question_uuid, False)
        elif action == "retry":
            changed = await self.store.retry_question_by_uuid(question_uuid)
            if changed:
                self._schedule_archive(question_uuid, notify=False)
        else:
            return error_response("unsupported action", status_code=400)
        if not changed:
            return error_response("question not found", status_code=404)
        return json_response({"saved": True, "action": action})

    def _should_process(self, event: AstrMessageEvent) -> bool:
        return (
            self._cfg_bool("enabled", True)
            and event.is_private_chat()
            and event.unified_msg_origin in self._umo_whitelist()
        )

    def _umo_whitelist(self) -> list[str]:
        return self._cfg_list("umo_whitelist")

    def _cfg_list(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        return value if isinstance(value, bool) else default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    async def _safe_current_provider(self, umo: str) -> str:
        try:
            return await self.context.get_current_chat_provider_id(umo)
        except Exception:
            return ""

    @staticmethod
    def _extract_model_id(resp: Any, provider_id: str) -> str:
        raw = getattr(resp, "raw_completion", None)
        model = getattr(raw, "model", None) if raw is not None else None
        return str(model or provider_id or "unknown")

    @staticmethod
    def _archive_result_notice(result: ArchiveResult) -> str:
        suffix = f"（{result.warning}）" if result.warning else ""
        return (
            f"已归档为 {result.public_id}｜{result.title}\n"
            f"科目：{result.subject}，收录 {result.event_count} 条有效消息。{suffix}"
        )

    @staticmethod
    def _question_notice(question: dict[str, Any]) -> str:
        return (
            f"{question.get('public_id') or '未编号'}｜{question.get('title') or '未命名'}\n"
            f"科目：{question.get('subject') or '待分类'}\n"
            f"{question.get('summary') or '暂无摘要'}"
        )
