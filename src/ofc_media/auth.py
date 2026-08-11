from __future__ import annotations

import hashlib
import hmac


def token_digest(token: str, pepper: str) -> str:
    return hashlib.sha256((pepper + token).encode("utf-8")).hexdigest()


def token_matches(token: str, pepper: str, expected: str) -> bool:
    return bool(token and expected) and hmac.compare_digest(
        token_digest(token, pepper), expected
    )


def internal_token_matches(supplied: str, expected: str) -> bool:
    return bool(supplied and expected) and hmac.compare_digest(supplied, expected)
