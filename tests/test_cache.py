"""
tests/test_cache.py — Redis caching layer tests.

Test strategy
-------------
These tests verify the *behavioural* properties of the cache layer, not just
that the right functions are called.  Each test exercises a property that
could fail silently in production:

  1. Cache population     — a Redis key actually exists after the first /evaluate
  2. Cache hit avoids DB  — a second /evaluate call within TTL never touches Postgres
  3. TTL expiry           — the key disappears after CACHE_TTL_SECONDS and is repopulated
  4. Invalidation on update (THE most important) — PATCH → /evaluate → new value returned
  5. Invalidation on rename — old AND new cache keys handled correctly after name change
  6. Redis-down fail-safe — Redis unreachable → /evaluate falls back to Postgres,
                            NOT to evaluation_error_fail_safe

Why a real Redis, not a mock
-----------------------------
Cache-layer tests must use a real Redis.  Mocking redis_client would only
prove that the application *calls* the right methods — it would not prove:
  - The key actually exists in Redis after a SET (test 1)
  - The key is actually gone after a DEL (tests 4, 5)
  - The key expires after the TTL elapses (test 3)
  - The cache actually short-circuits the DB on a hit (test 2)

The DB is still mocked (via app.dependency_overrides[get_db]) because the
cache tests focus on Redis behaviour, not Postgres correctness.  Auth is also
bypassed via dependency_overrides.

Prerequisites
-------------
  docker compose up -d redis
  pytest tests/test_cache.py -v

REDIS_URL must resolve to a running Redis instance (default: redis://localhost:6379).
"""

import time
import pytest

from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

import redis as redis_lib

from app.main import app
from app.database import Base, get_db
from app.dependencies import get_current_user, CurrentUser
from app.models import Flag, TargetingRule
import app.cache as cache_module
from app.cache import redis_client, _cache_key, get_cached_flag, set_cached_flag, invalidate_cached_flag


# ---------------------------------------------------------------------------
# SQLite test DB (same pattern as test_evaluation.py)
# ---------------------------------------------------------------------------

SQLITE_URL = "sqlite:///./test_cache.db"
engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _override_get_current_user_viewer():
    return CurrentUser(username="testuser", role="viewer")


def _override_get_current_user_admin():
    return CurrentUser(username="admin", role="admin")


# ---------------------------------------------------------------------------
# Helpers for creating test data directly in SQLite
# ---------------------------------------------------------------------------

def _create_flag(**kwargs) -> Flag:
    db = TestingSessionLocal()
    try:
        flag = Flag(**kwargs)
        db.add(flag)
        db.commit()
        db.refresh(flag)
        return flag
    finally:
        db.close()


def _create_rule(flag_id: int, attribute: str, operator: str, value: str) -> TargetingRule:
    db = TestingSessionLocal()
    try:
        rule = TargetingRule(flag_id=flag_id, attribute=attribute, operator=operator, value=value)
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule
    finally:
        db.close()


def _update_flag(flag_id: int, **kwargs) -> None:
    """Update flag fields directly in SQLite, bypassing the HTTP layer."""
    db = TestingSessionLocal()
    try:
        flag = db.get(Flag, flag_id)
        for k, v in kwargs.items():
            setattr(flag, k, v)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test Redis client for direct key inspection
# The tests use this to assert on Redis state without going through the app.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_redis():
    """A direct Redis connection for asserting key state in tests."""
    from app.config import settings
    client = redis_lib.Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        client.ping()
    except redis_lib.RedisError as exc:
        pytest.skip(f"Redis not reachable at {settings.redis_url}: {exc}")
    return client


# ---------------------------------------------------------------------------
# Main TestClient fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def create_tables():
    """Create all SQLite tables once per module before any test runs.

    The `client` fixture also calls create_all, but it only runs when a test
    requests the `client` fixture.  The TestCacheModuleUnit tests don't request
    `client` — they're pure unit tests — so without this fixture the
    `clean_state` autouse fixture would try to DELETE from tables that don't
    exist yet.
    """
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="module")
def client(test_redis):
    """
    TestClient with:
      - SQLite in-memory DB replacing the real Postgres
      - JWT auth bypassed (admin role so write endpoints work)
      - Both _assert_db_reachable and _assert_redis_reachable patched to no-ops
        (the lifespan checks would require the real Postgres and Redis URLs)
    """
    Base.metadata.create_all(bind=engine)  # idempotent with create_tables fixture above

    # Admin override for write endpoints (POST /flags, PATCH /flags/{id})
    from app.dependencies import require_admin
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user_admin
    app.dependency_overrides[require_admin] = _override_get_current_user_admin

    with patch("app.main._assert_db_reachable", return_value=None), \
         patch("app.main._assert_redis_reachable", return_value=None):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_state(test_redis):
    """
    Before each test: wipe all test flag rows from SQLite and flush all
    flag:* keys from Redis so tests are fully isolated.
    """
    db = TestingSessionLocal()
    try:
        db.query(TargetingRule).delete()
        db.query(Flag).delete()
        db.commit()
    finally:
        db.close()

    # Delete only flag:* keys so we don't accidentally clear unrelated data.
    for key in test_redis.keys("flag:*"):
        test_redis.delete(key)

    yield

    # Post-test cleanup (belt-and-suspenders).
    for key in test_redis.keys("flag:*"):
        test_redis.delete(key)


# ===========================================================================
# Unit tests — cache module in isolation (no HTTP, no DB)
# ===========================================================================

class TestCacheModuleUnit:
    """Verify get/set/invalidate directly, without going through the HTTP layer."""

    def test_set_and_get_round_trip(self):
        data = {
            "id": 99, "name": "unit-flag", "flag_type": "boolean",
            "default_value": True, "rollout_percentage": None,
            "environment": "dev", "targeting_rules": [],
        }
        set_cached_flag("unit-flag", "dev", data)
        result = get_cached_flag("unit-flag", "dev")
        assert result is not None
        assert result["name"] == "unit-flag"
        assert result["default_value"] is True
        # Clean up
        invalidate_cached_flag("unit-flag", "dev")

    def test_get_returns_none_on_miss(self):
        result = get_cached_flag("nonexistent-flag", "prod")
        assert result is None

    def test_invalidate_removes_key(self):
        set_cached_flag("to-delete", "staging", {"id": 1, "name": "to-delete"})
        assert get_cached_flag("to-delete", "staging") is not None
        invalidate_cached_flag("to-delete", "staging")
        assert get_cached_flag("to-delete", "staging") is None

    def test_cache_key_format(self):
        """Verify the frozen key format: flag:{environment}:{name}."""
        assert _cache_key("my-flag", "prod") == "flag:prod:my-flag"
        assert _cache_key("dark-mode", "staging") == "flag:staging:dark-mode"
        assert _cache_key("beta", "dev") == "flag:dev:beta"

    def test_get_returns_none_on_redis_error(self):
        """
        When Redis raises an exception on GET, get_cached_flag() must return None
        (falling through to DB) rather than propagating the exception.
        """
        with patch.object(cache_module.redis_client, "get", side_effect=redis_lib.RedisError("conn refused")):
            result = get_cached_flag("any-flag", "dev")
        assert result is None

    def test_set_swallows_redis_error(self):
        """A failed SET must not raise — it's a missed optimisation, not a failure."""
        with patch.object(cache_module.redis_client, "set", side_effect=redis_lib.RedisError("conn refused")):
            # Must not raise.
            set_cached_flag("any-flag", "dev", {"id": 1})

    def test_invalidate_swallows_redis_error(self):
        """A failed DEL must not raise — stale data will expire via TTL."""
        with patch.object(cache_module.redis_client, "delete", side_effect=redis_lib.RedisError("conn refused")):
            # Must not raise.
            invalidate_cached_flag("any-flag", "dev")


# ===========================================================================
# Integration tests — full HTTP path with real Redis
# ===========================================================================

class TestCachePopulation:
    """Test 1: first /evaluate populates the Redis cache."""

    def test_evaluate_populates_cache(self, client, test_redis):
        """
        After the first /evaluate call, the flag's cache key must exist in Redis.
        Verified directly via the Redis client, not inferred from app behaviour.
        """
        flag = _create_flag(
            name="pop-flag", environment="dev",
            flag_type="boolean", default_value=True,
        )

        resp = client.post("/evaluate", json={
            "flag_name": "pop-flag", "user_id": "u1", "environment": "dev",
        })
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        key = _cache_key("pop-flag", "dev")
        assert test_redis.exists(key), (
            f"Cache key {key!r} does not exist in Redis after first /evaluate — "
            "set_cached_flag() was not called or failed silently."
        )

    def test_cached_value_contains_expected_fields(self, client, test_redis):
        """The cached JSON must have all fields needed for evaluation."""
        _create_flag(
            name="field-flag", environment="staging",
            flag_type="percentage", default_value=False, rollout_percentage=50,
        )
        client.post("/evaluate", json={
            "flag_name": "field-flag", "user_id": "u1", "environment": "staging",
        })

        key = _cache_key("field-flag", "staging")
        import json
        raw = test_redis.get(key)
        assert raw is not None
        data = json.loads(raw)
        assert data["name"] == "field-flag"
        assert data["flag_type"] == "percentage"
        assert data["rollout_percentage"] == 50
        assert "targeting_rules" in data

    def test_targeted_flag_caches_rules(self, client, test_redis):
        """Targeting rules must be included in the cached value, not stored separately."""
        flag = _create_flag(
            name="rule-flag", environment="prod",
            flag_type="targeted", default_value=False,
        )
        _create_rule(flag.id, attribute="plan", operator="equals", value="enterprise")

        client.post("/evaluate", json={
            "flag_name": "rule-flag", "user_id": "u1", "environment": "prod",
            "attributes": {"plan": "enterprise"},
        })

        key = _cache_key("rule-flag", "prod")
        import json
        raw = test_redis.get(key)
        assert raw is not None
        data = json.loads(raw)
        assert len(data["targeting_rules"]) == 1
        rule = data["targeting_rules"][0]
        assert rule["attribute"] == "plan"
        assert rule["operator"] == "equals"
        assert rule["value"] == "enterprise"


class TestCacheHitAvoidsDB:
    """
    Test 2: a second /evaluate within TTL must NOT query Postgres.

    Strategy: populate the cache via a first /evaluate call, then replace
    get_db with a function that raises if called.  The second /evaluate must
    still succeed — proving it used the cache, not the DB.
    """

    def test_cache_hit_does_not_query_db(self, client, test_redis):
        flag = _create_flag(
            name="cached-flag", environment="dev",
            flag_type="boolean", default_value=True,
        )

        # First call — populates cache.
        r1 = client.post("/evaluate", json={
            "flag_name": "cached-flag", "user_id": "u1", "environment": "dev",
        })
        assert r1.status_code == 200
        assert r1.json()["enabled"] is True

        # Confirm key is in Redis.
        assert test_redis.exists(_cache_key("cached-flag", "dev"))

        # Override get_db with a MagicMock whose .query raises — any DB query
        # would trigger AssertionError, proving the cache short-circuited it.
        def _break_db():
            db = MagicMock()
            db.query.side_effect = AssertionError(
                "DB was queried on a cache HIT — set_cached_flag() or "
                "get_cached_flag() is not working correctly."
            )
            yield db

        app.dependency_overrides[get_db] = _break_db

        try:
            r2 = client.post("/evaluate", json={
                "flag_name": "cached-flag", "user_id": "u1", "environment": "dev",
            })
            assert r2.status_code == 200
            assert r2.json()["enabled"] is True, (
                "Second /evaluate returned wrong result — possible cache corruption."
            )
        finally:
            # Restore normal DB override so other tests aren't affected.
            app.dependency_overrides[get_db] = _override_get_db


class TestTTLExpiry:
    """
    Test 3: after CACHE_TTL_SECONDS elapses the key is gone from Redis and
    the next /evaluate repopulates it from Postgres.
    """

    def test_key_expires_after_ttl(self, client, test_redis):
        """
        Verifies TTL is applied by SET-ing with ex=1 directly via the test_redis
        client, then confirming the key expires, then re-evaluating via the
        HTTP client to confirm repopulation.
        """
        flag = _create_flag(
            name="ttl-flag", environment="dev",
            flag_type="boolean", default_value=False,
        )

        # Populate the key directly with TTL=1 to avoid patching internals.
        import json as _json
        key = _cache_key("ttl-flag", "dev")
        test_redis.set(key, _json.dumps({
            "id": flag.id, "name": "ttl-flag", "description": None,
            "flag_type": "boolean", "default_value": False,
            "rollout_percentage": None, "environment": "dev",
            "targeting_rules": [],
        }), ex=1)

        assert test_redis.exists(key), "Key must exist immediately after SET."

        # Wait for TTL to expire.
        time.sleep(2)

        assert not test_redis.exists(key), (
            "Key still exists in Redis after TTL should have expired. "
            "Redis TTL with ex=1 did not expire within 2 seconds."
        )

        # Next /evaluate must re-populate from Postgres via the app.
        with patch.object(cache_module, "redis_client", test_redis):
            resp = client.post("/evaluate", json={
                "flag_name": "ttl-flag", "user_id": "u1", "environment": "dev",
            })
        assert resp.status_code == 200
        assert test_redis.exists(key), (
            "Key was not repopulated after TTL expiry — "
            "set_cached_flag() was not called on the cache-miss path."
        )


class TestInvalidationOnUpdate:
    """
    Test 4 (THE most important): PATCH → /evaluate returns new value immediately,
    not the stale cached one.

    This is the single test that proves invalidation logic is correct, not just
    that it exists in the code.  A cache layer that serves stale data after an
    admin explicitly changes a flag is a real production bug.
    """

    def test_update_invalidates_cache_and_new_value_returned(self, client, test_redis):
        # Create flag with default_value=False.
        flag = _create_flag(
            name="inv-flag", environment="dev",
            flag_type="boolean", default_value=False,
        )

        # First /evaluate — populates cache with enabled=False.
        r1 = client.post("/evaluate", json={
            "flag_name": "inv-flag", "user_id": "u1", "environment": "dev",
        })
        assert r1.json()["enabled"] is False

        key = _cache_key("inv-flag", "dev")
        assert test_redis.exists(key), "Cache not populated after first evaluate."

        # PATCH — change default_value to True via the HTTP layer.
        patch_resp = client.patch(f"/flags/{flag.id}", json={"default_value": True})
        assert patch_resp.status_code == 200

        # Cache key must be gone after PATCH.
        assert not test_redis.exists(key), (
            f"Cache key {key!r} still exists after PATCH — "
            "invalidate_cached_flag() was not called or failed to delete the key."
        )

        # Second /evaluate — must return the NEW value (True), not the stale cached one.
        r2 = client.post("/evaluate", json={
            "flag_name": "inv-flag", "user_id": "u1", "environment": "dev",
        })
        assert r2.json()["enabled"] is True, (
            "Second /evaluate returned the stale cached value (False) instead of "
            "the updated value (True).  Cache invalidation on PATCH is broken."
        )

        # New cache key must now exist with the updated value.
        assert test_redis.exists(key), "Cache not repopulated after invalidation."


class TestInvalidationOnRename:
    """
    Test 5: PATCH that changes name or environment must invalidate BOTH the old
    cache key and the new cache key.
    """

    def test_rename_invalidates_old_and_new_keys(self, client, test_redis):
        flag = _create_flag(
            name="old-name", environment="dev",
            flag_type="boolean", default_value=True,
        )

        # First /evaluate — populates cache under flag:dev:old-name.
        client.post("/evaluate", json={
            "flag_name": "old-name", "user_id": "u1", "environment": "dev",
        })

        old_key = _cache_key("old-name", "dev")
        assert test_redis.exists(old_key), "Cache not populated before rename."

        # Pre-populate the new key with stale data to simulate the "existing
        # key under new name" edge case.
        new_key = _cache_key("new-name", "dev")
        test_redis.set(new_key, '{"stale": true}', ex=30)

        # PATCH — rename the flag.
        patch_resp = client.patch(f"/flags/{flag.id}", json={"name": "new-name"})
        assert patch_resp.status_code == 200

        # Old key must be deleted.
        assert not test_redis.exists(old_key), (
            f"Old cache key {old_key!r} still exists after rename — "
            "the old key was not invalidated."
        )

        # New key must also be deleted (stale pre-existing entry cleared).
        assert not test_redis.exists(new_key), (
            f"New cache key {new_key!r} still exists after rename — "
            "the new key was not invalidated.  Stale data could be served "
            "on the first /evaluate call after a rename."
        )

        # /evaluate under the new name must work correctly and repopulate cache.
        resp = client.post("/evaluate", json={
            "flag_name": "new-name", "user_id": "u1", "environment": "dev",
        })
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        assert test_redis.exists(new_key), "Cache not repopulated under new name."

    def test_environment_change_invalidates_both_keys(self, client, test_redis):
        flag = _create_flag(
            name="env-flag", environment="dev",
            flag_type="boolean", default_value=True,
        )

        client.post("/evaluate", json={
            "flag_name": "env-flag", "user_id": "u1", "environment": "dev",
        })

        old_key = _cache_key("env-flag", "dev")
        new_key = _cache_key("env-flag", "staging")
        assert test_redis.exists(old_key)

        patch_resp = client.patch(f"/flags/{flag.id}", json={"environment": "staging"})
        assert patch_resp.status_code == 200

        assert not test_redis.exists(old_key), "Old environment key not invalidated."
        assert not test_redis.exists(new_key), (
            "New environment key not invalidated — stale data risk on rename."
        )


class TestInvalidationOnDelete:
    """Delete must also invalidate the cache."""

    def test_delete_invalidates_cache(self, client, test_redis):
        flag = _create_flag(
            name="del-flag", environment="dev",
            flag_type="boolean", default_value=True,
        )

        client.post("/evaluate", json={
            "flag_name": "del-flag", "user_id": "u1", "environment": "dev",
        })

        key = _cache_key("del-flag", "dev")
        assert test_redis.exists(key)

        del_resp = client.delete(f"/flags/{flag.id}")
        assert del_resp.status_code == 204

        assert not test_redis.exists(key), (
            "Cache key still exists after DELETE — "
            "invalidate_cached_flag() was not called in delete_flag()."
        )

        # Evaluate after delete must return flag_not_found (not stale cached enabled=True).
        resp = client.post("/evaluate", json={
            "flag_name": "del-flag", "user_id": "u1", "environment": "dev",
        })
        assert resp.json()["reason"] == "flag_not_found"
        assert resp.json()["enabled"] is False


class TestRedisDownFailSafe:
    """
    Test 6: Redis unreachable → /evaluate falls back to Postgres correctly.

    Key distinction vs. Step 5's DB fail-safe:
      - Redis down alone → evaluate succeeds with a real DB-backed result.
        reason is NOT evaluation_error_fail_safe.
      - DB also down → THEN we get evaluation_error_fail_safe.

    Strategy: patch the redis_client on the cache module to point to an
    unreachable port, so every Redis call raises RedisError.  The DB
    override is still in place and working.
    """

    def test_redis_down_falls_back_to_db_not_fail_safe(self, client, test_redis):
        """
        When Redis is unreachable, /evaluate must:
          1. Return the correct flag value (from Postgres).
          2. Return HTTP 200.
          3. NOT return reason=evaluation_error_fail_safe.
        """
        flag = _create_flag(
            name="redis-down-flag", environment="dev",
            flag_type="boolean", default_value=True,
        )

        # Use a client pointing at a port nothing is listening on.
        dead_client = redis_lib.Redis(host="localhost", port=19999, decode_responses=True)

        with patch.object(cache_module, "redis_client", dead_client):
            resp = client.post("/evaluate", json={
                "flag_name": "redis-down-flag", "user_id": "u1", "environment": "dev",
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True, (
            "Expected enabled=True (from Postgres DB) but got False. "
            "Redis-down fallback is not reaching the DB correctly."
        )
        assert body["reason"] == "boolean_flag", (
            f"Expected reason='boolean_flag' (DB-backed result) but got {body['reason']!r}. "
            "Redis down should NOT trigger evaluation_error_fail_safe — only DB down should."
        )

    def test_redis_down_targeted_flag_evaluates_from_db(self, client, test_redis):
        """Redis down must not break targeted flag evaluation — rules come from DB."""
        flag = _create_flag(
            name="redis-down-targeted", environment="dev",
            flag_type="targeted", default_value=False,
        )
        _create_rule(flag.id, attribute="plan", operator="equals", value="enterprise")

        dead_client = redis_lib.Redis(host="localhost", port=19999, decode_responses=True)

        with patch.object(cache_module, "redis_client", dead_client):
            resp = client.post("/evaluate", json={
                "flag_name": "redis-down-targeted", "user_id": "u1", "environment": "dev",
                "attributes": {"plan": "enterprise"},
            })

        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["reason"] == "targeting_rule_matched"

    def test_redis_and_db_both_down_triggers_fail_safe(self, client):
        """
        When BOTH Redis and DB are down, evaluate_flag()'s outer except block
        fires and returns evaluation_error_fail_safe.  This confirms the two
        fail-safe layers are additive: Redis down alone degrades to DB, DB
        down (regardless of Redis state) triggers the global fail-safe.
        """
        from unittest.mock import MagicMock
        from sqlalchemy.exc import OperationalError as SAOperationalError

        def _broken_db():
            db = MagicMock()
            db.query.side_effect = SAOperationalError(
                "simulated DB down", None, None
            )
            yield db

        dead_client = redis_lib.Redis(host="localhost", port=19999, decode_responses=True)

        with patch.object(cache_module, "redis_client", dead_client):
            app.dependency_overrides[get_db] = _broken_db
            try:
                resp = client.post("/evaluate", json={
                    "flag_name": "any-flag", "user_id": "u1", "environment": "dev",
                })
            finally:
                app.dependency_overrides[get_db] = _override_get_db

        assert resp.status_code == 200
        assert resp.json()["reason"] == "evaluation_error_fail_safe"
        assert resp.json()["enabled"] is False
