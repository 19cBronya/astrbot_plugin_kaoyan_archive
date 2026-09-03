from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    provider_id: str
    error_type: str
    message: str

    def describe(self) -> str:
        detail = f": {self.message}" if self.message else ""
        return f"{self.provider_id}({self.error_type}{detail})"


class ProviderFallbackExhausted(RuntimeError):
    def __init__(self, failures: list[ProviderFailure]):
        self.failures = tuple(failures)
        super().__init__(format_provider_failures(failures) or "no provider available")


async def call_with_provider_fallback(
    *,
    context: Any,
    config: Any,
    primary_key: str,
    umo: str,
    operation: Callable[[str], Awaitable[T]],
) -> tuple[T, str, list[ProviderFailure]]:
    """Try operation provider, ordered shared backups, then the UMO provider."""
    configured = [
        str(config.get(primary_key, "") or "").strip(),
        *configured_fallback_provider_ids(config),
    ]
    attempted: set[str] = set()
    failures: list[ProviderFailure] = []

    for provider_id in configured:
        if not provider_id or provider_id in attempted:
            continue
        attempted.add(provider_id)
        try:
            return await operation(provider_id), provider_id, failures
        except Exception as exc:
            failures.append(_failure(provider_id, exc))

    try:
        current_provider_id = str(
            await context.get_current_chat_provider_id(umo) or ""
        ).strip()
    except Exception as exc:
        failures.append(_failure("UMO当前Provider", exc))
        raise ProviderFallbackExhausted(failures) from exc

    if not current_provider_id:
        failures.append(_failure("UMO当前Provider", ValueError("未配置")))
    elif current_provider_id not in attempted:
        try:
            return await operation(current_provider_id), current_provider_id, failures
        except Exception as exc:
            failures.append(_failure(current_provider_id, exc))

    raise ProviderFallbackExhausted(failures)


def configured_fallback_provider_ids(config: Any) -> list[str]:
    """Return ordered fallback IDs while accepting the pre-v0.8 single value."""
    values: list[str] = []
    configured = config.get("fallback_provider_ids", [])
    if isinstance(configured, list):
        values.extend(str(item).strip() for item in configured)
    elif configured:
        values.append(str(configured).strip())

    legacy = config.get("fallback_provider_id", "")
    if isinstance(legacy, list):
        values.extend(str(item).strip() for item in legacy)
    elif legacy:
        values.append(str(legacy).strip())

    result: list[str] = []
    seen: set[str] = set()
    for provider_id in values:
        if provider_id and provider_id not in seen:
            seen.add(provider_id)
            result.append(provider_id)
    return result


def format_provider_failures(failures: list[ProviderFailure] | tuple[ProviderFailure, ...]) -> str:
    return "；".join(failure.describe() for failure in failures)


def _failure(provider_id: str, exc: Exception) -> ProviderFailure:
    return ProviderFailure(
        provider_id=provider_id,
        error_type=type(exc).__name__,
        message=str(exc).replace("\n", " ")[:240],
    )
