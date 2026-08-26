#!/usr/bin/env bash
# End-to-end smoke test against a RUNNING sample-shop.
#
# Hits every endpoint with real ids taken from the seeded database, checks the
# status code, and prints the wall-clock time of each call. It is not a
# benchmark - one sample per endpoint proves nothing about latency - but it is
# the fastest way to confirm the whole stack is actually wired together.
#
#   ./scripts/run_api.sh          # in one terminal
#   ./scripts/smoke.sh            # in another
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_venv
load_env

BASE_URL="${BASE_URL:-http://127.0.0.1:${SHOP_PORT:-8000}}"
FAILURES=0
CHECKS=0

check() {
  local expected="$1" method="$2" path="$3"
  shift 3
  local out status time_total
  out="$(curl -s -o /dev/null -w '%{http_code} %{time_total}' -X "${method}" "$@" "${BASE_URL}${path}")"
  status="${out%% *}"
  time_total="${out##* }"
  CHECKS=$((CHECKS + 1))

  if [[ "${status}" == "${expected}" ]]; then
    printf '  \033[32mok\033[0m   %-6s %-45s %s  %ss\n' "${method}" "${path}" "${status}" "${time_total}"
  else
    printf '  \033[31mFAIL\033[0m %-6s %-45s got %s want %s\n' "${method}" "${path}" "${status}" "${expected}"
    FAILURES=$((FAILURES + 1))
  fi
}

log "Resolving real ids from the database"
IDS="$("${VENV_PY}" - <<'PY'
import asyncio, sys

import asyncpg
from shop.config import get_settings

async def main() -> None:
    conn = await asyncpg.connect(get_settings().asyncpg_dsn)
    try:
        product = await conn.fetchval("SELECT min(id) FROM products WHERE is_active")
        seller = await conn.fetchval("SELECT min(id) FROM users WHERE is_seller")
        order = await conn.fetchval("SELECT min(id) FROM orders")
        post = await conn.fetchval("SELECT min(id) FROM posts WHERE is_published")
        category = await conn.fetchval("SELECT min(id) FROM categories")
        buyer = await conn.fetchval(
            "SELECT user_id FROM orders GROUP BY user_id ORDER BY count(*) DESC LIMIT 1"
        )
        follower = await conn.fetchval(
            "SELECT follower_id FROM follows GROUP BY follower_id "
            "ORDER BY count(*) DESC LIMIT 1"
        )
    finally:
        await conn.close()

    if None in (product, seller, order, post, category, buyer, follower):
        print("EMPTY", file=sys.stderr)
        raise SystemExit(1)

    print(f"{product} {seller} {order} {post} {category} {buyer} {follower}")

asyncio.run(main())
PY
)" || {
  echo "ERROR: could not read ids from the database." >&2
  echo "Is it seeded?  ./scripts/seed.sh" >&2
  exit 1
}

read -r PRODUCT_ID SELLER_ID ORDER_ID POST_ID CATEGORY_ID BUYER_ID FOLLOWER_ID <<<"${IDS}"
AUTH=(-H "X-User-Id: ${BUYER_ID}")
JSON=(-H "Content-Type: application/json")

echo "  product=${PRODUCT_ID} seller=${SELLER_ID} order=${ORDER_ID} post=${POST_ID} buyer=${BUYER_ID} follower=${FOLLOWER_ID}"

log "Operational endpoints"
check 200 GET /health/live
check 200 GET /health/ready
check 200 GET /health/info

log "Catalogue"
check 200 GET /api/categories
check 200 GET "/api/products?limit=20&include_total=true"
check 200 GET "/api/products?category_id=${CATEGORY_ID}&limit=20"
check 200 GET "/api/products?q=lamp&limit=20"
check 200 GET "/api/products/${PRODUCT_ID}"
check 200 GET "/api/products/${PRODUCT_ID}/reviews?limit=20"
check 404 GET /api/products/99999999

log "Users and orders"
check 200 GET "/api/users/${BUYER_ID}"
check 200 GET "/api/users/${BUYER_ID}/orders?limit=20"
check 200 GET "/api/users/${BUYER_ID}/reviews?limit=20"
check 200 GET "/api/orders?limit=20" "${AUTH[@]}"
check 200 GET "/api/orders/${ORDER_ID}"
check 401 GET "/api/orders?limit=20"

log "Community"
check 200 GET "/api/feed?limit=20" -H "X-User-Id: ${FOLLOWER_ID}"
check 200 GET "/api/posts/${POST_ID}"
check 401 GET /api/feed

log "Back office"
check 200 GET /api/admin/stats "${AUTH[@]}"
check 200 GET /api/admin/export "${AUTH[@]}"
check 200 GET "/api/admin/export?since=2099-01-01T00:00:00Z" "${AUTH[@]}"

log "Writes (these mutate the local dataset)"
check 201 POST "/api/products/${PRODUCT_ID}/reviews" "${AUTH[@]}" "${JSON[@]}" \
  -d '{"rating":5,"title":"smoke test","body":"written by scripts/smoke.sh"}'
check 201 POST /api/posts -H "X-User-Id: ${SELLER_ID}" "${JSON[@]}" \
  -d "{\"title\":\"smoke test post\",\"body\":\"written by scripts/smoke.sh\",\"product_id\":${PRODUCT_ID}}"

NEW_ORDER="$(curl -s -X POST "${AUTH[@]}" "${JSON[@]}" \
  -d "{\"items\":[{\"product_id\":${PRODUCT_ID},\"quantity\":1}]}" \
  "${BASE_URL}/api/orders")"
NEW_ORDER_ID="$(printf '%s' "${NEW_ORDER}" | "${VENV_PY}" -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)"

if [[ -n "${NEW_ORDER_ID}" ]]; then
  printf '  \033[32mok\033[0m   %-6s %-45s 201  (order %s)\n' POST /api/orders "${NEW_ORDER_ID}"
  CHECKS=$((CHECKS + 1))
  check 200 POST /api/checkout "${AUTH[@]}" "${JSON[@]}" -d "{\"order_id\":${NEW_ORDER_ID}}"
  check 409 POST /api/checkout "${AUTH[@]}" "${JSON[@]}" -d "{\"order_id\":${NEW_ORDER_ID}}"
else
  printf '  \033[31mFAIL\033[0m %-6s %-45s could not create an order\n' POST /api/orders
  FAILURES=$((FAILURES + 1))
  CHECKS=$((CHECKS + 1))
fi

echo
if [[ "${FAILURES}" -eq 0 ]]; then
  printf '\033[32m%s/%s checks passed\033[0m\n' "${CHECKS}" "${CHECKS}"
  exit 0
fi
printf '\033[31m%s of %s checks failed\033[0m\n' "${FAILURES}" "${CHECKS}"
exit 1
