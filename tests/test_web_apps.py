import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.db.enums import FarmPlotState
from app.db.models import FarmPlot, FarmReward, TelegramAuthReplay, User
from app.services.farm import FarmError, get_farm, harvest, plant, reverse_reward, water
from app.web import create_app
from app.web.common import auth as web_auth
from app.web.common.telegram_auth import TelegramAuthError, verify_init_data
from app.web.user.api import QuoteBody

TEST_TOKEN = "synthetic-telegram-token"  # noqa: S105 - inert HMAC test fixture


def _init_data(token: str, *, telegram_id: int = 42, auth_date: datetime | None = None) -> str:
    timestamp = int((auth_date or datetime.now(UTC)).timestamp())
    fields = {
        "auth_date": str(timestamp),
        "query_id": "AA-test",
        "user": json.dumps(
            {"id": telegram_id, "first_name": "Ahmad", "username": "ahmad_test"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_valid_telegram_init_data_is_verified() -> None:
    identity = verify_init_data(
        _init_data(TEST_TOKEN),
        bot_token=TEST_TOKEN,
        max_age_seconds=300,
    )
    assert identity.telegram_id == 42
    assert identity.username == "ahmad_test"
    assert identity.full_name == "Ahmad"


def test_invalid_or_expired_telegram_init_data_is_rejected() -> None:
    with pytest.raises(TelegramAuthError):
        verify_init_data(
            _init_data(TEST_TOKEN),
            bot_token=TEST_TOKEN + "-wrong",
            max_age_seconds=300,
        )
    with pytest.raises(TelegramAuthError, match="eskirgan"):
        verify_init_data(
            _init_data(TEST_TOKEN, auth_date=datetime.now(UTC) - timedelta(minutes=10)),
            bot_token=TEST_TOKEN,
            max_age_seconds=300,
        )


def test_raw_init_data_is_not_in_auth_error() -> None:
    raw = "user=highly-sensitive-payload&hash=invalid"
    with pytest.raises(TelegramAuthError) as exc:
        verify_init_data(raw, bot_token=TEST_TOKEN, max_age_seconds=300)
    assert raw not in str(exc.value)
    assert "highly-sensitive-payload" not in str(exc.value)


def test_flask_user_and_admin_routes_are_distinct() -> None:
    client = create_app().test_client()
    user = client.get("/app/farm")
    admin = client.get("/admin/payments")
    missing = client.get("/admin/not-a-page")
    assert user.status_code == 200
    assert b"Pixel ferma" in user.data
    assert b"admin.css" in admin.data
    assert b"user.css" not in admin.data
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_farm_timer_and_duplicate_harvest_are_server_controlled(sessions) -> None:
    async with sessions.begin() as session:
        user = User(telegram_id=9911)
        session.add(user)
        await session.flush()
        user_id = user.id
        await get_farm(session, user_id=user_id)
        await plant(session, user_id=user_id, slot=0)
        await water(session, user_id=user_id, slot=0)

    async with sessions.begin() as session:
        with pytest.raises(FarmError, match="hali tayyor emas"):
            await harvest(session, user_id=user_id, slot=0)
        plot = await session.scalar(
            select(FarmPlot).where(FarmPlot.user_id == user_id, FarmPlot.slot == 0)
        )
        plot.ready_at = datetime.now(UTC) - timedelta(seconds=1)

    async with sessions.begin() as session:
        assert await harvest(session, user_id=user_id, slot=0) == 10

    async with sessions.begin() as session:
        with pytest.raises(FarmError):
            await harvest(session, user_id=user_id, slot=0)
        plot = await session.scalar(
            select(FarmPlot).where(FarmPlot.user_id == user_id, FarmPlot.slot == 0)
        )
        assert plot.state == FarmPlotState.EMPTY
        user = await session.get(User, user_id)
        assert user.farm_points == 10


@pytest.mark.asyncio
async def test_farm_reward_reversal_is_idempotent(sessions) -> None:
    async with sessions.begin() as session:
        user = User(telegram_id=9912)
        session.add(user)
        await session.flush()
        user_id = user.id
        await get_farm(session, user_id=user_id)
        await plant(session, user_id=user_id, slot=0)
        await water(session, user_id=user_id, slot=0)
        plot = await session.scalar(
            select(FarmPlot).where(FarmPlot.user_id == user_id, FarmPlot.slot == 0)
        )
        plot.ready_at = datetime.now(UTC) - timedelta(seconds=1)
        await harvest(session, user_id=user_id, slot=0)
        reward = await session.scalar(select(FarmReward).where(FarmReward.user_id == user_id))
        reward_id = reward.id

    async with sessions.begin() as session:
        await reverse_reward(session, reward_id=reward_id, actor_telegram_id=1)
        await reverse_reward(session, reward_id=reward_id, actor_telegram_id=1)

    async with sessions() as session:
        user = await session.get(User, user_id)
        assert user.farm_points == 0
        assert user.ranking_points == 0


def test_quote_rejects_client_supplied_price() -> None:
    with pytest.raises(ValueError):
        QuoteBody.model_validate(
            {"price_id": "server-price-id", "quantity": 100, "sale_price_som": 1}
        )


@pytest.mark.asyncio
async def test_replayed_init_data_hash_is_rejected_by_database(sessions) -> None:
    expires = datetime.now(UTC) + timedelta(minutes=5)
    async with sessions.begin() as session:
        session.add(
            TelegramAuthReplay(init_data_hash="same-hash", telegram_id=1, expires_at=expires)
        )
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(
                TelegramAuthReplay(init_data_hash="same-hash", telegram_id=1, expires_at=expires)
            )


@pytest.mark.asyncio
async def test_user_and_admin_without_permission_receive_403(monkeypatch) -> None:
    request = Request({"type": "http", "method": "GET", "path": "/admin", "headers": []})

    async def ordinary_user(_request):
        return SimpleNamespace(
            user=SimpleNamespace(is_admin=False, admin_active=True),
            is_superadmin=False,
            permissions=frozenset(),
        )

    monkeypatch.setattr(web_auth, "current_session", ordinary_user)
    with pytest.raises(Exception) as ordinary_error:
        await web_auth.require_admin(request)
    assert ordinary_error.value.status_code == 403

    async def restricted_admin(_request):
        return SimpleNamespace(
            user=SimpleNamespace(is_admin=True, admin_active=True),
            is_superadmin=False,
            permissions=frozenset(),
        )

    monkeypatch.setattr(web_auth, "current_session", restricted_admin)
    with pytest.raises(Exception) as permission_error:
        await web_auth.require_admin(request, "MANAGE_PRICING")
    assert permission_error.value.status_code == 403
