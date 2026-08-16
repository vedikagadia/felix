"""Client for the external payment gateway.

This is the slow, unreliable dependency. When the upstream gateway is degraded,
calls here take a long time and PaymentClient retries them PAYMENT_MAX_RETRIES
times with backoff. The retry loop is the key structural detail behind the
'payment latency cascades into a checkout outage' incident.
"""

import logging
import random
import time

from checkout_service import config

log = logging.getLogger("checkout.payment_gateway")

# ── Demo-only gateway simulation (does NOT change the retry structure) ────────
# The real service would make an HTTP call here; for the live-monitoring demo we
# instead SLEEP a realistic round-trip so a genuine `charge()` actually takes
# time — the timing probe on CheckoutHandler.process then measures real
# wall-clock latency, not a fabricated number.
#
# Every GATEWAY_SLOW_EVERY-th charge, the gateway is "degraded": its FIRST
# attempt fails with GatewayTimeout, forcing exactly ONE real retry through the
# existing loop below (backoff = base_delay * 2 ≈ 1s). That produces a true tail
# spike (>1000ms) over an otherwise-green window (~100ms baseline, mean <300ms)
# — the same avg-hides-the-tail shape puzzle B is about — while staying bounded
# (never an unbounded retry storm).
GATEWAY_BASE_LATENCY_S = (0.06, 0.11)
GATEWAY_SLOW_EVERY = 12


class GatewayTimeout(Exception):
    pass


class PaymentClient:
    def __init__(self):
        self.max_retries = config.PAYMENT_MAX_RETRIES
        self.base_delay = config.PAYMENT_BASE_DELAY_SECONDS
        self._charge_count = 0

    def charge(self, order_id, amount):
        """Charge with retries. Holds the caller's context for the full
        duration of all retries — see CheckoutHandler for why that matters."""
        self._charge_count += 1
        degraded = self._charge_count % GATEWAY_SLOW_EVERY == 0
        attempt = 0
        while attempt <= self.max_retries:
            try:
                return self._call_gateway(order_id, amount, attempt, degraded)
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

    def _call_gateway(self, order_id, amount, attempt=0, degraded=False):
        log.debug("payment.call order_id=%s amount=%s attempt=%s", order_id, amount, attempt)
        # Simulated gateway round-trip so `charge()` genuinely takes time.
        time.sleep(random.uniform(*GATEWAY_BASE_LATENCY_S))
        # A degraded gateway fails only its first attempt; the retry below then
        # succeeds — a bounded, real ~1s tail spike (see module note above).
        if degraded and attempt == 0:
            raise GatewayTimeout(f"gateway degraded on order_id={order_id}")
        return {"status": "authorized", "order_id": order_id}
