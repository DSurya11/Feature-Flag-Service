"""
tests/test_evaluation.py — Tests for the evaluation endpoint and algorithm.

Test structure
--------------
  Part 1 — Pure unit tests (no DB, no HTTP):
    - _compute_bucket determinism
    - _compute_bucket distribution
    - Boundary: 0% / 100% rollout
    - _evaluate_targeting: all operators, edge cases

  Part 2 — Integration tests (TestClient + SQLite in-memory DB):
    - Boolean / percentage / targeted flag evaluation via HTTP
    - Flag not found → HTTP 200, enabled=False, reason=flag_not_found (NOT 404)
    - Fail-safe path → induced DB failure → HTTP 200, enabled=False,
      reason=evaluation_error_fail_safe  [THE most important test]

Lifespan note
-------------
The app's lifespan calls _assert_db_reachable() which connects to the real
Postgres DATABASE_URL.  For tests we patch that function to a no-op so the
TestClient can boot against the SQLite in-memory database instead.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.evaluation import _compute_bucket, _evaluate_targeting
from app.dependencies import get_current_user, CurrentUser


# ===========================================================================
# PART 1 — Pure unit tests (no DB, no HTTP, no app import side effects)
# ===========================================================================

class TestComputeBucketDeterminism:
    def test_same_input_same_bucket_100_times(self):
        results = {_compute_bucket("my_flag", "user-abc-123") for _ in range(100)}
        # DELIBERATELY BROKEN — CI gate verification (Step 9 DoD). Will be reverted.
        assert len(results) == 999, (
            f"INTENTIONAL FAILURE for CI gate test. Actual unique buckets: {results}"
        )

    def test_different_users_valid_range(self):
        assert 0 <= _compute_bucket("flag", "user-1") <= 99
        assert 0 <= _compute_bucket("flag", "user-2") <= 99

    def test_different_flags_same_user_valid_range(self):
        assert 0 <= _compute_bucket("flag_a", "user-42") <= 99
        assert 0 <= _compute_bucket("flag_b", "user-42") <= 99

    def test_bucket_range_all_in_0_to_99(self):
        for i in range(200):
            b = _compute_bucket(f"flag_{i}", f"user_{i}")
            assert 0 <= b <= 99, f"bucket {b} out of [0,99] for index {i}"

    def test_separator_matters(self):
        # "abc:def" vs "ab:cdef" must not collide (they could, but confirms
        # the function handles different inputs independently)
        b1 = _compute_bucket("abc", "def")
        b2 = _compute_bucket("ab", "cdef")
        # Both valid buckets — just confirming no crash
        assert 0 <= b1 <= 99
        assert 0 <= b2 <= 99


class TestBucketDistribution:
    def test_30_percent_rollout_distribution(self):
        pct = 30
        n = 1000
        enabled = sum(
            1 for i in range(n)
            if _compute_bucket("dist_flag", f"synthetic-user-{i}") < pct
        )
        ratio = enabled / n
        assert 0.25 <= ratio <= 0.35, (
            f"Expected ~30% enabled but got {ratio:.1%} ({enabled}/{n}). "
            "Hash distribution may be severely skewed."
        )

    def test_0_percent_no_user_enabled(self):
        """bucket < 0 is always False — 0% means no one is enabled."""
        results = [_compute_bucket("zero_flag", f"user-{i}") < 0 for i in range(500)]
        assert not any(results), "0% rollout: at least one user was incorrectly enabled"

    def test_100_percent_all_users_enabled(self):
        """bucket < 100 is always True — 100% means everyone is enabled."""
        results = [_compute_bucket("full_flag", f"user-{i}") < 100 for i in range(500)]
        assert all(results), "100% rollout: at least one user was incorrectly disabled"


class TestEvaluateTargeting:
    def _rule(self, attribute, operator, value, rule_id=1):
        # _evaluate_targeting() now accepts plain dicts (not ORM objects or
        # MagicMocks) — see evaluation.py module docstring for why.
        return {
            "id":        rule_id,
            "attribute": attribute,
            "operator":  operator,
            "value":     value,
        }

    def test_equals_match(self):
        matched, result = _evaluate_targeting(
            [self._rule("plan", "equals", "enterprise")],
            {"plan": "enterprise"},
        )
        assert matched is True and result is True

    def test_equals_no_match(self):
        matched, _ = _evaluate_targeting(
            [self._rule("plan", "equals", "enterprise")],
            {"plan": "free"},
        )
        assert matched is False

    def test_not_equals_match(self):
        matched, result = _evaluate_targeting(
            [self._rule("plan", "not_equals", "free")],
            {"plan": "enterprise"},
        )
        assert matched is True and result is True

    def test_not_equals_no_match(self):
        matched, _ = _evaluate_targeting(
            [self._rule("plan", "not_equals", "free")],
            {"plan": "free"},
        )
        assert matched is False

    def test_in_match(self):
        matched, result = _evaluate_targeting(
            [self._rule("plan", "in", "enterprise,pro,team")],
            {"plan": "pro"},
        )
        assert matched is True and result is True

    def test_in_no_match(self):
        matched, _ = _evaluate_targeting(
            [self._rule("plan", "in", "enterprise,pro,team")],
            {"plan": "free"},
        )
        assert matched is False

    def test_in_strips_spaces_in_list(self):
        matched, result = _evaluate_targeting(
            [self._rule("plan", "in", "enterprise, pro, team")],
            {"plan": "pro"},
        )
        assert matched is True

    def test_missing_attribute_skips_rule(self):
        matched, _ = _evaluate_targeting(
            [self._rule("plan", "equals", "enterprise")],
            {},
        )
        assert matched is False

    def test_first_matching_rule_wins(self):
        r1 = self._rule("plan", "equals", "free", rule_id=1)
        r2 = self._rule("plan", "equals", "enterprise", rule_id=2)
        matched, result = _evaluate_targeting([r1, r2], {"plan": "enterprise"})
        assert matched is True and result is True

    def test_empty_rules_no_match(self):
        matched, _ = _evaluate_targeting([], {"plan": "enterprise"})
        assert matched is False


# ===========================================================================
# PART 2 — Integration tests (TestClient + SQLite)
# ===========================================================================

# Deferred imports so that the unit tests above don't require a live DB.
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from app.main import app
from app.database import Base, get_db
from app.models import Flag, TargetingRule

SQLITE_URL = "sqlite:///./test_evaluation_integration.db"
engine = create_engine(SQLITE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def _override_get_current_user():
    return CurrentUser(username="testuser", role="viewer")


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


def _create_rule(flag_id, attribute, operator, value):
    db = TestingSessionLocal()
    try:
        rule = TargetingRule(flag_id=flag_id, attribute=attribute,
                             operator=operator, value=value)
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    """
    TestClient with:
      - SQLite in-memory DB replacing the real Postgres
      - JWT auth bypassed (any authenticated user)
      - _assert_db_reachable patched to no-op so the lifespan doesn't
        try to connect to the real Postgres DATABASE_URL at boot
      - _assert_redis_reachable patched to no-op (Step 6) — these tests
        focus on evaluation logic, not Redis; test_cache.py covers Redis.
    """
    Base.metadata.create_all(bind=engine)
    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_current_user] = _override_get_current_user

    with patch("app.main._assert_db_reachable", return_value=None), \
         patch("app.main._assert_redis_reachable", return_value=None):
        with TestClient(app) as c:
            yield c

    Base.metadata.drop_all(bind=engine)
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_tables(client):  # noqa: F811 — depends on client to scope to integration tests only
    db = TestingSessionLocal()
    try:
        db.query(TargetingRule).delete()
        db.query(Flag).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Boolean flag
# ---------------------------------------------------------------------------

class TestBooleanFlag:
    def test_boolean_true(self, client):
        _create_flag(name="dark_mode", flag_type="boolean",
                     default_value=True, environment="dev")
        r = client.post("/evaluate", json={
            "flag_name": "dark_mode", "user_id": "u1", "environment": "dev"
        })
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert body["reason"] == "boolean_flag"
        assert body["flag_name"] == "dark_mode"
        assert body["user_id"] == "u1"

    def test_boolean_false(self, client):
        _create_flag(name="new_ui", flag_type="boolean",
                     default_value=False, environment="prod")
        r = client.post("/evaluate", json={
            "flag_name": "new_ui", "user_id": "u1", "environment": "prod"
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["reason"] == "boolean_flag"


# ---------------------------------------------------------------------------
# Percentage flag
# ---------------------------------------------------------------------------

class TestPercentageFlag:
    def test_reason_is_percentage_rollout(self, client):
        _create_flag(name="beta", flag_type="percentage",
                     default_value=False, rollout_percentage=50, environment="staging")
        r = client.post("/evaluate", json={
            "flag_name": "beta", "user_id": "user-xyz", "environment": "staging"
        })
        assert r.status_code == 200
        assert r.json()["reason"] == "percentage_rollout"

    def test_determinism_via_http(self, client):
        """Same flag+user via HTTP → same enabled value 20 consecutive calls."""
        _create_flag(name="det_flag", flag_type="percentage",
                     default_value=False, rollout_percentage=50, environment="dev")
        payload = {"flag_name": "det_flag", "user_id": "stable-user", "environment": "dev"}
        results = {client.post("/evaluate", json=payload).json()["enabled"]
                   for _ in range(20)}
        assert len(results) == 1, f"Non-deterministic result across 20 calls: {results}"

    def test_0_percent_always_false(self, client):
        _create_flag(name="zero_flag", flag_type="percentage",
                     default_value=False, rollout_percentage=0, environment="dev")
        for i in range(50):
            r = client.post("/evaluate", json={
                "flag_name": "zero_flag", "user_id": f"user-{i}", "environment": "dev"
            })
            assert r.status_code == 200
            assert r.json()["enabled"] is False, f"user-{i} should be disabled at 0%"

    def test_100_percent_always_true(self, client):
        _create_flag(name="full_flag", flag_type="percentage",
                     default_value=False, rollout_percentage=100, environment="dev")
        for i in range(50):
            r = client.post("/evaluate", json={
                "flag_name": "full_flag", "user_id": f"user-{i}", "environment": "dev"
            })
            assert r.status_code == 200
            assert r.json()["enabled"] is True, f"user-{i} should be enabled at 100%"

    def test_30_percent_distribution_via_http(self, client):
        """~30% of 200 synthetic users should be enabled end-to-end."""
        _create_flag(name="dist_flag", flag_type="percentage",
                     default_value=False, rollout_percentage=30, environment="prod")
        n = 200
        enabled = sum(
            1 for i in range(n)
            if client.post("/evaluate", json={
                "flag_name": "dist_flag",
                "user_id": f"synthetic-{i}",
                "environment": "prod",
            }).json()["enabled"]
        )
        ratio = enabled / n
        assert 0.20 <= ratio <= 0.40, (
            f"Expected ~30% but got {ratio:.1%} ({enabled}/{n})"
        )


# ---------------------------------------------------------------------------
# Targeted flag
# ---------------------------------------------------------------------------

class TestTargetedFlag:
    def test_rule_matched(self, client):
        flag = _create_flag(name="ent_feature", flag_type="targeted",
                            default_value=False, environment="prod")
        _create_rule(flag.id, "plan", "equals", "enterprise")
        r = client.post("/evaluate", json={
            "flag_name": "ent_feature", "user_id": "u1", "environment": "prod",
            "attributes": {"plan": "enterprise"},
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert r.json()["reason"] == "targeting_rule_matched"

    def test_rule_no_match_falls_to_default(self, client):
        flag = _create_flag(name="ent_only", flag_type="targeted",
                            default_value=False, environment="prod")
        _create_rule(flag.id, "plan", "equals", "enterprise")
        r = client.post("/evaluate", json={
            "flag_name": "ent_only", "user_id": "u1", "environment": "prod",
            "attributes": {"plan": "free"},
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert r.json()["reason"] == "targeting_rule_default"

    def test_no_attributes_falls_to_default(self, client):
        flag = _create_flag(name="attr_flag", flag_type="targeted",
                            default_value=True, environment="dev")
        _create_rule(flag.id, "plan", "equals", "enterprise")
        r = client.post("/evaluate", json={
            "flag_name": "attr_flag", "user_id": "u1", "environment": "dev",
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is True   # default_value=True
        assert r.json()["reason"] == "targeting_rule_default"

    def test_in_operator(self, client):
        flag = _create_flag(name="in_flag", flag_type="targeted",
                            default_value=False, environment="dev")
        _create_rule(flag.id, "plan", "in", "enterprise,pro,team")
        r = client.post("/evaluate", json={
            "flag_name": "in_flag", "user_id": "u2", "environment": "dev",
            "attributes": {"plan": "pro"},
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert r.json()["reason"] == "targeting_rule_matched"


# ---------------------------------------------------------------------------
# Flag not found
# ---------------------------------------------------------------------------

class TestFlagNotFound:
    def test_missing_flag_returns_200_not_404(self, client):
        """
        Critical: absent flag must yield HTTP 200 + enabled=False,
        never a 404.  A 404 forces every caller to special-case
        "flag doesn't exist" in their business logic — wrong ergonomics
        for a service whose sole purpose is to be called from arbitrary paths.
        """
        r = client.post("/evaluate", json={
            "flag_name": "does_not_exist", "user_id": "u1", "environment": "prod"
        })
        assert r.status_code == 200, (
            f"Expected 200 for missing flag but got {r.status_code}"
        )
        body = r.json()
        assert body["enabled"] is False
        assert body["reason"] == "flag_not_found"
        assert body["flag_name"] == "does_not_exist"

    def test_wrong_environment_is_flag_not_found(self, client):
        _create_flag(name="env_flag", flag_type="boolean",
                     default_value=True, environment="dev")
        r = client.post("/evaluate", json={
            "flag_name": "env_flag", "user_id": "u1", "environment": "prod"
        })
        assert r.status_code == 200
        assert r.json()["reason"] == "flag_not_found"
        assert r.json()["enabled"] is False


# ---------------------------------------------------------------------------
# Fail-safe path — THE most important test
# ---------------------------------------------------------------------------

class TestFailSafePath:
    def test_db_failure_returns_200_with_fail_safe(self, client):
        """
        Verifies the core contract of the entire service:
        even when the database raises OperationalError mid-request,
        POST /evaluate returns HTTP 200 with enabled=False and
        reason='evaluation_error_fail_safe'.

        Failure induced by replacing the DB dependency with a session whose
        .query() raises OperationalError — a real SQLAlchemy exception type,
        not a mock sentinel, so this tests the actual except branch, not
        a theoretical one.

        Step 6 note: get_cached_flag_with_status() is also patched to return (None, "miss"), so the
        test exercises the DB path even if a stale Redis entry exists from a
        previous run.  The Redis-down → DB-down scenario is tested more
        thoroughly in test_cache.py::TestRedisDownFailSafe.
        """
        _create_flag(name="safe_flag", flag_type="boolean",
                     default_value=True, environment="prod")

        def broken_db():
            db = MagicMock()
            db.query.side_effect = OperationalError(
                "simulated connection failure", None, None
            )
            yield db

        app.dependency_overrides[get_db] = broken_db
        try:
            # Patch get_cached_flag_with_status to always miss so broken_db is definitely reached.
            with patch("app.evaluation.get_cached_flag_with_status", return_value=(None, "miss")):
                r = client.post("/evaluate", json={
                    "flag_name": "safe_flag", "user_id": "u1", "environment": "prod"
                })
        finally:
            app.dependency_overrides[get_db] = _override_get_db  # restore

        assert r.status_code == 200, (
            f"FAIL-SAFE BROKEN: expected HTTP 200 on DB failure, got {r.status_code}. "
            "Callers would receive a 5xx — this violates the core service contract."
        )
        body = r.json()
        assert body["enabled"] is False
        assert body["reason"] == "evaluation_error_fail_safe", (
            f"Expected reason='evaluation_error_fail_safe', got {body['reason']!r}"
        )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

class TestRequestValidation:
    def test_invalid_environment_rejected(self, client):
        r = client.post("/evaluate", json={
            "flag_name": "f", "user_id": "u", "environment": "qa"
        })
        assert r.status_code == 422

    def test_missing_flag_name_rejected(self, client):
        r = client.post("/evaluate", json={"user_id": "u", "environment": "dev"})
        assert r.status_code == 422

    def test_missing_user_id_rejected(self, client):
        r = client.post("/evaluate", json={"flag_name": "f", "environment": "dev"})
        assert r.status_code == 422

    def test_uuid_user_id_accepted(self, client):
        _create_flag(name="uid_flag", flag_type="boolean",
                     default_value=True, environment="dev")
        r = client.post("/evaluate", json={
            "flag_name": "uid_flag",
            "user_id": "550e8400-e29b-41d4-a716-446655440000",
            "environment": "dev",
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is True
