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
    """Try operation provider, shared backup, then the UMO's current provider."""
    configured = [
        str(config.get(primary_key, "") or "").strip(),
        str(config.get("fallback_provider_id", "") or "").strip(),
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


def format_provider_failures(failures: list[ProviderFailure] | tuple[ProviderFailure, ...]) -> str:
    return "；".join(failure.describe() for failure in failures)


def _failure(provider_id: str, exc: Exception) -> ProviderFailure:
    return ProviderFailure(
        provider_id=provider_id,
        error_type=type(exc).__name__,
        message=str(exc).replace("\n", " ")[:240],
    )
