# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal, verification-free JWT payload reading (we only need `id` and `exp`).

The token is issued and verified by the server; we just read it to learn the
user id and when it expires.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime


class InvalidToken(ValueError):
    pass


@dataclass(frozen=True)
class TokenInfo:
    user_id: int
    issued_at: datetime | None
    expires_at: datetime | None


def normalise_token(raw: str) -> str:
    """Accept a bare token or a pasted 'JWT xxx' / 'Bearer xxx' header value."""
    token = raw.strip()
    for prefix in ("Authorization:", "JWT ", "Bearer ", "jwt ", "bearer "):
        if token.startswith(prefix):
            token = token[len(prefix):].strip()
    return token


def decode(token: str) -> TokenInfo:
    parts = normalise_token(token).split(".")
    if len(parts) != 3:
        raise InvalidToken("not a JWT (expected three dot-separated parts)")
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (binascii.Error, ValueError) as err:
        raise InvalidToken("JWT payload is not valid base64 JSON") from err
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
        raise InvalidToken("JWT payload has no numeric 'id'")

    def ts(key: str) -> datetime | None:
        v = payload.get(key)
        return datetime.fromtimestamp(v, UTC) if isinstance(v, (int, float)) else None

    return TokenInfo(user_id=payload["id"], issued_at=ts("iat"), expires_at=ts("exp"))
