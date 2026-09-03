from __future__ import annotations

import asyncio

from kaoyan_archive.provider_fallback import (
    call_with_provider_fallback,
    configured_fallback_provider_ids,
)


class ProviderContext:
    def __init__(self) -> None:
        self.lookups = 0

    async def get_current_chat_provider_id(self, umo: str) -> str:
        self.lookups += 1
        return "umo-provider"


def test_all_ordered_backups_are_tried_before_umo_provider() -> None:
    async def scenario():
        context = ProviderContext()
        calls = []

        async def operation(provider_id: str) -> str:
            calls.append(provider_id)
            if provider_id != "umo-provider":
                raise RuntimeError(f"{provider_id} unavailable")
            return "ok"

        result, provider_id, failures = await call_with_provider_fallback(
            context=context,
            config={
                "operation_provider": "primary",
                "fallback_provider_ids": ["backup-one", "backup-two"],
            },
            primary_key="operation_provider",
            umo="default:FriendMessage:1",
            operation=operation,
        )
        return result, provider_id, failures, calls, context.lookups

    result, provider_id, failures, calls, lookups = asyncio.run(scenario())

    assert result == "ok"
    assert provider_id == "umo-provider"
    assert calls == ["primary", "backup-one", "backup-two", "umo-provider"]
    assert [failure.provider_id for failure in failures] == [
        "primary",
        "backup-one",
        "backup-two",
    ]
    assert lookups == 1


def test_fallback_list_deduplicates_and_keeps_legacy_single_value() -> None:
    result = configured_fallback_provider_ids(
        {
            "fallback_provider_ids": [" backup-one ", "", "backup-one", "backup-two"],
            "fallback_provider_id": "legacy-backup",
        }
    )

    assert result == ["backup-one", "backup-two", "legacy-backup"]
