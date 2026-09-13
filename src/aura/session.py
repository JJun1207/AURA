from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnvironmentSnapshot:
    dnd_enabled: bool
    hide_all: bool | None
    airplane_enabled: bool
    wifi_enabled: bool
    wifi_connected: bool = False


def capture_environment(
    query: Callable[[], EnvironmentSnapshot],
    last_confirmed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        observed = query()
        return {
            "status": "observed",
            "observed": asdict(observed),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    except (Exception, KeyboardInterrupt) as error:
        return {
            "status": "unavailable",
            "observed": None,
            "observed_at": None,
            "query_failed_at": datetime.now(timezone.utc).isoformat(),
            "last_confirmed": last_confirmed,
            "error_type": type(error).__name__,
            "error": str(error) or type(error).__name__,
        }


@dataclass(frozen=True)
class DeviceSession:
    session_id: str
    serial: str
    system_ui_profile: str
    prepared_at: str
    status: str
    initial: EnvironmentSnapshot
    installed_apps: tuple[dict[str, Any], ...] = ()
    selected_apps: tuple[str, ...] = ()
    execution_plan: tuple[dict[str, Any], ...] = ()
    environment_transitions: tuple[dict[str, Any], ...] = ()
    executions: tuple[dict[str, Any], ...] = ()
    interruption: dict[str, Any] | None = None
    device_model: str | None = None
    android_version: str | None = None
    preparation: dict[str, Any] | None = None
    last_confirmed_state: dict[str, Any] | None = None
    final_state: dict[str, Any] | None = None
    applied_at: str | None = None
    finalized_at: str | None = None
    error: str | None = None

    def with_status(self, status: str, **changes: Any) -> DeviceSession:
        return replace(self, status=status, **changes)


class SessionStore:
    def __init__(self, runs_root: str | Path):
        self.root = Path(runs_root)
        self.active_path = self.root / "session.json"
        self.archive_root = self.root / "sessions"

    def create(self, session: DeviceSession) -> None:
        self._validate_id(session.session_id)
        if self.active_path.exists():
            raise SessionError("an active device session already exists")
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_replace(self.active_path, self._document(session))

    def load(self) -> DeviceSession:
        try:
            document = json.loads(
                self.active_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as error:
            raise SessionError("no active device session exists") from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SessionError("active device session is unreadable") from error
        return self._parse(document)

    def save(self, session: DeviceSession) -> None:
        active = self.load()
        if active.session_id != session.session_id:
            raise SessionError("active device session does not match")
        self._write_replace(self.active_path, self._document(session))

    def archive(self, session: DeviceSession) -> Path:
        active = self.load()
        if active.session_id != session.session_id:
            raise SessionError("active device session does not match")
        destination_root = self.archive_root / session.session_id
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / "session.json"
        hash_path = destination_root / "session.sha256"
        if hash_path.exists():
            raise SessionError(f"session hash already exists: {hash_path}")
        self._write_new(destination, self._document(session))
        try:
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            with hash_path.open("x", encoding="ascii", newline="\n") as stream:
                stream.write(f"{digest}  session.json\n")
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            destination.unlink(missing_ok=True)
            hash_path.unlink(missing_ok=True)
            raise
        self.active_path.unlink()
        return destination

    @staticmethod
    def _document(session: DeviceSession) -> dict[str, Any]:
        document = asdict(session)
        document["schema_version"] = 2
        document["device_session_id"] = document.pop("session_id")
        return document

    @classmethod
    def _parse(cls, document: Any) -> DeviceSession:
        if not isinstance(document, dict):
            raise SessionError("active device session must be an object")
        if document.get("schema_version") != 2:
            raise SessionError("legacy active device session is not supported; preserve its records separately before starting a new session")
        try:
            initial_value = document["initial"]
            if not isinstance(initial_value, dict):
                raise TypeError
            initial = EnvironmentSnapshot(
                dnd_enabled=cls._bool(
                    initial_value, "dnd_enabled"
                ),
                hide_all=cls._optional_bool(
                    initial_value, "hide_all"
                ),
                airplane_enabled=cls._bool(
                    initial_value, "airplane_enabled"
                ),
                wifi_enabled=cls._bool(
                    initial_value, "wifi_enabled"
                ),
                wifi_connected=cls._bool(
                    initial_value, "wifi_connected"
                ),
            )
            session = DeviceSession(
                session_id=cls._string(document, "device_session_id"),
                serial=cls._string(document, "serial"),
                system_ui_profile=cls._string(
                    document, "system_ui_profile"
                ),
                prepared_at=cls._string(document, "prepared_at"),
                status=cls._string(document, "status"),
                initial=initial,
                installed_apps=cls._object_tuple(
                    document, "installed_apps"
                ),
                selected_apps=cls._string_tuple(
                    document, "selected_apps"
                ),
                execution_plan=cls._object_tuple(
                    document, "execution_plan"
                ),
                environment_transitions=cls._object_tuple(
                    document, "environment_transitions"
                ),
                executions=cls._object_tuple(
                    document, "executions"
                ),
                interruption=cls._optional_object(
                    document, "interruption"
                ),
                device_model=cls._optional_string(document, "device_model"),
                android_version=cls._optional_string(document, "android_version"),
                preparation=cls._optional_object(document, "preparation"),
                last_confirmed_state=cls._optional_object(document, "last_confirmed_state"),
                final_state=cls._optional_object(document, "final_state"),
                applied_at=cls._optional_string(
                    document, "applied_at"
                ),
                finalized_at=cls._optional_string(
                    document, "finalized_at"
                ),
                error=cls._optional_string(document, "error"),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise SessionError("active device session is invalid") from error
        cls._validate_id(session.session_id)
        return session

    @staticmethod
    def _string(document: dict[str, Any], name: str) -> str:
        value = document[name]
        if not isinstance(value, str) or not value:
            raise ValueError
        return value

    @staticmethod
    def _optional_string(
        document: dict[str, Any], name: str
    ) -> str | None:
        value = document.get(name)
        if value is not None and not isinstance(value, str):
            raise ValueError
        return value

    @staticmethod
    def _bool(document: dict[str, Any], name: str) -> bool:
        value = document[name]
        if type(value) is not bool:
            raise ValueError
        return value

    @staticmethod
    def _optional_bool(
        document: dict[str, Any], name: str
    ) -> bool | None:
        value = document.get(name)
        if value is not None and type(value) is not bool:
            raise ValueError
        return value

    @staticmethod
    def _string_tuple(
        document: dict[str, Any], name: str
    ) -> tuple[str, ...]:
        value = document.get(name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise ValueError
        return tuple(value)

    @staticmethod
    def _object_tuple(
        document: dict[str, Any], name: str
    ) -> tuple[dict[str, Any], ...]:
        value = document.get(name, [])
        if not isinstance(value, list) or not all(
            isinstance(item, dict) for item in value
        ):
            raise ValueError
        return tuple(dict(item) for item in value)

    @staticmethod
    def _optional_object(
        document: dict[str, Any], name: str
    ) -> dict[str, Any] | None:
        value = document.get(name)
        if value is not None and not isinstance(value, dict):
            raise ValueError
        return None if value is None else dict(value)

    @staticmethod
    def _validate_id(session_id: str) -> None:
        if (
            not isinstance(session_id, str)
            or not session_id
            or session_id in {".", ".."}
            or "/" in session_id
            or "\\" in session_id
            or Path(session_id).is_absolute()
        ):
            raise SessionError(f"unsafe session_id: {session_id!r}")

    @classmethod
    def _write_new(
        cls, destination: Path, document: dict[str, Any]
    ) -> None:
        if destination.exists():
            raise SessionError(f"session record already exists: {destination}")
        temporary = cls._temporary(destination)
        try:
            cls._write(temporary, document)
            try:
                os.link(temporary, destination)
            except FileExistsError as error:
                raise SessionError(
                    f"session record already exists: {destination}"
                ) from error
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _write_replace(
        cls, destination: Path, document: dict[str, Any]
    ) -> None:
        temporary = cls._temporary(destination)
        try:
            cls._write(temporary, document)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _temporary(destination: Path) -> Path:
        return destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )

    @staticmethod
    def _write(path: Path, document: dict[str, Any]) -> None:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded + "\n")
            stream.flush()
            os.fsync(stream.fileno())
