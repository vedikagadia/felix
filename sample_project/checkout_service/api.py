"""HTTP API surface for the checkout service.

Thin routing layer. Each endpoint delegates to CheckoutHandler. No web
framework — the methods stand in for route handlers so the call graph is clear.
"""

import logging

from checkout_service.checkout import CheckoutHandler, CheckoutError

log = logging.getLogger("checkout.api")


class CheckoutAPI:
    def __init__(self):
        self.handler = CheckoutHandler()

    def post_checkout(self, request):
        """POST /checkout — process a payment for an order."""
        order_id = request.get("order_id")
        amount = request.get("amount")
        log.info("api.request path=/checkout order_id=%s", order_id)
        try:
            result = self.handler.process(order_id, amount)
            return {"status": 200, "body": result}
        except CheckoutError as e:
            log.error("api.error path=/checkout order_id=%s err=%s", order_id, e)
            return {"status": 502, "body": {"error": str(e)}}

    def get_health(self, request):
        """GET /health — liveness probe."""
        log.debug("api.request path=/health")
        return {"status": 200, "body": {"ok": True}}
