from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl


class TelegramAuthError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    telegram_id: int
    username: str | None
    full_name: str
    auth_date: datetime
    init_data_hash: str


def verify_init_data(
    init_data: str,
    *,
    bot_token: str,
    max_age_seconds: int,
    now: datetime | None = None,
) -> TelegramIdentity:
    if not init_data or len(init_data) > 8192:
        raise TelegramAuthError("Telegram ma’lumoti topilmadi")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    supplied_hash = pairs.pop("hash", "")
    if len(supplied_hash) != 64:
        raise TelegramAuthError("Telegram imzosi noto‘g‘ri")
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied_hash):
        raise TelegramAuthError("Telegram imzosi noto‘g‘ri")
    try:
        auth_date = datetime.fromtimestamp(int(pairs["auth_date"]), tz=UTC)
        user_data = json.loads(pairs["user"])
        telegram_id = int(user_data["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise TelegramAuthError("Telegram foydalanuvchi ma’lumoti noto‘g‘ri") from exc
    current = now or datetime.now(UTC)
    age = (current - auth_date).total_seconds()
    if age < -30 or age > max_age_seconds:
        raise TelegramAuthError("Telegram sessiyasi eskirgan")
    if telegram_id <= 0:
        raise TelegramAuthError("Telegram ID noto‘g‘ri")
    username = user_data.get("username")
    first = str(user_data.get("first_name") or "").strip()
    last = str(user_data.get("last_name") or "").strip()
    return TelegramIdentity(
        telegram_id=telegram_id,
        username=str(username)[:32] if username else None,
        full_name=" ".join(part for part in (first, last) if part)[:128],
        auth_date=auth_date,
        init_data_hash=hashlib.sha256(init_data.encode()).hexdigest(),
    )
