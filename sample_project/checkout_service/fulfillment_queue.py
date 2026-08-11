"""In-memory fulfillment queue.

After a successful charge, the order is enqueued for fulfillment. If the
downstream fulfillment worker stalls, this queue grows without bound up to
QUEUE_MAX_DEPTH, then starts rejecting enqueues.
"""

import logging
from collections import deque

from checkout_service import config

log = logging.getLogger("checkout.fulfillment_queue")


class QueueFull(Exception):
    pass


class FulfillmentQueue:
    def __init__(self, max_depth=config.QUEUE_MAX_DEPTH):
        self.max_depth = max_depth
        self._q = deque()

    def enqueue(self, order_id):
        if len(self._q) >= self.max_depth:
            log.error("fulfillment.queue.full depth=%s max=%s", len(self._q), self.max_depth)
            raise QueueFull(f"queue at capacity {self.max_depth}")
        self._q.append(order_id)
        log.info("fulfillment.enqueued order_id=%s depth=%s", order_id, len(self._q))

    def depth(self):
        return len(self._q)


_QUEUE = FulfillmentQueue()


def get_queue():
    return _QUEUE
