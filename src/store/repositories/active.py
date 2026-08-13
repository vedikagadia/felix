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
    ) -> str:
        """Open a new active-incident session. Returns its id (the session id)."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO active_incidents (alert, origin_node, incident_id)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (alert, origin_node, incident_id),
            )
            row = cur.fetchone()
        return str(row[0])

    def get_session(self, session_id: str) -> ActiveIncident | None:
        """Load a session with its ordered transcript, or None if unknown."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, alert, origin_node, incident_id, status
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
        )

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
