from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


REQUESTS_TOTAL = Counter(
    "llm_proxy_requests_total",
    "Total proxied requests",
    ["provider", "model"],
)

TOKENS_INPUT_TOTAL = Counter(
    "llm_proxy_tokens_input_total",
    "Total input tokens",
    ["provider", "model"],
)

TOKENS_OUTPUT_TOTAL = Counter(
    "llm_proxy_tokens_output_total",
    "Total output tokens",
    ["provider", "model"],
)

REQUEST_DURATION = Histogram(
    "llm_proxy_request_duration_seconds",
    "Request duration in seconds",
    ["provider", "model"],
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0],
)

ERRORS_TOTAL = Counter(
    "llm_proxy_errors_total",
    "Total errors",
    ["provider", "model", "status_code"],
)

SLOTS_IN_USE = Gauge(
    "llm_proxy_slots_in_use",
    "In-flight requests currently occupying a slot, per provider",
    ["provider"],
)

QUEUE_WAITING = Gauge(
    "llm_proxy_queue_waiting",
    "Requests currently waiting for a free slot",
)

QUEUE_AFFINITY_GRANTS = Counter(
    "llm_proxy_queue_affinity_grants_total",
    "Times a queued request was admitted ahead of an earlier one because the "
    "provider already had its model loaded",
    ["provider"],
)

QUEUE_STARVATION_YIELDS = Counter(
    "llm_proxy_queue_starvation_yields_total",
    "Times the affinity_max_skips cap forced FIFO admission to stop a passed-over "
    "request from waiting any longer",
    ["provider"],
)

FAILOVERS_TOTAL = Counter(
    "llm_proxy_failovers_total",
    "Times a request failed over from one backend to the next",
    ["provider"],
)

# Counters whose cumulative values survive restarts, keyed by their emitted
# sample name. Gauges (live state) and the latency histogram are intentionally
# not persisted — they reflect the current process, not a running total.
PERSISTABLE_COUNTERS = {
    "llm_proxy_requests_total": REQUESTS_TOTAL,
    "llm_proxy_tokens_input_total": TOKENS_INPUT_TOTAL,
    "llm_proxy_tokens_output_total": TOKENS_OUTPUT_TOTAL,
    "llm_proxy_errors_total": ERRORS_TOTAL,
    "llm_proxy_failovers_total": FAILOVERS_TOTAL,
    "llm_proxy_queue_affinity_grants_total": QUEUE_AFFINITY_GRANTS,
    "llm_proxy_queue_starvation_yields_total": QUEUE_STARVATION_YIELDS,
}


def metrics_response():
    data = generate_latest()
    return data, 200, {"Content-Type": CONTENT_TYPE_LATEST}
