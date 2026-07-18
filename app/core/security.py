from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_FRAGMENTS = (
    "api_key",
    "key",
    "authorization",
    "token",
    "secret",
    "card_number",
    "cvv",
    "pin",
    "sms_code",
    "bank_password",
)
MASK = "***REDACTED***"
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|token|secret)\s*[:=]\s*[^\s,;]+"
)
PAN_CANDIDATE = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def _luhn_valid(digits: str) -> bool:
    checksum = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        value = int(character)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        checksum += value
    return checksum % 10 == 0


def redact_card_numbers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not 13 <= len(digits) <= 19 or not _luhn_valid(digits):
            return match.group(0)
        return f"{digits[:4]} **** **** {digits[-4:]}"

    return PAN_CANDIDATE.sub(replace, text)


def is_sensitive_key(key: object) -> bool:
    normalized = str(key).casefold().replace("-", "_")
    return any(fragment in normalized for fragment in SENSITIVE_FRAGMENTS)


def sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): MASK if is_sensitive_key(key) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        redacted = SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={MASK}", value)
        return redact_card_numbers(redacted)
    return value


def is_superadmin(telegram_id: int, configured_ids: frozenset[int]) -> bool:
    return telegram_id in configured_ids
