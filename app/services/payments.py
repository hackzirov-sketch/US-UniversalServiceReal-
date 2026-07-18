from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import PaymentStatus
from app.db.models import AdminPermission, Payment, PaymentCard, User
from app.services.audit import write_audit
from app.services.balance import credit_approved_payment

PRIMARY_CARD_KEY = "PRIMARY"
REVIEW_PAYMENTS = "REVIEW_PAYMENTS"
MAX_RECEIPT_BYTES = 10 * 1024 * 1024
ALLOWED_RECEIPT_MIME_TYPES = frozenset({"image/jpeg", "image/png", "application/pdf"})


class PaymentError(ValueError):
    pass


class PaymentCardUnavailableError(PaymentError):
    pass


class PaymentCardAlreadyExistsError(PaymentError):
    pass


class PaymentPermissionError(PaymentError):
    pass


class PaymentValidationError(PaymentError):
    pass


class PaymentSecurityError(PaymentError):
    pass


@dataclass(frozen=True, slots=True)
class PaymentActor:
    telegram_id: int
    is_superadmin: bool
    can_review_payments: bool


@dataclass(frozen=True, slots=True)
class UserPaymentCardView:
    id: str
    formatted_card_number: str = field(repr=False)
    masked_card_number: str
    card_holder_name: str
    min_topup_som: int
    max_topup_som: int
    instructions: str | None


@dataclass(frozen=True, slots=True)
class AdminPaymentCardView:
    id: str
    masked_card_number: str
    card_holder_name: str
    min_topup_som: int
    max_topup_som: int
    bank_name: str | None
    instructions: str | None
    active: bool


class CardCipher:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise PaymentSecurityError("Payment card encryption key is invalid") from exc

    def encrypt(self, card_number: str) -> str:
        normalized = normalize_card_number(card_number)
        return self._fernet.encrypt(normalized.encode("ascii")).decode("ascii")

    def decrypt(self, encrypted_card_number: str) -> str:
        try:
            decrypted = self._fernet.decrypt(encrypted_card_number.encode("ascii"))
            return normalize_card_number(decrypted.decode("ascii"))
        except (InvalidToken, ValueError, UnicodeError) as exc:
            raise PaymentSecurityError("Stored payment card cannot be decrypted") from exc


def normalize_card_number(value: str) -> str:
    digits = re.sub(r"[\s-]", "", value.strip())
    if not digits.isascii() or not digits.isdecimal() or not 13 <= len(digits) <= 19:
        raise PaymentValidationError("Card number must contain 13 to 19 digits")
    if not _luhn_valid(digits):
        raise PaymentValidationError("Card number checksum is invalid")
    return digits


def format_card_number(card_number: str) -> str:
    digits = normalize_card_number(card_number)
    return " ".join(digits[index : index + 4] for index in range(0, len(digits), 4))


def mask_card_number(card_number: str) -> str:
    digits = normalize_card_number(card_number)
    return f"{digits[:4]} **** **** {digits[-4:]}"


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


async def payment_actor(
    session: AsyncSession, *, telegram_id: int, superadmin_ids: frozenset[int]
) -> PaymentActor:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    active_admin = bool(user and user.is_admin)
    if telegram_id in superadmin_ids:
        return PaymentActor(telegram_id, True, active_admin)
    if not active_admin:
        return PaymentActor(telegram_id, False, False)
    permission = await session.scalar(
        select(AdminPermission.id).where(
            AdminPermission.user_id == user.id,
            AdminPermission.permission == REVIEW_PAYMENTS,
        )
    )
    return PaymentActor(telegram_id, False, permission is not None)


async def payment_review_recipients(
    session: AsyncSession, *, superadmin_ids: frozenset[int]
) -> frozenset[int]:
    superadmin_rows = []
    if superadmin_ids:
        superadmin_rows = list(
            await session.scalars(
                select(User.telegram_id).where(
                    User.telegram_id.in_(superadmin_ids),
                    User.is_admin.is_(True),
                    User.admin_active.is_(True),
                )
            )
        )
    reviewers = list(
        await session.scalars(
            select(User.telegram_id)
            .join(AdminPermission, AdminPermission.user_id == User.id)
            .where(
                User.is_admin.is_(True),
                User.admin_active.is_(True),
                AdminPermission.permission == REVIEW_PAYMENTS,
            )
        )
    )
    return frozenset([*superadmin_rows, *reviewers])


async def grant_review_payments(
    session: AsyncSession,
    *,
    target_telegram_id: int,
    actor: PaymentActor,
) -> None:
    _require_superadmin(actor)
    target = await session.scalar(select(User).where(User.telegram_id == target_telegram_id))
    if target is None or not target.is_admin:
        raise PaymentValidationError("Target must be an active admin")
    existing = await session.scalar(
        select(AdminPermission.id).where(
            AdminPermission.user_id == target.id,
            AdminPermission.permission == REVIEW_PAYMENTS,
        )
    )
    if existing is None:
        session.add(
            AdminPermission(
                user_id=target.id,
                permission=REVIEW_PAYMENTS,
                granted_by_telegram_id=actor.telegram_id,
            )
        )
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor.telegram_id),
        action="REVIEW_PAYMENTS_GRANTED",
        entity_type="USER",
        entity_id=target.id,
        metadata={"target_telegram_id": target_telegram_id},
    )


async def create_primary_card(
    session: AsyncSession,
    *,
    card_number: str,
    card_holder_name: str,
    min_topup_som: int,
    max_topup_som: int,
    actor: PaymentActor,
    cipher: CardCipher,
    bank_name: str | None = None,
    instructions: str | None = None,
) -> PaymentCard:
    _require_superadmin(actor)
    number = normalize_card_number(card_number)
    holder = _validate_holder(card_holder_name)
    _validate_limits(min_topup_som, max_topup_som)
    existing = await session.scalar(
        select(PaymentCard.id).where(PaymentCard.singleton_key == PRIMARY_CARD_KEY)
    )
    if existing is not None:
        raise PaymentCardAlreadyExistsError("Primary payment card already exists")
    card = PaymentCard(
        singleton_key=PRIMARY_CARD_KEY,
        bank_name=_optional_text(bank_name, 128),
        card_number_encrypted=cipher.encrypt(number),
        card_number_last4=number[-4:],
        card_holder_name=holder,
        min_topup_som=min_topup_som,
        max_topup_som=max_topup_som,
        instructions=_optional_text(instructions, 1000),
        active=True,
        updated_by_admin_id=actor.telegram_id,
    )
    session.add(card)
    try:
        await session.flush()
    except IntegrityError as exc:
        raise PaymentCardAlreadyExistsError("Primary payment card already exists") from exc
    _audit_card_change(session, card, actor, "PAYMENT_CARD_CREATED")
    return card


async def replace_primary_card_number(
    session: AsyncSession,
    *,
    new_card_number: str,
    actor: PaymentActor,
    cipher: CardCipher,
    confirmed: bool,
) -> PaymentCard:
    _require_superadmin(actor)
    if not confirmed:
        raise PaymentValidationError("Card replacement must be confirmed")
    card = await _locked_primary_card(session)
    number = normalize_card_number(new_card_number)
    old_last4 = card.card_number_last4
    card.card_number_encrypted = cipher.encrypt(number)
    card.card_number_last4 = number[-4:]
    card.updated_by_admin_id = actor.telegram_id
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor.telegram_id),
        action="PAYMENT_CARD_NUMBER_REPLACED",
        entity_type="PAYMENT_CARD",
        entity_id=card.id,
        metadata={"old_last4": old_last4, "new_last4": card.card_number_last4},
    )
    return card


async def update_primary_card_holder(
    session: AsyncSession, *, card_holder_name: str, actor: PaymentActor
) -> PaymentCard:
    _require_superadmin(actor)
    card = await _locked_primary_card(session)
    card.card_holder_name = _validate_holder(card_holder_name)
    card.updated_by_admin_id = actor.telegram_id
    _audit_card_change(session, card, actor, "PAYMENT_CARD_HOLDER_UPDATED")
    return card


async def update_primary_card_limits(
    session: AsyncSession,
    *,
    actor: PaymentActor,
    min_topup_som: int | None = None,
    max_topup_som: int | None = None,
) -> PaymentCard:
    _require_superadmin(actor)
    card = await _locked_primary_card(session)
    minimum = min_topup_som if min_topup_som is not None else card.min_topup_som
    maximum = max_topup_som if max_topup_som is not None else card.max_topup_som
    _validate_limits(minimum, maximum)
    card.min_topup_som = minimum
    card.max_topup_som = maximum
    card.updated_by_admin_id = actor.telegram_id
    _audit_card_change(session, card, actor, "PAYMENT_CARD_LIMITS_UPDATED")
    return card


async def set_primary_card_active(
    session: AsyncSession, *, active: bool, actor: PaymentActor
) -> PaymentCard:
    _require_superadmin(actor)
    card = await _locked_primary_card(session)
    card.active = active
    card.updated_by_admin_id = actor.telegram_id
    _audit_card_change(
        session,
        card,
        actor,
        "PAYMENT_CARD_ACTIVATED" if active else "PAYMENT_CARD_DEACTIVATED",
    )
    return card


async def get_user_payment_card(
    session: AsyncSession, *, cipher: CardCipher
) -> UserPaymentCardView:
    card = await session.scalar(
        select(PaymentCard).where(
            PaymentCard.singleton_key == PRIMARY_CARD_KEY,
            PaymentCard.active.is_(True),
        )
    )
    if card is None:
        raise PaymentCardUnavailableError("Active payment card is unavailable")
    number = cipher.decrypt(card.card_number_encrypted)
    if number[-4:] != card.card_number_last4:
        raise PaymentSecurityError("Stored payment card integrity check failed")
    return UserPaymentCardView(
        id=card.id,
        formatted_card_number=format_card_number(number),
        masked_card_number=mask_card_number(number),
        card_holder_name=card.card_holder_name,
        min_topup_som=card.min_topup_som,
        max_topup_som=card.max_topup_som,
        instructions=card.instructions,
    )


async def get_admin_payment_card(
    session: AsyncSession, *, cipher: CardCipher
) -> AdminPaymentCardView:
    card = await session.scalar(
        select(PaymentCard).where(PaymentCard.singleton_key == PRIMARY_CARD_KEY)
    )
    if card is None:
        raise PaymentCardUnavailableError("Primary payment card is unavailable")
    number = cipher.decrypt(card.card_number_encrypted)
    return AdminPaymentCardView(
        id=card.id,
        masked_card_number=mask_card_number(number),
        card_holder_name=card.card_holder_name,
        min_topup_som=card.min_topup_som,
        max_topup_som=card.max_topup_som,
        bank_name=card.bank_name,
        instructions=card.instructions,
        active=card.active,
    )


async def create_topup_payment(
    session: AsyncSession,
    *,
    user_id: str,
    amount_som: int,
    cipher: CardCipher,
) -> Payment:
    card = await session.scalar(
        select(PaymentCard)
        .where(
            PaymentCard.singleton_key == PRIMARY_CARD_KEY,
            PaymentCard.active.is_(True),
        )
        .with_for_update()
    )
    if card is None:
        raise PaymentCardUnavailableError("Active payment card is unavailable")
    if not card.min_topup_som <= amount_som <= card.max_topup_som:
        raise PaymentValidationError("Top-up amount is outside the configured limits")
    number = cipher.decrypt(card.card_number_encrypted)
    payment = Payment(
        user_id=user_id,
        payment_card_id=card.id,
        amount_som=amount_som,
        card_number_first4_snapshot=number[:4],
        card_number_last4_snapshot=number[-4:],
        card_holder_name_snapshot=card.card_holder_name,
        status=PaymentStatus.AWAITING_RECEIPT.value,
    )
    session.add(payment)
    await session.flush()
    return payment


async def attach_payment_receipt(
    session: AsyncSession,
    *,
    payment_id: str,
    user_id: str,
    file_id: str,
    file_type: str,
    mime_type: str,
    file_size: int,
    file_unique_id: str | None = None,
    checksum: str | None = None,
) -> Payment:
    normalized_type = file_type.upper()
    if mime_type not in ALLOWED_RECEIPT_MIME_TYPES:
        raise PaymentValidationError("Receipt must be a JPEG, PNG or PDF file")
    if normalized_type not in {"PHOTO", "PDF"}:
        raise PaymentValidationError("Receipt type is not allowed")
    if file_size <= 0 or file_size > MAX_RECEIPT_BYTES:
        raise PaymentValidationError("Receipt file is too large")
    payment = await session.scalar(
        select(Payment)
        .where(Payment.id == payment_id, Payment.user_id == user_id)
        .with_for_update()
    )
    allowed_statuses = {PaymentStatus.AWAITING_RECEIPT.value, PaymentStatus.NEEDS_INFO.value}
    if payment is None or payment.status not in allowed_statuses:
        raise PaymentValidationError("Payment is not awaiting a receipt")
    payment.receipt_file_id = file_id
    payment.receipt_file_unique_id = file_unique_id
    payment.receipt_checksum = checksum
    payment.receipt_file_type = normalized_type
    payment.receipt_mime_type = mime_type
    payment.receipt_file_size = file_size
    payment.status = PaymentStatus.REVIEW_PENDING.value
    payment.review_note = None
    payment.submitted_at = datetime.now(UTC)
    write_audit(
        session,
        actor_type="USER",
        actor_id=payment.user_id,
        action="PAYMENT_RECEIPT_SUBMITTED",
        entity_type="PAYMENT",
        entity_id=payment.id,
        metadata={
            "amount_som": payment.amount_som,
            "card_last4": payment.card_number_last4_snapshot,
            "file_type": normalized_type,
        },
    )
    return payment


async def approve_payment(
    session: AsyncSession,
    *,
    payment_id: str,
    actor: PaymentActor,
    approved_amount_som: int | None = None,
) -> Payment:
    _require_reviewer(actor)
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise PaymentValidationError("Payment not found")
    if payment.status == PaymentStatus.APPROVED.value:
        return payment
    if payment.status != PaymentStatus.REVIEW_PENDING.value:
        raise PaymentValidationError("Payment is not pending review")
    approved = approved_amount_som if approved_amount_som is not None else payment.amount_som
    if approved <= 0:
        raise PaymentValidationError("Approved amount must be positive")
    payment.approved_amount_som = approved
    payment.reviewed_by_admin_id = actor.telegram_id
    await credit_approved_payment(
        session,
        payment_id=payment.id,
        reference=f"payment:{payment.id}:approve:v1",
    )
    write_audit(
        session,
        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
        actor_id=str(actor.telegram_id),
        action="PAYMENT_APPROVED",
        entity_type="PAYMENT",
        entity_id=payment.id,
        metadata={
            "requested_amount_som": payment.amount_som,
            "approved_amount_som": approved,
            "card_last4": payment.card_number_last4_snapshot,
        },
    )
    return payment


async def reject_payment(session: AsyncSession, *, payment_id: str, actor: PaymentActor) -> Payment:
    _require_reviewer(actor)
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.status != PaymentStatus.REVIEW_PENDING.value:
        raise PaymentValidationError("Payment is not pending review")
    payment.status = PaymentStatus.REJECTED.value
    payment.reviewed_by_admin_id = actor.telegram_id
    write_audit(
        session,
        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
        actor_id=str(actor.telegram_id),
        action="PAYMENT_REJECTED",
        entity_type="PAYMENT",
        entity_id=payment.id,
        metadata={"card_last4": payment.card_number_last4_snapshot},
    )
    return payment


async def request_payment_info(
    session: AsyncSession, *, payment_id: str, actor: PaymentActor, note: str
) -> Payment:
    _require_reviewer(actor)
    normalized = " ".join(note.strip().split())
    if not normalized or len(normalized) > 500:
        raise PaymentValidationError("Izoh 1-500 belgi bo‘lishi kerak")
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.status != PaymentStatus.REVIEW_PENDING.value:
        raise PaymentValidationError("Payment is not pending review")
    payment.status = PaymentStatus.NEEDS_INFO.value
    payment.review_note = normalized
    payment.reviewed_by_admin_id = actor.telegram_id
    write_audit(
        session,
        actor_type="SUPERADMIN" if actor.is_superadmin else "ADMIN",
        actor_id=str(actor.telegram_id),
        action="PAYMENT_INFO_REQUESTED",
        entity_type="PAYMENT",
        entity_id=payment.id,
        metadata={"card_last4": payment.card_number_last4_snapshot},
        reason=normalized,
    )
    return payment


async def _locked_primary_card(session: AsyncSession) -> PaymentCard:
    card = await session.scalar(
        select(PaymentCard).where(PaymentCard.singleton_key == PRIMARY_CARD_KEY).with_for_update()
    )
    if card is None:
        raise PaymentCardUnavailableError("Primary payment card is unavailable")
    return card


def _require_superadmin(actor: PaymentActor) -> None:
    if not actor.is_superadmin:
        raise PaymentPermissionError("Only a superadmin can change the primary payment card")


def _require_reviewer(actor: PaymentActor) -> None:
    if not actor.can_review_payments:
        raise PaymentPermissionError("REVIEW_PAYMENTS permission is required")


def _validate_holder(value: str) -> str:
    holder = " ".join(value.split())
    if not 3 <= len(holder) <= 128:
        raise PaymentValidationError("Card holder name is invalid")
    return holder


def _validate_limits(minimum: int, maximum: int) -> None:
    if minimum <= 0 or maximum < minimum:
        raise PaymentValidationError("Payment card limits are invalid")


def _optional_text(value: str | None, maximum: int) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if len(normalized) > maximum:
        raise PaymentValidationError("Text value is too long")
    return normalized


def _audit_card_change(
    session: AsyncSession,
    card: PaymentCard,
    actor: PaymentActor,
    action: str,
) -> None:
    write_audit(
        session,
        actor_type="SUPERADMIN",
        actor_id=str(actor.telegram_id),
        action=action,
        entity_type="PAYMENT_CARD",
        entity_id=card.id,
        metadata={
            "card_last4": card.card_number_last4,
            "min_topup_som": card.min_topup_som,
            "max_topup_som": card.max_topup_som,
            "active": card.active,
        },
    )
