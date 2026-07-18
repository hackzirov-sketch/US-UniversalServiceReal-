from __future__ import annotations

import asyncio
import os

from app.core.config import get_settings
from app.db.session import session_factory
from app.services.payments import CardCipher, PaymentActor, create_primary_card


async def seed_primary_card() -> None:
    settings = get_settings()
    if settings.payment_card_encryption_key is None:
        raise RuntimeError("PAYMENT_CARD_ENCRYPTION_KEY is required")
    if not settings.superadmin_ids:
        raise RuntimeError("At least one SUPERADMIN_ID is required")
    card_number = os.environ.get("PRIMARY_CARD_NUMBER", "")
    card_holder = os.environ.get("PRIMARY_CARD_HOLDER", "")
    if not card_number or not card_holder:
        raise RuntimeError("Primary card input is required")
    actor_id = min(settings.superadmin_ids)
    actor = PaymentActor(actor_id, True, True)
    cipher = CardCipher(settings.payment_card_encryption_key.get_secret_value())
    async with session_factory.begin() as session:
        await create_primary_card(
            session,
            card_number=card_number,
            card_holder_name=card_holder,
            min_topup_som=5_000,
            max_topup_som=2_000_000,
            actor=actor,
            cipher=cipher,
        )
    print("Primary payment card created securely")


if __name__ == "__main__":
    asyncio.run(seed_primary_card())
