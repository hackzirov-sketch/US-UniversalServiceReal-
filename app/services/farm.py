from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import FarmPlotState
from app.db.models import FarmPlot, FarmProfile, FarmReward, User
from app.services.audit import write_audit


class FarmError(ValueError):
    pass


async def get_farm(session: AsyncSession, *, user_id: str) -> tuple[FarmProfile, list[FarmPlot]]:
    profile = await session.get(FarmProfile, user_id)
    if profile is None:
        profile = FarmProfile(user_id=user_id)
        session.add(profile)
    plots = list(
        await session.scalars(
            select(FarmPlot).where(FarmPlot.user_id == user_id).order_by(FarmPlot.slot)
        )
    )
    existing = {plot.slot for plot in plots}
    for slot in range(6):
        if slot not in existing:
            plot = FarmPlot(
                user_id=user_id,
                slot=slot,
                state=FarmPlotState.EMPTY if slot < 4 else FarmPlotState.LOCKED,
            )
            session.add(plot)
            plots.append(plot)
    await session.flush()
    return profile, sorted(plots, key=lambda plot: plot.slot)


async def plant(session: AsyncSession, *, user_id: str, slot: int) -> FarmPlot:
    profile, _ = await get_farm(session, user_id=user_id)
    plot = await _locked_plot(session, user_id, slot)
    if plot.state != FarmPlotState.EMPTY:
        raise FarmError("Bu yer ekishga tayyor emas")
    if profile.energy < 1 or profile.seeds < 1:
        raise FarmError("Energiya yoki urug‘ yetarli emas")
    now = datetime.now(UTC)
    profile.energy -= 1
    profile.seeds -= 1
    plot.state = FarmPlotState.WATER_NEEDED
    plot.crop = "pixel_wheat"
    plot.planted_at = now
    plot.ready_at = now + timedelta(minutes=5)
    plot.version += 1
    return plot


async def water(session: AsyncSession, *, user_id: str, slot: int) -> FarmPlot:
    profile, _ = await get_farm(session, user_id=user_id)
    plot = await _locked_plot(session, user_id, slot)
    if plot.state != FarmPlotState.WATER_NEEDED:
        raise FarmError("Bu ekinga hozir suv kerak emas")
    if profile.water < 1:
        raise FarmError("Suv yetarli emas")
    profile.water -= 1
    plot.state = FarmPlotState.GROWING
    plot.version += 1
    return plot


async def harvest(session: AsyncSession, *, user_id: str, slot: int) -> int:
    profile, _ = await get_farm(session, user_id=user_id)
    plot = await _locked_plot(session, user_id, slot)
    now = datetime.now(UTC)
    ready_at = plot.ready_at
    if ready_at is not None and ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=UTC)
    if plot.state == FarmPlotState.GROWING and ready_at and ready_at <= now:
        plot.state = FarmPlotState.READY
    if plot.state != FarmPlotState.READY:
        raise FarmError("Hosil hali tayyor emas")
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise FarmError("User topilmadi")
    reward = 10
    user.farm_points += reward
    user.ranking_points += reward
    profile.xp += reward
    profile.level = max(1, profile.xp // 100 + 1)
    profile.seeds += 1
    plot.state = FarmPlotState.EMPTY
    plot.crop = None
    plot.harvested_at = now
    plot.planted_at = None
    plot.ready_at = None
    plot.version += 1
    farm_reward = FarmReward(user_id=user_id, plot_id=plot.id, amount=reward, granted_at=now)
    session.add(farm_reward)
    await session.flush()
    write_audit(
        session,
        actor_type="USER",
        actor_id=user_id,
        action="FARM_REWARD_GRANTED",
        entity_type="FARM_PLOT",
        entity_id=farm_reward.id,
        metadata={"reward": reward, "slot": slot, "plot_id": plot.id},
    )
    return reward


async def reverse_reward(
    session: AsyncSession, *, reward_id: str, actor_telegram_id: int
) -> FarmReward:
    reward = await session.scalar(
        select(FarmReward).where(FarmReward.id == reward_id).with_for_update()
    )
    if reward is None:
        raise FarmError("Farm reward topilmadi")
    if reward.reversed_at is not None:
        return reward
    user = await session.scalar(select(User).where(User.id == reward.user_id).with_for_update())
    if user is None:
        raise FarmError("User topilmadi")
    if user.farm_points < reward.amount or user.ranking_points < reward.amount:
        raise FarmError("Reward ishlatilgan; avtomatik reverse mumkin emas")
    user.farm_points -= reward.amount
    user.ranking_points -= reward.amount
    reward.reversed_at = datetime.now(UTC)
    reward.reversed_by_telegram_id = actor_telegram_id
    write_audit(
        session,
        actor_type="ADMIN",
        actor_id=str(actor_telegram_id),
        action="FARM_REWARD_REVERSED",
        entity_type="FARM_REWARD",
        entity_id=reward.id,
        metadata={"reward": reward.amount, "user_id": reward.user_id},
    )
    return reward


async def _locked_plot(session: AsyncSession, user_id: str, slot: int) -> FarmPlot:
    plot = await session.scalar(
        select(FarmPlot).where(FarmPlot.user_id == user_id, FarmPlot.slot == slot).with_for_update()
    )
    if plot is None:
        raise FarmError("Yer katagi topilmadi")
    return plot
