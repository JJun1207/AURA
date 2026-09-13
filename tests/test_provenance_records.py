import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
    Route,
)
from aura.profiles import (
    AcquisitionTarget,
    AppProfile,
    ProfileStore,
)
from aura.runtime import AcquisitionRuntime, RunContext
from fakes import FakeDevice, target_rules


def _profiles():
    return SimpleNamespace(
        app=AppProfile(
            app_id="notion",
            package_name="notion.id",
            app_version="1.2.3",
            routes=(Route.MATERIALIZE,),
            targets=(
                AcquisitionTarget(
                    target_id="notion.content",
                    item_types=("page",),
                    method=Route.MATERIALIZE,
                    acquisition_environments=("device_only", "controlled_online"),
                    expected_artifacts=("structured_data",),
                    rules=target_rules(),
                ),
            ),
            selectors={},
            timings={"default_timeout": 0, "poll_interval": 0},
            parameters={},
        ),
        system_ui=ProfileStore(
            Path(__file__).resolve().parents[1] / "profiles"
        ).load_system_ui("samsung"),
    )


def _context(tmp_path):
    profiles = _profiles()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    return RunContext(
        run_dir=tmp_path,
        base_context={"run_id": "run-1"},
        device=FakeDevice(),
        journal=journal,
        artifacts=ArtifactStore(tmp_path, journal),
        ui=SimpleNamespace(),
        app_profile=profiles.app,
        system_ui_profile=profiles.system_ui,
    )


def test_closed_attempt_cannot_emit_another_retention_action(tmp_path):
    context = _context(tmp_path)
    attempt = context.item_attempt("page", "notion.content", "page")
    context.finish_attempt(attempt, ProcedureStatus.INTERRUPTED, AcquisitionStatus.NOT_ACQUIRED, reason="interrupted")
    assert context.item_attempt("page", "notion.content", "page", retention=True) == attempt
    with pytest.raises(ValueError, match="closed"):
        context.linked_action("late_retention", attempt)


def test_validation_rejects_artifact_retained_after_attempt_end(tmp_path):
    context = _context(tmp_path)
    attempt = context.item_attempt("page", "notion.content", "page")
    action = context.linked_action("retain", attempt)
    context.finish_attempt(attempt, ProcedureStatus.INTERRUPTED, AcquisitionStatus.NOT_ACQUIRED, reason="interrupted")
    context.artifacts.write_bytes("late.json", b"{}", kind="ui_record", attempt_id=attempt, action=action)
    with pytest.raises(ValueError, match="outside.*attempt"):
        context.validate_records()


def test_rejects_invalid_attempt_status_combinations(tmp_path):
    context = _context(tmp_path)
    item_id = context.register_item(
        "notion.content",
        "page",
        source={"workspace": "ws-1", "page": "page-1"},
    )
    not_attempted = context.begin_attempt(
        item_id, Route.MATERIALIZE, "device_only"
    )
    completed = context.begin_attempt(
        item_id, Route.MATERIALIZE, "controlled_online"
    )

    with pytest.raises(ValueError, match="not_attempted"):
        context.finish_attempt(
            not_attempted,
            ProcedureStatus.NOT_ATTEMPTED,
            AcquisitionStatus.NOT_ACQUIRED,
            reason="not needed",
        )
    with pytest.raises(ValueError, match="acquisition_status"):
        context.finish_attempt(completed, ProcedureStatus.COMPLETED)


def test_rejects_unknown_item_and_attempt_references(tmp_path):
    context = _context(tmp_path)

    with pytest.raises(ValueError, match="acquisition item"):
        context.begin_attempt(
            "item-999999", Route.MATERIALIZE, "device_only"
        )
    with pytest.raises(ValueError, match="attempt"):
        context.finish_attempt(
            "attempt-999999",
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.ACQUIRED,
        )


def test_rejects_outcome_link_to_unrecorded_action(tmp_path):
    context = _context(tmp_path)
    item_id = context.register_item("notion.content", "page")
    attempt_id = context.begin_attempt(
        item_id, Route.MATERIALIZE, "device_only"
    )
    unrecorded = ActionRef(
        event_id="event-999999",
        action_id="action-999999",
        action="download_page",
        status="failed",
        attempt_id=attempt_id,
        context={},
    )

    with pytest.raises(ValueError, match="unknown action"):
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.NOT_ACQUIRED,
            reason="download_failed",
            action=unrecorded,
        )


def test_serializes_unique_linked_provenance_records(tmp_path):
    profiles = _profiles()

    def collector(context):
        item_id = context.register_item(
            "notion.content",
            "page",
            source={"workspace": "ws-1", "page": "page-1"},
        )
        attempt_id = context.begin_attempt(
            item_id, Route.MATERIALIZE, "device_only"
        )
        action = context.journal.record_action(
            "capture_page",
            status="success",
            attempt_id=attempt_id,
        )
        context.capture_observation(
            "page-1", attempt_id=attempt_id, action=action
        )
        context.artifacts.write_bytes(
            "notion/page-1.json",
            b'{"title":"Page 1"}',
            kind="structured_data",
            attempt_id=attempt_id,
            action=action,
        )
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.ACQUIRED,
        )
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        profiles.app,
        profiles.system_ui,
        Route.MATERIALIZE,
        {},
        collector,
        run_id="run-linked",
    )

    acquisition = json.loads(
        (result.run_dir / "acquisition.json").read_text(encoding="utf-8")
    )
    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    attempt = acquisition["attempts"][0]
    observation = acquisition["observations"][0]
    artifacts = acquisition["artifacts"]

    assert attempt == {
        "started_at": attempt["started_at"],
        "ended_at": attempt["ended_at"],
        "applied_rules": {"identification": "registered_ui_items", "traversal_completion": "explicit_ui_boundary", "result": "required_artifacts", "error_handling": "preserve_partial"},
        "acquisition_item_id": "item-000001",
        "acquisition_status": "acquired",
        "attempt_id": "attempt-000001",
        "context": {},
        "details": {},
        "method": "materialize",
        "acquisition_environment": "device_only",
        "procedure_status": "completed",
        "reason": None,
    }
    assert observation["attempt_id"] == "attempt-000001"
    assert observation["action_id"] == "action-000001"
    assert {record["observation_id"] for record in artifacts[:2]} == {
        "observation-000001"
    }
    assert {record["attempt_id"] for record in artifacts} == {
        "attempt-000001"
    }
    assert next(event for event in events if event["event_type"] == "action")[
        "action_id"
    ] == "action-000001"

    identifiers = [
        acquisition["items"][0]["acquisition_item_id"],
        attempt["attempt_id"],
        observation["observation_id"],
        *(record["artifact_id"] for record in artifacts),
        *(event["action_id"] for event in events if "action_id" in event),
    ]
    assert len(identifiers) == len(set(identifiers))


def test_partial_attempt_creates_linked_outcome(tmp_path):
    context = _context(tmp_path)
    item_id = context.register_item("notion.content", "page")
    attempt_id = context.begin_attempt(
        item_id, Route.MATERIALIZE, "controlled_online"
    )
    action = context.journal.record_action(
        "download_page",
        status="failed",
        attempt_id=attempt_id,
    )

    context.finish_attempt(
        attempt_id,
        ProcedureStatus.COMPLETED,
        AcquisitionStatus.PARTIAL,
        reason="attachment_unavailable",
        action=action,
    )

    assert context.outcomes[0].outcome_id == "outcome-000001"
    assert context.outcomes[0].attempt_id == attempt_id
    assert context.outcomes[0].acquisition_status is AcquisitionStatus.PARTIAL
    assert context.outcomes[0].action_id == "action-000001"


def test_app_item_attempt_is_reused_and_completed_from_retained_artifact(
    tmp_path,
):
    context = _context(tmp_path)

    first = context.item_attempt(
        "page:page-1",
        "notion.content",
        "page",
        source={"page_ref": "page-1"},
    )
    second = context.item_attempt(
        "page:page-1",
        "notion.content",
        "page",
        source={"page_ref": "page-1"},
    )
    source_action = context.journal.record_action(
        "open_page",
        status="success",
    )
    action = context.linked_action(
        "retain_page",
        first,
        source_action=source_action,
    )
    context.artifacts.write_bytes(
        "notion/page-1.json",
        b'{}',
        kind="ui_record",
        attempt_id=first,
        action=action,
    )

    context.complete_open_attempts(AcquisitionStatus.ACQUIRED)

    assert first == second == "attempt-000001"
    assert len(context.items) == 1
    assert context.attempts[0].acquisition_status is AcquisitionStatus.ACQUIRED
    assert action.attempt_id == first
    assert action.context["source_action_id"] == source_action.action_id


def test_retain_observation_uses_supplied_screen_and_tree(tmp_path):
    context = _context(tmp_path)
    attempt_id = context.item_attempt(
        "page:page-1",
        "notion.content",
        "page",
    )
    action = context.journal.record_action(
        "capture_page",
        status="success",
        attempt_id=attempt_id,
    )

    screen, tree = context.retain_observation(
        "notion/page-1/source",
        b"png-at-observation-time",
        b"<hierarchy observation='true'/>",
        attempt_id=attempt_id,
        action=action,
    )

    assert screen.relative_path == "artifacts/notion/page-1/source.png"
    assert tree.relative_path == "artifacts/notion/page-1/source.xml"
    assert context.observations[0].attempt_id == attempt_id
    assert context.observations[0].action_id == action.action_id


def test_identification_distinguishes_unstarted_zero_and_interrupted(tmp_path):
    context = _context(tmp_path)
    planned = context.identifications[0]
    assert planned.identification_status == "not_attempted"
    assert planned.started_at is planned.ended_at is None
    assert planned.reason
    context.begin_identification("notion.content")
    context.finish_identification("notion.content", completion_condition="all_workspaces_visited")
    completed = context.identifications[0]
    assert completed.identification_status == "completed"
    assert completed.traversal_complete is True
    assert completed.identified_item_count == 0
    assert completed.started_at <= completed.ended_at


def test_repeated_attempt_gets_new_id_without_duplicating_item(tmp_path):
    context = _context(tmp_path)
    first = context.item_attempt("page:1", "notion.content", "page")
    assert context.item_attempt("page:1", "notion.content", "page") == first
    context.finish_attempt(first, ProcedureStatus.INTERRUPTED,
                           AcquisitionStatus.NOT_ACQUIRED, reason="screen_unavailable")
    second = context.item_attempt("page:1", "notion.content", "page")
    assert second != first
    assert len(context.items) == 1
    assert context.attempts[0].started_at <= context.attempts[0].ended_at
    assert context.attempts[1].started_at >= context.attempts[0].ended_at


def test_result_completion_requires_declared_artifacts(tmp_path):
    context = _context(tmp_path)
    attempt = context.item_attempt("page:1", "notion.content", "page")
    context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
    assert context.attempts[0].acquisition_status is AcquisitionStatus.NOT_ACQUIRED
    assert context.attempts[0].reason == "required_artifacts_missing"


def test_direct_runtime_does_not_claim_profile_is_installed(tmp_path):
    profiles = _profiles()
    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        profiles.app, profiles.system_ui, Route.MATERIALIZE, {},
        lambda context: Outcome(OutcomeStatus.COMPLETE), run_id="unknown-version",
    )
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    assert document["run"]["installed_app_version"] is None
    assert document["run"]["app_profile_exact_match"] is False
    assert document["schema_version"] == 2
    assert document["profile"]["targets"][0]["target_id"] == "notion.content"


@pytest.mark.parametrize("app_id,version", [("chrome", "150.0.7871.124"), ("samsung_browser", "30.0.0.67")])
@pytest.mark.parametrize("interrupted", [False, True])
def test_browser_rows_bind_single_preserved_json_even_on_interruption(tmp_path, monkeypatch, app_id, version, interrupted):
    import importlib
    module = importlib.import_module(f"aura.apps.{app_id}.collector")
    context = _context(tmp_path)
    context.app_profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_app(app_id, version)
    # Recreate with the selected real profile so all planned targets exist.
    context = RunContext(run_dir=tmp_path, base_context={"condition": {"acquisition_environment": "device_only"}}, device=context.device,
                         journal=context.journal, artifacts=context.artifacts, ui=context.ui, app_profile=context.app_profile, system_ui_profile=context.system_ui_profile)
    action = context.journal.record_action("open_history", status="success")
    def swipe(*args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt("listing interrupted")
        return action
    context.ui.swipe = swipe
    rows = [{"title": "One", "url": "https://one.test"}, {"title": "Two", "url": "https://two.test"}]
    options = dict(surface="history", target_id=f"{app_id}.history", item_type="history_collection", parser=lambda xml: rows)
    if app_id == "chrome":
        monkeypatch.setattr(module, "_open_surface", lambda *args: action)
        options.update(menu_item="history", marker="history")
    else:
        options["opener"] = lambda: action
    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            module._collect_list(context, **options)
    else:
        module._collect_list(context, **options)
    identification = next(record for record in context.identifications if record.target_id == f"{app_id}.history")
    assert identification.identified_item_count == 2
    assert identification.traversal_complete is (not interrupted)
    items = [record for record in context.items if record.item_type != "history_collection"]
    assert len(items) == 2
    artifact = next(record for record in context.artifacts.records if record.kind == "structured_data")
    payload = json.loads((tmp_path / artifact.relative_path).read_text())
    for index, item in enumerate(items):
        assert item.source["collection_attempt_id"] == artifact.attempt_id
        assert item.source["artifact_id"] == artifact.artifact_id
        assert item.source["record_index"] == index
        assert payload["records"][index] == rows[index]


def test_source_snapshot_mapping_is_distinct_from_preserved_observation(tmp_path):
    context = _context(tmp_path)
    attempt = context.item_attempt("page:1", "notion.content", "page")
    action = context.journal.record_action("capture", status="success", attempt_id=attempt)
    context.retain_observation("source", b"png", b"<hierarchy/>", attempt_id=attempt, action=action,
                               context={"source_snapshot_id": "notion-snapshot:observation-000001"})
    observation = context.observations[0]
    assert observation.source_snapshot_id == "notion-snapshot:observation-000001"
    assert observation.source_snapshot_id != observation.observation_id


def test_fallback_finishes_only_after_declared_evidence(tmp_path):
    context = _context(tmp_path)
    context.app_profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_app("telegram", "12.9.2")
    attempt = context.item_attempt("attachment:1", "telegram.attachments", "video")
    action = context.journal.record_action("save_unavailable", status="success", attempt_id=attempt)
    context.artifacts.write_bytes("attachment.json", b"{}", kind="ui_record", attempt_id=attempt, action=action)
    with pytest.raises(ValueError, match="fallback.*evidence"):
        context.finish_fallback(attempt, "display_fallback", "save_action_unavailable")
    context.retain_observation("menu", b"png", b"<hierarchy/>", attempt_id=attempt, action=action)
    context.artifacts.write_bytes("audit.json", b"{}", kind="audit_record", attempt_id=attempt, action=action)
    context.finish_fallback(attempt, "display_fallback", "save_action_unavailable")
    assert context.attempts[0].acquisition_status is AcquisitionStatus.PARTIAL
    assert context.attempts[0].details["fallback"] == "display_fallback"


@pytest.mark.parametrize("app_id,version", [("chrome", "150.0.7871.124"), ("samsung_browser", "30.0.0.67"), ("telegram", "12.9.2"), ("notion", "0.6.4030"), ("notesnook", "3.4.5"), ("whatsapp", "2.26.27.85"), ("google_drive", "2.26.337.0.all.alldpi")])
def test_each_collector_rejects_wrong_ui_identification_criteria_before_device_ui(tmp_path, app_id, version):
    import importlib
    module = importlib.import_module(f"aura.apps.{app_id}")
    context = _context(tmp_path)
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_app(app_id, version)
    profile.targets[0].rules["identification"]["parameters"]["ui_criteria"] = {"invented": True}
    context.app_profile = profile
    context.base_context.update(condition={}, route="materialize")
    with pytest.raises(ValueError, match="identification.*criteria"):
        module.collect(context)
    assert context.device.started == []


def test_record_validation_rejects_cross_attempt_observation_pair(tmp_path):
    from dataclasses import replace
    context = _context(tmp_path)
    ids = []
    for index in range(2):
        attempt = context.item_attempt(f"page:{index}", "notion.content", "page")
        action = context.journal.record_action("capture", status="success", attempt_id=attempt)
        context.retain_observation(f"page-{index}", b"png", b"<hierarchy/>", attempt_id=attempt, action=action)
        context.finish_attempt(attempt, ProcedureStatus.INTERRUPTED, AcquisitionStatus.NOT_ACQUIRED, reason="no_data")
        ids.append(attempt)
    context._observations[0] = replace(context.observations[0], hierarchy_artifact_id=context.observations[1].hierarchy_artifact_id)
    with pytest.raises(ValueError, match="same attempt"):
        context.validate_records()


def test_telegram_list_summary_does_not_identify_an_account(tmp_path):
    from aura.apps.telegram.adapter import TelegramOutputs
    context = _context(tmp_path)
    context.app_profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_app("telegram", "12.9.2")
    action = context.journal.record_action("list_boundary", status="success")
    outputs = TelegramOutputs(context, SimpleNamespace(
        actions={"list_boundary": action}, observation_actions={"source-list": action},
        read_observation=lambda source, kind: b"png" if kind == "screen_image" else b"<hierarchy/>",
    ), "account-scope")
    outputs.write_json(artifact_id="chatroom-list", output_kind="ui_record", artifact_class="chatroom_list",
                       value={"record_kind": "telegram_message_history_acquisition", "containers": []}, action_id="list_boundary",
                       observation_id="source-list", comparison_ref="chatroom-list")
    assert context.items[0].item_type == "account_summary"
