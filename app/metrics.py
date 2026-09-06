from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST

from app import version


# The conventional Prometheus way to expose build identity: a gauge that is
# always 1, carrying the interesting values as labels. Makes the running version
# queryable and joinable —
#   llm_proxy_build_info                          -> what is deployed
#   count(count by (version) (llm_proxy_build_info)) > 1  -> a mixed-version fleet
# Set once at import; the labels never change for the life of the process.
BUILD_INFO = Gauge(
    "llm_proxy_build_info",
    "Build identity of the running process; always 1, read the labels",
    ["version", "revision"],
)
BUILD_INFO.labels(version=version.VERSION, revision=version.REVISION or "").set(1)


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

TRIMS_TOTAL = Counter(
    "llm_proxy_trims_total",
    "Requests whose conversation was shrunk to fit the num_ctx they declared "
    "(see the trim: config section)",
    ["model"],
)

# NOTE: these counters are deliberately NOT persisted across restarts. A restart
# resets them to zero, which Prometheus recognises as a counter reset and handles
# correctly in rate()/increase(). Re-seeding them from a snapshot is what breaks
# that: the file always lags the last scrape a little, so the counter comes back
# *lower* than the value Prometheus already read. Prometheus reads any decrease
# as a reset-to-zero and credits the entire pre-drop value as fresh increase — a
# two-request lag once turned into a phantom 1190-request, 36M-token spike inside
# a 21-minute window. For lifetime totals use the `:increase5m` recording rules,
# which are reset-proof, instead of reading the raw counter.


def metrics_response():
    data = generate_latest()
    return data, 200, {"Content-Type": CONTENT_TYPE_LATEST}
