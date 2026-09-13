from __future__ import annotations

import hashlib
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from .journal import EventJournal
from .models import ActionRef, ArtifactRecord


class ArtifactStore:
    def __init__(self, run_dir: str | Path, journal: EventJournal):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = journal
        self._records: list[ArtifactRecord] = []

    @property
    def records(self) -> tuple[ArtifactRecord, ...]:
        return tuple(self._records)

    def write_bytes(
        self,
        relative_path: str,
        value: bytes,
        *,
        kind: str,
        attempt_id: str,
        action: ActionRef,
        observation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        if not isinstance(value, bytes):
            raise TypeError("artifact value must be bytes")
        self._validate_link(attempt_id, action, observation_id)
        destination, relative = self._destination(relative_path)
        created = False
        try:
            with destination.open("xb") as stream:
                created = True
                stream.write(value)
            return self._register(
                destination,
                relative,
                kind,
                attempt_id,
                action,
                observation_id,
                context,
            )
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise

    def retain_file(
        self,
        source: str | Path,
        relative_path: str,
        *,
        kind: str,
        attempt_id: str,
        action: ActionRef,
        observation_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        source_path = Path(source)
        if not source_path.is_file():
            raise ValueError("artifact source must be a regular file")
        self._validate_link(attempt_id, action, observation_id)
        destination, relative = self._destination(relative_path)
        created = False
        try:
            with source_path.open("rb") as source_stream, destination.open(
                "xb"
            ) as destination_stream:
                created = True
                shutil.copyfileobj(source_stream, destination_stream)
            return self._register(
                destination,
                relative,
                kind,
                attempt_id,
                action,
                observation_id,
                context,
            )
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise

    def _destination(self, relative_path: str) -> tuple[Path, PurePosixPath]:
        if not isinstance(relative_path, str) or not relative_path or "\\" in relative_path:
            raise ValueError("artifact path must be a safe relative path")
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts or relative == PurePosixPath("."):
            raise ValueError("artifact path must be a safe relative path")
        destination = self.root.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination, relative

    def _register(
        self,
        path: Path,
        relative: PurePosixPath,
        kind: str,
        attempt_id: str,
        action: ActionRef,
        observation_id: str | None,
        context: dict[str, Any] | None,
    ) -> ArtifactRecord:
        if not kind:
            raise ValueError("artifact kind is required")
        if action.status != "success":
            raise ValueError("artifact must link to a successful action")
        artifact_id = f"artifact-{len(self._records) + 1:06d}"
        linked_context = {**dict(context or {}), **action.context}
        record = ArtifactRecord(
            artifact_id=artifact_id,
            kind=kind,
            relative_path=f"artifacts/{relative.as_posix()}",
            size=path.stat().st_size,
            sha256=self._sha256(path),
            action_id=action.action_id,
            attempt_id=attempt_id,
            observation_id=observation_id,
            action=action.action,
            context=linked_context,
        )
        self.journal.append(
            "artifact_retained",
            status="success",
            context=linked_context,
            details={
                "artifact_id": artifact_id,
                "kind": kind,
                "path": record.relative_path,
                "size": record.size,
                "sha256": record.sha256,
                "action_id": action.action_id,
                "attempt_id": attempt_id,
                "observation_id": observation_id,
            },
        )
        self._records.append(record)
        return record

    @staticmethod
    def _validate_link(
        attempt_id: str,
        action: ActionRef,
        observation_id: str | None,
    ) -> None:
        if not isinstance(attempt_id, str) or not attempt_id:
            raise ValueError("artifact attempt_id is required")
        if action.attempt_id != attempt_id:
            raise ValueError("artifact action must reference the same attempt_id")
        if observation_id is not None and (
            not isinstance(observation_id, str) or not observation_id
        ):
            raise ValueError("observation_id must be a non-empty string")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
