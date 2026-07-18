# UniversalService Render deployment

This repository is prepared for a Render Blueprint, but the Blueprint must not be applied until the
production secrets, backup and Myxvest contract checks are complete.

## Architecture

- `universalservice-web`: FastAPI ASGI host with mounted Flask User/Admin Web Apps.
- `universalservice-bot`: one aiogram polling background worker.
- `universalservice-worker`: ARQ jobs and provider reconciliation.
- `universalservice-db`: PostgreSQL 17.
- `universalservice-redis`: Render Key Value with `noeviction`.

All resources default to `singapore`, the closest current Render region to the primary Uzbekistan
audience. Change every `region` field before the first Blueprint sync if another region is required.
Render does not allow an existing service region to be changed in place.

## Git and Blueprint setup

1. Create a private GitHub repository and commit the project. Never commit `.env`.
2. Push `render.yaml` to the default branch.
3. Open Render Dashboard, choose **New > Blueprint**, and connect the repository.
4. Confirm Render detected `render.yaml` at repository root.
5. Review every resource, region and plan before applying.
6. Fill all variables marked `sync: false` in Render Dashboard.

This workspace currently has no Git metadata. Create the remote only when ready; this preparation
does not push code or start deployment.

## Required secrets

Set these in Render Dashboard, never source control:

- `SECRET_KEY`: at least 32 cryptographically random characters.
- `SESSION_ENCRYPTION_KEY`: at least 32 cryptographically random characters.
- `PAYMENT_CARD_ENCRYPTION_KEY`: valid Fernet key used by existing card encryption.
- `TELEGRAM_BOT_TOKEN`.
- `SUPERADMIN_IDS`: comma-separated numeric Telegram IDs.
- `INITIAL_ADMIN_IDS`: optional bootstrap admin IDs.
- `MYXVEST_BASE_URL` and `MYXVEST_API_KEY`.
- `PAYMENT_REVIEW_CHAT_ID`.
- `USER_WEBAPP_URL=https://<web-service-domain>/app`.
- `ADMIN_WEBAPP_URL=https://<web-service-domain>/admin`.
- `CORS_ALLOWED_ORIGINS=https://<web-service-domain>`.
- `TRUSTED_HOSTS=<web-service-domain>`.
- `BACKUP_VERIFIED_AT`: ISO-8601 time of a tested, recent backup.

`DATABASE_URL` and `REDIS_URL` come from Blueprint resource references. Do not copy their raw values
into `render.yaml`.

## Migrations

Free web instances cannot use Render pre-deploy commands. `bin/render-start.sh` therefore starts a
Python migration gate that:

1. waits for PostgreSQL;
2. acquires PostgreSQL advisory lock `8201824177`;
3. runs `alembic upgrade head`;
4. exits on migration failure;
5. releases the lock and `exec`s Gunicorn on `0.0.0.0:$PORT`.

Bot and ARQ workers never run migrations. For a paid web plan, preferred configuration is:

```yaml
preDeployCommand: alembic upgrade head
startCommand: >-
  gunicorn app.web.asgi:app --worker-class uvicorn.workers.UvicornWorker
  --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

When enabling `preDeployCommand`, remove migration execution from the start path to avoid redundant
migrations.

## Telegram Mini App setup

1. Deploy web service and verify `/health` returns database and Redis `ok`.
2. Set `USER_WEBAPP_URL` and `ADMIN_WEBAPP_URL` on web and bot services.
3. In BotFather, configure the exact HTTPS Web App domain.
4. Restart only the bot worker after URL changes.
5. Test user and admin buttons with separate Telegram accounts.

Telegram `initData` is verified server-side. The backend checks HMAC, `auth_date`, replay record,
numeric Telegram ID, DB role, permissions and admin session version. Raw `initData` is never audited.

## Health and logs

`GET /health` performs small `SELECT 1` and Redis `PING` checks. Expected healthy response:

```json
{"status":"ok","web":"running","database":"ok","redis":"ok"}
```

Use Render service **Logs** for startup failures. Never paste raw tokens, full database URLs or full
card data into tickets. Check web, bot and ARQ logs separately.

## Free and paid resources

- Free web is for testing. It sleeps after inactivity and has ephemeral filesystem.
- Background workers do not support the free plan. The checked-in free-test Blueprint omits Bot and
  ARQ workers; add them back with `starter` or higher plans before production.
- Free PostgreSQL has no backups and can expire; not suitable for sales production.
- Free Key Value has no persistence; queued work can be lost after restart.
- Production recommendation: paid web, workers, PostgreSQL with backups, and persistent Key Value.
- Paid web supports `preDeployCommand`, preferred for migrations.

## Files and uploads

Render local filesystem is not persistent. Receipt upload bytes are MIME-signature and size checked,
sent to the private Telegram review chat, then only Telegram `file_id`, `file_unique_id`, checksum and
safe metadata are stored. Do not save receipts, backups, audit exports or secrets locally.

## Purchase gate

Deployment keeps `MYXVEST_PURCHASE_ENABLED=false`. Migration creates
`runtime_settings.real_sales_enabled=false`. Real purchases require both values true plus a fresh
successful preflight. Deployment never enables either gate automatically.

Safe enable procedure after separate approval:

1. Verify Myxvest request/response contract and idempotency lookup.
2. Verify restore-capable backup and rotate exposed secrets.
3. Run full preflight; resolve every failure.
4. Set environment gate true and redeploy.
5. Run another preflight.
6. Superadmin enables runtime gate with two-step confirmation.
7. Start with one controlled low-value order and monitor reconciliation.

## Rollback and secret rotation

- Roll back web, bot and worker to the same compatible commit.
- Never downgrade schema before confirming the old code supports the current schema.
- Rotate Telegram token in BotFather, Myxvest key at provider, and database/Redis credentials in their
  dashboards.
- Update Render secrets, redeploy all consumers, verify old credentials fail.
- Payment card encryption key rotation requires a controlled re-encryption procedure; do not replace
  it blindly.

## Post-deployment checklist

- Blueprint resources live and in one region.
- `/health` returns HTTP 200 with DB and Redis `ok`.
- Alembic current equals `20260718_0010`.
- Exactly one bot polling instance runs.
- ARQ heartbeat exists and scheduled jobs run.
- User `/app` and Admin `/admin` open through Telegram.
- Ordinary user receives 403 from every admin API.
- Receipt upload reaches private review chat.
- Payment approval credits balance once under concurrent attempts.
- No secrets or full card number appear in logs/audit.
- Environment and runtime purchase gates remain false.
- Backup restore has been tested before production sales.

## Supabase alternative

The connected Supabase account currently contains only an unrelated inactive project named
`UzFreelanceHub`; it was not changed. If UniversalService later uses Supabase instead of Render
PostgreSQL, migrate deliberately, use a session pooler for persistent Render services, and either
disable the Data API or enable RLS on every exposed table. Do not run two production primary
databases.
