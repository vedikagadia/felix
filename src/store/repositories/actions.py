"""ActionRepository — the agent_actions audit log (append-only)."""

from __future__ import annotations

import json
from typing import Any

from .base import BaseRepository


class ActionRepository(BaseRepository):
    def log(
        self,
        *,
        action_type: str,
        tool_called: str | None = None,
        input: Any = None,
        output: Any = None,
        model: str | None = None,
        tokens: int | None = None,
    ) -> None:
        """Append one row to the agent_actions audit log.

        `input`/`output` are stored as JSONB; pass dicts/lists/primitives (they're
        json.dumps'd here) or a pre-serialized JSON string.
        """
        input_json = input if isinstance(input, str) or input is None else json.dumps(input)
        output_json = output if isinstance(output, str) or output is None else json.dumps(output)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_actions
                    (action_type, tool_called, input, output, model, tokens)
                VALUES
                    (%s, %s, %s, %s, %s, %s)
                """,
                (action_type, tool_called, input_json, output_json, model, tokens),
            )
