from prometheus_client import Counter, Gauge, Histogram

provider_request_total = Counter(
    "provider_request_total", "Provider HTTP requests", ["provider", "action"]
)
provider_request_errors_total = Counter(
    "provider_request_errors_total",
    "Provider HTTP request errors",
    ["provider", "action", "error_code"],
)
provider_request_duration = Histogram(
    "provider_request_duration_seconds", "Provider HTTP request duration", ["provider", "action"]
)
provider_balance_som = Gauge("provider_balance_som", "Latest provider balance in UZS", ["provider"])
orders_waiting_provider_funding = Gauge(
    "orders_waiting_provider_funding", "Orders waiting for provider funding", ["provider"]
)
orders_processing = Gauge("orders_processing", "Provider orders currently processing", ["provider"])
order_completion_total = Counter(
    "order_completion_total", "Successfully completed provider orders", ["provider"]
)
refund_total = Counter("refund_total", "Refunded orders", ["provider"])
duplicate_prevented_total = Counter(
    "duplicate_prevented_total", "Duplicate ledger or provider operations prevented", ["operation"]
)
