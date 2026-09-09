"""Plain filesystem Workbench directory for one account."""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import shutil
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.logging_config import get_logger
from core.platform import get_data_dir
from services.memory.dir import account_root, safe_account_id

logger = get_logger(__name__)

_TEXT_EXTENSIONS = {
    ".csv",
    ".html",
    ".json",
    ".log",
    ".md",
    ".rtf",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_WORKBENCH_FILE_BYTES = 50 * 1024 * 1024
MAX_WORKBENCH_UPLOAD_BYTES = MAX_WORKBENCH_FILE_BYTES
MAX_WORKBENCH_TOTAL_BYTES = 250 * 1024 * 1024
MAX_WORKBENCH_FILE_COUNT = 500
_MAX_INLINE_EXPORT_BYTES = 5 * 1024 * 1024
_MAX_WORKBENCH_FILENAME_CHARS = 180
_WINDOWS_RESERVED_BASENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_WINDOWS_FORBIDDEN_FILENAME_CHARS = frozenset('<>:"|?*')
_BLOCKED_ACTIVE_EXTENSIONS = frozenset(
    {
        ".ade",
        ".adp",
        ".app",
        ".apk",
        ".bat",
        ".cmd",
        ".com",
        ".cpl",
        ".dll",
        ".dmg",
        ".exe",
        ".hta",
        ".html",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".msi",
        ".msp",
        ".pif",
        ".ps1",
        ".reg",
        ".scr",
        ".sh",
        ".svg",
        ".vb",
        ".vbe",
        ".vbs",
        ".ws",
        ".wsf",
        ".wsh",
    }
)
_IMAGE_MAGIC_PREFIXES = {
    ".gif": (b"GIF87a", b"GIF89a"),
    ".jpeg": (b"\xff\xd8\xff",),
    ".jpg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
}


@dataclass(frozen=True)
class WorkbenchFile:
    name: str
    size: int
    modified_at: str
    mime: str


class WorkbenchValidationError(ValueError):
    """Raised when a Workbench upload fails fail-closed validation."""


def _looks_like_webp(content: bytes) -> bool:
    return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"


def sanitize_workbench_filename(name: str) -> str:
    raw = str(name or "")
    if not raw.strip():
        raise WorkbenchValidationError("filename is required")
    normalized = unicodedata.normalize("NFC", raw)
    if normalized != normalized.strip():
        raise WorkbenchValidationError("filename must not start or end with whitespace")
    if "/" in normalized or "\\" in normalized:
        raise WorkbenchValidationError("filename must not contain path separators")
    if any(ch in _WINDOWS_FORBIDDEN_FILENAME_CHARS for ch in normalized):
        raise WorkbenchValidationError("filename contains characters reserved by Windows")
    if normalized in {".", ".."} or Path(normalized).is_absolute():
        raise WorkbenchValidationError("filename must be a plain filename")
    if normalized.endswith(".") or normalized.endswith(" "):
        raise WorkbenchValidationError("filename must not end with a space or dot")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in normalized):
        raise WorkbenchValidationError("filename contains disallowed control characters")
    if len(normalized) > _MAX_WORKBENCH_FILENAME_CHARS:
        raise WorkbenchValidationError("filename is too long")
    reserved_name = normalized.split(".", 1)[0].upper()
    if reserved_name in _WINDOWS_RESERVED_BASENAMES:
        raise WorkbenchValidationError("filename is reserved by Windows")
    if Path(normalized).suffix.lower() in _BLOCKED_ACTIVE_EXTENSIONS:
        raise WorkbenchValidationError("file type is not accepted for Workbench uploads")
    return normalized


def _safe_filename(name: str) -> str:
    return sanitize_workbench_filename(name)


def _filename_key(name: str) -> str:
    return unicodedata.normalize("NFKC", _safe_filename(name)).casefold()


def validate_workbench_upload(name: str, content: bytes) -> str:
    filename = sanitize_workbench_filename(name)
    limit = min(MAX_WORKBENCH_FILE_BYTES, MAX_WORKBENCH_UPLOAD_BYTES)
    size = len(content)
    if size > limit:
        raise WorkbenchValidationError("Workbench uploads may be at most %d MB." % (limit // (1024 * 1024)))
    if content.startswith(b"MZ"):
        raise WorkbenchValidationError("Windows executables are not accepted")
    suffix = Path(filename).suffix.lower()
    prefixes = _IMAGE_MAGIC_PREFIXES.get(suffix)
    if prefixes is not None and content and not any(content.startswith(prefix) for prefix in prefixes):
        raise WorkbenchValidationError("file content does not match image extension")
    if suffix == ".webp" and content and not _looks_like_webp(content):
        raise WorkbenchValidationError("file content does not match image extension")
    return filename


class WorkbenchDir:
    """Manage files the user explicitly wants Viola to check first."""

    def __init__(self, user_id: str, root: Path | None = None) -> None:
        self.user_id = safe_account_id(user_id)
        base = Path(root) if root is not None else get_data_dir()
        self.account_dir = account_root(user_id, base)
        self.root = self.account_dir / "workbench"
        self.root.mkdir(parents=True, exist_ok=True)
        self._migrate_knowledge_folder()

    def _migrate_knowledge_folder(self) -> None:
        old_root = self.account_dir / "knowledge"
        if not old_root.exists() or old_root.resolve() == self.root.resolve():
            return
        if not old_root.is_dir():
            return
        moved = 0
        for item in sorted(old_root.iterdir()):
            if item.is_symlink() or not item.is_file():
                logger.debug("Skipping non-regular legacy knowledge item during migration: %s", item)
                continue
            try:
                size = item.stat().st_size
                if size > MAX_WORKBENCH_FILE_BYTES:
                    raise WorkbenchValidationError("legacy knowledge item exceeds Workbench file size limit")
                content = item.read_bytes()
                filename = validate_workbench_upload(item.name, content)
                target = self._resolve_safe(filename)
                if target.exists() or self._equivalent_path(filename) is not None:
                    target = self._dedupe_path(target)
                self._enforce_quota(target, size)
            except (OSError, WorkbenchValidationError):
                logger.warning("Skipping unsafe legacy knowledge item during Workbench migration: %s", item)
                continue
            if target.exists():
                target = self._dedupe_path(target)
            shutil.move(str(item), str(target))
            moved += 1
        try:
            old_root.rmdir()
        except OSError:
            logger.debug("Old knowledge folder not empty after workbench migration: %s", old_root)
        if moved:
            logger.info("Migrated %d file(s) from knowledge folder to workbench for user %s", moved, self.user_id)

    def _resolve(self, name: str) -> Path:
        filename = _safe_filename(name)
        return self._resolve_safe(filename)

    def _resolve_safe(self, filename: str) -> Path:
        candidate = self.root / filename
        if candidate.is_symlink():
            raise WorkbenchValidationError("Workbench files must be regular files")
        path = candidate.resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("workbench path must stay inside the account workbench directory") from exc
        return path

    def normalize_name(self, name: str) -> str:
        return _safe_filename(name)

    def _equivalent_path(self, filename: str) -> Path | None:
        key = _filename_key(filename)
        for existing in self.root.iterdir():
            try:
                if existing.is_symlink() or not existing.is_file():
                    continue
                if _filename_key(existing.name) == key:
                    return existing
            except (OSError, WorkbenchValidationError):
                logger.debug("Skipping non-canonical workbench entry for user %s: %s", self.user_id, existing)
        return None

    def _row_for_path(self, path: Path) -> dict[str, Any]:
        stat = path.stat()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return WorkbenchFile(
            name=path.name,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            mime=mime,
        ).__dict__

    def find(self, name: str) -> dict[str, Any] | None:
        filename = _safe_filename(name)
        path = self._equivalent_path(filename)
        if path is None:
            return None
        return self._row_for_path(path)

    def _regular_files(self) -> list[Path]:
        files: list[Path] = []
        for path in self.root.iterdir():
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                files.append(path)
            except OSError:
                logger.debug("Skipping unreadable workbench entry for user %s: %s", self.user_id, path)
        return files

    def _enforce_quota(self, target: Path, incoming_size: int) -> None:
        files = self._regular_files()
        existing_size = 0
        if target.exists() and not target.is_symlink():
            try:
                existing_size = target.stat().st_size
            except OSError:
                existing_size = 0
        if not target.exists() and len(files) >= MAX_WORKBENCH_FILE_COUNT:
            raise WorkbenchValidationError("Workbench file count limit reached")
        total_size = sum(path.stat().st_size for path in files) - existing_size + incoming_size
        if total_size > MAX_WORKBENCH_TOTAL_BYTES:
            raise WorkbenchValidationError(
                "Workbench storage may be at most %d MB." % (MAX_WORKBENCH_TOTAL_BYTES // (1024 * 1024))
            )

    def list(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self._regular_files(), key=lambda item: item.stat().st_mtime, reverse=True):
            rows.append(self._row_for_path(path))
        return rows

    def add(self, name: str, content: bytes, *, replace: bool = True) -> dict[str, Any]:
        filename = validate_workbench_upload(name, content)
        path = self._resolve_safe(filename)
        equivalent = self._equivalent_path(filename)
        if equivalent is not None:
            if replace:
                if equivalent.resolve() != path.resolve():
                    equivalent.unlink()
            else:
                path = self._dedupe_path(path)
        if path.exists() and not replace:
            path = self._dedupe_path(path)
        if path.exists() and path.is_symlink():
            raise WorkbenchValidationError("Workbench uploads cannot replace symlinks")
        if path.exists() and not path.is_file():
            raise WorkbenchValidationError("Workbench uploads cannot replace non-file entries")
        self._enforce_quota(path, len(content))
        path.write_bytes(content)
        stat = path.stat()
        return {
            "ok": True,
            "name": path.name,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            "mime": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        }

    def _dedupe_path(self, path: Path) -> Path:
        stem = path.stem
        suffix = path.suffix
        for index in range(2, 1000):
            candidate = path.with_name("%s-%d%s" % (stem, index, suffix))
            if not candidate.exists() and self._equivalent_path(candidate.name) is None:
                return candidate
        raise ValueError("too many duplicate filenames")

    def remove(self, name: str) -> bool:
        path = self._equivalent_path(_safe_filename(name)) or self._resolve(name)
        if not path.exists():
            return False
        path.unlink()
        return True

    def path_for(self, name: str) -> Path:
        path = self._equivalent_path(_safe_filename(name)) or self._resolve(name)
        if not path.exists():
            raise FileNotFoundError(name)
        if path.is_symlink() or not path.is_file():
            raise WorkbenchValidationError("Workbench files must be regular files")
        return path

    def search(self, query: str) -> list[dict[str, Any]]:
        needle = (query or "").strip().lower()
        tokens = [token for token in re.findall(r"[A-Za-z0-9_'-]+", needle) if len(token) > 1]
        results: list[dict[str, Any]] = []
        for item in self.list():
            name = str(item["name"])
            lower_name = name.lower()
            snippet = ""
            matched = bool(needle and needle in lower_name) or any(token in lower_name for token in tokens)
            if not matched:
                snippet = self._text_snippet(self.root / name, tokens)
                matched = bool(snippet)
            if matched:
                results.append({"file": name, "snippet": snippet, **item})
        return results

    def _text_snippet(self, path: Path, tokens: list[str]) -> str:
        if path.suffix.lower() not in _TEXT_EXTENSIONS:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        if not tokens:
            return " ".join(text.split())[:240]
        lower = text.lower()
        positions = [lower.find(token) for token in tokens if lower.find(token) >= 0]
        if not positions:
            return ""
        start = max(0, min(positions) - 80)
        end = min(len(text), start + 260)
        return " ".join(text[start:end].split())

    def export_records(self, *, max_inline_bytes: int = _MAX_INLINE_EXPORT_BYTES) -> list[dict[str, Any]]:
        """Return a GDPR-exportable snapshot of user workbench files."""
        records: list[dict[str, Any]] = []
        for item in self.list():
            path = self.path_for(str(item["name"]))
            data = path.read_bytes()
            record: dict[str, Any] = {
                "name": item["name"],
                "size": item["size"],
                "modified_at": item["modified_at"],
                "mime": item["mime"],
                "sha256": hashlib.sha256(data).hexdigest(),
                "content_exported": False,
            }
            if len(data) <= max(0, int(max_inline_bytes)):
                if path.suffix.lower() in _TEXT_EXTENSIONS:
                    record["encoding"] = "utf-8"
                    record["text"] = data.decode("utf-8", errors="replace")
                else:
                    record["encoding"] = "base64"
                    record["content_base64"] = base64.b64encode(data).decode("ascii")
                record["content_exported"] = True
            else:
                record["content_omitted_reason"] = "file_exceeds_inline_export_limit"
            records.append(record)
        return records

    def delete_all_for_user(self) -> int:
        """Remove all files in this user's workbench directory."""
        root = self.root.resolve()
        account_dir = self.account_dir.resolve()
        try:
            root.relative_to(account_dir)
        except ValueError as exc:
            raise ValueError("workbench root must stay inside the account directory") from exc
        if not root.exists():
            return 0
        count = sum(1 for item in root.rglob("*") if item.is_file())
        shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        return count


def get_workbench_dir(user_id: str, root: Path | None = None) -> WorkbenchDir:
    return WorkbenchDir(user_id=user_id, root=root)


__all__ = [
    "MAX_WORKBENCH_FILE_BYTES",
    "MAX_WORKBENCH_FILE_COUNT",
    "MAX_WORKBENCH_TOTAL_BYTES",
    "MAX_WORKBENCH_UPLOAD_BYTES",
    "WorkbenchDir",
    "WorkbenchFile",
    "WorkbenchValidationError",
    "get_workbench_dir",
    "sanitize_workbench_filename",
    "validate_workbench_upload",
]
