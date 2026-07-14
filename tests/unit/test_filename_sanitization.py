"""Unit tests for filename sanitization in DocumentUploadRequest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.api.schemas.document import DocumentUploadRequest

_VALID_SHA256 = "a" * 64
_VALID_MIME = "application/pdf"
_VALID_SIZE = 1024
_VALID_COLLECTION = "00000000-0000-0000-0000-000000000001"


def _make_request(filename: str) -> DocumentUploadRequest:
    return DocumentUploadRequest(
        collection_id=_VALID_COLLECTION,
        filename=filename,
        mime_type=_VALID_MIME,
        size_bytes=_VALID_SIZE,
        sha256=_VALID_SHA256,
    )


def test_path_traversal_stripped() -> None:
    req = _make_request("../../etc/passwd")
    assert req.filename == "passwd"


def test_null_byte_removed() -> None:
    req = _make_request("file\x00name.pdf")
    assert req.filename == "filename.pdf"


def test_dot_raises() -> None:
    with pytest.raises(ValidationError):
        _make_request(".")


def test_dotdot_raises() -> None:
    with pytest.raises(ValidationError):
        _make_request("..")


def test_unsafe_chars_replaced() -> None:
    req = _make_request("file<script>.pdf")
    assert req.filename == "file_script_.pdf"


def test_normal_filename_unchanged() -> None:
    req = _make_request("my-document.pdf")
    assert req.filename == "my-document.pdf"


def test_windows_path_traversal_stripped() -> None:
    req = _make_request("..\\..\\Windows\\System32\\config")
    assert req.filename == "config"


def test_control_char_removed() -> None:
    req = _make_request("file\x1fname.pdf")
    assert req.filename == "filename.pdf"
