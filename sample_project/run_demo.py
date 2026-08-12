"""Tiny driver that exercises the checkout service so it emits logs.

Run: python -m sample_project.run_demo   (from repo root)
Not part of the agent; just makes the sample service produce a log stream.
"""

import logging

from checkout_service.api import CheckoutAPI

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")


def main():
    api = CheckoutAPI()
    api.get_health({})
    api.post_checkout({"order_id": "ord-1001", "amount": 4200})


if __name__ == "__main__":
    main()
