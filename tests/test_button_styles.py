from types import SimpleNamespace

import pytest

from app.bot import buttons
from app.bot.admin_management import _permissions_keyboard, admin_management_menu
from app.bot.buttons import inline_button, keyboard_button
from app.bot.menu import main_menu, purchase_confirmation_keyboard
from app.bot.payment_messages import (
    payment_card_menu,
    payment_review_keyboard,
    topup_entry_keyboard,
)
from app.bot.pricing import pricing_menu
from app.bot.production import sales_menu
from app.services.button_design import BUTTON_SPECS, clear_button_design_cache


@pytest.fixture(autouse=True)
def _empty_button_design_cache():
    clear_button_design_cache()
    yield
    clear_button_design_cache()


def _by_callback(markup) -> dict[str, object]:
    return {
        button.callback_data: button
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    }


def test_main_menu_uses_bot_api_94_button_styles() -> None:
    menu = _by_callback(main_menu(is_admin=False, is_superadmin=False))
    assert menu["menu:contest"].style == "danger"
    assert menu["topup:start"].style == "primary"
    assert menu["menu:account"].style == "primary"
    assert menu["menu:stars"].style == "success"
    assert menu["menu:premium"].style == "success"
    assert menu["menu:gifts"].style == "success"
    assert menu["menu:farm"].style == "primary"
    assert menu["menu:rating"].style == "primary"
    assert menu["menu:profile"].style == "primary"


def test_confirmation_rejection_and_cancellation_styles() -> None:
    confirmation = _by_callback(purchase_confirmation_keyboard(back_callback="menu:stars"))
    review = _by_callback(payment_review_keyboard("synthetic-payment-id"))
    topup = _by_callback(topup_entry_keyboard())
    assert confirmation["purchase:confirm"].style == "success"
    assert review["pay:ok:synthetic-payment-id"].style == "success"
    assert review["pay:no:synthetic-payment-id"].style == "danger"
    assert topup["topup:cancel"].style == "danger"


def test_unicode_is_fallback_when_custom_emoji_id_is_missing(monkeypatch) -> None:
    monkeypatch.setattr(
        buttons,
        "get_settings",
        lambda: SimpleNamespace(button_custom_emoji_ids={}),
    )
    button = inline_button(
        text="🎁 Konkurs",
        callback_data="menu:contest",
        style="danger",
        emoji_key="contest",
    )
    assert button.text == "🎁 Konkurs"
    assert button.icon_custom_emoji_id is None


def test_configured_custom_emoji_replaces_unicode_prefix(monkeypatch) -> None:
    monkeypatch.setattr(
        buttons,
        "get_settings",
        lambda: SimpleNamespace(button_custom_emoji_ids={"contest": "5368324170671202286"}),
    )
    button = inline_button(
        text="🎁 Konkurs",
        callback_data="menu:contest",
        style="danger",
        emoji_key="contest",
    )
    assert button.text == "Konkurs"
    assert button.icon_custom_emoji_id == "5368324170671202286"


def test_reply_keyboard_button_supports_same_style_and_icon_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        buttons,
        "get_settings",
        lambda: SimpleNamespace(button_custom_emoji_ids={"confirm": "5368324170671202286"}),
    )
    button = keyboard_button(
        text="✅ Tasdiqlash",
        style="success",
        emoji_key="confirm",
    )
    assert button.text == "Tasdiqlash"
    assert button.style == "success"
    assert button.icon_custom_emoji_id == "5368324170671202286"


def test_admin_second_level_buttons_use_central_design(monkeypatch) -> None:
    emoji_id = "5368324170671202286"
    monkeypatch.setattr(
        buttons,
        "get_settings",
        lambda: SimpleNamespace(button_custom_emoji_ids={key: emoji_id for key in BUTTON_SPECS}),
    )
    markups = (
        admin_management_menu(),
        pricing_menu(),
        sales_menu(),
        _permissions_keyboard(["PAYMENT_REVIEW"], {"PAYMENT_REVIEW"}),
        payment_review_keyboard("synthetic-payment-id"),
        payment_card_menu(card_exists=True, active=True),
    )
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                assert button.icon_custom_emoji_id == emoji_id

    admin_buttons = _by_callback(admin_management_menu())
    assert admin_buttons["adm:add"].style == "success"
    assert admin_buttons["adm:list"].style == "primary"
    assert admin_buttons["adm:blocked"].style == "danger"
