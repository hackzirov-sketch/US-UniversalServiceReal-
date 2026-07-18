class MyxvestError(Exception):
    code = "myxvest_error"
    retryable = False

    def __init__(
        self, message: str = "Myxvest request failed", *, retry_after: float | None = None
    ):
        super().__init__(message)
        self.retry_after = retry_after


class MyxvestAuthenticationError(MyxvestError):
    code = "authentication_error"


class MyxvestInsufficientFundsError(MyxvestError):
    code = "insufficient_funds"


class MyxvestRateLimitError(MyxvestError):
    code = "rate_limit"
    retryable = True


class MyxvestValidationError(MyxvestError):
    code = "validation_error"


class MyxvestProviderUnavailableError(MyxvestError):
    code = "provider_unavailable"
    retryable = True


class MyxvestTimeoutError(MyxvestError):
    code = "timeout"
    retryable = True


class MyxvestInvalidResponseError(MyxvestError):
    code = "invalid_response"


class MyxvestOrderNotFoundError(MyxvestError):
    code = "order_not_found"
