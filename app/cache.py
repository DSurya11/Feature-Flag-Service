"""
cache.py — Redis caching layer for feature flag evaluation.

Cache key format — frozen contract
------------------------------------
All cache keys follow the format:

    flag:{environment}:{name}

This format MUST NOT change once the service is live.  Changing it would
orphan every existing key in Redis under the old format (they would linger
until their TTL expires, causing no incorrect behaviour since they become
unreachable, but creating confusing "ghost" keys during debugging).  The
format is chosen to be:

  - Unambiguous: the ``flag:`` prefix prevents collisions with any other Redis
    usage in the same instance.
  - Inspectable: ``redis-cli KEYS 'flag:prod:*'`` instantly lists all cached
    prod flags.
  - Consistent with the evaluation hot path: the two fields that uniquely
    identify a flag evaluation are (name, environment) — both are in the key.

Cached value shape
------------------
Each key stores a JSON-serialized dict with the full flag row plus its
targeting rules (pre-joined), so that a cache hit for a targeted flag never
needs a second DB query for the rules:

    {
      "id": 1,
      "name": "dark-mode",
      "description": "...",
      "flag_type": "targeted",          # "boolean" | "percentage" | "targeted"
      "default_value": false,
      "rollout_percentage": null,        # null for non-percentage flags
      "environment": "prod",
      "targeting_rules": [              # [] for non-targeted flags
        {
          "id": 7,
          "attribute": "plan",
          "operator": "equals",         # "equals" | "not_equals" | "in"
          "value": "enterprise"
        }
      ]
    }

Failure model
-------------
Redis is a *performance* layer, not a *correctness* layer.  Any RedisError
in get / set / invalidate is caught, logged as WARNING, and treated as a
no-op.  On a GET failure the caller falls back to Postgres, so Redis being
unreachable degrades gracefully to slower (DB-backed) evaluations, never to
errors or the global fail-safe default.  This is a deliberate second
fail-safe layer on top of the Step 5 database fail-safe.
"""

import json
import logging

import redis

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Singleton Redis client
# ---------------------------------------------------------------------------
# One connection pool, reused per request — mirrors the DB engine singleton
# in database.py.  from_url() handles both plain redis:// (local / Docker)
# and rediss:// (TLS, used by Upstash and other managed providers).
# decode_responses=True ensures GET always returns str | None, never bytes.
# ---------------------------------------------------------------------------

redis_client: redis.Redis = redis.Redis.from_url(
    settings.redis_url, decode_responses=True
)


# ---------------------------------------------------------------------------
# Internal: cache key — frozen contract
# ---------------------------------------------------------------------------

def _cache_key(name: str, environment: str) -> str:
    """Return the canonical cache key for a flag.

    Format: ``flag:{environment}:{name}``

    This format is a frozen contract — see the module docstring for the
    reasoning.  Never construct cache keys ad-hoc outside this function.
    """
    return f"flag:{environment}:{name}"


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

# Cache result status constants — used as metric label values.
CACHE_HIT            = "hit"
CACHE_MISS           = "miss"
CACHE_REDIS_UNAVAILABLE = "redis_unavailable"


def get_cached_flag(name: str, environment: str) -> dict | None:
    """Return the cached flag+rules dict, or None on miss / Redis unreachable.

    The caller cannot distinguish "cache miss" from "Redis down" — both
    return None.  This is intentional: the correct response in both cases is
    identical (fall back to Postgres), so exposing the distinction would only
    add complexity with no benefit.

    For metrics instrumentation use get_cached_flag_with_status() instead,
    which returns the cache result label alongside the data.

    Returns
    -------
    dict
        Deserialized flag data (see module docstring for shape) on a cache hit.
    None
        On a cache miss, a Redis error, or a corrupted (non-JSON) value.
    """
    data, _ = get_cached_flag_with_status(name, environment)
    return data


def get_cached_flag_with_status(
    name: str, environment: str
) -> tuple[dict | None, str]:
    """Return (flag_data, cache_status) where cache_status is one of the
    CACHE_* constants defined in this module.

    This variant exists solely to allow the evaluation layer to increment
    flag_evaluations_cache_result_total with the correct label.  The status
    string distinguishes three outcomes that get_cached_flag() intentionally
    collapses into a single None:

      CACHE_HIT             — Redis returned a valid, deserializable value.
      CACHE_MISS            — Redis is reachable but has no key for this flag.
      CACHE_REDIS_UNAVAILABLE — Redis raised RedisError (connection refused,
                               timeout, etc.); caller falls back to Postgres.

    JSON deserialization errors are treated as CACHE_MISS (the corrupted entry
    is a data error, not a Redis connectivity error).

    Returns
    -------
    (dict, CACHE_HIT)
        On a cache hit — dict is the deserialized flag data.
    (None, CACHE_MISS)
        On a cache miss or JSON deserialization error.
    (None, CACHE_REDIS_UNAVAILABLE)
        When Redis raises RedisError.
    """
    key = _cache_key(name, environment)
    try:
        raw = redis_client.get(key)
        if raw is None:
            logger.debug("Cache MISS: %s", key)
            return None, CACHE_MISS
        data = json.loads(raw)
        logger.debug("Cache HIT: %s", key)
        return data, CACHE_HIT
    except redis.RedisError as exc:
        logger.warning(
            "Cache GET failed for key=%r — falling through to DB. Error: %s",
            key,
            exc,
        )
        return None, CACHE_REDIS_UNAVAILABLE
    except (json.JSONDecodeError, TypeError) as exc:
        # Corrupted value — treat as a miss so the DB re-populates it cleanly.
        logger.warning(
            "Cache value for key=%r could not be deserialized — treating as miss. Error: %s",
            key,
            exc,
        )
        return None, CACHE_MISS


def set_cached_flag(name: str, environment: str, data: dict) -> None:
    """Serialize *data* to JSON and write it to Redis with TTL.

    TTL is taken from ``settings.cache_ttl_seconds`` (env: ``CACHE_TTL_SECONDS``,
    default 10).  A failed SET is logged as WARNING but never propagated —
    failing to populate the cache is a missed optimisation, not a correctness
    failure.

    Parameters
    ----------
    name:
        Flag name (part of the cache key).
    environment:
        Flag environment (part of the cache key).
    data:
        Plain dict to serialize — must match the shape in the module docstring.
    """
    key = _cache_key(name, environment)
    try:
        redis_client.set(key, json.dumps(data), ex=settings.cache_ttl_seconds)
        logger.debug("Cache SET: %s (TTL=%ds)", key, settings.cache_ttl_seconds)
    except (redis.RedisError, TypeError) as exc:
        logger.warning(
            "Cache SET failed for key=%r — evaluation continues without cache. Error: %s",
            key,
            exc,
        )


def invalidate_cached_flag(name: str, environment: str) -> None:
    """Delete the cache entry for the given flag, if it exists.

    Called by write endpoints (POST / PATCH / DELETE on /flags) after a
    successful DB commit.  Deletion is strictly safer than an in-place cache
    update: it forces the next /evaluate call to re-fetch from Postgres,
    guaranteeing fresh data.  Any failure to delete is logged as WARNING but
    not propagated — a stale cache entry will expire naturally within
    ``CACHE_TTL_SECONDS``.

    Parameters
    ----------
    name:
        Flag name (part of the cache key).
    environment:
        Flag environment (part of the cache key).
    """
    key = _cache_key(name, environment)
    try:
        redis_client.delete(key)
        logger.debug("Cache INVALIDATED: %s", key)
    except redis.RedisError as exc:
        logger.warning(
            "Cache DEL failed for key=%r — stale data may be served until TTL expires. Error: %s",
            key,
            exc,
        )
