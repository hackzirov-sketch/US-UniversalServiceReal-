# Myxvest read-only contract verification

Verified on 2026-07-17 using only the `services` and `my_balance` actions. No purchase action was
called. Raw responses and credentials were not persisted.

## HTTP envelope

Both actions returned HTTP 200 with `application/json` content.

`services` returned:

- top level: `ok: boolean`, `services: array`
- five service objects
- service fields: `action: string`, `name: string`, `params: array[string]`
- `price_per_unit: integer` exists for Stars and is absent for the other advertised products
- no null fields were observed
- no `id`, `service_id`, `type`, `active`, or `currency` field exists per service

The Telegram entries were:

- `buy_stars`: parameters `username`, `amount (50-10000)`; `price_per_unit` was 189
- `buy_premium`: parameters `username`, `months (3|6|12)`; no price was returned
- `buy_gift`: parameters `username`, `gift_name`, `comment?`; no price was returned

Two unrelated `donat_buy` entries were also present. The mapper safely ignores unsupported actions.

`my_balance` returned:

- `ok: boolean`
- `name: string`
- `balance: integer`
- `currency: string`
- `total_orders: integer`
- `total_spent: integer`
- no null fields were observed

The balance value itself is intentionally omitted from this report.

## Contract gaps that block purchases

- No Premium package cost is available from the read-only catalog.
- No Gift catalog, allowed `gift_name` values, identifier format, or price is available.
- No service-level active/inactive field is exposed.
- `idempotency_key` is not advertised in any purchase action's `params` list.
- `status` is not advertised as an available action in the service catalog.
- Duplicate response fields and semantics are not documented by the read-only response.
- Purchase success response fields, provider order identifier, charged amount, and status vocabulary
  cannot be verified without an official contract or sandbox.
- Reconciliation by idempotency key cannot be proven from read-only actions.

Production purchase traffic must remain disabled until the provider supplies an official contract or
a no-charge sandbox that resolves these gaps.
