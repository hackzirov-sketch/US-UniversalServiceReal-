from __future__ import annotations

from typing import Any, Literal

from aiogram.types import InlineKeyboardButton, KeyboardButton

from app.core.config import get_settings
from app.services.button_design import get_cached_button_design

ButtonStyle = Literal["primary", "success", "danger"]


def inline_button(
    *,
    text: str,
    callback_data: str | None = None,
    style: ButtonStyle | None = None,
    emoji_key: str | None = None,
    **kwargs: Any,
) -> InlineKeyboardButton:
    label, emoji_id, resolved_style = _presentation(text, emoji_key, style)
    return InlineKeyboardButton(
        text=label,
        callback_data=callback_data,
        style=resolved_style,
        icon_custom_emoji_id=emoji_id,
        **kwargs,
    )


def keyboard_button(
    *,
    text: str,
    style: ButtonStyle | None = None,
    emoji_key: str | None = None,
    **kwargs: Any,
) -> KeyboardButton:
    label, emoji_id, resolved_style = _presentation(text, emoji_key, style)
    return KeyboardButton(
        text=label,
        style=resolved_style,
        icon_custom_emoji_id=emoji_id,
        **kwargs,
    )


def _presentation(
    text: str, emoji_key: str | None, style: ButtonStyle | None
) -> tuple[str, str | None, ButtonStyle | None]:
    if emoji_key is None:
        return text, None, style
    normalized_key = emoji_key.casefold()
    design = get_cached_button_design(normalized_key)
    if design is not None:
        emoji_id = design.custom_emoji_id
        resolved_style = None if design.button_style == "default" else design.button_style
    else:
        emoji_id = get_settings().button_custom_emoji_ids.get(normalized_key)
        resolved_style = style
    if emoji_id is None:
        return text, None, resolved_style
    _icon, separator, label = text.partition(" ")
    return (label if separator and label else text), emoji_id, resolved_style
