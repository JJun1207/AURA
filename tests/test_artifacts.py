import hashlib
import json

import pytest

from aura.artifacts import ArtifactStore
from aura.journal import EventJournal


def test_retains_hashed_artifact_linked_to_action_and_context(tmp_path):
    journal = EventJournal(
        tmp_path / "events.jsonl",
        {"run_id": "run-1", "route": "materialize"},
    )
    action = journal.record_action(
        "download_attachment",
        status="success",
        attempt_id="attempt-000001",
        context={"target": "message-1", "run_id": "spoof-action"},
    )

    record = ArtifactStore(tmp_path, journal).write_bytes(
        "telegram/file.bin",
        b"AURA",
        kind="attachment",
        attempt_id="attempt-000001",
        action=action,
        context={"availability": "available", "run_id": "spoof-artifact"},
    )

    assert (tmp_path / "artifacts" / "telegram" / "file.bin").read_bytes() == b"AURA"
    assert record.action_id == "action-000001"
    assert record.attempt_id == "attempt-000001"
    assert record.action == "download_attachment"
    assert record.context == {
        "run_id": "run-1",
        "route": "materialize",
        "target": "message-1",
        "availability": "available",
    }
    assert record.sha256 == hashlib.sha256(b"AURA").hexdigest()
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_id"] for event in events] == [
        "event-000001",
        "event-000002",
    ]
    assert events[0]["action_id"] == "action-000001"
    assert events[-1]["details"]["action_id"] == "action-000001"


def test_retain_file_copies_source_into_run(tmp_path):
    source = tmp_path / "source.zip"
    source.write_bytes(b"export")
    run_dir = tmp_path / "run"
    journal = EventJournal(run_dir / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "export", status="success", attempt_id="attempt-000001"
    )

    record = ArtifactStore(run_dir, journal).retain_file(
        source,
        "notion/export.zip",
        kind="export",
        attempt_id="attempt-000001",
        action=action,
    )

    assert (run_dir / record.relative_path).read_bytes() == b"export"
    assert source.read_bytes() == b"export"


@pytest.mark.parametrize("relative_path", ["../escape.bin", "/absolute.bin", r"a\\b.bin"])
def test_rejects_unsafe_artifact_path(tmp_path, relative_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "export", status="success", attempt_id="attempt-000001"
    )

    with pytest.raises(ValueError, match="relative"):
        ArtifactStore(tmp_path, journal).write_bytes(
            relative_path,
            b"x",
            kind="export",
            attempt_id="attempt-000001",
            action=action,
        )


def test_refuses_to_overwrite_retained_artifact(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "export", status="success", attempt_id="attempt-000001"
    )
    store = ArtifactStore(tmp_path, journal)
    store.write_bytes(
        "export.zip",
        b"first",
        kind="export",
        attempt_id="attempt-000001",
        action=action,
    )

    with pytest.raises(FileExistsError):
        store.write_bytes(
            "export.zip",
            b"second",
            kind="export",
            attempt_id="attempt-000001",
            action=action,
        )

    assert (tmp_path / "artifacts" / "export.zip").read_bytes() == b"first"


def test_rejects_artifact_link_to_failed_action(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "export", status="failed", attempt_id="attempt-000001"
    )

    with pytest.raises(ValueError, match="successful action"):
        ArtifactStore(tmp_path, journal).write_bytes(
            "export.zip",
            b"x",
            kind="export",
            attempt_id="attempt-000001",
            action=action,
        )


def test_rejects_artifact_without_matching_attempt(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "export", status="success", attempt_id="attempt-000001"
    )

    with pytest.raises(ValueError, match="same attempt_id"):
        ArtifactStore(tmp_path, journal).write_bytes(
            "export.zip",
            b"x",
            kind="export",
            attempt_id="attempt-000002",
            action=action,
        )
