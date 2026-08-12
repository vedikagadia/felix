"""Client for the external payment gateway.

This is the slow, unreliable dependency. When the upstream gateway is degraded,
calls here take a long time and PaymentClient retries them PAYMENT_MAX_RETRIES
times with backoff. The retry loop is the key structural detail behind the
'payment latency cascades into a checkout outage' incident.
"""

import logging
import time

from checkout_service import config

log = logging.getLogger("checkout.payment_gateway")


class GatewayTimeout(Exception):
    pass


class PaymentClient:
    def __init__(self):
        self.max_retries = config.PAYMENT_MAX_RETRIES
        self.base_delay = config.PAYMENT_BASE_DELAY_SECONDS

    def charge(self, order_id, amount):
        """Charge with retries. Holds the caller's context for the full
        duration of all retries — see CheckoutHandler for why that matters."""
        attempt = 0
        while attempt <= self.max_retries:
            try:
                return self._call_gateway(order_id, amount)
            except GatewayTimeout:
                attempt += 1
                delay = self.base_delay * (2 ** attempt)
                log.warning(
                    "payment.retry order_id=%s attempt=%s/%s backoff=%.1fs",
                    order_id, attempt, self.max_retries, delay,
                )
                time.sleep(delay)
        log.error("payment.exhausted order_id=%s attempts=%s", order_id, self.max_retries)
        raise GatewayTimeout(f"gateway timed out after {self.max_retries} retries")

    def _call_gateway(self, order_id, amount):
        log.debug("payment.call order_id=%s amount=%s", order_id, amount)
        # Placeholder for the real HTTP call to the external gateway.
        return {"status": "authorized", "order_id": order_id}
