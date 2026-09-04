from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace


class _NoopLogger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _Filter:
    class EventMessageType:
        PRIVATE_MESSAGE = "private"

    @staticmethod
    def event_message_type(*_args, **_kwargs):
        return lambda function: function

    @staticmethod
    def on_agent_done(*_args, **_kwargs):
        return lambda function: function

    @staticmethod
    def command_group(*_args, **_kwargs):
        def decorate(function):
            function.command = lambda *_a, **_kw: (lambda handler: handler)
            return function

        return decorate


class _Star:
    def __init__(self, context):
        self.context = context


class _FakeContext:
    def __init__(self) -> None:
        self.routes = []
        self.llm_calls = []
        self.runtime_configs = {}
        self.astrbot_config_mgr = SimpleNamespace(
            get_conf_info=lambda umo: {
                "id": "default",
                "name": "default",
                "path": "cmd_config.json",
            }
        )

    def register_web_api(self, route, handler, methods, description) -> None:
        self.routes.append((route, handler, methods, description))

    async def get_current_chat_provider_id(self, umo: str) -> str:
        return "classifier-provider"

    def get_config(self, umo: str | None = None):
        return self.runtime_configs.get(umo, {"plugin_set": ["*"]})

    async def llm_generate(self, **kwargs):
        self.llm_calls.append(kwargs)
        return SimpleNamespace(
            completion_text=(
                '{"kind":"question","content":"操作系统的进程是什么？",'
                '"intent":"new_question","confidence":0.99}'
            ),
            raw_completion=SimpleNamespace(model="classifier-model"),
        )


class _Config(dict):
    def save_config(self) -> None:
        pass


class _Component:
    pass


class _MessageChain:
    def __init__(self, chain):
        self.chain = chain


class _Plain:
    def __init__(self, text):
        self.text = text


def _install_astrbot_api_stubs(monkeypatch, data_root: Path) -> None:
    modules = {
        "astrbot": types.ModuleType("astrbot"),
        "astrbot.api": types.ModuleType("astrbot.api"),
        "astrbot.api.event": types.ModuleType("astrbot.api.event"),
        "astrbot.api.message_components": types.ModuleType(
            "astrbot.api.message_components"
        ),
        "astrbot.api.star": types.ModuleType("astrbot.api.star"),
        "astrbot.api.web": types.ModuleType("astrbot.api.web"),
        "astrbot.core": types.ModuleType("astrbot.core"),
        "astrbot.core.message": types.ModuleType("astrbot.core.message"),
        "astrbot.core.message.message_event_result": types.ModuleType(
            "astrbot.core.message.message_event_result"
        ),
        "astrbot.core.message.components": types.ModuleType(
            "astrbot.core.message.components"
        ),
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": types.ModuleType(
            "astrbot.core.utils.astrbot_path"
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    modules["astrbot.api"].AstrBotConfig = _Config
    modules["astrbot.api"].logger = _NoopLogger()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = _Filter
    for name in ("File", "Image", "Record", "Video"):
        setattr(modules["astrbot.api.message_components"], name, type(name, (_Component,), {}))
    modules["astrbot.api.star"].Context = _FakeContext
    modules["astrbot.api.star"].Star = _Star
    modules["astrbot.api.star"].register = lambda *_a, **_kw: (
        lambda plugin_class: plugin_class
    )
    modules["astrbot.api.web"].request = SimpleNamespace(username="tester")
    modules["astrbot.api.web"].json_response = lambda payload, **_kw: payload
    modules["astrbot.api.web"].error_response = lambda message, **kwargs: {
        "error": message,
        **kwargs,
    }
    modules["astrbot.core.message.message_event_result"].MessageChain = _MessageChain
    modules["astrbot.core.message.components"].Plain = _Plain
    modules["astrbot.core.utils.astrbot_path"].get_astrbot_plugin_data_path = (
        lambda: data_root
    )


def _load_plugin_module(monkeypatch, tmp_path: Path):
    _install_astrbot_api_stubs(monkeypatch, tmp_path)
    plugin_root = Path(__file__).parents[1]
    package_paths = {
        "data": plugin_root.parent.parent,
        "data.plugins": plugin_root.parent,
        "data.plugins.astrbot_plugin_kaoyan_archive": plugin_root,
    }
    for package_name, package_path in package_paths.items():
        package = types.ModuleType(package_name)
        package.__path__ = [str(package_path)]
        monkeypatch.setitem(sys.modules, package_name, package)

    module_name = "data.plugins.astrbot_plugin_kaoyan_archive.main"
    plugin_path = plugin_root / "main.py"
    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_plugin_entry_registers_page_and_defaults_to_deny(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    context = _FakeContext()
    plugin = module.KaoyanArchivePlugin(context, _Config())

    assert len(context.routes) == 9
    assert {route for route, *_ in context.routes} == {
        f"/{module.PLUGIN_NAME}/stats",
        f"/{module.PLUGIN_NAME}/config",
        f"/{module.PLUGIN_NAME}/questions",
        f"/{module.PLUGIN_NAME}/questions/<question_uuid>",
        f"/{module.PLUGIN_NAME}/questions/<question_uuid>/edit",
        f"/{module.PLUGIN_NAME}/questions/<question_uuid>/action",
        f"/{module.PLUGIN_NAME}/repairs",
        f"/{module.PLUGIN_NAME}/repairs/action",
        f"/{module.PLUGIN_NAME}/attachments/<sha256>",
    }
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
    )
    assert plugin._should_process(event) is False


def test_initialize_migrates_legacy_fallback_provider(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    config = _Config(fallback_provider_id="legacy-provider")
    plugin = module.KaoyanArchivePlugin(_FakeContext(), config)

    asyncio.run(plugin.initialize())

    assert config["fallback_provider_ids"] == ["legacy-provider"]


def test_fallback_provider_config_uses_astrbot_multi_selector() -> None:
    schema_path = Path(__file__).parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fallback = schema["fallback_provider_ids"]

    assert fallback["type"] == "list"
    assert fallback["items"] == {"type": "string"}
    assert fallback["_special"] == "select_providers"


def test_page_edit_endpoint_validates_and_saves_archive(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    plugin = module.KaoyanArchivePlugin(
        _FakeContext(), _Config(subjects=["数学", "操作系统"])
    )
    captured = {}

    async def request_json(default=None):
        return {
            "subject": "操作系统",
            "title": "修改后的标题",
            "overview": "修改后的概览",
            "knowledge_points": ["知识点一", "知识点二"],
            "summary": "## 修改后的总结",
        }

    async def update_question_archive(**kwargs):
        captured.update(kwargs)
        return {"uuid": kwargs["question_uuid"], "title": kwargs["title"]}

    module.request.json = request_json
    plugin.store.update_question_archive = update_question_archive
    result = asyncio.run(plugin.web_question_edit("question-uuid"))

    assert result["saved"] is True
    assert captured["question_uuid"] == "question-uuid"
    assert captured["subject"] == "操作系统"
    assert captured["editor"] == "tester"
    assert captured["knowledge_points"] == ["知识点一", "知识点二"]


def test_page_rearchive_action_queues_existing_question(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    plugin = module.KaoyanArchivePlugin(_FakeContext(), _Config())
    scheduled = []

    async def request_json(default=None):
        return {"action": "rearchive"}

    async def rearchive_question(question_uuid):
        assert question_uuid == "question-uuid"
        return True

    module.request.json = request_json
    plugin.store.rearchive_question_by_uuid = rearchive_question
    plugin._schedule_archive = lambda question_uuid, notify: scheduled.append(
        (question_uuid, notify)
    )

    result = asyncio.run(plugin.web_question_action("question-uuid"))

    assert result == {"saved": True, "action": "rearchive"}
    assert scheduled == [("question-uuid", False)]


def test_authenticated_image_preview_is_content_addressed_and_path_safe(
    monkeypatch, tmp_path: Path
) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    plugin = module.KaoyanArchivePlugin(_FakeContext(), _Config())
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    source = tmp_path / "pixel.png"
    source.write_bytes(image_bytes)

    async def prepare():
        await plugin.initialize()
        event_id = await plugin.store.add_event(
            umo="default:FriendMessage:image",
            direction="user",
            platform_message_id="image-1",
            parent_event_id=None,
            sender_id="user",
            sender_name="user",
            kind="question",
            text="题图",
            body_text="题图",
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
        captured = plugin.attachment_store._capture_sync(
            str(source),
            source.name,
            "Image",
        )
        await plugin.store.link_attachment(event_id, captured)
        preview = await plugin.web_attachment(captured.sha256)
        return captured, preview

    captured, preview = asyncio.run(prepare())
    assert preview["mime_type"] == "image/png"
    assert base64.b64decode(preview["data_url"].split(",", 1)[1]) == image_bytes

    outside = plugin.data_dir / "outside.png"
    outside.write_bytes(image_bytes)
    with sqlite3.connect(plugin.store.db_path) as db:
        db.execute(
            "UPDATE attachments SET stored_path='outside.png' WHERE sha256=?",
            (captured.sha256,),
        )
    rejected = asyncio.run(plugin.web_attachment(captured.sha256))
    assert rejected["status_code"] == 404

    module.request.username = None
    unauthorized = asyncio.run(plugin.web_attachment(captured.sha256))
    assert unauthorized["status_code"] == 401


def test_plugin_gate_requires_private_and_exact_umo(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    context = _FakeContext()
    config = _Config(
        enabled=True,
        umo_whitelist=["default:FriendMessage:10001"],
    )
    plugin = module.KaoyanArchivePlugin(context, config)

    allowed = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
    )
    different = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:100010",
    )
    group = SimpleNamespace(
        is_private_chat=lambda: False,
        unified_msg_origin="default:FriendMessage:10001",
    )
    assert plugin._should_process(allowed) is True
    assert plugin._should_process(different) is False
    assert plugin._should_process(group) is False

    asyncio.run(plugin.initialize())
    assert plugin.store.db_path.exists()


def test_routing_status_reports_profile_that_excludes_plugin(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    umo = "napcat:FriendMessage:10001"
    context = _FakeContext()
    context.runtime_configs[umo] = {"plugin_set": ["another_plugin"]}
    context.astrbot_config_mgr = SimpleNamespace(
        get_conf_info=lambda value: {
            "id": "profile-id",
            "name": "私聊答疑",
            "path": "abconf.json",
        }
    )
    plugin = module.KaoyanArchivePlugin(
        context,
        _Config(enabled=True, umo_whitelist=[umo]),
    )

    status = plugin._routing_statuses()[0]

    assert status["handler_enabled"] is False
    assert status["config_name"] == "私聊答疑"
    assert status["plugin_set"] == ["another_plugin"]
    assert module.PLUGIN_NAME in status["warning"]


def test_routing_status_accepts_wildcard_or_explicit_plugin(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    umo = "napcat:FriendMessage:10001"
    context = _FakeContext()
    plugin = module.KaoyanArchivePlugin(
        context,
        _Config(enabled=True, umo_whitelist=[umo]),
    )

    assert plugin._routing_status(umo)["handler_enabled"] is True

    context.runtime_configs[umo] = {"plugin_set": [module.PLUGIN_NAME]}
    assert plugin._routing_status(umo)["handler_enabled"] is True


def test_normal_message_is_observed_without_interception(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    context = _FakeContext()
    config = _Config(
        enabled=True,
        umo_whitelist=["default:FriendMessage:10001"],
    )
    plugin = module.KaoyanArchivePlugin(context, config)
    extras = {}

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the archive observer must not intercept the event")

    event = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
        message_str="操作系统的进程是什么？",
        message_obj=SimpleNamespace(
            message_id="message-1",
            timestamp=1,
            raw_message={"message": "操作系统的进程是什么？"},
        ),
        get_messages=lambda: [],
        get_sender_id=lambda: "10001",
        get_sender_name=lambda: "student",
        set_extra=lambda key, value: extras.__setitem__(key, value),
        stop_event=forbidden,
        set_result=forbidden,
        clear_result=forbidden,
    )

    asyncio.run(plugin.initialize())
    result = asyncio.run(plugin.capture_private_message(event))
    stats = asyncio.run(plugin.store.stats(umo=event.unified_msg_origin))

    assert result is None
    assert stats["events"] == 1
    assert stats["questions"] == 0
    assert extras[f"{module.PLUGIN_NAME}:analysis"]["kind"] == "question"
    assert len(context.llm_calls) == 1


def test_classifier_outage_creates_visible_pending_repair(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    context = _FakeContext()

    async def unavailable(**kwargs):
        context.llm_calls.append(kwargs)
        raise RuntimeError("provider unavailable")

    context.llm_generate = unavailable
    plugin = module.KaoyanArchivePlugin(
        context,
        _Config(enabled=True, umo_whitelist=["default:FriendMessage:10001"]),
    )
    extras = {}
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
        message_str="解释一下死锁条件",
        message_obj=SimpleNamespace(
            message_id="pending-message",
            timestamp=1,
            raw_message={"message": "解释一下死锁条件"},
        ),
        get_messages=lambda: [],
        get_sender_id=lambda: "10001",
        get_sender_name=lambda: "student",
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )

    asyncio.run(plugin.initialize())
    asyncio.run(plugin.capture_private_message(event))
    repairs = asyncio.run(plugin.web_repairs())
    stats = asyncio.run(plugin.store.stats())

    assert extras[f"{module.PLUGIN_NAME}:analysis"]["kind"] == (
        "pending_classification"
    )
    assert stats["pending_classifications"] == 1
    assert repairs["classifications"][0]["text"] == "解释一下死锁条件"
    assert "provider unavailable" in repairs["classifications"][0]["error"]


def test_page_can_manually_repair_pending_classification(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    plugin = module.KaoyanArchivePlugin(_FakeContext(), _Config())

    async def prepare():
        await plugin.initialize()
        event_id = await plugin.store.add_event(
            umo="default:FriendMessage:10001",
            direction="user",
            platform_message_id="manual-repair",
            parent_event_id=None,
            sender_id="10001",
            sender_name="student",
            kind="pending_classification",
            text="查询昨天的归档",
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
        await plugin.store.record_classification_failure(event_id, "outage")
        return event_id

    event_id = asyncio.run(prepare())

    async def request_json(default=None):
        return {
            "target": "classification",
            "action": "manual_instruction",
            "ids": [str(event_id)],
        }

    module.request.json = request_json
    result = asyncio.run(plugin.web_repairs_action())
    repairs = asyncio.run(plugin.web_repairs())

    assert result["repaired"] == 1
    assert result["errors"] == []
    assert repairs["classifications"] == []
    with sqlite3.connect(plugin.store.db_path) as db:
        revision = db.execute(
            "SELECT kind,source,editor FROM classification_revisions WHERE event_id=?",
            (event_id,),
        ).fetchone()
    assert revision == ("instruction", "manual", "tester")


def test_page_can_attach_unarchived_follow_up_to_existing_question(
    monkeypatch, tmp_path: Path
) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    plugin = module.KaoyanArchivePlugin(_FakeContext(), _Config())
    captured = {}
    scheduled = []

    async def request_json(default=None):
        return {
            "target": "unarchived",
            "action": "attach_existing",
            "ids": ["41", "42"],
            "question_uuid": "existing-question",
        }

    async def attach_unarchived_messages(**kwargs):
        captured.update(kwargs)
        return {
            "question_uuid": kwargs["question_uuid"],
            "message_count": len(kwargs["event_ids"]),
            "event_count": 4,
        }

    module.request.json = request_json
    plugin.store.attach_unarchived_messages = attach_unarchived_messages
    plugin._schedule_archive = lambda question_uuid, notify: scheduled.append(
        (question_uuid, notify)
    )

    result = asyncio.run(plugin.web_repairs_action())

    assert result["saved"] is True
    assert captured == {
        "question_uuid": "existing-question",
        "event_ids": [41, 42],
        "editor": "tester",
    }
    assert scheduled == [("existing-question", False)]


def test_registered_framework_command_skips_classifier(monkeypatch, tmp_path: Path) -> None:
    module = _load_plugin_module(monkeypatch, tmp_path)
    context = _FakeContext()
    plugin = module.KaoyanArchivePlugin(
        context,
        _Config(enabled=True, umo_whitelist=["default:FriendMessage:10001"]),
    )
    extras = {}
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
        # AstrBot removes its configured wake prefix before plugin handlers run.
        message_str="kaoyan status",
        message_obj=SimpleNamespace(
            message_id="command-1",
            timestamp=1,
            raw_message={"message": "/kaoyan status"},
        ),
        get_messages=lambda: [],
        get_sender_id=lambda: "10001",
        get_sender_name=lambda: "student",
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )

    asyncio.run(plugin.initialize())
    asyncio.run(plugin.capture_private_message(event))

    assert context.llm_calls == []
    assert extras[f"{module.PLUGIN_NAME}:analysis"]["kind"] == "instruction"
    assert extras[f"{module.PLUGIN_NAME}:analysis"]["intent"] == (
        "framework-command:status"
    )
