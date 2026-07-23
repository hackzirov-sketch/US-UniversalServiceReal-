import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_admin_ids_are_strictly_parsed() -> None:
    settings = Settings(
        _env_file=None,
        superadmin_ids="7521446360, 6907502858",
        initial_admin_ids="7696442804",
    )
    assert settings.superadmin_ids == frozenset({7521446360, 6907502858})
    assert settings.initial_admin_ids == frozenset({7696442804})


def test_admin_ids_are_read_from_dotenv(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SUPERADMIN_IDS=7521446360,6907502858\nINITIAL_ADMIN_IDS=7696442804\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=env_file)
    assert settings.superadmin_ids == frozenset({7521446360, 6907502858})
    assert settings.initial_admin_ids == frozenset({7696442804})


@pytest.mark.parametrize("value", ["1,,2", "1,abc", "-1", "1.5"])
def test_invalid_admin_ids_fail_startup(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, superadmin_ids=value)


def test_production_requires_superadmin() -> None:
    with pytest.raises(ValidationError, match="SUPERADMIN_IDS"):
        Settings(_env_file=None, app_env="production", superadmin_ids="")


def test_direct_sales_does_not_require_provider_secret() -> None:
    settings = Settings(_env_file=None, direct_sales_enabled=True)
    assert settings.direct_sales_enabled
