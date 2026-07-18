# Universal Service

Telegram commerce backend with a just-in-time funded Myxvest provider integration.

## Stack

- FastAPI and aiogram 3
- SQLAlchemy 2 with PostgreSQL
- Alembic migrations
- Redis and ARQ background workers
- httpx async provider client
- pytest with mock HTTP transport
- Flask User/Admin Telegram Mini Apps mounted inside the FastAPI ASGI service

## Configuration

Copy `.env.example` to `.env` and set secrets locally. Never commit `.env`.

Required in production:

- `DATABASE_URL`
- `REDIS_URL`
- `TELEGRAM_BOT_TOKEN`
- `SUPERADMIN_IDS` — comma-separated numeric Telegram IDs; production refuses to start if empty
- `INITIAL_ADMIN_IDS` — bootstrap-only comma-separated IDs
- `MYXVEST_BASE_URL`
- `MYXVEST_API_KEY`
- `MYXVEST_ENABLED`
- Myxvest timeout, sync, polling, quote TTL and alert settings shown in `.env.example`

Rotate an exposed Telegram token through BotFather and rotate an exposed Myxvest key through
the provider. Update only the runtime secret store, restart services, and confirm old credentials no
longer work. Logs sanitize keys containing `api_key`, `key`, `authorization`, `token`, or `secret`.

## Run locally

```text
docker compose up --build
```

Or run components separately after installing `.[dev]`:

```text
alembic upgrade head
uvicorn app.api.main:app
arq app.workers.settings.WorkerSettings
python -m app.bot.main
pytest
```

The API exposes `/health` and `/metrics`. Telegram admins use `/provider`; users use
`/order ORDER_NUMBER`.

## Telegram Web Apps

User Web App lives at `/app`; Admin Web App lives at `/admin`. Flask renders two isolated retro/pixel
interfaces while authenticated async data and business actions stay in FastAPI under `/web-api`.
This avoids unsafe per-request event-loop bridges and keeps existing async service logic shared.

Telegram HMAC, auth age, replay, DB session revocation, CSRF, permissions, rate limits and secure
headers are enforced server-side. Configure `USER_WEBAPP_URL` and `ADMIN_WEBAPP_URL`; the bot shows
the appropriate Web App button by role.

Render preparation and safety steps: [docs/render_deployment.md](docs/render_deployment.md).

## Single-card balance top-up

Balance top-up uses exactly one encrypted `PRIMARY` payment card. Set
`PAYMENT_CARD_ENCRYPTION_KEY` to a Fernet key in the server secret manager and optionally set
`PAYMENT_REVIEW_CHAT_ID` to the private review group ID. Never store the card number in source,
migrations, documentation, or logs.

After the encryption key is configured, a superadmin opens `/admin` and selects
`💳 Asosiy karta` to enter the card locally. The bot deletes the card-number message when Telegram
allows it. `/payment_review_grant TELEGRAM_ID` grants `REVIEW_PAYMENTS` to an existing active admin.
Users start the direct single-card flow with `/topup` or `💳 Balansni to‘ldirish`.

## Provider funding flow

After payment approval, user funds are atomically reserved and the order enters
`AWAITING_PROVIDER_FUNDING`. The balance worker reads the real provider balance. FIFO dispatch uses
database row locks and submits only affordable orders. The stable idempotency key is committed with
`SUBMITTING` before the external request.

After an admin funds the provider, use the Telegram “Balansni qayta tekshirish” button. It first
fetches the real API balance and only then dispatches pending orders. A manual accounting entry alone
does not release the queue.

Timeouts are not treated as failed purchases. The reconciliation worker checks `SUBMITTING`,
`PROVIDER_TIMEOUT`, and `NEEDS_REVIEW` orders using the original idempotency key. Never generate a new
key for a retry.

## Tests

Tests use SQLite for transactional domain checks and `httpx.MockTransport` for provider responses.
They never contact or spend funds on the real provider API.

## Production checklist

1. Rotate any credential ever pasted into chat, logs, or tickets.
2. Store secrets in the deployment platform secret manager.
3. Use PostgreSQL and Redis with backups, authentication, and TLS where applicable.
4. Run `alembic upgrade head` before starting workers.
5. Confirm `SUPERADMIN_IDS` and provider service limits.
6. Run the test suite and a mock-provider smoke test.
7. Start API, one bot polling instance, and workers.
8. Verify health, metrics, balance sync, alerts, and reconciliation without a purchase.
9. Enable purchase traffic gradually after the exact provider response contract is confirmed.

## Contract safety

The public specification does not fully define response field names, Premium month options, rate
units, or idempotency lookup behavior. The mapper accepts only documented/common envelope variants
and sends unknown or ambiguous responses to review. Confirm these fields against official provider
documentation before enabling production purchases.

The latest sanitized read-only verification is documented in
`docs/myxvest_contract_verification.md`.
