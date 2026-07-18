from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import AdminRole
from app.db.models import Provider, RuntimeSetting, User


async def bootstrap_defaults(
    session: AsyncSession,
    *,
    initial_admin_ids: frozenset[int],
    superadmin_ids: frozenset[int],
    myxvest_enabled: bool,
) -> None:
    admin_ids = initial_admin_ids | superadmin_ids
    existing_users = {}
    if admin_ids:
        existing_users = {
            user.telegram_id: user
            for user in await session.scalars(select(User).where(User.telegram_id.in_(admin_ids)))
        }
    for telegram_id in admin_ids:
        user = existing_users.get(telegram_id)
        if user is None:
            session.add(
                User(
                    telegram_id=telegram_id,
                    is_admin=True,
                    admin_active=True,
                    role=(
                        AdminRole.SUPERADMIN.value
                        if telegram_id in superadmin_ids
                        else AdminRole.ADMIN.value
                    ),
                )
            )
        elif not user.is_admin:
            user.is_admin = True
            user.admin_active = True
            user.role = (
                AdminRole.SUPERADMIN.value
                if telegram_id in superadmin_ids
                else AdminRole.ADMIN.value
            )

    provider = await session.scalar(select(Provider).where(Provider.code == "MYXVEST"))
    if provider is None:
        session.add(Provider(code="MYXVEST", name="Myxvest", enabled=myxvest_enabled))
    else:
        provider.enabled = myxvest_enabled

    runtime_gate = await session.get(RuntimeSetting, "real_sales_enabled")
    if runtime_gate is None:
        session.add(RuntimeSetting(key="real_sales_enabled", bool_value=False))


def has_admin_access(user: User, superadmin_ids: frozenset[int]) -> bool:
    return user.telegram_id in superadmin_ids or (user.is_admin and user.admin_active)


def can_remove_admin(user: User, superadmin_ids: frozenset[int]) -> bool:
    return user.telegram_id not in superadmin_ids
