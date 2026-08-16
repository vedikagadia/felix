"""Live-monitoring instrumentation — the reusable timing wrapper.

`Probe` measures how long any callable/block takes and records one sample into
the `metrics` table (felix's CDC source), so the live-monitoring panel — which
tails a CHANGEFEED on `metrics` — sees it in real time. It's deliberately
generic: attach it to the checkout service today, any other service tomorrow.
"""

from .probe import Probe

__all__ = ["Probe"]
