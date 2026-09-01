from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from kaoyan_archive.attachments import AttachmentStore


def test_attachment_is_content_addressed_and_deduplicated(tmp_path: Path) -> None:
    source = tmp_path / "题图.png"
    payload = b"not-a-real-png-but-stable"
    source.write_bytes(payload)
    store = AttachmentStore(tmp_path / "data" / "attachments", max_file_bytes=1024)

    first = store._capture_sync(str(source), source.name, "Image")
    second = store._capture_sync(str(source), "renamed.png", "Image")

    assert first.sha256 == hashlib.sha256(payload).hexdigest()
    assert first.sha256 == second.sha256
    assert first.stored_path == second.stored_path
    assert (tmp_path / "data" / first.stored_path).read_bytes() == payload


def test_attachment_rejects_oversized_file_and_symlink(tmp_path: Path) -> None:
    source = tmp_path / "large.bin"
    source.write_bytes(b"12345")
    store = AttachmentStore(tmp_path / "attachments", max_file_bytes=4)

    with pytest.raises(ValueError, match="size limit"):
        store._capture_sync(str(source), source.name, "File")

    link = tmp_path / "link.bin"
    link.symlink_to(source)
    with pytest.raises(ValueError, match="symbolic-link"):
        store._capture_sync(str(link), link.name, "File")
