from types import SimpleNamespace

import pytest
from aiogram.enums import MessageEntityType
from sqlalchemy import select

from app.bot.button_design_admin import extract_custom_emoji_id
from app.bot.buttons import inline_button
from app.db.models import AuditLog, ButtonDesign
from app.services.button_design import (
    ButtonDesignError,
    clear_button_design_cache,
    get_cached_button_design,
    load_button_design_cache,
    reset_button_design,
    save_button_design,
)


@pytest.fixture(autouse=True)
def _clean_design_cache():
    clear_button_design_cache()
    yield
    clear_button_design_cache()


@pytest.mark.asyncio
async def test_design_is_saved_loaded_and_reset_with_audit(sessions) -> None:
    async with sessions.begin() as session:
        row = await save_button_design(
            session,
            button_key="contest",
            button_style="primary",
            custom_emoji_id="5368324170671202286",
            actor_telegram_id=1001,
        )
        assert row.button_text
        assert row.unicode_emoji_fallback

    async with sessions() as session:
        stored = await session.get(ButtonDesign, "contest")
        assert stored is not None
        assert stored.button_style == "primary"
        assert stored.custom_emoji_id == "5368324170671202286"
        assert stored.updated_by == 1001
        assert await session.scalar(
            select(AuditLog).where(AuditLog.action == "BUTTON_DESIGN_UPDATED")
        )

        clear_button_design_cache()
        assert await load_button_design_cache(session) == 1
        assert get_cached_button_design("CONTEST") is not None

    async with sessions.begin() as session:
        await reset_button_design(
            session,
            button_key="contest",
            actor_telegram_id=1001,
        )

    async with sessions() as session:
        assert await session.get(ButtonDesign, "contest") is None
        assert await session.scalar(
            select(AuditLog).where(AuditLog.action == "BUTTON_DESIGN_RESET")
        )
    assert get_cached_button_design("contest") is None


@pytest.mark.asyncio
async def test_database_design_overrides_factory_style_and_icon(sessions) -> None:
    async with sessions.begin() as session:
        await save_button_design(
            session,
            button_key="contest",
            button_style="success",
            custom_emoji_id="5368324170671202286",
            actor_telegram_id=1001,
        )

    button = inline_button(
        text="🎁 Konkurs",
        callback_data="menu:contest",
        style="danger",
        emoji_key="contest",
    )
    assert button.text == "Konkurs"
    assert button.style == "success"
    assert button.icon_custom_emoji_id == "5368324170671202286"


@pytest.mark.asyncio
async def test_invalid_style_and_custom_emoji_id_are_rejected(sessions) -> None:
    async with sessions.begin() as session:
        with pytest.raises(ButtonDesignError):
            await save_button_design(
                session,
                button_key="contest",
                button_style="warning",
                custom_emoji_id=None,
                actor_telegram_id=1001,
            )
        with pytest.raises(ButtonDesignError):
            await save_button_design(
                session,
                button_key="contest",
                button_style="primary",
                custom_emoji_id="not-an-id",
                actor_telegram_id=1001,
            )


def test_exactly_one_custom_emoji_is_extracted() -> None:
    custom = SimpleNamespace(
        type=MessageEntityType.CUSTOM_EMOJI,
        custom_emoji_id="5368324170671202286",
    )
    plain = SimpleNamespace(type=MessageEntityType.BOLD, custom_emoji_id=None)
    assert (
        extract_custom_emoji_id(SimpleNamespace(entities=[custom], caption_entities=None))
        == "5368324170671202286"
    )
    assert extract_custom_emoji_id(SimpleNamespace(entities=[plain], caption_entities=None)) is None
    assert (
        extract_custom_emoji_id(SimpleNamespace(entities=[custom, custom], caption_entities=None))
        is None
    )
