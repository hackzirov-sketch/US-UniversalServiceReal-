#!/usr/bin/env sh
set -eu

if [ -n "${PRIMARY_CARD_NUMBER:-}" ] && [ -n "${PRIMARY_CARD_HOLDER:-}" ]; then
  python -m app.seed_primary_card
fi

exec python -m app.render_start
