"""
metrics.py — Prometheus metric singleton definitions.

Four metrics are defined here — no more, no less.  Each maps to a concrete
operational question an operator would ask in production:

  flag_evaluations_total
    "Is fail-safe actually firing in production?  Are callers referencing
     deleted or misspelled flags?"  A spike in reason="evaluation_error_fail_safe"
     or reason="flag_not_found" is the primary incident signal for this service.

  flag_evaluations_cache_result_total
    "Is the caching layer actually working?"  A hit ratio near zero means the
     TTL is too short or cache invalidation is too aggressive, and /evaluate is
     silently hammering Postgres on every request.

  flag_evaluation_duration_seconds
    "Are we actually meeting the sub-50ms latency requirement?"  Right now that's
     an assumption; this metric turns it into a measured fact under real traffic.

  flag_mutations_total
    "How much flag churn is happening?"  Useful when correlating an incident with
     "did someone just change a flag 5 minutes ago."

Design — module-level singletons
---------------------------------
Each metric object is instantiated once at import time, exactly like the DB
engine (database.py) and Redis client (cache.py).  Importing this module from
anywhere gives you the same Counter/Histogram object.  Duplicate registration is
suppressed by prometheus-client's internal CollectorRegistry — reimporting is safe.

Design — no labels on the histogram
-------------------------------------
Labels multiply bucket storage: a histogram with N labels of cardinality K
creates N×K×(number of buckets) time series.  For this service there is no
concrete operational question that requires per-flag or per-environment histogram
slicing, so labels are omitted to keep cardinality minimal.  The four counters
(which have low-cardinality labels) cover the "what happened" question; the
histogram covers only "how long did it take overall."

Safety contract
---------------
Counter.labels(...).inc() and Histogram.observe() are effectively infallible in
practice (the prometheus-client library is extremely defensive internally), but
the caller (evaluation.py, routers/evaluate.py, routers/flags.py) wraps increments
in try/except so that any unexpected exception in observability code can never
propagate into the request path.  Observability code must never become a new
source of production incidents.
"""

from prometheus_client import Counter, Histogram

# ---------------------------------------------------------------------------
# flag_evaluations_total
# Labels: reason (the six constants from evaluation.py), environment
# ---------------------------------------------------------------------------

flag_evaluations_total = Counter(
    "flag_evaluations_total",
    "Total number of feature flag evaluations, labelled by outcome reason and environment.",
    labelnames=["reason", "environment"],
)

# ---------------------------------------------------------------------------
# flag_evaluations_cache_result_total
# Labels: result (hit | miss | redis_unavailable)
# ---------------------------------------------------------------------------

flag_evaluations_cache_result_total = Counter(
    "flag_evaluations_cache_result_total",
    "Total number of flag evaluation cache lookups, labelled by result.",
    labelnames=["result"],
)

# ---------------------------------------------------------------------------
# flag_evaluation_duration_seconds
# No labels — see module docstring for cardinality rationale.
# Default buckets cover sub-millisecond through several-second latencies.
# ---------------------------------------------------------------------------

flag_evaluation_duration_seconds = Histogram(
    "flag_evaluation_duration_seconds",
    "Wall-clock duration of the full POST /evaluate handler, from cache lookup through response.",
    # Custom buckets tuned for a service with a sub-50ms latency target:
    # .005 = 5ms, .010 = 10ms, .025 = 25ms, .050 = 50ms (SLO boundary),
    # .100 = 100ms, .250 = 250ms, .500 = 500ms, 1.0 = 1s, 2.5 = 2.5s
    buckets=[0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0, 2.5],
)

# ---------------------------------------------------------------------------
# flag_mutations_total
# Labels: action (created | updated | deleted)
# ---------------------------------------------------------------------------

flag_mutations_total = Counter(
    "flag_mutations_total",
    "Total number of flag write operations (create / update / delete), labelled by action.",
    labelnames=["action"],
)
