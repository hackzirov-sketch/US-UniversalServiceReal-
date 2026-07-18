from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.logging import logger
from app.core.metrics import (
    provider_request_duration,
    provider_request_errors_total,
    provider_request_total,
)
from app.core.security import sanitize
from app.integrations.providers.myxvest.exceptions import (
    MyxvestAuthenticationError,
    MyxvestError,
    MyxvestInsufficientFundsError,
    MyxvestInvalidResponseError,
    MyxvestOrderNotFoundError,
    MyxvestProviderUnavailableError,
    MyxvestRateLimitError,
    MyxvestTimeoutError,
    MyxvestValidationError,
)
from app.integrations.providers.myxvest.mapper import map_service_type, map_status, require_dict
from app.integrations.providers.myxvest.schemas import (
    GiftPurchaseRequest,
    PremiumPurchaseRequest,
    ProviderBalance,
    ProviderOrderResult,
    ProviderOrderStatus,
    ProviderService,
    StarsPurchaseRequest,
)

Sleep = Callable[[float], Awaitable[None]]


class MyxvestClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 20,
        max_retries: int = 3,
        http_client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        failure_threshold: int = 5,
        circuit_reset_seconds: int = 60,
    ) -> None:
        timeout = httpx.Timeout(
            timeout_seconds,
            connect=min(timeout_seconds, 10),
            read=timeout_seconds,
            write=timeout_seconds,
            pool=min(timeout_seconds, 10),
        )
        self._client = http_client or httpx.AsyncClient(timeout=timeout, verify=True)
        self._owns_client = http_client is None
        self._base_url = base_url
        self._api_key = api_key
        self._max_retries = max_retries
        self._sleep = sleep
        self._failure_threshold = failure_threshold
        self._circuit_reset_seconds = circuit_reset_seconds
        self._consecutive_failures = 0
        self._circuit_opened_at: float | None = None

    async def __aenter__(self) -> MyxvestClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health_check(self) -> bool:
        await self.get_balance()
        return True

    async def get_balance(self) -> ProviderBalance:
        data = await self._request("my_balance")
        value = data.get("balance_som", data.get("balance"))
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise MyxvestInvalidResponseError("Balance response has no integer balance")
        try:
            return ProviderBalance(
                balance_som=int(value),
                currency=str(data["currency"]) if data.get("currency") is not None else None,
                provider_name=str(data["name"]) if data.get("name") is not None else None,
                total_orders=int(data["total_orders"])
                if data.get("total_orders") is not None
                else None,
                total_spent_som=int(data["total_spent"])
                if data.get("total_spent") is not None
                else None,
            )
        except (ValueError, ValidationError) as exc:
            raise MyxvestInvalidResponseError("Invalid provider balance") from exc

    async def get_services(self) -> list[ProviderService]:
        data = await self._request("services")
        raw_services = data.get("services", data.get("data"))
        if not isinstance(raw_services, list):
            raise MyxvestInvalidResponseError("Services response has no service list")
        services: list[ProviderService] = []
        for item in raw_services:
            raw = require_dict(item)
            try:
                action = str(raw.get("action", raw.get("type", raw.get("service_type", ""))))
                try:
                    service_type = map_service_type(action)
                except MyxvestInvalidResponseError:
                    continue
                price_value = raw.get("price_per_unit", raw.get("price_som", raw.get("price")))
                if isinstance(price_value, float):
                    raise ValueError("Floating-point provider prices are not accepted")
                params = raw.get("params", [])
                if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
                    raise ValueError("Service params must be a string list")
                minimum, maximum = self._quantity_limits(raw, params)
                services.append(
                    ProviderService(
                        external_service_id=str(raw.get("id", raw.get("service_id", action))),
                        service_type=service_type,
                        name=str(raw.get("name", "")),
                        provider_price_som=int(price_value) if price_value is not None else None,
                        min_quantity=minimum,
                        max_quantity=maximum,
                        active=bool(raw.get("active", True)),
                        raw_metadata=sanitize(raw),
                        required_params=params,
                    )
                )
            except (TypeError, ValueError, ValidationError) as exc:
                raise MyxvestInvalidResponseError("Invalid service entry") from exc
        return services

    async def buy_stars(self, request: StarsPurchaseRequest) -> ProviderOrderResult:
        return await self._purchase(
            "buy_stars",
            {"username": request.username, "amount": request.quantity},
            request.idempotency_key,
        )

    async def buy_premium(self, request: PremiumPurchaseRequest) -> ProviderOrderResult:
        return await self._purchase(
            "buy_premium",
            {"username": request.username, "months": int(request.months)},
            request.idempotency_key,
        )

    async def buy_gift(self, request: GiftPurchaseRequest) -> ProviderOrderResult:
        params: dict[str, Any] = {
            "username": request.username,
            "gift_name": request.resolved_gift_name,
        }
        if request.comment is not None:
            params["comment"] = request.comment
        return await self._purchase(
            "buy_gift",
            params,
            request.idempotency_key,
        )

    async def get_order_status(self, provider_order_id: str) -> ProviderOrderStatus:
        data = await self._request("status", {"order_id": provider_order_id})
        order_id = data.get("provider_order_id", data.get("order_id", data.get("id")))
        status = map_status(data.get("status"))
        if not order_id or status.value == "UNKNOWN":
            raise MyxvestInvalidResponseError("Invalid status response")
        refund = data.get("refunded_amount_som", data.get("refund_amount"))
        return ProviderOrderStatus(
            provider_order_id=str(order_id),
            status=status,
            refunded_amount_som=int(refund) if refund is not None else None,
        )

    async def reconcile_order(
        self, *, provider_order_id: str | None, idempotency_key: str
    ) -> ProviderOrderStatus:
        params = {"idempotency_key": idempotency_key}
        if provider_order_id:
            params["order_id"] = provider_order_id
        data = await self._request("status", params)
        resolved_id = data.get(
            "provider_order_id", data.get("order_id", data.get("id", provider_order_id))
        )
        status = map_status(data.get("status"))
        if not resolved_id or status.value == "UNKNOWN":
            raise MyxvestInvalidResponseError("Ambiguous reconciliation response")
        return ProviderOrderStatus(provider_order_id=str(resolved_id), status=status)

    async def _purchase(
        self, action: str, params: dict[str, Any], idempotency_key: str
    ) -> ProviderOrderResult:
        data = await self._request(action, {**params, "idempotency_key": idempotency_key})
        order_id = data.get("provider_order_id", data.get("order_id", data.get("id")))
        status = map_status(data.get("status"))
        if not order_id or status.value == "UNKNOWN":
            raise MyxvestInvalidResponseError("Ambiguous purchase response")
        charged = data.get("charged_amount_som", data.get("amount"))
        return ProviderOrderResult(
            provider_order_id=str(order_id),
            status=status,
            charged_amount_som=int(charged) if charged is not None else None,
            duplicate=bool(data.get("duplicate", False)),
        )

    async def _request(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_circuit_available()
        payload = {"action": action, "api_key": self._api_key, **(params or {})}
        for attempt in range(1, self._max_retries + 1):
            started = time.perf_counter()
            provider_request_total.labels(provider="MYXVEST", action=action).inc()
            try:
                response = await self._client.post(self._base_url, data=payload)
                data = self._decode_response(response)
                self._raise_for_response(response, data)
                self._record_success()
                provider_request_duration.labels(provider="MYXVEST", action=action).observe(
                    time.perf_counter() - started
                )
                logger.info(
                    "provider_request",
                    provider="MYXVEST",
                    action=action,
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    http_status=response.status_code,
                )
                return data
            except httpx.TimeoutException as exc:
                error: MyxvestError = MyxvestTimeoutError("Provider request timed out")
                cause = exc
            except httpx.NetworkError as exc:
                error = MyxvestProviderUnavailableError("Provider network error")
                cause = exc
            except MyxvestError as exc:
                error = exc
                cause = exc

            self._record_failure(error)
            provider_request_errors_total.labels(
                provider="MYXVEST", action=action, error_code=error.code
            ).inc()
            provider_request_duration.labels(provider="MYXVEST", action=action).observe(
                time.perf_counter() - started
            )
            logger.warning(
                "provider_request_failed",
                provider="MYXVEST",
                action=action,
                attempt=attempt,
                duration_ms=round((time.perf_counter() - started) * 1000),
                sanitized_error_code=error.code,
            )
            if not error.retryable or attempt == self._max_retries:
                raise error from cause
            delay = error.retry_after or min(2 ** (attempt - 1), 10) + random.uniform(  # noqa: S311
                0, 0.25
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _decode_response(response: httpx.Response) -> dict[str, Any]:
        try:
            return require_dict(response.json())
        except (ValueError, TypeError) as exc:
            raise MyxvestInvalidResponseError("Provider returned invalid JSON") from exc

    @staticmethod
    def _raise_for_response(response: httpx.Response, data: dict[str, Any]) -> None:
        code = str(data.get("error_code", data.get("code", ""))).casefold()
        message = str(data.get("message", data.get("error", "Provider request failed")))
        is_error = (
            response.is_error
            or data.get("success") is False
            or data.get("ok") is False
            or bool(data.get("error"))
        )
        if not is_error:
            return
        if response.status_code in (401, 403) or code in {"invalid_api_key", "unauthorized"}:
            raise MyxvestAuthenticationError("Provider authentication failed")
        if response.status_code == 429 or code == "rate_limit":
            retry_header = response.headers.get("Retry-After")
            retry_after = float(retry_header) if retry_header and retry_header.isdigit() else None
            raise MyxvestRateLimitError("Provider rate limit reached", retry_after=retry_after)
        if code in {"insufficient_funds", "not_enough_balance"}:
            raise MyxvestInsufficientFundsError("Provider balance is insufficient")
        if response.status_code == 404 or code == "order_not_found":
            raise MyxvestOrderNotFoundError("Provider order was not found")
        if response.status_code in (400, 422) or code in {"validation_error", "invalid_params"}:
            raise MyxvestValidationError(message[:200])
        if response.status_code >= 500:
            raise MyxvestProviderUnavailableError("Provider is temporarily unavailable")
        raise MyxvestInvalidResponseError("Unrecognized provider error")

    def _ensure_circuit_available(self) -> None:
        if self._circuit_opened_at is None:
            return
        if time.monotonic() - self._circuit_opened_at >= self._circuit_reset_seconds:
            self._circuit_opened_at = None
            self._consecutive_failures = 0
            return
        raise MyxvestProviderUnavailableError("Provider circuit breaker is open")

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_opened_at = None

    def _record_failure(self, error: MyxvestError) -> None:
        if not error.retryable:
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            self._circuit_opened_at = time.monotonic()

    @staticmethod
    def _quantity_limits(raw: dict[str, Any], params: list[str]) -> tuple[int | None, int | None]:
        minimum = raw.get("min_quantity", raw.get("min"))
        maximum = raw.get("max_quantity", raw.get("max"))
        if minimum is not None or maximum is not None:
            return minimum, maximum
        for param in params:
            match = re.search(r"\((\d+)\s*-\s*(\d+)\)", param)
            if match:
                return int(match.group(1)), int(match.group(2))
        return None, None
