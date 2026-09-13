from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import ActionRef


class EventJournal:
    def __init__(
        self,
        path: str | Path,
        base_context: dict[str, Any] | None = None,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=False)
        self.base_context = dict(base_context or {})
        self._sequence = 0
        self._action_sequence = 0
        self._actions: list[ActionRef] = []

    @property
    def actions(self) -> tuple[ActionRef, ...]:
        return tuple(self._actions)

    def append(
        self,
        event_type: str,
        *,
        status: str,
        action: str | None = None,
        action_id: str | None = None,
        attempt_id: str | None = None,
        context: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not event_type or not status:
            raise ValueError("event_type and status are required")
        self._sequence += 1
        event = {
            "event_id": f"event-{self._sequence:06d}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "status": status,
            "context": {**dict(context or {}), **self.base_context},
            "details": dict(details or {}),
        }
        if action is not None:
            event["action"] = action
        if action_id is not None:
            event["action_id"] = action_id
        if attempt_id is not None:
            event["attempt_id"] = attempt_id
        encoded = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return event

    def record_action(
        self,
        name: str,
        *,
        status: str,
        attempt_id: str | None = None,
        context: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> ActionRef:
        if not name:
            raise ValueError("action name is required")
        if attempt_id is not None and (
            not isinstance(attempt_id, str) or not attempt_id
        ):
            raise ValueError("attempt_id must be a non-empty string")
        self._action_sequence += 1
        action_id = f"action-{self._action_sequence:06d}"
        event = self.append(
            "action",
            status=status,
            action=name,
            action_id=action_id,
            attempt_id=attempt_id,
            context=context,
            details=details,
        )
        reference = ActionRef(
            event_id=event["event_id"],
            action_id=action_id,
            action=name,
            status=status,
            attempt_id=attempt_id,
            context=event["context"],
        )
        self._actions.append(reference)
        return reference
