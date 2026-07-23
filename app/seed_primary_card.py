from __future__ import annotations

import asyncio
import base64
import json
import os
import re

from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import PaymentCard
from app.db.session import session_factory
from app.services.payments import (
    CardCipher,
    PaymentActor,
    create_primary_card,
    replace_primary_card_number,
    set_primary_card_active,
    update_primary_card_holder,
)


async def seed_primary_card() -> None:
    settings = get_settings()
    if settings.payment_card_encryption_key is None:
        raise RuntimeError("PAYMENT_CARD_ENCRYPTION_KEY is required")
    if not settings.superadmin_ids:
        raise RuntimeError("At least one SUPERADMIN_ID is required")
    encoded_payload = os.environ.get("PRIMARY_CARD_PAYLOAD_B64", "")
    if encoded_payload:
        try:
            payload = json.loads(base64.b64decode(encoded_payload).decode("utf-8"))
            card_number_input = str(payload["number"])
            card_holder = str(payload["holder"])
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Primary card payload is invalid") from exc
    else:
        card_number_input = os.environ.get("PRIMARY_CARD_NUMBER", "")
        card_holder = os.environ.get("PRIMARY_CARD_HOLDER", "")
    card_number = re.sub(r"\D", "", card_number_input)
    if not card_number or not card_holder:
        raise RuntimeError("Primary card input is required")
    actor_id = min(settings.superadmin_ids)
    actor = PaymentActor(actor_id, True, True)
    cipher = CardCipher(settings.payment_card_encryption_key.get_secret_value())
    async with session_factory.begin() as session:
        existing = await session.scalar(
            select(PaymentCard.id).where(PaymentCard.singleton_key == "PRIMARY")
        )
        if existing is None:
            await create_primary_card(
                session,
                card_number=card_number,
                card_holder_name=card_holder,
                min_topup_som=5_000,
                max_topup_som=2_000_000,
                actor=actor,
                cipher=cipher,
            )
        else:
            await replace_primary_card_number(
                session,
                new_card_number=card_number,
                actor=actor,
                cipher=cipher,
                confirmed=True,
            )
            await update_primary_card_holder(
                session,
                card_holder_name=card_holder,
                actor=actor,
            )
            await set_primary_card_active(session, active=True, actor=actor)
    print("Primary payment card stored securely")


if __name__ == "__main__":
    asyncio.run(seed_primary_card())
