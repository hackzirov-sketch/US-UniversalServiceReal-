from app.db.enums import ServiceType
from app.seed_catalog import catalog_prices, progressive_markup


def test_progressive_markup_stays_between_one_and_ten_thousand() -> None:
    assert progressive_markup(1) == 1
    assert progressive_markup(159_587) == 1_596
    assert progressive_markup(5_000_000) == 10_000


def test_reference_catalog_contains_requested_services_and_prices() -> None:
    rows = catalog_prices()
    stars = next(row for row in rows if row.service_type == ServiceType.STARS)
    premiums = [row for row in rows if row.service_type == ServiceType.PREMIUM]
    gifts = [row for row in rows if row.service_type == ServiceType.GIFT]
    assert (stars.provider_cost_som, stars.sale_price_som) == (189, 190)
    assert [row.premium_months for row in premiums] == [3, 6, 12]
    assert len(gifts) == 11
    assert all(1 <= row.sale_price_som - row.provider_cost_som <= 10_000 for row in rows)
