"""Core checkout orchestration.

CheckoutHandler is the hub of the service: it acquires a DB connection, charges
the customer via PaymentClient, and enqueues the order for fulfillment.

STRUCTURAL DETAIL (root cause of the 'code-only' incident):
CheckoutHandler.process acquires a DB connection at the *start* and only
releases it in `finally`, AFTER PaymentClient.charge() has run. Because charge()
can block for the full retry sequence (PAYMENT_MAX_RETRIES with exponential
backoff), every slow checkout holds a scarce pool connection for tens of
seconds. Under load this drains ConnectionPool (DB_POOL_SIZE) and unrelated
requests fail with ConnectionPoolExhausted — even though the *symptom* looks
like a database problem, the cause is the payment retry loop holding the
connection. You can only see this by reading how these components connect.
"""

import logging

from checkout_service import db
from checkout_service.payment_gateway import PaymentClient, GatewayTimeout
from checkout_service.fulfillment_queue import get_queue, QueueFull

log = logging.getLogger("checkout.checkout")


class CheckoutError(Exception):
    pass


class CheckoutHandler:
    def __init__(self):
        self.payments = PaymentClient()
        self.queue = get_queue()

    def process(self, order_id, amount):
        log.info("checkout.start order_id=%s amount=%s", order_id, amount)
        pool = db.get_pool()
        conn = pool.acquire()  # connection acquired up front...
        try:
            # ...and held across the (potentially very slow) charge call.
            result = self.payments.charge(order_id, amount)
            self.queue.enqueue(order_id)
            log.info("checkout.success order_id=%s", order_id)
            return result
        except GatewayTimeout as e:
            log.error("checkout.payment_failed order_id=%s err=%s", order_id, e)
            raise CheckoutError("payment failed") from e
        except QueueFull as e:
            log.error("checkout.enqueue_failed order_id=%s err=%s", order_id, e)
            raise CheckoutError("fulfillment unavailable") from e
        finally:
            conn.close()  # released only here, after charge() returns
