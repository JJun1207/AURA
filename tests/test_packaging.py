import hashlib
import json
from zipfile import ZipFile

import pytest

from aura.packaging import PackageError, package_run


def test_package_run_writes_reviewable_archive_and_hash(tmp_path):
    run_dir = tmp_path / "run-1"
    artifact = run_dir / "artifacts" / "item.bin"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"evidence")
    (run_dir / ".DS_Store").write_bytes(b"finder")
    (artifact.parent / ".hidden").write_bytes(b"hidden")
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "acquisition.json").write_text(
        '{"outcome":{"status":"complete"}}\n',
        encoding="utf-8",
    )

    result = package_run(run_dir)

    assert result.archive_path == tmp_path / "run-1.zip"
    assert result.hash_path == tmp_path / "run-1.zip.sha256"
    assert result.size == result.archive_path.stat().st_size
    assert result.sha256 == hashlib.sha256(
        result.archive_path.read_bytes()
    ).hexdigest()
    assert result.hash_path.read_text(encoding="ascii").strip() == result.sha256
    assert run_dir.is_dir()
    assert not (run_dir / "files.json").exists()

    expected = {}
    for relative in (
        "acquisition.json",
        "artifacts/item.bin",
        "events.jsonl",
    ):
        value = (run_dir / relative).read_bytes()
        expected[relative] = {
            "path": relative,
            "size": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    with ZipFile(result.archive_path) as archive:
        assert archive.namelist() == [
            "acquisition.json",
            "artifacts/item.bin",
            "events.jsonl",
            "files.json",
        ]
        files = json.loads(archive.read("files.json"))
    assert files == {
        "schema_version": 1,
        "members": [expected[path] for path in sorted(expected)],
    }
    assert all(member["path"] != "files.json" for member in files["members"])


def test_package_run_refuses_existing_archive(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "acquisition.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "run-1.zip").write_bytes(b"existing")

    with pytest.raises(PackageError, match="already exists"):
        package_run(run_dir)

    assert (tmp_path / "run-1.zip").read_bytes() == b"existing"


def test_package_failure_keeps_run_and_removes_partial_outputs(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "acquisition.json").write_text("{}\n", encoding="utf-8")

    def fail_zip(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("aura.packaging.ZipFile", fail_zip)

    with pytest.raises(PackageError, match="disk full"):
        package_run(run_dir)

    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "acquisition.json").is_file()
    assert not (run_dir / "files.json").exists()
    assert not (tmp_path / "run-1.zip").exists()
    assert not (tmp_path / "run-1.zip.sha256").exists()


def test_package_run_rejects_symbolic_links_without_touching_run(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "acquisition.json").write_text("{}\n", encoding="utf-8")
    target = tmp_path / "outside.bin"
    target.write_bytes(b"outside")
    link = run_dir / "artifacts" / "linked.bin"
    link.parent.mkdir()
    link.symlink_to(target)

    with pytest.raises(PackageError, match="symbolic link"):
        package_run(run_dir)

    assert link.is_symlink()
    assert not (run_dir / "files.json").exists()
    assert not (tmp_path / "run-1.zip").exists()
    assert not (tmp_path / "run-1.zip.sha256").exists()
