import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "reviewable-session"
VALIDATOR = ROOT / "tools" / "validate_aura_results.py"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if path.name == "session.json" and document.get("schema_version") == 2:
        path.with_name("session.sha256").write_text(
            f"{_sha256(path.read_bytes())}  session.json\n", encoding="ascii"
        )


def _write_archive(
    session_path: Path,
    run_id: str,
    members: dict[str, bytes],
    *,
    manifest: dict | None = None,
    update_session: bool = True,
) -> Path:
    archive = session_path.parent / f"{run_id}.zip"
    if manifest is None:
        manifest = {
            "schema_version": 1,
            "members": [
                {
                    "path": path,
                    "size": len(value),
                    "sha256": _sha256(value),
                }
                for path, value in sorted(members.items())
                if path != "files.json"
            ],
        }
    payloads = {**members, "files.json": (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()}
    with ZipFile(archive, "w", ZIP_DEFLATED) as package:
        for path, value in sorted(payloads.items()):
            package.writestr(path, value)
    if update_session:
        session = json.loads(session_path.read_text(encoding="utf-8"))
        execution = next(
            row for row in session["executions"] if row["run_id"] == run_id
        )
        value = archive.read_bytes()
        execution["archive_path"] = archive.name
        execution["archive_size"] = len(value)
        execution["sha256"] = _sha256(value)
        _write_json(session_path, session)
    return archive


def _materialize_fixture(tmp_path: Path) -> Path:
    session_root = tmp_path / "reviewable-session"
    shutil.copytree(FIXTURE, session_root)
    run_dir = session_root / "run-001"
    acquisition_path = run_dir / "acquisition.json"
    acquisition = json.loads(acquisition_path.read_text(encoding="utf-8"))
    for artifact in acquisition["artifacts"]:
        value = (run_dir / artifact["relative_path"]).read_bytes()
        artifact["size"] = len(value)
        artifact["sha256"] = _sha256(value)
    _write_json(acquisition_path, acquisition)
    members = {
        path.relative_to(run_dir).as_posix(): path.read_bytes()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    session_path = session_root / "session.json"
    _write_archive(session_path, "run-001", members)
    return session_path


def _read_archive(session_path: Path, run_id: str = "run-001"):
    archive = session_path.parent / f"{run_id}.zip"
    with ZipFile(archive) as package:
        members = {
            path: package.read(path)
            for path in package.namelist()
            if path != "files.json"
        }
        manifest = json.loads(package.read("files.json"))
    return members, manifest


def _run(session_path: Path, *arguments: str):
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(session_path), *arguments],
        capture_output=True,
        check=False,
        text=True,
    )


def test_valid_session_and_artifact_trace_are_reported(tmp_path):
    session_path = _materialize_fixture(tmp_path)

    verified = _run(session_path)
    traced = _run(session_path, "--trace", "artifact-000001")
    outcome_traced = _run(session_path, "--trace", "outcome-000001")

    assert verified.returncode == 0
    assert json.loads(verified.stdout) == {
        "execution_count": 1,
        "session_path": str(session_path.resolve()),
        "valid": True,
        "validation_scope": "legacy_integrity_and_references",
    }
    assert traced.returncode == 0
    trace = json.loads(traced.stdout)["trace"]
    assert trace["run"]["run_id"] == "run-001"
    assert trace["item"]["acquisition_item_id"] == "item-000001"
    assert trace["attempt"]["attempt_id"] == "attempt-000001"
    assert trace["action"]["action_id"] == "action-000001"
    assert trace["observation"]["observation_id"] == "observation-000001"
    assert trace["artifact"]["artifact_id"] == "artifact-000001"
    assert outcome_traced.returncode == 0
    assert json.loads(outcome_traced.stdout)["trace"]["outcome"][
        "outcome_id"
    ] == "outcome-000001"


def test_duplicate_identifier_is_rejected(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, _ = _read_archive(session_path)
    acquisition = json.loads(members["acquisition.json"])
    acquisition["items"].append(dict(acquisition["items"][0]))
    members["acquisition.json"] = json.dumps(acquisition).encode()
    _write_archive(session_path, "run-001", members)

    result = _run(session_path)

    assert result.returncode == 1
    assert "duplicate identifier item-000001" in json.loads(result.stdout)["errors"]


def test_unknown_record_reference_is_rejected(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, _ = _read_archive(session_path)
    acquisition = json.loads(members["acquisition.json"])
    acquisition["attempts"][0]["acquisition_item_id"] = "item-missing"
    members["acquisition.json"] = json.dumps(acquisition).encode()
    _write_archive(session_path, "run-001", members)

    result = _run(session_path)

    assert result.returncode == 1
    assert "attempt-000001 references unknown item item-missing" in json.loads(
        result.stdout
    )["errors"]


def test_missing_and_unrecorded_archive_members_are_rejected(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, manifest = _read_archive(session_path)
    missing = dict(manifest["members"][0])
    missing["path"] = "artifacts/missing.bin"
    manifest["members"].append(missing)
    members["extra.bin"] = b"not recorded"
    _write_archive(session_path, "run-001", members, manifest=manifest)

    result = _run(session_path)

    assert result.returncode == 1
    errors = json.loads(result.stdout)["errors"]
    assert "run-001.zip is missing member artifacts/missing.bin" in errors
    assert "run-001.zip contains unrecorded member extra.bin" in errors


def test_member_size_and_sha256_mismatch_are_rejected(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, manifest = _read_archive(session_path)
    manifest["members"][0]["size"] += 1
    manifest["members"][1]["sha256"] = "0" * 64
    _write_archive(session_path, "run-001", members, manifest=manifest)

    result = _run(session_path)

    assert result.returncode == 1
    errors = json.loads(result.stdout)["errors"]
    assert any("size mismatch" in error for error in errors)
    assert any("SHA-256 mismatch" in error for error in errors)


def test_session_archive_size_and_sha256_mismatch_are_rejected(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["executions"][0]["archive_size"] += 1
    session["executions"][0]["sha256"] = "0" * 64
    _write_json(session_path, session)

    result = _run(session_path)

    assert result.returncode == 1
    errors = json.loads(result.stdout)["errors"]
    assert "run-001.zip archive size mismatch" in errors
    assert "run-001.zip archive SHA-256 mismatch" in errors


def test_trace_requires_run_when_identifier_occurs_in_multiple_runs(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, _ = _read_archive(session_path)
    acquisition = json.loads(members["acquisition.json"])
    acquisition["run"]["run_id"] = "run-002"
    members["acquisition.json"] = json.dumps(acquisition).encode()
    events = [json.loads(line) for line in members["events.jsonl"].splitlines()]
    events[0]["context"]["run_id"] = "run-002"
    members["events.jsonl"] = (json.dumps(events[0]) + "\n").encode()
    session = json.loads(session_path.read_text(encoding="utf-8"))
    second = dict(session["executions"][0])
    second.update({"archive_path": "run-002.zip", "order": 2, "run_id": "run-002"})
    session["executions"].append(second)
    session["execution_plan"].append(
        {**session["execution_plan"][0], "order": 2, "run_id": "run-002"}
    )
    _write_json(session_path, session)
    _write_archive(session_path, "run-002", members)

    ambiguous = _run(session_path, "--trace", "artifact-000001")
    selected = _run(
        session_path,
        "--run",
        "run-002",
        "--trace",
        "artifact-000001",
    )

    assert ambiguous.returncode == 1
    assert "trace identifier artifact-000001 is ambiguous; pass --run" in json.loads(
        ambiguous.stdout
    )["errors"]
    assert selected.returncode == 0
    assert json.loads(selected.stdout)["trace"]["run"]["run_id"] == "run-002"


def test_malformed_record_is_reported_without_a_traceback(tmp_path):
    session_path = _materialize_fixture(tmp_path)
    members, _ = _read_archive(session_path)
    acquisition = json.loads(members["acquisition.json"])
    acquisition["artifacts"][0]["relative_path"] = []
    members["acquisition.json"] = json.dumps(acquisition).encode()
    events = [json.loads(line) for line in members["events.jsonl"].splitlines()]
    events[0]["context"] = []
    members["events.jsonl"] = (json.dumps(events[0]) + "\n").encode()
    _write_archive(session_path, "run-001", members)

    result = _run(session_path)

    assert result.returncode == 1
    assert result.stderr == ""
    errors = json.loads(result.stdout)["errors"]
    assert "action-000001 context must be an object" in errors
    assert "artifact-000001 has an invalid relative_path" in errors


def _generated_session(tmp_path, monkeypatch, *, profile=None, collector=None, prepare_failed=False):
    """Real session, runtime, journal and packages; synthetic acquisition on a fake phone."""
    from dataclasses import replace
    from aura import cli
    from aura.models import Outcome, OutcomeStatus, ProcedureStatus
    from fakes import FakeEnvironmentDevice, target_rules
    from test_provenance_records import _profiles

    profiles = _profiles()
    if profile is None:
        target = replace(profiles.app.targets[0], expected_artifacts=("structured_data", "ui_observation"),
                         rules=target_rules(("page",), ("structured_data", "ui_observation")))
        profile = replace(profiles.app, targets=(target,))
    device = FakeEnvironmentDevice(wifi_enabled=True)
    if prepare_failed:
        original_shell = device.shell
        def shell(command):
            if command in ("svc wifi disable", "cmd wifi set-wifi-enabled disabled"):
                raise RuntimeError("synthetic Wi-Fi failure")
            return original_shell(command)
        monkeypatch.setattr(device, "shell", shell)
    resolution = cli.ProfileResolution(profile.app_id, profile.package_name, profile.app_version, "exact", profile)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_connected_device", lambda: ("SERIAL", device))
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profiles.system_ui)
    monkeypatch.setattr(cli, "_match_installed_apps", lambda *args: (resolution,))
    monkeypatch.setattr(cli, "_resolve_app_profile", lambda *args: resolution)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)

    def collect(context):
        context.begin_identification("notion.content")
        for number in range(2):
            attempt = context.item_attempt(f"page:{number}", "notion.content", "page")
            action = context.journal.record_action("capture_page", status="success", attempt_id=attempt)
            source = {}
            if number == 0:
                screen, tree = context.retain_observation("page", b"png", b"<hierarchy/>", attempt_id=attempt, action=action,
                                                        source_snapshot_id="notion-snapshot:page")
                source = {"observation_id": screen.observation_id, "screen_artifact_id": screen.artifact_id,
                          "hierarchy_artifact_id": tree.artifact_id, "source_snapshot_id": "notion-snapshot:page"}
            context.artifacts.write_bytes(f"page-{number}.json", json.dumps({"source": source, "title": f"Page {number}"}).encode(),
                                          kind="ui_record", attempt_id=attempt, action=action)
            context.finish_attempt(attempt, ProcedureStatus.COMPLETED if number == 0 else ProcedureStatus.INTERRUPTED,
                                   "acquired" if number == 0 else "partial", reason=None if number == 0 else "screen_unavailable")
        context.finish_identification("notion.content", completion_condition="all_workspaces_visited")
        return Outcome(OutcomeStatus.PARTIAL, "screen_unavailable")

    monkeypatch.setitem(cli.COLLECTORS, profile.app_id, collector or collect)
    if prepare_failed:
        with pytest.raises(cli.CliError):
            cli._execute_acquire((profile.app_id,))
        session_path = next((tmp_path / "runs/sessions").glob("*/session.json"))
    else:
        payload, _ = cli._execute_acquire((profile.app_id,))
        session_path = Path(payload["session_record"])
    # Existing corruption helpers operate on portable archive paths beside session.json.
    session = json.loads(session_path.read_text())
    for execution in session["executions"]:
        archive = Path(execution["archive_path"])
        shutil.copyfile(archive, session_path.parent / archive.name)
        execution["archive_path"] = archive.name
    _write_json(session_path, session)
    return session_path


def _mutate_generated(session_path, mutate):
    session = json.loads(session_path.read_text())
    run_id = session["executions"][0]["run_id"]
    members, manifest = _read_archive(session_path, run_id)
    acquisition = json.loads(members["acquisition.json"])
    events = [json.loads(line) for line in members["events.jsonl"].splitlines()]
    mutate(acquisition, events, members)
    members["acquisition.json"] = json.dumps(acquisition).encode()
    members["events.jsonl"] = b"".join((json.dumps(event) + "\n").encode() for event in events)
    _write_archive(session_path, run_id, members)


def test_generated_schema2_session_validates_and_traces_partial(tmp_path, monkeypatch):
    path = _generated_session(tmp_path, monkeypatch)
    result = _run(path, "--trace", "outcome-000001", "--run", json.loads(path.read_text())["executions"][0]["run_id"])
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert result.returncode == 0, document
    assert document["validation_scope"] == "schema2_record_consistency"
    assert document["trace"]["attempt"]["acquisition_status"] == "partial"
    assert document["trace"]["attempt"]["procedure_status"] == "interrupted"


@pytest.mark.parametrize("mutation,expected", [
    (lambda a, e, m: a.update(schema_version=3), "schema_version"),
    (lambda a, e, m: a["run"].update(installed_app_version="9.9"), "installed_app_version"),
    (lambda a, e, m: a["run"].update(android_version="9"), "android_version"),
    (lambda a, e, m: a["run"]["verified_state"]["observed"].update(wifi_enabled=True), "verified_state"),
    (lambda a, e, m: a["identifications"][0].update(identified_item_count=3), "identified_item_count"),
    (lambda a, e, m: a["identifications"][0].update(identified_item_count=True), "identified_item_count"),
    (lambda a, e, m: a["identifications"][0].update(completion_condition="invented"), "completion_condition"),
    (lambda a, e, m: a["identifications"][0].update(ended_at=None), "ended_at"),
    (lambda a, e, m: a["attempts"][0].update(acquisition_status=None), "acquisition_status"),
    (lambda a, e, m: a["attempts"][0].update(procedure_status="not_attempted"), "not_attempted"),
    (lambda a, e, m: a["attempts"][1].update(acquisition_status="acquired"), "required artifacts"),
    (lambda a, e, m: a["attempts"][0]["applied_rules"].update(result="invented"), "applied_rules"),
    (lambda a, e, m: a["attempts"][0].update(started_at="2099-01-01T00:00:00+00:00"), "interval"),
    (lambda a, e, m: a["attempts"][0].update(acquisition_item_id=[]), "item"),
    (lambda a, e, m: a["artifacts"][0].update(attempt_id={}), "attempt"),
    (lambda a, e, m: a["artifacts"][0].update(attempt_id="attempt-000002"), "same attempt"),
    (lambda a, e, m: a["outcomes"][0].update(action_id=a["observations"][0]["action_id"]), "same attempt"),
    (lambda a, e, m: a["observations"][0].update(source_snapshot_id="observation-000001"), "source_snapshot"),
    (lambda a, e, m: next(x for x in e if x["event_type"] == "action")["context"].update(source_action_id=[]), "action"),
    (lambda a, e, m: next(x for x in e if x["event_type"] == "action")["context"].update(source_action_id=next(x for x in e if x["event_type"] == "action")["action_id"]), "cycle"),
    (lambda a, e, m: next(x for x in e if x["event_type"] == "action")["context"].update(run_id="other"), "run_id"),
    (lambda a, e, m: next(x for x in e if x["event_type"] == "artifact_retained").update(timestamp="2099-01-01T00:00:00+00:00"), "interval"),
    (lambda a, e, m: m.update({"../unsafe": b"x"}), "unsafe"),
])
def test_schema2_semantic_corruption_survives_rehashing(tmp_path, monkeypatch, mutation, expected):
    path = _generated_session(tmp_path, monkeypatch)
    _mutate_generated(path, mutation)
    result = _run(path)
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert result.returncode == 1, document
    assert any(expected in error for error in document["errors"]), document


def test_schema2_session_hash_required_and_exact(tmp_path, monkeypatch):
    path = _generated_session(tmp_path, monkeypatch)
    path.with_name("session.sha256").write_text(_sha256(path.read_bytes()) + "\n")
    result = _run(path)
    assert result.returncode == 1
    assert any("session.sha256" in error for error in json.loads(result.stdout)["errors"])


def _collection_session(tmp_path, monkeypatch, app_id, *, count=None):
    from types import SimpleNamespace
    from aura.models import Outcome, OutcomeStatus
    from aura.profiles import ProfileStore
    from aura.apps.telegram.adapter import TelegramDeviceAdapter, TelegramOutputs
    profile = ProfileStore(ROOT / "profiles").load_app(app_id, {"chrome": "150.0.7871.124", "notion": "0.6.4030", "telegram": "12.9.2"}[app_id])
    count = count if count is not None else (1 if app_id == "telegram" else 2)

    def collect(context):
        if app_id == "telegram":
            chat = "telegram-chat-" + "a" * 64
            outputs = TelegramOutputs(context, TelegramDeviceAdapter(context), "account", logical_chatroom_id=chat)
            outputs.begin_history()
            action = context.journal.record_action("history", status="success")
            outputs.device.actions[action.action_id] = action
            outputs.device.last_action = action
            outputs.device.observe("window-1")
            for ordinal in range(1, count + 1):
                message_ref = f"message-{ordinal:06d}"
                outputs.write_json(artifact_id=f"message-ui-{ordinal:06d}", artifact_class="message", output_kind="ui_record",
                                   action_id=action.action_id, observation_id="window-1", comparison_ref=message_ref,
                                   value={"record_kind": "message", "message_ref": message_ref, "logical_chatroom_id": chat,
                                          "body": "Synthetic preserved message"})
        else:
            target, scope, item_type, key = (("chrome.history", "history_collection", "history_entry", "records") if app_id == "chrome"
                                            else ("notion.content", "account_collection", "account", "accounts"))
            context.begin_identification(target)
            attempt = context.item_attempt("collection", target, scope)
            action = context.journal.record_action("read_collection", status="success", attempt_id=attempt)
            context.retain_observation("collection", b"png", b"<hierarchy/>", attempt_id=attempt, action=action)
            rows = ([{"title": "One", "url": "https://one.test"}, {"title": "Two", "url": "https://two.test"}] if app_id == "chrome"
                    else [{"email": "one@example.test", "account_ref": "account-000001", "workspace_refs": ["ws-1", "ws-2"]},
                          {"email": "two@example.test", "account_ref": "account-000002", "workspace_refs": ["ws-3"]}])
            rows = rows[:count]
            payload = {"record_kind": "chrome_history" if app_id == "chrome" else "notion_accounts", key: rows}
            if app_id == "chrome":
                payload["record_count"] = len(rows)
            artifact = context.artifacts.write_bytes(f"{app_id}/collection.json", json.dumps(payload).encode(), kind="structured_data",
                                                     attempt_id=attempt, action=action)
            context.register_collection_records(target, artifact, rows, item_type=item_type, record_key=key)
            context.finish_identification(target, completion_condition="repeated_hierarchy" if app_id == "chrome" else "all_workspaces_visited")
        context.complete_open_attempts("acquired")
        return Outcome(OutcomeStatus.COMPLETE)

    return _generated_session(tmp_path, monkeypatch, profile=profile, collector=collect)


def _chrome_account_session(tmp_path, monkeypatch):
    from aura.apps.chrome import collector as chrome
    from aura.models import Outcome, OutcomeStatus
    from aura.profiles import ProfileStore

    profile = ProfileStore(ROOT / "profiles").load_app("chrome", "150.0.7871.124")
    hierarchy = """<hierarchy>
      <node resource-id="com.android.chrome:id/account_management_account_row">
        <node resource-id="android:id/title" text="Example User" />
        <node resource-id="android:id/summary" text="user@example.test" />
      </node>
    </hierarchy>"""

    def collect(context):
        monkeypatch.setattr(context.device, "hierarchy", lambda: hierarchy)
        monkeypatch.setattr(
            chrome,
            "_open_surface",
            lambda context, *args: context.journal.record_action(
                "open_settings", status="success"
            ),
        )
        _, action = chrome._collect_list(
            context,
            surface="account",
            target_id="chrome.account",
            item_type="account_context",
            menu_item="chrome.menu.settings",
            marker="chrome.screen.settings",
            parser=chrome._parse_account_list,
            scroll=False,
        )
        return Outcome(OutcomeStatus.COMPLETE, action=action)

    return _generated_session(
        tmp_path, monkeypatch, profile=profile, collector=collect
    )


def test_chrome_account_context_binds_each_real_producer_row(tmp_path, monkeypatch):
    path = _chrome_account_session(tmp_path, monkeypatch)
    run_id = json.loads(path.read_text())["executions"][0]["run_id"]
    members, _ = _read_archive(path, run_id)
    acquisition = json.loads(members["acquisition.json"])
    owner = next(item for item in acquisition["items"] if item["item_type"] == "account_context")
    entry = next(item for item in acquisition["items"] if item["item_type"] == "account_entry")
    artifact = next(record for record in acquisition["artifacts"] if record["relative_path"] == "artifacts/chrome/account.json")

    result = _run(path, "--trace", artifact["artifact_id"], "--run", run_id)
    document = json.loads(result.stdout)

    assert result.returncode == 0, document
    assert entry["target_id"] == owner["target_id"] == "chrome.account"
    assert entry["source"] == {
        "artifact_id": artifact["artifact_id"],
        "collection_attempt_id": artifact["attempt_id"],
        "record_index": 0,
    }
    assert json.loads(members[artifact["relative_path"]]) == {
        "record_kind": "chrome_account",
        "record_count": 1,
        "records": [{"display_name": "Example User", "email": "user@example.test"}],
    }
    assert [item["acquisition_item_id"] for item in document["trace"]["associated_items"]] == [entry["acquisition_item_id"]]


def test_chrome_account_binding_rejects_mismatched_owner_scope(tmp_path, monkeypatch):
    path = _chrome_account_session(tmp_path, monkeypatch)

    def mutate(acquisition, events, members):
        owner = next(item for item in acquisition["items"] if item["item_type"] == "account_context")
        owner["item_type"] = "account_entry"
        acquisition["identifications"][0]["identified_item_count"] = 2

    _mutate_generated(path, mutate)
    result = _run(path)

    assert result.returncode == 1, result.stdout
    assert any(
        "invalid array key/index/row/scope" in error
        for error in json.loads(result.stdout)["errors"]
    )


def _telegram_artifact_source_session(tmp_path, monkeypatch):
    from aura.models import AcquisitionStatus, Outcome, OutcomeStatus, ProcedureStatus
    from aura.profiles import ProfileStore

    profile = ProfileStore(ROOT / "profiles").load_app("telegram", "12.9.2")

    def collect(context):
        context.begin_identification("telegram.attachments")
        source_action = None
        for number in (1, 2):
            attempt = context.item_attempt(
                f"attachment:{number}", "telegram.attachments", "file"
            )
            action = context.journal.record_action(
                f"retain_attachment_{number}",
                status="success",
                attempt_id=attempt,
                context=(
                    {"source_action_id": source_action.action_id}
                    if source_action is not None
                    else {}
                ),
            )
            screen, _ = context.retain_observation(
                f"telegram/attachment-{number}",
                b"png",
                b"<hierarchy/>",
                attempt_id=attempt,
                action=action,
                source_snapshot_id=f"telegram-snapshot:source-{number}",
            )
            context.artifacts.write_bytes(
                f"telegram/attachment-{number}.json",
                b'{"record_kind":"attachment"}',
                kind="ui_record",
                attempt_id=attempt,
                action=action,
            )
            context.artifacts.write_bytes(
                f"telegram/attachment-{number}.bin",
                b"original",
                kind="original_artifact",
                attempt_id=attempt,
                action=action,
                observation_id=screen.observation_id,
                context={
                    "source_snapshot_id": f"telegram-snapshot:source-{number}"
                },
            )
            context.finish_attempt(
                attempt,
                ProcedureStatus.COMPLETED,
                AcquisitionStatus.ACQUIRED,
            )
            source_action = action
        context.finish_identification(
            "telegram.attachments",
            completion_condition="chat_list_and_histories_exhausted",
        )
        return Outcome(OutcomeStatus.COMPLETE, action=source_action)

    return _generated_session(
        tmp_path, monkeypatch, profile=profile, collector=collect
    )


def test_artifact_source_snapshot_accepts_same_attempt_and_cross_attempt_action_ancestry(tmp_path, monkeypatch):
    path = _telegram_artifact_source_session(tmp_path, monkeypatch)

    result = _run(path)

    assert result.returncode == 0, result.stdout


def test_artifact_source_snapshot_must_be_preserved(tmp_path, monkeypatch):
    path = _telegram_artifact_source_session(tmp_path, monkeypatch)

    def mutate(acquisition, events, members):
        artifact = next(
            record
            for record in acquisition["artifacts"]
            if record["relative_path"].endswith("attachment-1.bin")
        )
        artifact["context"]["source_snapshot_id"] = "telegram-snapshot:missing"

    _mutate_generated(path, mutate)
    result = _run(path)

    assert result.returncode == 1, result.stdout
    assert any(
        "artifact-000004 context unknown source_snapshot reference" in error
        for error in json.loads(result.stdout)["errors"]
    )


def test_artifact_canonical_observation_must_preserve_its_declared_source(tmp_path, monkeypatch):
    path = _telegram_artifact_source_session(tmp_path, monkeypatch)

    def mutate(acquisition, events, members):
        artifact = next(
            record
            for record in acquisition["artifacts"]
            if record["relative_path"].endswith("attachment-1.bin")
        )
        artifact["context"]["source_snapshot_id"] = "telegram-snapshot:source-2"

    _mutate_generated(path, mutate)
    result = _run(path)

    assert result.returncode == 1, result.stdout
    assert any(
        "artifact-000004 context source_snapshot does not map to canonical observation"
        in error
        for error in json.loads(result.stdout)["errors"]
    )


def test_artifact_canonical_observation_stays_in_its_attempt(tmp_path, monkeypatch):
    path = _telegram_artifact_source_session(tmp_path, monkeypatch)

    def mutate(acquisition, events, members):
        artifact = next(
            record
            for record in acquisition["artifacts"]
            if record["relative_path"].endswith("attachment-2.bin")
        )
        observation_id = acquisition["observations"][0]["observation_id"]
        artifact["observation_id"] = observation_id
        event = next(
            record
            for record in events
            if record.get("event_type") == "artifact_retained"
            and record["details"]["artifact_id"] == artifact["artifact_id"]
        )
        event["details"]["observation_id"] = observation_id

    _mutate_generated(path, mutate)
    result = _run(path)

    assert result.returncode == 1, result.stdout
    assert any(
        "artifact-000008 observation_id must reference the same attempt" in error
        for error in json.loads(result.stdout)["errors"]
    )


def test_legacy_artifact_context_keeps_noncanonical_source_hint(tmp_path):
    path = _materialize_fixture(tmp_path)
    members, _ = _read_archive(path)
    acquisition = json.loads(members["acquisition.json"])
    acquisition["artifacts"][0]["context"]["source_snapshot_id"] = "legacy-source"
    members["acquisition.json"] = json.dumps(acquisition).encode()
    _write_archive(path, "run-001", members)

    result = _run(path)

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("app_id,count", [("chrome", 2), ("notion", 2), ("telegram", 1)])
def test_trace_exposes_real_collection_bound_items(tmp_path, monkeypatch, app_id, count):
    path = _collection_session(tmp_path, monkeypatch, app_id)
    run_id = json.loads(path.read_text())["executions"][0]["run_id"]
    members, _ = _read_archive(path, run_id)
    acquisition = json.loads(members["acquisition.json"])
    bound = next(item for item in acquisition["items"] if "collection_attempt_id" in item["source"])
    result = _run(path, "--trace", bound["source"]["artifact_id"], "--run", run_id)
    document = json.loads(result.stdout)
    assert result.returncode == 0, document
    assert len(document["trace"]["associated_items"]) == count
    assert document["trace"]["attempt"]["attempt_id"] == bound["source"]["collection_attempt_id"]
    assert document["trace"]["item"]["item_type"] in ("history_collection", "account_collection", "conversation")


@pytest.mark.parametrize("app_id,field,value", [
    ("chrome", "record_index", 999), ("chrome", "record_index", True),
    ("chrome", "record_index", 1), ("chrome", "record_key", "accounts"),
    ("notion", "record_key", "workspaces"), ("notion", "collection_attempt_id", {}),
    ("telegram", "message_ref", "message-000002"), ("telegram", "logical_chatroom_id", "other"),
    ("telegram", "record_index", 0),
])
def test_collection_binding_corruption_is_rejected(tmp_path, monkeypatch, app_id, field, value):
    path = _collection_session(tmp_path, monkeypatch, app_id)
    def mutate(a, e, m):
        next(item for item in a["items"] if "collection_attempt_id" in item["source"])["source"][field] = value
    _mutate_generated(path, mutate)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any("binding" in error for error in json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("app_id,count,remove_count", [("chrome", 2, 2), ("notion", 2, 2),
                                                       ("telegram", 1, 1), ("telegram", 2, 1)])
def test_original_requires_bindings_after_items_and_counts_are_removed(tmp_path, monkeypatch, app_id, count, remove_count):
    path = _collection_session(tmp_path, monkeypatch, app_id, count=count)
    assert _run(path).returncode == 0

    def mutate(a, e, m):
        removed = [item for item in a["items"] if "collection_attempt_id" in item["source"]][:remove_count]
        assert len(removed) == remove_count
        for item in removed:
            a["items"].remove(item)
            identification = next(record for record in a["identifications"] if record["target_id"] == item["target_id"])
            identification["identified_item_count"] -= 1

    _mutate_generated(path, mutate)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any("binding" in error for error in json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("app_id", ["chrome", "notion"])
def test_empty_collection_original_needs_no_individual_bindings(tmp_path, monkeypatch, app_id):
    result = _run(_collection_session(tmp_path, monkeypatch, app_id, count=0))
    assert result.returncode == 0, result.stdout


def _mutate_payload(a, events, members, change):
    artifact = next(record for record in a["artifacts"] if record["relative_path"].endswith("page-0.json"))
    payload = json.loads(members[artifact["relative_path"]])
    change(payload)
    value = json.dumps(payload).encode()
    members[artifact["relative_path"]] = value
    artifact.update(size=len(value), sha256=_sha256(value))
    event = next(row for row in events if row["event_type"] == "artifact_retained" and row["details"]["artifact_id"] == artifact["artifact_id"])
    event["details"].update(size=len(value), sha256=_sha256(value))


@pytest.mark.parametrize("field,value", [("observation_id", []), ("screen_artifact_id", "artifact-000004"),
                                        ("source_snapshot_id", "notion-snapshot:missing"), ("hierarchy_artifact_id", "artifact-000001"),
                                        ("artifact_id", {})])
def test_app_json_preserved_source_mapping_corruption(tmp_path, monkeypatch, field, value):
    path = _generated_session(tmp_path, monkeypatch)
    _mutate_generated(path, lambda a, e, m: _mutate_payload(a, e, m, lambda p: p["source"].update({field: value})))
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any("JSON" in error for error in json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("mutation,expected", [
    (lambda s: s.update(schema_version=99), "schema_version"),
    (lambda s: s["preparation"]["observed"].update(wifi_enabled=True), "preparation"),
    (lambda s: s["executions"][0].update(status="complete"), "status"),
    (lambda s: s["executions"][0].update(archive_path=None), "incomplete validation"),
    (lambda s: s["execution_plan"][0].update(acquisition_environment="controlled_online"), "acquisition_environment"),
    (lambda s: s["installed_apps"][0].update(version_name="0.1"), "version"),
    (lambda s: s["final_state"].update(status="unavailable"), "unavailable"),
    (lambda s: s["executions"][0].update(completed_at=s["prepared_at"]), "interval"),
])
def test_session_record_corruption_rehashed(tmp_path, monkeypatch, mutation, expected):
    path = _generated_session(tmp_path, monkeypatch)
    session = json.loads(path.read_text())
    mutation(session)
    _write_json(path, session)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any(expected in error for error in json.loads(result.stdout)["errors"])


def test_real_notion_summary_preserves_account_and_workspace_bindings(tmp_path):
    from dataclasses import replace
    from aura.apps.notion import collect
    from aura.models import Route
    from aura.profiles import ProfileStore
    from aura.runtime import AcquisitionRuntime
    from test_notion import NotionDevice
    profiles = ProfileStore(ROOT / "profiles")
    profile = replace(profiles.load_app("notion", "0.6.4030"), timings={"default_timeout": .01, "poll_interval": 0, "application_start_settle": 0, "transition_settle": 0})
    result = AcquisitionRuntime(tmp_path, NotionDevice()).run(profile, profiles.load_system_ui("samsung"), Route.MATERIALIZE,
        {"acquisition_environment": "device_only", "target": {"kind": "account", "ref": "account.notion.test.001"}}, collect, run_id="notion-real-summary")
    acquisition = _assert_app_records(result.run_dir)
    accounts = [item for item in acquisition["items"] if item["item_type"] == "account"]
    workspaces = [item for item in acquisition["items"] if item["item_type"] == "workspace"]
    assert len(accounts) == 1 and len(workspaces) == 2


def _assert_app_records(run_dir):
    import runpy
    acquisition = json.loads((run_dir / "acquisition.json").read_text())
    members = {f.relative_to(run_dir).as_posix(): f.read_bytes() for f in run_dir.rglob("*") if f.is_file()}
    validator = runpy.run_path(str(VALIDATOR), run_name="validator")
    errors = []
    events = [json.loads(line) for line in members["events.jsonl"].splitlines()]
    actions = validator["_index"]([e for e in events if e["event_type"] == "action"], "action_id", errors)
    indexes = validator["_validate_references"](acquisition, actions, members, errors)
    validator["_validate_app_json"](indexes, members, acquisition["run"], errors)
    assert not errors
    return acquisition


@pytest.mark.parametrize("app_id,version", [("chrome", "150.0.7871.124"), ("samsung_browser", "30.0.0.67"),
                                          ("google_drive", "2.26.337.0.all.alldpi"), ("whatsapp", "2.26.27.85"), ("notesnook", "3.4.5")])
def test_existing_app_producers_preserve_valid_json_references(tmp_path, monkeypatch, app_id, version):
    import importlib
    from dataclasses import replace
    from types import SimpleNamespace
    from aura.models import Route, Outcome, OutcomeStatus
    from aura.profiles import ProfileStore
    from aura.runtime import AcquisitionRuntime
    from fakes import FakeDevice
    profiles = ProfileStore(ROOT / "profiles")
    profile = replace(profiles.load_app(app_id, version), timings={"default_timeout": .01, "poll_interval": 0, "transition_settle": 0, "application_start_settle": 0})
    condition = {"acquisition_environment": "device_only"}
    device = FakeDevice()
    if app_id == "google_drive":
        from test_google_drive import DriveTreeDevice, run_drive
        result, _ = run_drive(tmp_path, monkeypatch, DriveTreeDevice({("My Drive",): [("report.txt", "Text", "1 pm")]}))
        document = _assert_app_records(result.run_dir)
        assert document["profile"]["app_version"] == "2.26.337.0.all.alldpi"
        return
    if app_id == "whatsapp":
        from test_whatsapp_materialize import TextHistoryDevice
        device = TextHistoryDevice()
        condition["target"] = {"kind": "chat", "ref": "chat.whatsapp.test.001", "display_name": "TEST TEST"}
        collect = importlib.import_module("aura.apps.whatsapp").collect
    elif app_id == "notesnook":
        from aura.apps.notesnook.adapter import NotesnookOutputs
        def collect(context):
            action = context.journal.record_action("read_version", status="success")
            device = SimpleNamespace(actions={"read": action}, read_observation=lambda ref, kind: b"png" if kind == "screen_image" else b"<hierarchy/>")
            outputs = NotesnookOutputs(context, device, "container.notesnook.authenticated-workspace.001")
            outputs.write_record("materialize/notes/note-000001/history/version-000001.json",
                {"note_ref": "note-000001", "version_ref": "version-000001", "source": {"observation_id": "version"}},
                action_id="read", observation_id="version", include_observation=True)
            outputs.finalize()
            return Outcome(OutcomeStatus.COMPLETE)
    else:
        module = importlib.import_module(f"aura.apps.{app_id}.collector")
        def collect(context):
            opener = lambda: context.journal.record_action("open_history", status="success")
            options = dict(surface="history", target_id=f"{app_id}.history", item_type="history_collection", parser=lambda xml: [{"title": "One", "url": "https://one.test"}])
            if app_id == "chrome":
                monkeypatch.setattr(module, "_open_surface", lambda *args: opener())
                options.update(menu_item="history", marker="history")
            else:
                options["opener"] = opener
            module._collect_list(context, **options)
            return Outcome(OutcomeStatus.COMPLETE)
    result = AcquisitionRuntime(tmp_path, device).run(profile, profiles.load_system_ui("samsung"), Route.MATERIALIZE, condition, collect, run_id="actual-app-output")
    _assert_app_records(result.run_dir)


def test_real_partial_telegram_history_json_and_snapshots(tmp_path, monkeypatch):
    from test_telegram import test_partial_history_uses_real_scope_before_acquisition_and_retains_message
    test_partial_history_uses_real_scope_before_acquisition_and_retains_message(tmp_path, monkeypatch)
    document = _assert_app_records(tmp_path / "partial-history")
    assert document["outcome"]["status"] == "partial"


@pytest.mark.parametrize("mutation,expected", [
    (lambda a, e, m: a["identifications"][0].update(started_at=a["started_at"]), "identification_started"),
    (lambda a, e, m: a["attempts"][0].update(ended_at=a["ended_at"]), "attempt_finished"),
    (lambda a, e, m: a["profile"]["targets"][0].update(item_types={}), "item_type"),
    (lambda a, e, m: a["profile"]["targets"][0]["rules"]["result"]["parameters"].update(required_artifacts={"structured_data": ["screen_image"]}), "rule"),
    (lambda a, e, m: a["artifacts"][0].update(size=True), "size"),
    (lambda a, e, m: e.remove(next(row for row in e if row["event_type"] == "artifact_retained")), "artifact_retained"),
])
def test_record_and_event_correspondence(tmp_path, monkeypatch, mutation, expected):
    path = _generated_session(tmp_path, monkeypatch)
    _mutate_generated(path, mutation)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any(expected in error for error in json.loads(result.stdout)["errors"])


def test_failed_preparation_is_valid_record_without_acquisition(tmp_path, monkeypatch):
    path = _generated_session(tmp_path, monkeypatch, prepare_failed=True)
    result = _run(path)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["execution_count"] == 0
    assert json.loads(path.read_text())["status"] == "prepare_failed"


@pytest.mark.parametrize("mutation,expected", [
    (lambda a, e, m: a["run"].update(condition=[]), "condition"),
    (lambda a, e, m: a["profile"]["targets"][0].update(item_types=12), "item_type"),
    (lambda a, e, m: a["artifacts"][0]["context"].update(run_id="other"), "run_id"),
    (lambda a, e, m: a["outcomes"][0].update(attempt_id=None), "attempt"),
    (lambda a, e, m: a["outcome"].update(action_id=[]), "action"),
    (lambda a, e, m: a["artifacts"][0].update(action="invented"), "action"),
])
def test_malformed_nested_record_never_raises(tmp_path, monkeypatch, mutation, expected):
    path = _generated_session(tmp_path, monkeypatch)
    _mutate_generated(path, mutation)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any(expected in error for error in json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("member", ["acquisition.json", "files.json"])
def test_nonobject_package_records_are_invalid(tmp_path, monkeypatch, member):
    path = _generated_session(tmp_path, monkeypatch)
    run_id = json.loads(path.read_text())["executions"][0]["run_id"]
    members, _ = _read_archive(path, run_id)
    if member == "acquisition.json":
        members[member] = b"[]"
        _write_archive(path, run_id, members)
    else:
        _write_archive(path, run_id, members, manifest=[])
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any(member in error for error in json.loads(result.stdout)["errors"])


@pytest.mark.parametrize("corrupt", [False, True])
def test_unavailable_save_inventory_is_unperformed_not_copied(tmp_path, monkeypatch, corrupt):
    from aura.models import Outcome, OutcomeStatus
    from aura.profiles import ProfileStore
    profile = ProfileStore(ROOT / "profiles").load_app("telegram", "12.9.2")
    def collect(context):
        attempt = context.item_attempt("attachment:1", "telegram.attachments", "video")
        action = context.journal.record_action("save_unavailable", status="success", attempt_id=attempt)
        context.retain_observation("menu", b"png", b"<hierarchy/>", attempt_id=attempt, action=action)
        context.artifacts.write_bytes("attachment.json", b'{"record_kind":"attachment"}', kind="ui_record", attempt_id=attempt, action=action)
        audit = {"record_kind": "attachment_materialize_audit", "limitation": "attachment_materialize_save_action_unavailable",
                 "before_count": 1, "before_manifest_sha256": "a" * 64, "after_inventory_status": "not_performed",
                 "after_count": 1 if corrupt else None, "after_manifest_sha256": "a" * 64 if corrupt else None}
        context.artifacts.write_bytes("audit.json", json.dumps(audit).encode(), kind="audit_record", attempt_id=attempt, action=action)
        context.finish_fallback(attempt, "display_fallback", "save_action_unavailable")
        return Outcome(OutcomeStatus.PARTIAL, "save_action_unavailable")
    path = _generated_session(tmp_path, monkeypatch, profile=profile, collector=collect)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == int(corrupt), result.stdout
    if corrupt:
        assert any("after inventory" in error for error in json.loads(result.stdout)["errors"])


def test_original_json_user_fields_are_not_aura_references(tmp_path, monkeypatch):
    from aura.models import Outcome, OutcomeStatus
    def collect(context):
        attempt = context.item_attempt("page:1", "notion.content", "page")
        action = context.journal.record_action("pull", status="success", attempt_id=attempt)
        context.artifacts.write_bytes("user-file.json", b'{"observation_id":{},"artifact_id":[],"source_snapshot_id":"user text"}',
                                      kind="original_artifact", attempt_id=attempt, action=action)
        context.complete_open_attempts("acquired")
        return Outcome(OutcomeStatus.PARTIAL, "required_artifacts_missing")
    path = _generated_session(tmp_path, monkeypatch, collector=collect)
    result = _run(path)
    assert result.returncode == 0, result.stdout


def test_interrupted_session_keeps_valid_partial_package(tmp_path, monkeypatch):
    def collect(context):
        attempt = context.item_attempt("page:1", "notion.content", "page")
        action = context.journal.record_action("read_partial", status="success", attempt_id=attempt)
        context.artifacts.write_bytes("partial.json", b'{}', kind="ui_record", attempt_id=attempt, action=action)
        raise KeyboardInterrupt("synthetic interruption")
    path = _generated_session(tmp_path, monkeypatch, collector=collect)
    result = _run(path, "--trace", "outcome-000001")
    document = json.loads(result.stdout)
    assert result.returncode == 0, document
    assert document["trace"]["session"]["status"] == "interrupted"
    assert document["trace"]["attempt"]["acquisition_status"] == "partial"


@pytest.mark.parametrize("location,exit_code", [("run_verified", 1), ("transition_started", 1), ("session_final", 0), ("transition_completed", 0)])
def test_unavailable_state_cannot_replace_successful_verification(tmp_path, monkeypatch, location, exit_code):
    path = _generated_session(tmp_path, monkeypatch)
    session = json.loads(path.read_text())
    unavailable = {"status": "unavailable", "observed": None, "observed_at": None,
                   "query_failed_at": session["finalized_at"], "last_confirmed": None,
                   "error": "synthetic query failure", "error_type": "RuntimeError"}
    if location == "run_verified":
        _mutate_generated(path, lambda a, e, m: a["run"].update(verified_state=unavailable))
    else:
        if location == "transition_started":
            session["environment_transitions"][0]["state"] = unavailable
        elif location == "transition_completed":
            transition = next(row for row in session["environment_transitions"] if row["status"] == "completed")
            transition["state"] = {**unavailable, "query_failed_at": transition["at"],
                                   "last_confirmed": session["environment_transitions"][0]["state"]}
        else:
            session["final_state"] = unavailable
        _write_json(path, session)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == exit_code, result.stdout
    if exit_code:
        assert any("unavailable" in error for error in json.loads(result.stdout)["errors"])


def test_completed_transition_records_actual_changed_state(tmp_path, monkeypatch):
    path = _generated_session(tmp_path, monkeypatch)
    session = json.loads(path.read_text())
    completed = next(row for row in session["environment_transitions"] if row["status"] == "completed")
    completed["state"]["observed"]["wifi_enabled"] = True
    _write_json(path, session)
    result = _run(path)
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("location", ["preparation", "run_verification", "run_verification_after_attempt_start"])
def test_verification_observation_precedes_actual_acquisition(tmp_path, monkeypatch, location):
    path = _generated_session(tmp_path, monkeypatch)
    if location == "preparation":
        session = json.loads(path.read_text())
        session["preparation"]["observed_at"] = "2099-01-01T00:00:00+00:00"
        _write_json(path, session)
    else:
        def mutate(a, e, m):
            a["run"]["verified_state"]["observed_at"] = (
                a["attempts"][0]["ended_at"] if location == "run_verification_after_attempt_start"
                else "2099-01-01T00:00:00+00:00")
        _mutate_generated(path, mutate)
    result = _run(path)
    assert result.stderr == ""
    assert result.returncode == 1, result.stdout
    assert any("observed_at" in error for error in json.loads(result.stdout)["errors"])
