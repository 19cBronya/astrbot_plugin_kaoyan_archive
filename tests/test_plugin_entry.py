from __future__ import annotations

import asyncio
import importlib.util
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

    def register_web_api(self, route, handler, methods, description) -> None:
        self.routes.append((route, handler, methods, description))

    async def get_current_chat_provider_id(self, umo: str) -> str:
        return "classifier-provider"

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

    assert len(context.routes) == 5
    assert {route for route, *_ in context.routes} == {
        f"/{module.PLUGIN_NAME}/stats",
        f"/{module.PLUGIN_NAME}/config",
        f"/{module.PLUGIN_NAME}/questions",
        f"/{module.PLUGIN_NAME}/questions/<question_uuid>",
        f"/{module.PLUGIN_NAME}/questions/<question_uuid>/action",
    }
    event = SimpleNamespace(
        is_private_chat=lambda: True,
        unified_msg_origin="default:FriendMessage:10001",
    )
    assert plugin._should_process(event) is False


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
