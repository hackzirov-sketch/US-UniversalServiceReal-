# UniversalService

UniversalService is a Telegram bot and web application for selling Telegram
Stars, Premium, and gifts with direct fulfillment.

## Current fulfillment model

- No third-party purchase provider is connected.
- Product prices are maintained by admins.
- Customer payments are reviewed through the admin interface.
- Direct sales stay disabled until `DIRECT_SALES_ENABLED=true` and the runtime
  sales gate is enabled.
- Existing database provider columns are retained only to keep historical data
  and migrations compatible.

## Local setup

1. Copy `.env.example` to `.env` and fill in the required secrets.
2. Install the package with `pip install -e ".[dev]"`.
3. Apply migrations with `alembic upgrade head`.
4. Start the API with `uvicorn app.api.main:app --reload`.
5. Start the polling bot with `python -m app.bot.main` when webhook mode is not
   being used.

## Production

The included `render.yaml` provisions the web service, PostgreSQL, and Redis on
Render. The web service runs database migrations and serves both the Telegram
webhook and web application.

Required production secrets include:

- `SECRET_KEY`
- `SESSION_ENCRYPTION_KEY`
- `PAYMENT_CARD_ENCRYPTION_KEY`
- `TELEGRAM_BOT_TOKEN`
- `SUPERADMIN_IDS`
- `USER_WEBAPP_URL`
- `ADMIN_WEBAPP_URL`
- `CORS_ALLOWED_ORIGINS`
- `TRUSTED_HOSTS`

Provider API keys are not used.

## Verification

Run:

```text
python -m ruff check app tests
python -m pytest -q
```

Health checks are available at `/health`.
