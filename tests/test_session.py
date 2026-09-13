import hashlib
import json

import pytest

from aura.session import (
    DeviceSession,
    EnvironmentSnapshot,
    SessionError,
    SessionStore,
)


def make_session(session_id="session-1", *, status="prepared"):
    return DeviceSession(
        session_id=session_id,
        serial="SERIAL",
        system_ui_profile="samsung",
        prepared_at="2026-07-31T00:00:00+00:00",
        status=status,
        initial=EnvironmentSnapshot(
            dnd_enabled=False,
            hide_all=False,
            airplane_enabled=False,
            wifi_enabled=True,
            wifi_connected=True,
        ),
        installed_apps=(
            {
                "app_id": "whatsapp",
                "package_name": "com.whatsapp",
                "version_name": "2.26.27.85",
                "profile_status": "exact",
            },
        ),
        selected_apps=("whatsapp",),
        execution_plan=(
            {
                "order": 1,
                "run_id": "run-1",
                "app_id": "whatsapp",
                "method": "materialize",
                "acquisition_environment": "device_only",
                "status": "planned",
            },
        ),
        environment_transitions=(
            {
                "acquisition_environment": "device_only",
                "status": "started",
                "at": "2026-07-31T00:00:01+00:00",
            },
        ),
        executions=(
            {
                "run_id": "run-1",
                "status": "complete",
                "archive_path": "runs/run-1.zip",
                "archive_size": 123,
                "sha256": "a" * 64,
            },
        ),
    )


def test_session_store_creates_loads_and_archives(tmp_path):
    store = SessionStore(tmp_path)
    session = make_session()

    store.create(session)

    assert store.load() == session
    archived = store.archive(session)
    assert store.active_path == tmp_path / "session.json"
    assert archived == tmp_path / "sessions" / "session-1" / "session.json"
    assert not store.active_path.exists()
    document = json.loads(archived.read_text(encoding="utf-8"))
    assert document["device_session_id"] == "session-1"
    assert document["execution_plan"][0]["run_id"] == "run-1"
    hash_path = archived.parent / "session.sha256"
    assert hash_path.read_text(encoding="ascii") == (
        hashlib.sha256(archived.read_bytes()).hexdigest() + "  session.json\n"
    )
    assert "restoration" not in document
    assert "restored_at" not in document


def test_stale_active_session_is_rejected_without_rewriting(tmp_path):
    store = SessionStore(tmp_path)
    store.create(make_session())
    document = json.loads(store.active_path.read_text())
    document.pop("schema_version", None)
    document["restoration"] = None
    store.active_path.write_text(json.dumps(document))
    original = store.active_path.read_bytes()
    with pytest.raises(SessionError, match="legacy"):
        store.load()
    assert store.active_path.read_bytes() == original


def test_session_store_refuses_to_replace_active_session(tmp_path):
    store = SessionStore(tmp_path)
    store.create(make_session())

    with pytest.raises(SessionError, match="active"):
        store.create(make_session("session-2"))


def test_session_store_updates_only_matching_active_session(tmp_path):
    store = SessionStore(tmp_path)
    store.create(make_session())

    updated = make_session(status="restore_failed")
    store.save(updated)

    assert store.load().status == "restore_failed"
    with pytest.raises(SessionError, match="does not match"):
        store.save(make_session("session-2"))
    assert not tuple(tmp_path.glob(".session.json.*.tmp"))


def test_session_store_rejects_unsafe_session_identifier(tmp_path):
    store = SessionStore(tmp_path)

    with pytest.raises(SessionError, match="session_id"):
        store.create(make_session("../escape"))
