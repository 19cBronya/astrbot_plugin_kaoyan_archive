from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CapturedAttachment:
    sha256: str
    size: int
    mime_type: str
    stored_path: str
    original_name: str
    component_type: str


class AttachmentStore:
    def __init__(self, root: Path, *, max_file_bytes: int) -> None:
        self.root = root
        self.max_file_bytes = max(1, max_file_bytes)

    async def capture(
        self,
        *,
        source: str,
        original_name: str,
        component_type: str,
    ) -> CapturedAttachment:
        return await asyncio.to_thread(
            self._capture_sync,
            source,
            original_name,
            component_type,
        )

    def _capture_sync(
        self,
        source: str,
        original_name: str,
        component_type: str,
    ) -> CapturedAttachment:
        path = Path(source)
        if path.is_symlink():
            raise ValueError("symbolic-link attachments are not accepted")
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("attachment is not a regular file")
        if info.st_size > self.max_file_bytes:
            raise ValueError("attachment exceeds configured size limit")

        digest = hashlib.sha256()
        self.root.mkdir(parents=True, exist_ok=True)
        temp_fd, temp_name = tempfile.mkstemp(prefix="capture-", dir=self.root)
        size = 0
        try:
            with os.fdopen(temp_fd, "wb") as target, resolved.open("rb") as source_file:
                while chunk := source_file.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_file_bytes:
                        raise ValueError("attachment exceeds configured size limit")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())

            sha256 = digest.hexdigest()
            suffix = Path(original_name).suffix.lower()[:16]
            target_dir = self.root / sha256[:2] / sha256[2:4]
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / f"{sha256}{suffix}"
            if target_path.exists():
                Path(temp_name).unlink(missing_ok=True)
            else:
                shutil.move(temp_name, target_path)
                target_path.chmod(0o600)
            mime_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
            return CapturedAttachment(
                sha256=sha256,
                size=size,
                mime_type=mime_type,
                stored_path=str(target_path.relative_to(self.root.parent)),
                original_name=Path(original_name).name[:255] or "file",
                component_type=component_type,
            )
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
