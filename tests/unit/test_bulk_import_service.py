"""Unit tests for BulkImportService — path traversal, ZIP bomb, file count limit."""

from __future__ import annotations

import io
import zipfile

import pytest

from src.domain.bulk_import_service import (
    _ZIP_BOMB_RATIO,
    _check_path_traversal,
    _check_zip_bomb,
    _sanitize_filename,
)

# ── Path traversal ──────────────────────────────────────────────────────────


def test_path_traversal_relative_safe() -> None:
    assert _check_path_traversal("documents/report.pdf") is True


def test_path_traversal_filename_only_safe() -> None:
    assert _check_path_traversal("report.pdf") is True


def test_path_traversal_absolute_rejected() -> None:
    assert _check_path_traversal("/etc/passwd") is False


def test_path_traversal_dotdot_rejected() -> None:
    assert _check_path_traversal("../../etc/passwd") is False


def test_path_traversal_dotdot_in_middle_rejected() -> None:
    assert _check_path_traversal("foo/../../../etc/shadow") is False


def test_path_traversal_backslash_absolute_rejected() -> None:
    assert _check_path_traversal("\\windows\\system32\\bad.dll") is False


# ── Filename sanitization ───────────────────────────────────────────────────


def test_sanitize_strips_directory_components() -> None:
    assert _sanitize_filename("documents/subdir/report.pdf") == "report.pdf"


def test_sanitize_strips_absolute_path() -> None:
    assert _sanitize_filename("/etc/passwd") == "passwd"


def test_sanitize_plain_filename_unchanged() -> None:
    assert _sanitize_filename("report.pdf") == "report.pdf"


def test_sanitize_replaces_forward_slash() -> None:
    # After Path.name stripping, any remaining slash would be replaced
    result = _sanitize_filename("a/b/c.txt")
    assert "/" not in result


# ── ZIP bomb detection ──────────────────────────────────────────────────────


def _make_zip_with_ratio(
    compressed_size: int, uncompressed_size: int
) -> tuple[zipfile.ZipFile, int]:
    """Create a ZipFile mock that reports given sizes via infolist()."""

    class _FakeInfo:
        def __init__(self, size: int) -> None:
            self.file_size = size
            self.is_dir = lambda: False

    class _FakeZip:
        def infolist(self) -> list:
            return [_FakeInfo(uncompressed_size)]

    return _FakeZip(), compressed_size  # type: ignore[return-value]


def test_zip_bomb_rejected_when_ratio_exceeded() -> None:
    zf, compressed = _make_zip_with_ratio(
        compressed_size=1024,
        uncompressed_size=1024 * (_ZIP_BOMB_RATIO + 1),
    )
    from src.core.exceptions import DomainValidationError

    with pytest.raises(DomainValidationError, match="ZIP bomb"):
        _check_zip_bomb(zf, compressed)  # type: ignore[arg-type]


def test_zip_bomb_accepted_when_ratio_ok() -> None:
    zf, compressed = _make_zip_with_ratio(
        compressed_size=1024,
        uncompressed_size=1024 * (_ZIP_BOMB_RATIO - 1),
    )
    # Should not raise
    _check_zip_bomb(zf, compressed)  # type: ignore[arg-type]


def test_zip_bomb_zero_compressed_size_skipped() -> None:
    """Zero compressed size (e.g. empty archive) should not raise ZeroDivisionError."""
    zf, _ = _make_zip_with_ratio(0, 0)
    _check_zip_bomb(zf, 0)  # type: ignore[arg-type]


# ── File count limit ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_count_limit_raises_error() -> None:
    """ZIP with more than MAX_FILES_PER_IMPORT files should raise DomainValidationError."""
    from unittest.mock import AsyncMock, MagicMock

    from src.core.exceptions import DomainValidationError
    from src.domain.bulk_import_service import _MAX_FILES_PER_IMPORT, BulkImportService

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(_MAX_FILES_PER_IMPORT + 1):
            zf.writestr(f"file_{i}.txt", f"content {i}")
    archive_bytes = buf.getvalue()

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))
    )
    session.flush = AsyncMock()

    svc = BulkImportService(session)
    with pytest.raises(DomainValidationError, match="maximum"):
        import uuid

        await svc.create_zip_import(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), archive_bytes)


@pytest.mark.asyncio
async def test_invalid_zip_raises_error() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from src.core.exceptions import DomainValidationError
    from src.domain.bulk_import_service import BulkImportService

    session = MagicMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=MagicMock()))
    )
    session.flush = AsyncMock()

    svc = BulkImportService(session)
    with pytest.raises(DomainValidationError, match="Invalid ZIP"):
        import uuid

        await svc.create_zip_import(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), b"not a zip file")
