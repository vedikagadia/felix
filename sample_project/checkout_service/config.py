"""Configuration for the checkout service.

Central place for tunables. Several of these are the kind of value that gets
changed in a merge and later turns out to be the root cause of an incident.
"""

import logging

log = logging.getLogger("checkout.config")

# Database connection pool. The pool is deliberately small — this matters for
# the connection-exhaustion failure mode (see WORLD.md).
DB_POOL_SIZE = 10
DB_CONNECT_TIMEOUT_SECONDS = 5

# Payment gateway retry behaviour. max_retries was raised from 5 to 8 in an
# earlier merge; combined with the pool-holding bug this is what turns a slow
# gateway into a full outage.
PAYMENT_MAX_RETRIES = 8
PAYMENT_BASE_DELAY_SECONDS = 0.5
PAYMENT_GATEWAY_TIMEOUT_SECONDS = 30

# Fulfillment queue.
QUEUE_MAX_DEPTH = 5000


def load():
    log.info("config.loaded pool_size=%s payment_max_retries=%s", DB_POOL_SIZE, PAYMENT_MAX_RETRIES)
    return {
        "db_pool_size": DB_POOL_SIZE,
        "payment_max_retries": PAYMENT_MAX_RETRIES,
    }
