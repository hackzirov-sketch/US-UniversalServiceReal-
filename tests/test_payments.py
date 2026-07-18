from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from app.bot.payment_messages import (
    admin_payment_message,
    receipt_request_message,
    topup_amount_prompt,
    user_topup_card_message,
)
from app.core.security import sanitize
from app.db.enums import LedgerType, PaymentStatus
from app.db.models import AuditLog, LedgerEntry, Payment, PaymentCard, User
from app.services.payments import (
    CardCipher,
    PaymentActor,
    PaymentCardAlreadyExistsError,
    PaymentCardUnavailableError,
    PaymentPermissionError,
    approve_payment,
    attach_payment_receipt,
    create_primary_card,
    create_topup_payment,
    format_card_number,
    get_user_payment_card,
    replace_primary_card_number,
    set_primary_card_active,
)


def synthetic_pan(prefix: str = "4") -> str:
    body = prefix + ("0" * 14)
    for check_digit in range(10):
        candidate = body + str(check_digit)
        digits = [int(character) for character in candidate]
        checksum = 0
        parity = len(digits) % 2
        for index, value in enumerate(digits):
            if index % 2 == parity:
                value *= 2
                if value > 9:
                    value -= 9
            checksum += value
        if checksum % 10 == 0:
            return candidate
    raise AssertionError("Unable to generate a synthetic PAN")


@pytest.fixture
def cipher() -> CardCipher:
    return CardCipher(Fernet.generate_key().decode("ascii"))


def superadmin() -> PaymentActor:
    return PaymentActor(telegram_id=90001, is_superadmin=True, can_review_payments=True)


def ordinary_admin() -> PaymentActor:
    return PaymentActor(telegram_id=80001, is_superadmin=False, can_review_payments=True)


async def save_primary_card(session, cipher: CardCipher, *, pan: str | None = None):
    return await create_primary_card(
        session,
        card_number=pan or synthetic_pan(),
        card_holder_name="Test Card Holder",
        min_topup_som=5_000,
        max_topup_som=2_000_000,
        actor=superadmin(),
        cipher=cipher,
    )


async def save_user(session, *, balance: int = 0) -> User:
    user = User(telegram_id=10001, available_balance_som=balance)
    session.add(user)
    await session.flush()
    return user


@pytest.mark.asyncio
async def test_only_one_primary_card_is_created(sessions, cipher) -> None:
    async with sessions.begin() as session:
        card = await save_primary_card(session, cipher)
        assert card.singleton_key == "PRIMARY"
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentCard)) == 1


@pytest.mark.asyncio
async def test_second_primary_card_is_blocked(sessions, cipher) -> None:
    async with sessions.begin() as session:
        await save_primary_card(session, cipher)
    with pytest.raises(PaymentCardAlreadyExistsError):
        async with sessions.begin() as session:
            await save_primary_card(session, cipher, pan=synthetic_pan("5"))


@pytest.mark.asyncio
async def test_active_card_is_shown_to_user(sessions, cipher) -> None:
    pan = synthetic_pan()
    async with sessions.begin() as session:
        await save_primary_card(session, cipher, pan=pan)
    async with sessions() as session:
        view = await get_user_payment_card(session, cipher=cipher)
        stored = await session.scalar(select(PaymentCard))
    message = user_topup_card_message(view)
    assert pan not in stored.card_number_encrypted
    assert format_card_number(pan) in message
    assert "Test Card Holder" in message


@pytest.mark.asyncio
async def test_amount_is_requested_before_full_card_is_shown(sessions, cipher) -> None:
    pan = synthetic_pan()
    async with sessions.begin() as session:
        await save_primary_card(session, cipher, pan=pan)
        user = await save_user(session)
        view = await get_user_payment_card(session, cipher=cipher)
        payment = await create_topup_payment(
            session,
            user_id=user.id,
            amount_som=22_222,
            cipher=cipher,
        )
    amount_prompt = topup_amount_prompt(view)
    transfer_message = receipt_request_message(
        payment,
        full_card_number=view.formatted_card_number,
    )
    assert format_card_number(pan) not in amount_prompt
    assert format_card_number(pan) in transfer_message
    assert "22 222 so‘m" in transfer_message


@pytest.mark.asyncio
async def test_inactive_card_blocks_topup(sessions, cipher) -> None:
    async with sessions.begin() as session:
        await save_primary_card(session, cipher)
        await set_primary_card_active(session, active=False, actor=superadmin())
    with pytest.raises(PaymentCardUnavailableError):
        async with sessions() as session:
            await get_user_payment_card(session, cipher=cipher)


def test_full_card_number_is_redacted_from_logs() -> None:
    pan = synthetic_pan()
    formatted = format_card_number(pan)
    sanitized = sanitize({"event": f"card received: {formatted}"})
    assert formatted not in sanitized["event"]
    assert sanitized["event"].endswith(f"{pan[:4]} **** **** {pan[-4:]}")


@pytest.mark.asyncio
async def test_admin_message_contains_only_masked_card(sessions, cipher) -> None:
    pan = synthetic_pan()
    async with sessions.begin() as session:
        await save_primary_card(session, cipher, pan=pan)
        user = await save_user(session)
        payment = await create_topup_payment(
            session, user_id=user.id, amount_som=50_000, cipher=cipher
        )
    message = admin_payment_message(payment, user_telegram_id=user.telegram_id)
    assert format_card_number(pan) not in message
    assert f"{pan[:4]} **** **** {pan[-4:]}" in message


@pytest.mark.asyncio
async def test_unauthorized_admin_cannot_replace_card(sessions, cipher) -> None:
    async with sessions.begin() as session:
        await save_primary_card(session, cipher)
    with pytest.raises(PaymentPermissionError):
        async with sessions.begin() as session:
            await replace_primary_card_number(
                session,
                new_card_number=synthetic_pan("5"),
                actor=ordinary_admin(),
                cipher=cipher,
                confirmed=True,
            )


@pytest.mark.asyncio
async def test_superadmin_can_replace_card_without_auditing_pan(sessions, cipher) -> None:
    new_pan = synthetic_pan("5")
    async with sessions.begin() as session:
        await save_primary_card(session, cipher)
        card = await replace_primary_card_number(
            session,
            new_card_number=new_pan,
            actor=superadmin(),
            cipher=cipher,
            confirmed=True,
        )
        assert card.card_number_last4 == new_pan[-4:]
    async with sessions() as session:
        audit = await session.scalar(
            select(AuditLog).where(AuditLog.action == "PAYMENT_CARD_NUMBER_REPLACED")
        )
        assert new_pan not in str(audit.sanitized_metadata)
        assert audit.sanitized_metadata["new_last4"] == new_pan[-4:]


@pytest.mark.asyncio
async def test_card_replacement_does_not_change_old_payment_snapshot(sessions, cipher) -> None:
    old_pan = synthetic_pan()
    new_pan = synthetic_pan("5")
    async with sessions.begin() as session:
        await save_primary_card(session, cipher, pan=old_pan)
        user = await save_user(session)
        payment = await create_topup_payment(
            session, user_id=user.id, amount_som=50_000, cipher=cipher
        )
        payment_id = payment.id
        await replace_primary_card_number(
            session,
            new_card_number=new_pan,
            actor=superadmin(),
            cipher=cipher,
            confirmed=True,
        )
    async with sessions() as session:
        historical = await session.get(Payment, payment_id)
        assert historical.card_number_first4_snapshot == old_pan[:4]
        assert historical.card_number_last4_snapshot == old_pan[-4:]


@pytest.mark.asyncio
async def test_receipt_approval_credits_balance_only_once(sessions, cipher) -> None:
    async with sessions.begin() as session:
        await save_primary_card(session, cipher)
        user = await save_user(session, balance=100)
        payment = await create_topup_payment(
            session, user_id=user.id, amount_som=50_000, cipher=cipher
        )
        await attach_payment_receipt(
            session,
            payment_id=payment.id,
            user_id=user.id,
            file_id="synthetic-telegram-file-id",
            file_type="PDF",
            mime_type="application/pdf",
            file_size=1024,
        )
        payment_id = payment.id
        user_id = user.id
    async with sessions.begin() as session:
        await approve_payment(session, payment_id=payment_id, actor=superadmin())
    async with sessions.begin() as session:
        payment = await approve_payment(session, payment_id=payment_id, actor=superadmin())
        assert payment.status == PaymentStatus.APPROVED.value
    async with sessions() as session:
        user = await session.get(User, user_id)
        credits = await session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.type == LedgerType.PAYMENT_CREDIT)
        )
        assert user.available_balance_som == 50_100
        assert credits == 1
