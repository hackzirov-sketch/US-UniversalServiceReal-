from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import ValidationError

from app.core.logging import configure_logging
from app.integrations.providers.myxvest.client import MyxvestClient
from app.integrations.providers.myxvest.exceptions import (
    MyxvestAuthenticationError,
    MyxvestInsufficientFundsError,
    MyxvestInvalidResponseError,
    MyxvestProviderUnavailableError,
    MyxvestTimeoutError,
)
from app.integrations.providers.myxvest.schemas import (
    GiftPurchaseRequest,
    PremiumMonths,
    PremiumPurchaseRequest,
    StarsPurchaseRequest,
)

URL = "https://provider.invalid/api"
SECRET = "never-print-this-secret"  # noqa: S105 - synthetic test value


def client_for(handler, *, retries: int = 1, sleep=None) -> MyxvestClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return MyxvestClient(
        base_url=URL,
        api_key=SECRET,
        max_retries=retries,
        http_client=http_client,
        sleep=sleep or _no_sleep,
    )


async def _no_sleep(_delay: float) -> None:
    return None


@pytest.mark.asyncio
async def test_services_success() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "services": [
                    {
                        "id": "stars",
                        "type": "stars",
                        "name": "Telegram Stars",
                        "price": 130,
                        "min": 50,
                        "max": 10000,
                    }
                ],
            },
        )

    services = await client_for(handler).get_services()
    assert services[0].provider_price_som == 130
    assert services[0].min_quantity == 50


@pytest.mark.asyncio
async def test_balance_success() -> None:
    balance = await client_for(
        lambda _request: httpx.Response(200, json={"success": True, "balance": "50000"})
    ).get_balance()
    assert balance.balance_som == 50_000


@pytest.mark.asyncio
async def test_live_balance_contract_with_integer_and_metadata() -> None:
    client = client_for(
        lambda _request: httpx.Response(
            200,
            json={
                "ok": True,
                "name": "Partner",
                "balance": 50000,
                "currency": "UZS",
                "total_orders": 3,
                "total_spent": 1200,
                "future_field": "accepted",
            },
        )
    )
    balance = await client.get_balance()
    assert balance.balance_som == 50_000
    assert balance.currency == "UZS"
    assert balance.total_orders == 3
    assert balance.total_spent_som == 1_200


@pytest.mark.asyncio
async def test_health_check_and_invalid_balance_shapes() -> None:
    assert (
        await client_for(
            lambda _request: httpx.Response(200, json={"ok": True, "balance": 1})
        ).health_check()
        is True
    )
    for value in (True, "not-an-integer"):
        with pytest.raises(MyxvestInvalidResponseError):
            await client_for(
                lambda _request, current=value: httpx.Response(
                    200, json={"ok": True, "balance": current}
                )
            ).get_balance()


@pytest.mark.asyncio
async def test_live_services_contract_is_mapped_without_guessing_missing_prices() -> None:
    payload = {
        "ok": True,
        "services": [
            {
                "action": "buy_stars",
                "name": "Telegram Stars",
                "params": ["username", "amount (50-10000)"],
                "price_per_unit": 189,
                "future_field": "accepted",
            },
            {
                "action": "buy_premium",
                "name": "Telegram Premium",
                "params": ["username", "months (3|6|12)"],
            },
            {
                "action": "buy_gift",
                "name": "Telegram Gift",
                "params": ["username", "gift_name", "comment?"],
            },
            {
                "action": "donat_buy",
                "name": "Unrelated product",
                "params": ["game"],
            },
        ],
    }
    services = await client_for(lambda _request: httpx.Response(200, json=payload)).get_services()
    assert [service.external_service_id for service in services] == [
        "buy_stars",
        "buy_premium",
        "buy_gift",
    ]
    assert services[0].provider_price_som == 189
    assert (services[0].min_quantity, services[0].max_quantity) == (50, 10_000)
    assert services[1].provider_price_som is None
    assert services[2].required_params == ["username", "gift_name", "comment?"]


@pytest.mark.asyncio
async def test_invalid_service_catalog_shapes_are_rejected() -> None:
    missing_list = client_for(
        lambda _request: httpx.Response(200, json={"ok": True, "services": None})
    )
    with pytest.raises(MyxvestInvalidResponseError):
        await missing_list.get_services()
    float_price = client_for(
        lambda _request: httpx.Response(
            200,
            json={
                "ok": True,
                "services": [
                    {
                        "action": "buy_stars",
                        "name": "Stars",
                        "params": ["username"],
                        "price_per_unit": 1.5,
                    }
                ],
            },
        )
    )
    with pytest.raises(MyxvestInvalidResponseError):
        await float_price.get_services()


@pytest.mark.asyncio
async def test_invalid_api_key() -> None:
    client = client_for(
        lambda _request: httpx.Response(
            401, json={"success": False, "error_code": "invalid_api_key"}
        )
    )
    with pytest.raises(MyxvestAuthenticationError):
        await client.get_balance()


@pytest.mark.asyncio
async def test_http_200_api_error_and_ok_false_are_not_success() -> None:
    auth_client = client_for(
        lambda _request: httpx.Response(200, json={"ok": False, "error_code": "invalid_api_key"})
    )
    with pytest.raises(MyxvestAuthenticationError):
        await auth_client.get_balance()
    generic_client = client_for(lambda _request: httpx.Response(200, json={"success": False}))
    with pytest.raises(MyxvestInvalidResponseError):
        await generic_client.get_balance()


@pytest.mark.asyncio
async def test_provider_insufficient_funds() -> None:
    client = client_for(
        lambda _request: httpx.Response(
            400, json={"success": False, "error_code": "insufficient_funds"}
        )
    )
    request = StarsPurchaseRequest(
        username="valid_user", quantity=50, idempotency_key="ute:one:myxvest:v1"
    )
    with pytest.raises(MyxvestInsufficientFundsError):
        await client.buy_stars(request)


@pytest.mark.asyncio
async def test_429_retries_and_honors_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"success": False, "error_code": "rate_limit"},
            )
        return httpx.Response(200, json={"success": True, "balance": 7})

    balance = await client_for(handler, retries=2, sleep=sleep).get_balance()
    assert balance.balance_som == 7
    assert calls == 2
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_5xx_is_bounded_and_typed() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(502, json={"error": "temporary"})

    with pytest.raises(MyxvestProviderUnavailableError):
        await client_for(handler, retries=3).get_balance()
    assert calls == 3


@pytest.mark.asyncio
async def test_broken_json_is_rejected() -> None:
    client = client_for(lambda _request: httpx.Response(200, text="<html>gateway error</html>"))
    with pytest.raises(MyxvestInvalidResponseError):
        await client.get_balance()


@pytest.mark.asyncio
async def test_timeout_reuses_same_idempotency_key() -> None:
    payloads: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(parse_qs(request.content.decode()))
        if len(payloads) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"id": "p-1", "status": "processing"})

    request = StarsPurchaseRequest(
        username="@valid_user", quantity=50, idempotency_key="ute:stable:myxvest:v1"
    )
    result = await client_for(handler, retries=2).buy_stars(request)
    assert result.provider_order_id == "p-1"
    assert payloads[0]["idempotency_key"] == payloads[1]["idempotency_key"]


@pytest.mark.asyncio
async def test_final_timeout_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(MyxvestTimeoutError):
        await client_for(handler, retries=1).get_balance()


@pytest.mark.asyncio
async def test_duplicate_response_requires_provider_order_id() -> None:
    request = StarsPurchaseRequest(
        username="valid_user", quantity=50, idempotency_key="ute:duplicate:myxvest:v1"
    )
    result = await client_for(
        lambda _request: httpx.Response(
            200,
            json={
                "ok": True,
                "duplicate": True,
                "provider_order_id": "provider-7",
                "status": "processing",
            },
        )
    ).buy_stars(request)
    assert result.duplicate is True
    assert result.provider_order_id == "provider-7"
    missing = client_for(
        lambda _request: httpx.Response(
            200, json={"ok": True, "duplicate": True, "status": "processing"}
        )
    )
    with pytest.raises(MyxvestInvalidResponseError):
        await missing.buy_stars(request)


@pytest.mark.asyncio
async def test_unknown_purchase_and_status_values_are_rejected() -> None:
    request = StarsPurchaseRequest(
        username="valid_user", quantity=50, idempotency_key="ute:unknown:myxvest:v1"
    )
    client = client_for(
        lambda _request: httpx.Response(200, json={"id": "provider-8", "status": "mystery"})
    )
    with pytest.raises(MyxvestInvalidResponseError):
        await client.buy_stars(request)
    with pytest.raises(MyxvestInvalidResponseError):
        await client.get_order_status("provider-8")


@pytest.mark.asyncio
async def test_gift_uses_live_gift_name_and_optional_comment_fields() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"id": "gift-1", "status": "processing"})

    request = GiftPurchaseRequest(
        username="valid_user",
        gift_name="Rose",
        comment="Salom",
        idempotency_key="ute:gift:myxvest:v1",
    )
    await client_for(handler).buy_gift(request)
    assert captured["gift_name"] == ["Rose"]
    assert captured["comment"] == ["Salom"]
    assert "gift_id" not in captured


@pytest.mark.asyncio
async def test_premium_status_and_reconciliation_mappings() -> None:
    actions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = parse_qs(request.content.decode())
        action = payload["action"][0]
        actions.append(action)
        if action == "buy_premium":
            return httpx.Response(200, json={"id": "premium-1", "status": "processing"})
        return httpx.Response(
            200,
            json={
                "provider_order_id": "premium-1",
                "status": "refunded",
                "refund_amount": 900,
            },
        )

    client = client_for(handler)
    await client.buy_premium(
        PremiumPurchaseRequest(
            username="valid_user",
            months=PremiumMonths.THREE,
            idempotency_key="ute:premium:myxvest:v1",
        )
    )
    status = await client.get_order_status("premium-1")
    reconciled = await client.reconcile_order(
        provider_order_id="premium-1", idempotency_key="ute:premium:myxvest:v1"
    )
    assert status.refunded_amount_som == 900
    assert reconciled.status.value == "REFUNDED"
    assert actions == ["buy_premium", "status", "status"]


def test_username_validation_and_normalization() -> None:
    request = StarsPurchaseRequest(
        username="  @valid_user  ", quantity=50, idempotency_key="ute:valid:myxvest:v1"
    )
    assert request.username == "valid_user"
    with pytest.raises(ValidationError):
        StarsPurchaseRequest(
            username="bad user", quantity=50, idempotency_key="ute:valid:myxvest:v1"
        )


@pytest.mark.parametrize("quantity", [0, 49, 1_000_001])
def test_stars_limits(quantity: int) -> None:
    with pytest.raises(ValidationError):
        StarsPurchaseRequest(
            username="valid_user", quantity=quantity, idempotency_key="ute:valid:myxvest:v1"
        )


def test_premium_months_are_restricted() -> None:
    with pytest.raises(ValidationError):
        PremiumPurchaseRequest(
            username="valid_user", months=4, idempotency_key="ute:valid:myxvest:v1"
        )


@pytest.mark.asyncio
async def test_api_key_is_not_logged(capsys) -> None:
    configure_logging()
    client = client_for(
        lambda _request: httpx.Response(
            401, json={"success": False, "error_code": "invalid_api_key"}
        )
    )
    with pytest.raises(MyxvestAuthenticationError):
        await client.get_balance()
    output = capsys.readouterr()
    assert SECRET not in output.out
    assert SECRET not in output.err
