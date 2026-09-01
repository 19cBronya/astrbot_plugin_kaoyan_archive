from kaoyan_archive.analyzer import MessageAnalyzer, MessageKind


def analyze(text: str):
    return MessageAnalyzer().analyze(
        text,
        end_phrases=["我问完了", "问完了", "整理入库"],
        command_prefixes=["/", "!"],
        control_phrases=["查询历史", "删除题目"],
    )


def test_exact_boundary_is_detected() -> None:
    result = analyze("我问完了")
    assert result.kind is MessageKind.BOUNDARY
    assert result.body_text == ""


def test_negated_boundary_is_not_detected() -> None:
    for text in ("我还没问完", "不是问完了，我还有问题", "暂时不整理归档"):
        assert analyze(text).kind is MessageKind.QUESTION


def test_boundary_question_is_not_detected() -> None:
    assert analyze("我问完了吗？").kind is MessageKind.QUESTION


def test_mixed_content_keeps_non_boundary_text() -> None:
    result = analyze("最后补充：这里为什么是 O(n)？我问完了")
    assert result.kind is MessageKind.BOUNDARY
    assert "O(n)" in result.body_text
    assert "问完" not in result.body_text


def test_commands_and_control_messages_are_excluded() -> None:
    assert analyze("/kaoyan status").kind is MessageKind.COMMAND
    assert analyze("查询历史题目").kind is MessageKind.COMMAND
