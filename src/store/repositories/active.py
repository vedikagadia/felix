"""ActiveIncidentRepository — working memory for in-flight incident conversations.

An `active_incidents` row is one live conversation (a session); its
`active_incident_turns` children are the ordered user/agent transcript. This is
the multi-turn loop's scratchpad, distinct from the episodic `incidents` table:
follow-up turns append here (and are fed back into the LLM prompt) without
minting a new episodic incident each time.
"""

from __future__ import annotations

from ...models import ActiveIncident, ActiveIncidentTurn
from .base import BaseRepository


class ActiveIncidentRepository(BaseRepository):
    def create_session(
        self,
        *,
        alert: str,
        origin_node: str | None = None,
        incident_id: str | None = None,
        source: str = "chat",
    ) -> str:
        """Open a new active-incident session. Returns its id (the session id)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO active_incidents (alert, origin_node, incident_id, source)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (alert, origin_node, incident_id, source),
            )
            row = cur.fetchone()
        return str(row[0])

    def get_session(self, session_id: str) -> ActiveIncident | None:
        """Load a session with its ordered transcript, or None if unknown."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alert, origin_node, incident_id, status, source
                FROM active_incidents
                WHERE id = %s
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                SELECT turn_order, role, content
                FROM active_incident_turns
                WHERE session_id = %s
                ORDER BY turn_order
                """,
                (session_id,),
            )
            turn_rows = cur.fetchall()
        return ActiveIncident(
            id=str(row[0]),
            alert=row[1],
            origin_node=row[2],
            incident_id=str(row[3]) if row[3] is not None else None,
            status=row[4],
            turns=[
                ActiveIncidentTurn(turn_order=int(t[0]), role=t[1], content=t[2])
                for t in turn_rows
            ],
            source=row[5],
        )

    def list_alerts(self, source: str = "cdc", status: str = "open") -> list[dict]:
        """Sessions of a given source+status, newest-first, as lightweight dicts
        (the API maps these to its AlertPayload). `origin_node` is included so the
        caller can parse `service`/`metric` out of a `"cdc:<service>:<metric>"`
        key — the frozen AlertPayload needs those fields."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alert, origin_node, source, status, created_at
                FROM active_incidents
                WHERE source = %s AND status = %s
                ORDER BY created_at DESC
                """,
                (source, status),
            )
            rows = cur.fetchall()
        return [
            {
                "id": str(r[0]),
                "alert": r[1],
                "origin_node": r[2],
                "source": r[3],
                "status": r[4],
                "created_at": r[5],
            }
            for r in rows
        ]

    def count_open(self, source: str, origin_node: str) -> int:
        """Number of OPEN sessions of a given source with this origin_node — the
        watcher's DB-backed cooldown/dedup guard (one open cdc session per
        `(service, metric)`, encoded as `origin_node = "cdc:<service>:<metric>"`)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*) FROM active_incidents
                WHERE status = 'open' AND source = %s AND origin_node = %s
                """,
                (source, origin_node),
            )
            return int(cur.fetchone()[0])

    def append_turn(self, session_id: str, *, role: str, content: str) -> int:
        """Append a turn to a session, auto-assigning the next turn_order.
        Bumps the session's updated_at. Returns the new turn_order."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(turn_order), 0) + 1 FROM active_incident_turns WHERE session_id = %s",
                (session_id,),
            )
            turn_order = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO active_incident_turns (session_id, turn_order, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (session_id, turn_order, role, content),
            )
            cur.execute(
                "UPDATE active_incidents SET updated_at = now() WHERE id = %s",
                (session_id,),
            )
        return turn_order

    def set_status(self, session_id: str, status: str) -> None:
        """Mark a session open/resolved (bumps updated_at)."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE active_incidents SET status = %s, updated_at = now() WHERE id = %s",
                (status, session_id),
            )
