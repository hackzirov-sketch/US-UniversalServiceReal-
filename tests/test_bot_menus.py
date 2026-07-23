from types import SimpleNamespace

from app.bot.menu import (
    USER_MENU_ROWS,
    _purchase_total,
    account_menu,
    can_open_admin_menu,
    farm_menu,
    gift_menu,
    insufficient_balance_keyboard,
    main_menu,
    premium_menu,
    purchase_confirmation_keyboard,
    simple_internal_keyboard,
    stars_menu,
)
from app.db.enums import ServiceType


def _button_texts(markup) -> list[str]:
    rows = markup.keyboard if hasattr(markup, "keyboard") else markup.inline_keyboard
    return [button.text for row in rows for button in row]


def _callback_data(markup) -> list[str]:
    return [button.callback_data or "" for row in markup.inline_keyboard for button in row]


def _gift(*, active: bool = True):
    return SimpleNamespace(
        id="gift-alpha",
        display_name="Juda uzun nomli Telegram sovg‘asi",
        service_type=ServiceType.GIFT,
        active=active,
        sale_price_som=2_929,
    )


def test_customer_main_menu_uses_two_columns() -> None:
    menu = main_menu(is_admin=False, is_superadmin=False)
    assert all(len(row) == 2 for row in menu.inline_keyboard[1:])
    assert (
        tuple(tuple(button.text for button in row) for row in menu.inline_keyboard)
        == USER_MENU_ROWS
    )


def test_contest_button_is_full_width() -> None:
    menu = main_menu(is_admin=False, is_superadmin=False)
    assert len(menu.inline_keyboard[0]) == 1
    assert menu.inline_keyboard[0][0].text == "🎁 Konkursda ishtirok etish"


def test_no_menu_contains_selling_button() -> None:
    menus = [
        main_menu(is_admin=False, is_superadmin=False),
        main_menu(is_admin=True, is_superadmin=False),
        main_menu(is_admin=True, is_superadmin=True),
        stars_menu(),
        premium_menu(),
        gift_menu([_gift()]),
        account_menu(),
        farm_menu(),
    ]
    assert all("sotish" not in text.casefold() for menu in menus for text in _button_texts(menu))


def test_stars_menu_is_purchase_only() -> None:
    texts = _button_texts(stars_menu())
    assert texts[:5] == [
        "⭐ 50 Stars",
        "⭐ 75 Stars",
        "⭐ 100 Stars",
        "⭐ 150 Stars",
        "⭐ 250 Stars",
    ]
    assert all("sotish" not in text.casefold() for text in texts)


def test_premium_menu_is_purchase_only() -> None:
    texts = _button_texts(premium_menu())
    assert texts[:3] == ["💎 3 oy", "💎 6 oy", "💎 12 oy"]
    assert all("sotish" not in text.casefold() for text in texts)


def test_gift_menu_is_purchase_only_and_filters_inactive_items() -> None:
    texts = _button_texts(gift_menu([_gift(), _gift(active=False)]))
    gift_buttons = [text for text in texts if text.startswith("🎁")]
    assert len(gift_buttons) == 1
    assert "2 929 so‘m" in gift_buttons[0]
    assert "…" in gift_buttons[0]
    assert all("sotish" not in text.casefold() for text in texts)


def test_every_internal_menu_has_back_and_home() -> None:
    menus = [
        stars_menu(),
        premium_menu(),
        gift_menu([_gift()]),
        account_menu(),
        farm_menu(),
        purchase_confirmation_keyboard(back_callback="menu:stars"),
        simple_internal_keyboard(),
    ]
    for menu in menus:
        final_row = [button.text for button in menu.inline_keyboard[-1]]
        assert final_row == ["◀️ Orqaga", "🏠 Bosh menyu"]


def test_unauthorized_user_cannot_open_admin_menu() -> None:
    assert not can_open_admin_menu(is_admin=False, is_superadmin=False)
    customer_texts = _button_texts(main_menu(is_admin=False, is_superadmin=False))
    assert "📊 Provider" not in customer_texts
    assert "💳 Asosiy karta" not in customer_texts


def test_callback_data_contains_actions_or_ids_but_no_secrets() -> None:
    menus = [
        stars_menu(),
        premium_menu(),
        gift_menu([_gift()]),
        account_menu(),
        farm_menu(),
        purchase_confirmation_keyboard(back_callback="menu:stars"),
    ]
    callbacks = [value for menu in menus for value in _callback_data(menu)]
    forbidden = ("api_key", "token", "secret", "card_number", "5614", "6285")
    assert callbacks
    assert all(":" in value for value in callbacks)
    assert all(word not in value.casefold() for value in callbacks for word in forbidden)


def test_insufficient_balance_offers_only_topup_action() -> None:
    keyboard = insufficient_balance_keyboard()
    assert _callback_data(keyboard) == ["topup:start"]
    assert "Balansni to‘ldirish" in _button_texts(keyboard)[0]


def test_purchase_total_uses_quantity_only_for_stars() -> None:
    stars = SimpleNamespace(active=True, service_type=ServiceType.STARS, sale_price_som=210)
    premium = SimpleNamespace(active=True, service_type=ServiceType.PREMIUM, sale_price_som=165_000)
    assert _purchase_total(stars, {"quantity": 100}) == 21_000
    assert _purchase_total(premium, {"months": 3}) == 165_000


def test_admin_menus_do_not_offer_balance_topup() -> None:
    for menu in (
        main_menu(is_admin=True, is_superadmin=False),
        main_menu(is_admin=True, is_superadmin=True),
    ):
        assert all("to‘ldirish" not in text for text in _button_texts(menu))
