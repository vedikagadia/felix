"""Metrics emission for the checkout service.

Emits latency and outcome metrics. The aggregation used here is the root cause
of the 'merge-only' incident: a recent merge switched the checkout latency
metric from a p99 aggregation to a mean ('avg') aggregation. The code looks
perfectly reasonable on its own — nothing here is buggy in isolation. The only
way to know this is the cause is to see that the aggregation *changed recently*
(the code_changes record), because the mean hides the tail latency that the
on-call dashboards and alerts depend on.
"""

import logging

log = logging.getLogger("checkout.metrics")

# Aggregation for the checkout latency metric. Changed from "p99" to "avg" in a
# recent merge (see the code_changes seed). Reads fine here; the problem is the
# change, not the line.
LATENCY_AGGREGATION = "avg"


class MetricsEmitter:
    def emit_latency(self, name, value_ms):
        log.debug("metrics.latency name=%s value_ms=%s agg=%s", name, value_ms, LATENCY_AGGREGATION)

    def emit_count(self, name, value=1):
        log.debug("metrics.count name=%s value=%s", name, value)


_EMITTER = MetricsEmitter()


def get_emitter():
    return _EMITTER
