"""Shared helpers for the high-altitude probe calibration release service."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re

MAX_ARTIFACT_BYTES = 64 * 1024  # 64 KiB of decoded candidate bytes
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArtifactError(ValueError):
    """Raised when the submitted artifact is invalid."""


def validate_release_id(release_id: str) -> str:
    if not isinstance(release_id, str) or not ID_PATTERN.match(release_id):
        raise ArtifactError(
            "发布标识非法：须为 1-128 个字符，字母/数字开头，可含 . _ -"
        )
    return release_id


def decode_artifact(b64_text: str) -> bytes:
    """Decode a Base64 artifact, enforcing canonical encoding and the 64 KiB limit."""
    if not isinstance(b64_text, str):
        raise ArtifactError("工件必须是 Base64 字符串")
    if b64_text == "":
        raise ArtifactError("工件为空：请提交 Base64 编码的标定包")
    # Reject whitespace/newlines so the input must be a single canonical token.
    if any(ch in b64_text for ch in " \t\r\n"):
        raise ArtifactError("非法 Base64：不得包含空白字符")
    try:
        raw = base64.b64decode(b64_text, validate=True)
    except (binascii.Error, ValueError):
        raise ArtifactError("非法 Base64：无法解码为标定包字节")
    # Canonical re-encode check: reject "lenient" inputs (wrong padding etc.
    # already covered by validate=True; this also catches stray variants).
    if base64.b64encode(raw).decode("ascii") != b64_text:
        raise ArtifactError("非法 Base64：编码不是规范形式")
    if len(raw) == 0:
        raise ArtifactError("工件为空：解码后标定包为 0 字节")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ArtifactError(
            f"工件超限：解码后为 {len(raw)} 字节，不得超过 {MAX_ARTIFACT_BYTES} 字节 (64 KiB)"
        )
    return raw


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_operation_key(master_secret: str, release_id: str) -> str:
    """Derive the registry-side operation key from a release identifier.

    The same release id always derives the same key (idempotent replay), while
    different release ids derive different keys.
    """
    return hmac.new(
        master_secret.encode("utf-8"),
        release_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
