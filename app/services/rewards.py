from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services.audit import write_audit


class RewardError(ValueError):
    pass


async def grant_bonus(
    session: AsyncSession,
    *,
    telegram_id: int,
    amount_som: int,
    actor_telegram_id: int,
    reason: str,
) -> User:
    if amount_som <= 0:
        raise RewardError("Bonus musbat bo‘lishi kerak")
    normalized_reason = " ".join(reason.strip().split())
    if not normalized_reason or len(normalized_reason) > 300:
        raise RewardError("Sabab 1-300 belgi bo‘lishi kerak")
    user = await session.scalar(
        select(User).where(User.telegram_id == telegram_id).with_for_update()
    )
    if user is None:
        raise RewardError("User topilmadi")
    old_balance = user.bonus_balance_som
    user.bonus_balance_som += amount_som
    write_audit(
        session,
        actor_type="ADMIN",
        actor_id=str(actor_telegram_id),
        action="BONUS_GRANTED",
        entity_type="USER",
        entity_id=user.id,
        old_values={"bonus_balance_som": old_balance},
        new_values={"bonus_balance_som": user.bonus_balance_som},
        reason=normalized_reason,
    )
    return user


async def ranking(session: AsyncSession, *, limit: int = 50) -> list[User]:
    return list(
        await session.scalars(
            select(User)
            .where(User.ranking_points > 0)
            .order_by(User.ranking_points.desc(), User.created_at)
            .limit(limit)
        )
    )
