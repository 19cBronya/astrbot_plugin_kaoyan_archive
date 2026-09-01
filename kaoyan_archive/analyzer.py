from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class MessageKind(str, Enum):
    QUESTION = "question"
    BOUNDARY = "boundary"
    COMMAND = "command"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    kind: MessageKind
    body_text: str
    matched_rule: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind.value,
            "body_text": self.body_text,
            "matched_rule": self.matched_rule,
        }


class MessageAnalyzer:
    _negative_boundary = re.compile(
        r"(?:还|尚|没|没有|并没|并没有|不是|不算|暂时不).{0,6}(?:问完|结束|整理|归档)"
    )
    _question_boundary = re.compile(r"(?:问完|结束|归档).{0,3}[吗嘛么？?]$")
    _space = re.compile(r"\s+")

    @classmethod
    def _normalize(cls, text: str) -> str:
        return cls._space.sub("", text).strip().lower()

    def analyze(
        self,
        text: str,
        *,
        end_phrases: list[str],
        command_prefixes: list[str],
        control_phrases: list[str],
        has_attachment: bool = False,
    ) -> AnalysisResult:
        stripped = text.strip()
        normalized = self._normalize(stripped)
        if not stripped and not has_attachment:
            return AnalysisResult(MessageKind.EMPTY, "")

        if any(stripped.startswith(prefix) for prefix in command_prefixes if prefix):
            return AnalysisResult(MessageKind.COMMAND, "", "command-prefix")
        if any(normalized.startswith(self._normalize(p)) for p in control_phrases if p):
            return AnalysisResult(MessageKind.COMMAND, "", "control-phrase")

        if normalized and not self._negative_boundary.search(normalized):
            if not self._question_boundary.search(normalized):
                for phrase in sorted(end_phrases, key=len, reverse=True):
                    normalized_phrase = self._normalize(phrase)
                    if normalized_phrase and normalized_phrase in normalized:
                        body = re.sub(re.escape(phrase), "", stripped, count=1).strip(
                            " ，,。.!！;；\n\t"
                        )
                        return AnalysisResult(MessageKind.BOUNDARY, body, phrase)

        body = stripped if stripped else "[附件消息]"
        return AnalysisResult(MessageKind.QUESTION, body)
