from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


class PackageError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageResult:
    archive_path: Path
    hash_path: Path
    size: int
    sha256: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def package_run(run_dir: str | Path) -> PackageResult:
    run_dir = Path(run_dir)
    if not run_dir.is_dir():
        raise PackageError("run directory does not exist")
    for required in ("acquisition.json", "events.jsonl"):
        if not (run_dir / required).is_file():
            raise PackageError(f"run is missing {required}")

    archive_path = run_dir.with_suffix(".zip")
    hash_path = Path(f"{archive_path}.sha256")
    files_path = run_dir / "files.json"
    for destination in (archive_path, hash_path, files_path):
        if destination.exists():
            raise PackageError(f"{destination.name} already exists")

    package_paths = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_symlink():
            raise PackageError("run directory contains a symbolic link")
        relative = path.relative_to(run_dir)
        if not path.is_file() or any(
            part.startswith(".") for part in relative.parts
        ):
            continue
        package_paths.append(path)
    members = []
    for path in package_paths:
        relative = path.relative_to(run_dir).as_posix()
        members.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    temporary = files_path.with_suffix(".json.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(
                {"schema_version": 1, "members": members},
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, files_path)

        with ZipFile(archive_path, "x", ZIP_DEFLATED) as archive:
            for path in (*package_paths, files_path):
                archive.write(path, path.relative_to(run_dir).as_posix())

        digest = _sha256(archive_path)
        size = archive_path.stat().st_size
        with hash_path.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{digest}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as error:
        temporary.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)
        hash_path.unlink(missing_ok=True)
        if isinstance(error, PackageError):
            raise
        raise PackageError(str(error)) from error
    finally:
        files_path.unlink(missing_ok=True)

    return PackageResult(archive_path, hash_path, size, digest)
