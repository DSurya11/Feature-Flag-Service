"""
evaluation.py — Pure evaluation logic for feature flags.

This module is deliberately separated from the HTTP layer (routers/evaluate.py)
so that the algorithm can be unit-tested without a live database or HTTP stack.

Hashing design — why not Python's built-in hash()
----------------------------------------------------
Python's built-in hash() is salted per-process (PYTHONHASHSEED) by default
since Python 3.3.  This means hash("foo") returns a different value on every
process restart and across different pods in a Kubernetes cluster.  Using it
for rollout bucket assignment would silently route the same user to *different*
buckets depending on which replica handles their request — the correctness
property we need (same user always in same bucket) would be violated, and the
bug would be nearly invisible in local testing (same process = same hash).

Decision: use hashlib.md5 — deterministic, stable across processes, runtimes,
and architectures.  MD5 is not used for security here, only for uniform
distribution, so its cryptographic weaknesses are irrelevant.

Concatenation format — why it's fixed and documented
-----------------------------------------------------
The hash input is the string  "{flag_name}:{user_id}"  (colon separator).
This format must never change once flags are live.  Changing it would silently
reshuffle every user's bucket, meaning users who were previously in the
"enabled" 30% could fall out and vice versa — invisible, hard-to-debug churn.
The format is documented here so that any future engineer touching this code
understands why it must be treated as a frozen contract.

Targeting rule operators
------------------------
  equals     — attribute value must exactly match rule value (string equality)
  not_equals — attribute value must NOT match rule value
  in         — rule value is a comma-separated list; attribute must be one of them
               (e.g. value="enterprise,pro" matches attribute value "enterprise")

Data shape — why plain dicts, not ORM objects
---------------------------------------------
_evaluate_targeting() accepts a list of plain dicts (keys: id, attribute,
operator, value) rather than SQLAlchemy TargetingRule ORM instances.  This is
a deliberate design decision enabling the cache-aside layer in evaluate_flag():

  - A cache HIT returns targeting rules already deserialized from Redis JSON as
    plain dicts.
  - A cache MISS fetches TargetingRule ORM rows from Postgres, but immediately
    converts them to the same plain dict shape before evaluation.

Both paths converge on a single dict representation before calling
_evaluate_targeting(), which means there is exactly one object shape, one
evaluation code path, and one test surface — regardless of whether data came
from Redis or Postgres.

The alternative — using SimpleNamespace to mimic ORM objects from cached data
— would create two diverging shapes that only stay in sync as long as
_evaluate_targeting() never accesses any field besides .attribute, .operator,
.value, .id.  That's a latent bug waiting for a new field or isinstance() check
to surface it, and a cache-hit-on-targeted-flag is exactly the scenario you
wouldn't think to test unless you'd been burned by it before.

Cache-aside strategy
--------------------
evaluate_flag() implements a cache-aside (lazy-loading) pattern:

  1. Compute cache key: flag:{environment}:{name}
  2. GET from Redis via get_cached_flag() — returns None on miss OR Redis down
  3. Cache HIT  → deserialize and evaluate immediately, no DB query
  4. Cache MISS → query Postgres, serialize to JSON, SET in Redis with TTL,
                   then evaluate
  5. Redis unreachable during the read → get_cached_flag() returns None and
     logs a WARNING; evaluate_flag() falls through to step 4 (DB query) and
     continues normally.  This is the Redis-down fail-safe: Redis being down
     makes evaluation slower, not broken.  It does NOT trigger the global
     REASON_FAIL_SAFE path unless the DB is also down.
"""

import hashlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.cache import (
    CACHE_HIT,
    get_cached_flag_with_status,
    set_cached_flag,
)
from app.models import Flag, TargetingRule

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public reason constants — used in the response body
# ---------------------------------------------------------------------------

REASON_BOOLEAN_FLAG       = "boolean_flag"
REASON_PERCENTAGE_ROLLOUT = "percentage_rollout"
REASON_TARGETING_MATCHED  = "targeting_rule_matched"
REASON_TARGETING_DEFAULT  = "targeting_rule_default"
REASON_FLAG_NOT_FOUND     = "flag_not_found"
REASON_FAIL_SAFE          = "evaluation_error_fail_safe"


# ---------------------------------------------------------------------------
# Hashing — stable, cross-process bucket assignment
# ---------------------------------------------------------------------------

def _compute_bucket(flag_name: str, user_id: str) -> int:
    """
    Return a bucket integer in [0, 99] for the given (flag_name, user_id) pair.

    The same inputs always produce the same bucket, regardless of:
      - which process or pod handles the request
      - how many times the function is called
      - Python version / PYTHONHASHSEED

    Hash input format: "{flag_name}:{user_id}"
    This format is a frozen contract — see module docstring for why.
    """
    raw = f"{flag_name}:{user_id}"
    digest = hashlib.md5(raw.encode("utf-8"), usedforsecurity=False).hexdigest()
    # Take the first 8 hex characters (32 bits) — plenty of resolution for
    # modulo 100, and avoids any integer overflow concern.
    return int(digest[:8], 16) % 100


# ---------------------------------------------------------------------------
# Targeting rule evaluation — operates on plain dicts
# ---------------------------------------------------------------------------

def _evaluate_targeting(
    rules: list[dict],
    attributes: dict[str, Any],
) -> tuple[bool, bool]:
    """
    Evaluate a list of targeting rule dicts against the supplied user attributes.

    Each rule dict must have keys: ``id``, ``attribute``, ``operator``, ``value``.
    This shape is produced both by _fetch_flag_data_from_db() (from ORM rows)
    and by the Redis deserialization path (from JSON) — see module docstring for
    why a single dict shape is used instead of ORM objects or SimpleNamespace.

    Returns (matched: bool, result: bool):
      - matched=True,  result=True  — a rule fired; feature is enabled
      - matched=False, result=False — no rule fired; caller falls back to default_value

    Rules are evaluated in insertion order (ordered by id).  The first
    matching rule wins — subsequent rules are not evaluated.

    Operator semantics:
      equals     → str(attribute_value) == rule["value"]
      not_equals → str(attribute_value) != rule["value"]
      in         → rule["value"] is comma-separated; attribute_value must be one of them
    """
    for rule in rules:
        attr_val = attributes.get(rule["attribute"])
        if attr_val is None:
            # Attribute not supplied — this rule cannot match.
            continue

        attr_str = str(attr_val)

        if rule["operator"] == "equals":
            matched = attr_str == rule["value"]
        elif rule["operator"] == "not_equals":
            matched = attr_str != rule["value"]
        elif rule["operator"] == "in":
            allowed = {v.strip() for v in rule["value"].split(",")}
            matched = attr_str in allowed
        else:
            logger.warning(
                "Unknown targeting rule operator %r for rule id=%s — skipping.",
                rule["operator"],
                rule["id"],
            )
            continue

        if matched:
            logger.debug(
                "Targeting rule id=%s matched: attribute=%r operator=%r value=%r",
                rule["id"],
                rule["attribute"],
                rule["operator"],
                rule["value"],
            )
            return True, True

    return False, False


# ---------------------------------------------------------------------------
# DB fetch — returns plain dict (or None if flag not found)
# ---------------------------------------------------------------------------

def _fetch_flag_data_from_db(
    db: Session,
    flag_name: str,
    environment: str,
) -> dict | None:
    """
    Fetch a flag and its targeting rules from Postgres, returning a plain dict.

    The returned dict matches the shape stored in Redis (see module docstring),
    so it can be passed directly to set_cached_flag() and _evaluate_from_data()
    without any further conversion.

    Returns None if no flag with (flag_name, environment) exists.

    Targeting rules are only queried for ``flag_type == "targeted"`` — there's
    no point paying for a rules query on boolean or percentage flags.
    """
    flag: Flag | None = (
        db.query(Flag)
        .filter(Flag.name == flag_name, Flag.environment == environment)
        .first()
    )

    if flag is None:
        return None

    data: dict[str, Any] = {
        "id":                 flag.id,
        "name":               flag.name,
        "description":        flag.description,
        "flag_type":          flag.flag_type,
        "default_value":      flag.default_value,
        "rollout_percentage": flag.rollout_percentage,
        "environment":        flag.environment,
        "targeting_rules":    [],
    }

    if flag.flag_type == "targeted":
        rules = (
            db.query(TargetingRule)
            .filter(TargetingRule.flag_id == flag.id)
            .order_by(TargetingRule.id)
            .all()
        )
        data["targeting_rules"] = [
            {
                "id":        r.id,
                "attribute": r.attribute,
                "operator":  r.operator,
                "value":     r.value,
            }
            for r in rules
        ]

    return data


# ---------------------------------------------------------------------------
# Evaluation from plain dict — single shared path for cache hit and miss
# ---------------------------------------------------------------------------

def _evaluate_from_data(
    flag_data: dict,
    flag_name: str,
    user_id: str,
    environment: str,
    attrs: dict[str, Any],
) -> dict[str, Any]:
    """
    Evaluate a flag from its plain dict representation and return a result dict.

    This is the single evaluation path used for both cache hits and cache
    misses.  Receiving a plain dict (rather than an ORM object) means this
    function is completely agnostic to whether the data came from Redis or
    Postgres.

    Always returns a dict with keys:
      flag_name, user_id, environment, enabled (bool), reason (str)
    """
    flag_type = flag_data["flag_type"]

    # --- Boolean flag ---
    if flag_type == "boolean":
        return {
            "flag_name":   flag_name,
            "user_id":     user_id,
            "environment": environment,
            "enabled":     bool(flag_data["default_value"]),
            "reason":      REASON_BOOLEAN_FLAG,
        }

    # --- Percentage flag ---
    if flag_type == "percentage":
        bucket = _compute_bucket(flag_name, user_id)
        pct = flag_data["rollout_percentage"] if flag_data["rollout_percentage"] is not None else 0
        enabled = bucket < pct
        logger.debug(
            "evaluate percentage: flag=%r user=%r bucket=%d pct=%d enabled=%s",
            flag_name, user_id, bucket, pct, enabled,
        )
        return {
            "flag_name":   flag_name,
            "user_id":     user_id,
            "environment": environment,
            "enabled":     enabled,
            "reason":      REASON_PERCENTAGE_ROLLOUT,
        }

    # --- Targeted flag ---
    if flag_type == "targeted":
        matched, result = _evaluate_targeting(flag_data["targeting_rules"], attrs)
        if matched:
            return {
                "flag_name":   flag_name,
                "user_id":     user_id,
                "environment": environment,
                "enabled":     result,
                "reason":      REASON_TARGETING_MATCHED,
            }
        return {
            "flag_name":   flag_name,
            "user_id":     user_id,
            "environment": environment,
            "enabled":     bool(flag_data["default_value"]),
            "reason":      REASON_TARGETING_DEFAULT,
        }

    # Unknown flag_type — DB enum should prevent this, but be conservative.
    logger.error(
        "evaluate: unknown flag_type %r for flag id=%s — returning fail-safe.",
        flag_type,
        flag_data["id"],
    )
    return {
        "flag_name":   flag_name,
        "user_id":     user_id,
        "environment": environment,
        "enabled":     False,
        "reason":      REASON_FAIL_SAFE,
    }


# ---------------------------------------------------------------------------
# Core evaluation — single entry point, called by the HTTP handler
# ---------------------------------------------------------------------------

def evaluate_flag(
    db: Session,
    flag_name: str,
    user_id: str,
    environment: str,
    attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Evaluate a feature flag for a given user and return a result dict.

    Implements a cache-aside (lazy-loading) pattern:
      1. Check Redis for a cached flag entry.
      2. Cache HIT  → evaluate directly from cached dict (no DB query).
      3. Cache MISS → fetch from Postgres, populate cache, then evaluate.
      4. Redis down → get_cached_flag_with_status() returns
                      (None, "redis_unavailable"), fall through to DB.

    Always returns a dict with keys:
      flag_name, user_id, environment, enabled (bool), reason (str)

    Fail-safe contract
    ------------------
    Any exception during cache lookup, DB query, or rule evaluation is caught
    here, logged, and a safe default (enabled=False, reason=REASON_FAIL_SAFE)
    is returned.  The HTTP handler can always return HTTP 200 because this
    function never raises.

    Important distinction: Redis being unreachable is NOT a fail-safe trigger.
    It causes a silent fallback to Postgres (get_cached_flag_with_status returns
    (None, "redis_unavailable"), logged as WARNING in cache.py) and evaluation
    continues normally.  REASON_FAIL_SAFE is only returned if the DB is also
    unreachable or an unhandled exception escapes from the evaluation logic itself.

    Metrics
    -------
    Increments flag_evaluations_cache_result_total and flag_evaluations_total.
    Both increments are wrapped in bare except so that any (extremely unlikely)
    failure in prometheus-client never propagates into the evaluation path.
    Observability code must never become a new source of production incidents.
    """
    # Deferred import to avoid any potential circular import at module load time.
    from app.metrics import (  # noqa: PLC0415
        flag_evaluations_cache_result_total,
        flag_evaluations_total,
    )

    attrs = attributes or {}

    try:
        # ------------------------------------------------------------------ #
        # Step 1: try cache                                                    #
        # ------------------------------------------------------------------ #
        import time
        t_cache_start = time.perf_counter()
        flag_data, cache_status = get_cached_flag_with_status(flag_name, environment)
        t_cache_elapsed = time.perf_counter() - t_cache_start
        logger.info(f"TIMING cache_lookup={t_cache_elapsed:.4f}s status={cache_status}")

        # Increment cache result counter — wrapped so it can never raise.
        try:
            flag_evaluations_cache_result_total.labels(result=cache_status).inc()
        except Exception:  # noqa: BLE001
            pass

        # ------------------------------------------------------------------ #
        # Step 2: cache miss (or Redis down) — fetch from Postgres            #
        # ------------------------------------------------------------------ #
        if flag_data is None:
            t_db_start = time.perf_counter()
            flag_data = _fetch_flag_data_from_db(db, flag_name, environment)
            t_db_elapsed = time.perf_counter() - t_db_start
            logger.info(f"TIMING db_fetch={t_db_elapsed:.4f}s")

            if flag_data is None:
                logger.info(
                    "evaluate: flag not found — name=%r environment=%r",
                    flag_name,
                    environment,
                )
                result = {
                    "flag_name":   flag_name,
                    "user_id":     user_id,
                    "environment": environment,
                    "enabled":     False,
                    "reason":      REASON_FLAG_NOT_FOUND,
                }
                try:
                    flag_evaluations_total.labels(
                        reason=REASON_FLAG_NOT_FOUND, environment=environment
                    ).inc()
                except Exception:  # noqa: BLE001
                    pass
                return result

            # Populate cache for subsequent requests within the TTL window.
            set_cached_flag(flag_name, environment, flag_data)

        # ------------------------------------------------------------------ #
        # Step 3: evaluate (same path regardless of cache hit or miss)        #
        # ------------------------------------------------------------------ #
        result = _evaluate_from_data(flag_data, flag_name, user_id, environment, attrs)

        # Increment evaluation counter after outcome is known.
        try:
            flag_evaluations_total.labels(
                reason=result["reason"], environment=environment
            ).inc()
        except Exception:  # noqa: BLE001
            pass

        return result

    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "evaluate: unhandled exception for flag=%r user=%r environment=%r — "
            "returning fail-safe. Error: %s",
            flag_name, user_id, environment, exc,
        )
        result = {
            "flag_name":   flag_name,
            "user_id":     user_id,
            "environment": environment,
            "enabled":     False,
            "reason":      REASON_FAIL_SAFE,
        }
        try:
            flag_evaluations_total.labels(
                reason=REASON_FAIL_SAFE, environment=environment
            ).inc()
        except Exception:  # noqa: BLE001
            pass
        return result
