#!/usr/bin/env bash
# verify_step8.sh — Step 8 end-to-end verification script.
#
# Checks in order:
#   1.  docker build succeeds
#   2.  No build tools (gcc) in final image
#   3.  Container runs as non-root (appuser)
#   4.  docker-compose up starts both services; api waits for redis healthy
#   5.  /health returns 200 from host
#   6.  Redis cache miss→hit PROVEN via /metrics (real requests, real counters)
#   7.  HEALTHCHECK marks container unhealthy when DATABASE_URL is broken
#
# Run from the project root:
#   bash scripts/verify_step8.sh
#
# Requirements: docker, docker compose (v2 plugin), curl, python3

set -euo pipefail

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓  $*${NC}"; }
fail() { echo -e "${RED}✗  $*${NC}"; exit 1; }
step() { echo -e "\n${YELLOW}--- $* ---${NC}"; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"


# ── [1/7] docker build ────────────────────────────────────────────────────────
step "[1/7] Building Docker image"
docker build -t feature-flag-service:step8-verify .
ok "docker build succeeded"


# ── [2/7] No gcc in final image ───────────────────────────────────────────────
step "[2/7] Verifying build tools are NOT in the final image"
GCC_PATH=$(docker run --rm feature-flag-service:step8-verify which gcc 2>/dev/null || true)
if [ -n "$GCC_PATH" ]; then
    fail "gcc found in final image at: $GCC_PATH — multi-stage build not working correctly"
fi
ok "gcc not present in final image (multi-stage build confirmed)"


# ── [3/7] Non-root user ───────────────────────────────────────────────────────
step "[3/7] Verifying container runs as non-root"
WHOAMI=$(docker run --rm feature-flag-service:step8-verify whoami)
if [ "$WHOAMI" = "root" ]; then
    fail "Container is running as root — non-root USER directive not applied"
fi
ok "Container runs as: $WHOAMI (non-root confirmed)"


# ── [4/7] docker-compose up ───────────────────────────────────────────────────
step "[4/7] Starting full stack with docker compose"

# Tear down any previous compose run cleanly
docker compose down --remove-orphans 2>/dev/null || true

# Remove ANY container still holding port 8000 — catches leftovers from
# failed previous runs that `compose down` doesn't know about.
HOLDING=$(docker ps -aq --filter "publish=8000" 2>/dev/null || true)
if [ -n "$HOLDING" ]; then
    echo "  Found container(s) holding port 8000 — removing: $HOLDING"
    docker rm -f $HOLDING
fi

# Kill any host process still on 8000 (e.g. uvicorn left running)
HOST_PID=$(lsof -ti :8000 2>/dev/null || true)
if [ -n "$HOST_PID" ]; then
    echo "  Killing host process on port 8000: PID $HOST_PID"
    kill "$HOST_PID" 2>/dev/null || true
    sleep 1
fi

echo "Starting services (redis first via depends_on: service_healthy)…"
docker compose up -d --build

echo "Waiting for api container to become healthy (up to 90s)…"
DEADLINE=$(( $(date +%s) + 90 ))
while true; do
    API_STATUS=$(docker inspect --format='{{.State.Health.Status}}' feature-flag-api 2>/dev/null || echo "not_found")
    if [ "$API_STATUS" = "healthy" ]; then
        break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "Container health status: $API_STATUS"
        docker compose logs api | tail -30
        fail "api container did not become healthy within 90 seconds"
    fi
    sleep 3
done
ok "Both services up; api container is healthy"


# ── [5/7] /health from host ───────────────────────────────────────────────────
step "[5/7] Probing /health from host"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/health)
if [ "$HTTP_CODE" != "200" ]; then
    fail "/health returned HTTP $HTTP_CODE (expected 200)"
fi
HEALTH_BODY=$(curl -s http://localhost:8000/health)
echo "  Response: $HEALTH_BODY"
ok "/health returned 200 with body: $HEALTH_BODY"


# ── [6/7] Redis cache miss→hit inside container network ───────────────────────
step "[6/7] Proving Redis cache hit via /metrics (miss→hit sequence)"
#
# This is the check that proves REDIS_URL=redis://redis:6379 actually works —
# not just that the value is written in docker-compose.yml.
# Every sub-step must succeed. There is NO fallback. A skip here is a failure.

# 6a — Seed admin user in Neon (same DB the containerised API uses).
# create_admin.py is idempotent in the sense that "already exists" is also fine.
echo "  Ensuring admin user exists in Neon DB…"
SEED_OUT=$(python3 scripts/create_admin.py --username admin --password AdminPass123! 2>&1 || true)
echo "  Seed result: $SEED_OUT"
if echo "$SEED_OUT" | grep -qE "created successfully|already exists"; then
    ok "Admin user ready"
else
    fail "Admin seeding failed — cannot continue without a login-able user. Output: $SEED_OUT"
fi

# 6b — Obtain JWT from the CONTAINERISED API.
# Correct endpoint: POST /auth/login  (confirmed from app/routers/auth.py)
# The earlier run hit /auth/token which doesn't exist — that was the bug.
echo "  Obtaining JWT from containerised API (POST /auth/login)…"
TOKEN_RESPONSE=$(curl -s -X POST http://localhost:8000/auth/login \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=admin&password=AdminPass123!")
echo "  Login response: $TOKEN_RESPONSE"

TOKEN=$(echo "$TOKEN_RESPONSE" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null || true)
if [ -z "$TOKEN" ]; then
    fail "JWT login failed — cannot continue. Response: $TOKEN_RESPONSE"
fi
ok "JWT token obtained from containerised API"

# 6c — Create a uniquely-named flag via the containerised API.
FLAG_NAME="step8-cache-probe-$(date +%s)"
ENVIRONMENT="dev"
echo "  Creating flag: $FLAG_NAME"
CREATE_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8000/flags \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$FLAG_NAME\",\"description\":\"Step 8 cache probe\",\"flag_type\":\"boolean\",\"default_value\":true,\"environment\":\"$ENVIRONMENT\"}")
[ "$CREATE_STATUS" = "201" ] || fail "Flag creation returned HTTP $CREATE_STATUS (expected 201)"
ok "Flag '$FLAG_NAME' created (HTTP 201)"

# 6d — Snapshot /metrics BEFORE evaluations.
BEFORE=$(curl -s http://localhost:8000/metrics)
B_MISS=$(echo "$BEFORE" | grep 'flag_evaluations_cache_result_total{result="miss"}' | grep -v '#' | awk '{print $NF}' || echo "0")
B_HIT=$(echo "$BEFORE"  | grep 'flag_evaluations_cache_result_total{result="hit"}'  | grep -v '#' | awk '{print $NF}' || echo "0")
echo "  Before — miss=${B_MISS:-0}  hit=${B_HIT:-0}"

# 6e — Evaluate twice through the containerised API.
# Call 1 → cache MISS  (flag just created, nothing in Redis yet)
# Call 2 → cache HIT   (same TTL window, value is now in Redis)
# The API container reaches Redis via REDIS_URL=redis://redis:6379 over the
# compose internal bridge. If that were wrong, the app would have crashed at
# startup (ping fails). The redis_unavailable=0 assertion below additionally
# confirms no silent degradation happened during evaluation itself.
echo "  Evaluate call 1 (expect MISS)…"
E1=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8000/evaluate \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"flag_name\":\"$FLAG_NAME\",\"user_id\":\"u1\",\"environment\":\"$ENVIRONMENT\"}")
[ "$E1" = "200" ] || fail "Evaluate call 1 returned HTTP $E1 (expected 200)"

echo "  Evaluate call 2 (expect HIT)…"
E2=$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST http://localhost:8000/evaluate \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"flag_name\":\"$FLAG_NAME\",\"user_id\":\"u2\",\"environment\":\"$ENVIRONMENT\"}")
[ "$E2" = "200" ] || fail "Evaluate call 2 returned HTTP $E2 (expected 200)"

# 6f — Read /metrics AFTER and assert counters moved.
AFTER=$(curl -s http://localhost:8000/metrics)
A_MISS=$(echo "$AFTER"    | grep 'flag_evaluations_cache_result_total{result="miss"}' | grep -v '#' | awk '{print $NF}' || echo "0")
A_HIT=$(echo "$AFTER"     | grep 'flag_evaluations_cache_result_total{result="hit"}'  | grep -v '#' | awk '{print $NF}' || echo "0")
A_UNAVAIL=$(echo "$AFTER" | grep 'flag_evaluations_cache_result_total{result="redis_unavailable"}' | grep -v '#' | awk '{print $NF}' || echo "0")

echo "  After  — miss=${A_MISS:-0}  hit=${A_HIT:-0}  redis_unavailable=${A_UNAVAIL:-0}"
echo "  Raw counter lines from /metrics:"
echo "$AFTER" | grep 'flag_evaluations_cache_result_total' | grep -v '#' | sed 's/^/    /'

# Strip .0 suffix for integer comparison
BI_MISS=$(echo "${B_MISS:-0}" | cut -d. -f1)
AI_MISS=$(echo "${A_MISS:-0}" | cut -d. -f1)
BI_HIT=$(echo "${B_HIT:-0}"   | cut -d. -f1)
AI_HIT=$(echo "${A_HIT:-0}"   | cut -d. -f1)
UNAVAIL=$(echo "${A_UNAVAIL:-0}" | cut -d. -f1)

[ "$AI_MISS" -gt "$BI_MISS" ] \
    || fail "miss counter did not increment (before=$BI_MISS after=$AI_MISS) — cache miss not recorded"
[ "$AI_HIT" -gt "$BI_HIT" ] \
    || fail "hit counter did not increment (before=$BI_HIT after=$AI_HIT) — cache hit not recorded"
[ "$UNAVAIL" = "0" ] \
    || fail "redis_unavailable=$UNAVAIL (non-zero) — Redis not reachable inside compose network"

ok "miss counter: $BI_MISS → $AI_MISS"
ok "hit  counter: $BI_HIT → $AI_HIT"
ok "redis_unavailable=0 — API container is genuinely reaching Redis over compose network"


# ── [7/7] HEALTHCHECK transitions to unhealthy when DATABASE_URL is broken ────
step "[7/7] Verifying HEALTHCHECK reports unhealthy on broken DATABASE_URL"

echo "  Stopping normal api container…"
docker compose stop api

# Determine the compose network name so the test container can reach redis
COMPOSE_NETWORK=$(docker network ls --filter "name=featureflagservice" --format "{{.Name}}" | head -1 || true)

echo "  Starting api with a deliberately broken DATABASE_URL…"
docker rm -f feature-flag-api-unhealthy-test 2>/dev/null || true

if [ -n "$COMPOSE_NETWORK" ]; then
    docker run -d \
        --name feature-flag-api-unhealthy-test \
        --network "$COMPOSE_NETWORK" \
        -e DATABASE_URL="postgresql://baduser:badpass@invalid-host/baddb" \
        -e REDIS_URL="redis://redis:6379" \
        -e JWT_SECRET_KEY="testsecret_not_real" \
        -p 8001:8000 \
        feature-flag-service:step8-verify
else
    # Fallback: no network (Redis ping will also fail, but DB fails first)
    docker run -d \
        --name feature-flag-api-unhealthy-test \
        -e DATABASE_URL="postgresql://baduser:badpass@invalid-host/baddb" \
        -e REDIS_URL="redis://localhost:6379" \
        -e JWT_SECRET_KEY="testsecret_not_real" \
        -p 8001:8000 \
        feature-flag-service:step8-verify
fi

echo "  Waiting for HEALTHCHECK to report unhealthy (up to 90s)…"
echo "  (start-period=10s then interval=30s × retries=3 — first unhealthy after ~40s)"
DEADLINE=$(( $(date +%s) + 90 ))
LAST_STATUS=""
while true; do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' \
        feature-flag-api-unhealthy-test 2>/dev/null || echo "not_found")
    if [ "$STATUS" != "$LAST_STATUS" ]; then
        echo "  Health status: $STATUS"
        LAST_STATUS="$STATUS"
    fi
    if [ "$STATUS" = "unhealthy" ]; then
        ok "HEALTHCHECK correctly reports 'unhealthy' when /health is unreachable"
        break
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
        echo "  Final health status: $STATUS"
        docker logs feature-flag-api-unhealthy-test 2>&1 | tail -20
        fail "HEALTHCHECK did not reach 'unhealthy' within 90s (final status: $STATUS)"
    fi
    sleep 5
done

docker rm -f feature-flag-api-unhealthy-test 2>/dev/null || true

echo "  Restoring normal docker-compose stack…"
docker compose up -d


# ── Summary ───────────────────────────────────────────────────────────────────
echo -e "\n${GREEN}======================================${NC}"
echo -e "${GREEN}  Step 8 VERIFIED — All checks passed ${NC}"
echo -e "${GREEN}======================================${NC}"
echo ""
echo "Stack is running. To stop:  docker compose down"
echo "To rebuild:                 docker compose up --build"
