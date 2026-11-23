from __future__ import annotations

import hashlib
import shutil
import shlex
from tempfile import TemporaryDirectory
from pathlib import Path, PurePosixPath
from typing import Any

from .journal import EventJournal
from .models import ActionRef, ArtifactRecord


def parse_inventory(output, roots, *, format, allow_root=True, validate_roots=False):
    """Decode the producer's format, then validate paths and numeric metadata."""
    if format == 'nul_triples':
        fields = output.split('\0')
        if fields[-1:] != [''] or (len(fields) - 1) % 3:
            raise ValueError('invalid inventory')
        rows = (fields[index:index + 3] for index in range(0, len(fields) - 1, 3))
    elif format == 'tab_lines':
        rows = (line.rsplit('\t', 2) for line in output.splitlines())
    else:
        raise ValueError('unsupported inventory format')
    roots = tuple(PurePosixPath(root) for root in roots)
    records = []
    seen = set()
    for row in rows:
        if len(row) != 3:
            raise ValueError('invalid inventory')
        remote, size_raw, mtime_raw = row
        path = PurePosixPath(remote)
        size, mtime = int(size_raw), int(mtime_raw)
        if (not path.is_absolute() or '..' in path.parts
                or (not allow_root and path in roots)
                or not any(path.is_relative_to(root) for root in roots)
                or (validate_roots and any(not root.is_absolute() or '..' in root.parts for root in roots))
                or size < 0 or mtime < 0 or path.as_posix() in seen):
            raise ValueError('invalid inventory')
        seen.add(path.as_posix())
        records.append((path.as_posix(), size, mtime))
    return tuple(sorted(records))


def nul_inventory_command(
    roots: tuple[PurePosixPath, ...],
) -> str:
    values = " ".join(
        shlex.quote(root.as_posix()) for root in roots
    )
    # Like Drive's batch query, keep filenames in argv (including tabs/newlines).
    # A failed stat must invalidate the whole batch, never shift metadata to
    # another filename or turn a partial scan into an apparently empty result.
    emit = (
        'metadata=$(stat -c "%s %Y" "$@") || '
        '{ printf "AURA_STAT_FAILED"; exit 1; }; '
        'printf "%s\\n" "$metadata" | '
        'while IFS=" " read -r size mtime; do '
        'file=$1; shift; '
        'printf "%s\\000%s\\000%s\\000" "$file" "$size" "$mtime"; '
        'done'
    )
    return (
        f"for root in {values}; do "
        '[ ! -d "$root" ] || find "$root" -mindepth 1 -type f '
        f"-exec sh -c {shlex.quote(emit)} sh {{}} +; done"
    )


def changed_inventory(before, after, *, key=lambda row: row[0], signature=lambda row: row[1:]):
    """Return new/modified observations, preserving after-order and multiplicity.

    Inputs have already passed the caller's inventory validation. The caller
    also owns filename matching, ambiguity, stability and acquisition status.
    """
    previous = {key(row): signature(row) for row in before}
    return tuple(row for row in after if previous.get(key(row)) != signature(row))


def select_inventory_file(before, after, *, matches=lambda row: True, allow_existing=False):
    candidates = tuple(row for row in after if matches(row))
    changed = changed_inventory(before, candidates)
    if len(changed) == 1:
        return changed[0], True
    available = candidates if allow_existing and not changed else changed
    if len(available) != 1:
        raise ValueError('ambiguous' if available else 'unavailable')
    return available[0], False


def displayed_size_matches(size, displayed_size, *, allow_empty=False):
    """Compare bytes with an exact B label or a rounded KB/MB/GB UI label."""
    import re
    if size <= 0:
        return size == 0 and allow_empty and displayed_size == '0 B'
    if not displayed_size:
        return True
    match = re.fullmatch(r'(\d+(?:\.\d+)?) (B|KB|MB|GB)', displayed_size)
    if match is None:
        return False
    amount, unit = match.groups()
    if unit == 'B':
        return size == float(amount)
    tolerance = 0.5 * 10 ** -len(amount.partition('.')[2])
    power = {'KB': 1, 'MB': 2, 'GB': 3}[unit]
    return ((float(amount) - tolerance) * 1000 ** power <= size
            <= (float(amount) + tolerance) * 1024 ** power)


def pull_bytes(device, remote_path, *, filename='artifact', directory=None):
    """Read a caller-validated remote file; remove the temporary copy on failure too."""
    with TemporaryDirectory(dir=directory) as temporary:
        local = Path(temporary) / filename
        device.pull(remote_path, local)
        return local.read_bytes()


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
