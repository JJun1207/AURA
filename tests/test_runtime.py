from fakes import browser_profile
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.models import (
    AcquisitionStatus,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
    Route,
)
from aura.profiles import (
    AcquisitionTarget,
    AppProfile,
    ProfileStore,
    SystemUIProfile,
)
from aura.runtime import AcquisitionRunError, AcquisitionRuntime, RunContext
from fakes import (
    FakeBluetoothDevice,
    FakeDevice,
    FakeDndDevice,
    FakeEnvironmentDevice,
)


@pytest.fixture
def profiles():
    return SimpleNamespace(
        app=AppProfile(
            app_id="notion",
            package_name="notion.id",
            app_version="1.2.3",
            routes=(Route.MATERIALIZE, Route.EXPORT),
            targets=(
                AcquisitionTarget(
                    target_id="notion.content",
                    item_types=("page",),
                    method=Route.MATERIALIZE,
                    acquisition_environments=("device_only", "controlled_online"),
                    expected_artifacts=("structured_data",),
                    rules=__import__("fakes").target_rules(),
                ),
                AcquisitionTarget(
                    target_id="notion.export",
                    item_types=("page_export",),
                    method=Route.EXPORT,
                    acquisition_environments=("device_only",),
                    expected_artifacts=("export_file",),
                    rules=__import__("fakes").target_rules(("page_export",), ("export_file",)),
                ),
            ),
            selectors={},
            timings={**browser_profile('telegram', '13.1.1').timings,
                "default_timeout": 0,
                "poll_interval": 0,
                "application_start_settle": 1.0,
                "transition_settle": 0.0,
            },
            parameters={},
        ),
        system_ui=ProfileStore(
            Path(__file__).resolve().parents[1] / "profiles"
        ).load_system_ui("samsung"),
    )


def read_manifest(run_dir):
    return json.loads(
        (run_dir / "acquisition.json").read_text(encoding="utf-8")
    )


def test_keyboard_interrupt_preserves_partial_run_and_metadata(tmp_path, profiles):
    device = FakeEnvironmentDevice()

    def collect(context):
        attempt = begin_attempt(context, Route.MATERIALIZE)
        action = context.journal.record_action("retain", status="success", attempt_id=attempt)
        context.artifacts.write_bytes("partial.bin", b"partial", kind="structured_data", attempt_id=attempt, action=action)
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app, profiles.system_ui, Route.MATERIALIZE, {}, collect,
            run_id="interrupted",
        )
    manifest = read_manifest(tmp_path / "interrupted")
    assert manifest["outcome"]["status"] == "interrupted"
    assert manifest["attempts"][0]["procedure_status"] == "interrupted"
    assert manifest["attempts"][0]["acquisition_status"] == "partial"
    assert manifest["artifacts"][0]["relative_path"] == "artifacts/partial.bin"
    assert manifest["run"]["device_model"] == "Fake phone"
    assert manifest["run"]["android_version"] == "16"


def test_final_query_keyboard_interrupt_is_not_success(tmp_path, profiles):
    class Device(FakeEnvironmentDevice):
        interrupt_query = False

        def shell(self, command):
            if self.interrupt_query and command == "settings get global zen_mode":
                raise KeyboardInterrupt()
            return super().shell(command)

    device = Device()

    def collect(context):
        device.interrupt_query = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(KeyboardInterrupt):
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app, profiles.system_ui, Route.MATERIALIZE,
            {"notifications": "suppress_all"}, collect, run_id="final-query-interrupt",
        )
    manifest = read_manifest(tmp_path / "final-query-interrupt")
    assert manifest["outcome"]["status"] == "interrupted"
    assert manifest["run"]["final_state"]["observed"] is None


def test_bluetooth_cleanup_interrupt_preserves_run(tmp_path, profiles):
    class Device(FakeBluetoothDevice):
        def shell(self, command):
            if command == "cmd bluetooth_manager disable":
                raise KeyboardInterrupt()
            return super().shell(command)

    device = Device(paired_targets=("AURA Receiver",))
    with pytest.raises(KeyboardInterrupt):
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app, profiles.system_ui, Route.EXPORT,
            {"bluetooth": {"target_name": "AURA Receiver"}},
            lambda context: Outcome(OutcomeStatus.COMPLETE), run_id="cleanup-interrupt",
        )
    assert read_manifest(tmp_path / "cleanup-interrupt")["outcome"]["status"] == "interrupted"


def begin_attempt(context, route):
    if route is Route.EXPORT:
        target_id, item_type = "notion.export", "page_export"
    else:
        target_id, item_type = "notion.content", "page"
    item_id = context.register_item(target_id, item_type)
    return context.begin_attempt(item_id, route, "device_only")


def test_profile_version_mismatch_is_recorded_in_manifest_and_events(
    tmp_path,
    profiles,
):
    profile = replace(profiles.app, app_version="12.9.0")

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        profile,
        profiles.system_ui,
        Route.MATERIALIZE,
        {},
        lambda context: Outcome(OutcomeStatus.COMPLETE),
        run_id="run-version-mismatch",
        installed_app_version="12.7.2",
    )

    manifest = read_manifest(result.run_dir)
    assert manifest["run"]["installed_app_version"] == "12.7.2"
    assert manifest["run"]["profile_app_version"] == "12.9.0"
    assert manifest["run"]["app_profile_exact_match"] is False
    assert "app_profile_version" not in manifest["run"]
    assert "profile_version" not in manifest["run"]

    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event.get("action") for event in events[:2]] == [
        "app_profile_version_mismatch",
        None,
    ]
    assert events[0]["status"] == "warning"
    assert events[1]["event_type"] == "run_started"
    assert all(
        event["context"]["installed_app_version"] == "12.7.2"
        and event["context"]["profile_app_version"] == "12.9.0"
        and event["context"]["app_profile_exact_match"] is False
        for event in events
    )


@pytest.mark.parametrize("release", ["9", "", "Q", "10preview"])
def test_runtime_rejects_incompatible_android_before_acquisition(tmp_path, profiles, release):
    profile = replace(profiles.app, android_versions={"min": 10})
    device = SimpleNamespace(shell=lambda command: release)
    with pytest.raises(ValueError, match="Android"):
        AcquisitionRuntime(tmp_path, device).run(
            profile, profiles.system_ui, Route.MATERIALIZE, {},
            lambda context: pytest.fail("incompatible profile reached collector"),
            run_id="restricted",
        )
    assert not (tmp_path / "restricted").exists()


def test_runtime_records_android_release_and_canonical_environment(tmp_path, profiles):
    profile = replace(profiles.app, android_versions={"min": 10})
    device = SimpleNamespace(shell=lambda command: " 16\n")

    def collector(context):
        attempt = context.item_attempt("page", "notion.content", "page")
        context.finish_attempt(attempt, ProcedureStatus.COMPLETED, AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, device).run(
        profile, profiles.system_ui, Route.MATERIALIZE,
        {"acquisition_environment": "device_only"}, collector, run_id="restricted",
    )
    document = read_manifest(result.run_dir)
    assert document["run"]["android_version"] == "16"
    assert document["run"]["condition"]["acquisition_environment"] == "device_only"
    assert document["attempts"][0]["acquisition_environment"] == "device_only"
    assert "phase" not in document["attempts"][0]


@pytest.mark.parametrize("environment", ["device-only", "controlled-online", "local_first", "invalid", None, []])
def test_runtime_rejects_noncanonical_environment_before_acquisition(tmp_path, profiles, environment):
    with pytest.raises(ValueError, match="acquisition environment"):
        AcquisitionRuntime(tmp_path, FakeDevice()).run(
            profiles.app, profiles.system_ui, Route.MATERIALIZE,
            {"acquisition_environment": environment},
            lambda context: pytest.fail("invalid environment reached collector"),
            run_id="invalid-environment",
        )
    assert not (tmp_path / "invalid-environment").exists()


def test_run_context_start_app_waits_for_stable_profile_package(
    tmp_path, profiles, monkeypatch
):
    class SettlingDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.hierarchy_calls = 0

        def hierarchy(self):
            self.hierarchy_calls += 1
            generation = min(self.hierarchy_calls, 2)
            return (
                '<hierarchy><node package="notion.id" '
                f'text="generation-{generation}"/></hierarchy>'
            )

    device = SettlingDevice()
    waited = []
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def wait_until(predicate, **kwargs):
        return any(predicate() for _ in range(4))

    context = RunContext(
        run_dir=tmp_path,
        base_context={},
        device=device,
        journal=SimpleNamespace(),
        artifacts=SimpleNamespace(),
        ui=SimpleNamespace(
            wait_for=lambda selector, **kwargs: waited.append(dict(selector)) or True,
            wait_until=wait_until,
        ),
        app_profile=profiles.app,
        system_ui_profile=profiles.system_ui,
    )

    assert hasattr(context, "start_app")
    assert context.start_app() is True
    assert device.started == ["notion.id"]
    assert waited == [{"packageName": "notion.id"}]
    assert sleeps == [1.0]
    assert device.hierarchy_calls == 3


def test_run_context_start_app_retries_once_after_unstable_launch(
    tmp_path, profiles, monkeypatch
):
    class RetryingDevice(FakeDevice):
        @staticmethod
        def hierarchy():
            return '<hierarchy><node package="notion.id"/></hierarchy>'

    device = RetryingDevice()
    waits = {"until": 0}
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    def wait_until(predicate, **kwargs):
        waits["until"] += 1
        if waits["until"] == 1:
            return False
        return predicate() or predicate()

    context = RunContext(
        run_dir=tmp_path,
        base_context={},
        device=device,
        journal=SimpleNamespace(),
        artifacts=SimpleNamespace(),
        ui=SimpleNamespace(
            wait_for=lambda selector, **kwargs: True,
            wait_until=wait_until,
        ),
        app_profile=profiles.app,
        system_ui_profile=profiles.system_ui,
    )

    assert context.start_app() is True
    assert device.started == ["notion.id", "notion.id"]
    assert sleeps == [1.0, 1.0]


def test_suppression_remains_enabled_after_collector(tmp_path, profiles):
    device = FakeDndDevice()
    observed = []

    def collector(context):
        observed.append((device.dnd_enabled, device.hide_all))
        return Outcome(OutcomeStatus.COMPLETE)

    AcquisitionRuntime(tmp_path, device).run(
        profiles.app,
        profiles.system_ui,
        Route.MATERIALIZE,
        {"notifications": "suppress_all"},
        collector,
        run_id="run-suppressed",
    )

    assert observed == [(True, True)]
    assert device.dnd_enabled is True
    assert device.hide_all is True


def test_prepared_session_verifies_without_reapplying_environment(
    tmp_path, profiles
):
    device = FakeEnvironmentDevice(
        dnd_enabled=True,
        hide_all=True,
        airplane_enabled=True,
        wifi_enabled=True,
        wifi_connected=True,
    )
    observed = []

    result = AcquisitionRuntime(tmp_path, device).run(
        profiles.app,
        profiles.system_ui,
        Route.MATERIALIZE,
        {
            "notifications": "suppress_all",
            "airplane_mode": "enabled",
            "acquisition_environment": "controlled_online",
        },
        lambda context: observed.append(
            {
                "dnd": device.dnd_enabled,
                "hide_all": device.hide_all,
                "airplane": device.airplane_enabled,
                "wifi": device.wifi_enabled,
            }
        )
        or Outcome(OutcomeStatus.COMPLETE),
        run_id="run-prepared",
        prepared_session_id="session-1",
    )

    assert observed == [
        {
            "dnd": True,
            "hide_all": True,
            "airplane": True,
            "wifi": True,
        }
    ]
    assert read_manifest(result.run_dir)["run"]["device_session_id"] == "session-1"
    assert device.dnd_enabled is True
    assert device.hide_all is True
    assert device.airplane_enabled is True
    assert device.wifi_enabled is True
    assert device.hide_navigation_attempts == 1  # Fresh post-collector read on the same screen.


@pytest.mark.parametrize(
    ("device_kwargs", "error"),
    [
        ({"dnd_enabled": False}, "drifted"),
        ({"wifi_connected": False}, "drifted"),
        ({"wifi_enabled": False}, "drifted"),
    ],
)
def test_prepared_session_environment_failure_stops_before_collector(
    tmp_path, profiles, device_kwargs, error
):
    initial = {
        "dnd_enabled": True,
        "hide_all": True,
        "airplane_enabled": True,
        "wifi_enabled": True,
        **device_kwargs,
    }
    device = FakeEnvironmentDevice(**initial)
    called = False

    def collector(context):
        nonlocal called
        called = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(AcquisitionRunError, match=error) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {
                "notifications": "suppress_all",
                "airplane_mode": "enabled",
                "acquisition_environment": "controlled_online",
            },
            collector,
            run_id=f"run-prepared-{error.replace(' ', '-')}",
            prepared_session_id="session-1",
        )

    assert called is False
    assert device.wifi_enabled == initial["wifi_enabled"]
    assert read_manifest(raised.value.run_dir)["outcome"]["status"] == "failed"


@pytest.mark.parametrize("initial_enabled", [False, True])
def test_bluetooth_delivery_wraps_collector_and_restores_adapter(
    tmp_path,
    profiles,
    initial_enabled,
):
    device = FakeBluetoothDevice(
        enabled=initial_enabled,
        paired_targets=("Lab Receiver",),
    )
    observed = []

    def collector(context):
        observed.append(device.bluetooth_enabled)
        return Outcome(OutcomeStatus.COMPLETE)

    AcquisitionRuntime(tmp_path, device).run(
        profiles.app,
        profiles.system_ui,
        Route.EXPORT,
        {"bluetooth": {"target_name": "Lab Receiver"}},
        collector,
        run_id=f"run-bluetooth-{initial_enabled}",
    )

    assert observed == [True]
    assert device.bluetooth_enabled is initial_enabled


def test_unpaired_bluetooth_target_stops_before_collector_and_restores(
    tmp_path,
    profiles,
):
    device = FakeBluetoothDevice(enabled=False)
    called = False

    def collector(context):
        nonlocal called
        called = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(AcquisitionRunError) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.EXPORT,
            {"bluetooth": {"target_name": "Lab Receiver"}},
            collector,
            run_id="run-bluetooth-unpaired",
        )

    assert called is False
    assert device.bluetooth_enabled is False
    assert read_manifest(raised.value.run_dir)["outcome"]["action"] == (
        "system_ui_prepare_exception"
    )


@pytest.mark.parametrize("drift_during_bluetooth", [None, "wifi", "dnd", "airplane", "hide_all"])
def test_prepared_run_rechecks_environment_after_bluetooth(tmp_path, profiles, drift_during_bluetooth):
    class Device(FakeEnvironmentDevice):
        def __init__(self):
            super().__init__(dnd_enabled=True, hide_all=True, airplane_enabled=True, wifi_enabled=False)
            self.bluetooth_enabled = False
            self.refuse_shell_toggle = False
            self.paired_targets = ("Lab Receiver",)

        def shell(self, command):
            if command == "cmd bluetooth_manager enable":
                self.screen = "bluetooth"
            if command == "cmd bluetooth_manager enable" and drift_during_bluetooth:
                if drift_during_bluetooth == "wifi":
                    self.wifi_enabled = True
                else:
                    setattr(self, {"dnd": "dnd_enabled", "airplane": "airplane_enabled",
                                   "hide_all": "hide_all"}[drift_during_bluetooth], False)
            if "bluetooth" in command:
                return FakeBluetoothDevice.shell(self, command)
            return super().shell(command)

    device = Device()
    collected = []
    def collect(context):
        assert device.screen == "bluetooth", "post-Bluetooth verification reopened notification settings"
        collected.append(device.bluetooth_enabled)
        context.journal.record_action("test_collector", status="success")
        return Outcome(OutcomeStatus.COMPLETE)
    args = (profiles.app, profiles.system_ui, Route.EXPORT, {
        "notifications": "suppress_all", "airplane_mode": "enabled",
        "acquisition_environment": "device_only", "bluetooth": {"target_name": "Lab Receiver"},
    }, collect)
    runtime = AcquisitionRuntime(tmp_path, device)
    if drift_during_bluetooth in {"wifi", "dnd", "airplane"}:
        with pytest.raises(AcquisitionRunError, match="drifted"):
            runtime.run(*args, run_id="bluetooth-order", prepared_session_id="session-1")
        assert collected == []
    else:
        result = runtime.run(*args, run_id="bluetooth-order", prepared_session_id="session-1")
        assert collected == [True]
        actions = [json.loads(line).get("action") for line in (result.run_dir / "events.jsonl").read_text().splitlines()]
        assert [a for a in actions if a in {"system_ui_environment_verify", "system_ui_bluetooth_prepare", "test_collector"}] == [
            "system_ui_environment_verify", "system_ui_bluetooth_prepare", "system_ui_environment_verify", "test_collector",
        ]
        state = json.loads((result.run_dir / "acquisition.json").read_text())["run"]["verified_state"]
        assert state["observed"]["hide_all"] is True
        assert state["reused_fields"] == ["hide_all"]
        assert state["hide_all_observed_at"] < state["observed_at"]
    events = [json.loads(line) for line in (tmp_path / "bluetooth-order/events.jsonl").read_text().splitlines()]
    verified = [e for e in events if e.get("action") == "system_ui_environment_verify"]
    assert len(verified) == 2
    assert verified[1]["details"]["hide_all_source_action_id"] == verified[0]["action_id"]
    assert verified[1]["details"]["hide_all_reobserved"] is False
    prepared = next(i for i, e in enumerate(events) if e.get("action") == "system_ui_bluetooth_delivery_ready")
    recheck = events.index(verified[1])
    assert not any(e.get("action") in {"system_ui_dnd_navigation", "system_ui_hide_notifications_navigation"}
                   for e in events[prepared + 1:recheck])
    assert device.bluetooth_enabled is False


def test_complete_run_writes_terminal_manifest(tmp_path, profiles):
    def collector(context):
        assert context.system_ui_profile is profiles.system_ui
        assert context.app_profile is profiles.app
        attempt_id = begin_attempt(context, Route.EXPORT)
        action = context.journal.record_action(
            "export",
            status="success",
            attempt_id=attempt_id,
            context={"target": "workspace-1"},
        )
        context.artifacts.write_bytes(
            "export.zip",
            b"zip",
            kind="export",
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
        Route.EXPORT,
        {"network": "online"},
        collector,
        run_id="run-1",
    )

    manifest = read_manifest(result.run_dir)
    assert manifest["run"]["route"] == "export"
    assert manifest["run"]["condition"] == {"network": "online"}
    assert manifest["run"]["system_ui_profile"] == "samsung"
    assert "device_profile" not in manifest["run"]
    assert manifest["outcome"]["status"] == "complete"
    assert manifest["artifacts"][0]["action_id"] == "action-000001"
    assert manifest["artifacts"][0]["context"]["target"] == "workspace-1"


def test_partial_run_preserves_incomplete_action_context(tmp_path, profiles):
    def collector(context):
        attempt_id = begin_attempt(context, Route.MATERIALIZE)
        action = context.journal.record_action(
            "materialize_image",
            status="failed",
            attempt_id=attempt_id,
            context={"target": "image-7", "screen": "chat"},
        )
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.NOT_ACQUIRED,
            reason="artifact_not_available",
            action=action,
            details={"boundary": "download_action"},
        )
        return Outcome(OutcomeStatus.PARTIAL, "one image unresolved")

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        profiles.app,
        profiles.system_ui,
        Route.MATERIALIZE,
        {"cache": "warm"},
        collector,
        run_id="run-partial",
    )

    incomplete = read_manifest(result.run_dir)["outcomes"][0]
    assert incomplete["acquisition_status"] == "not_acquired"
    assert incomplete["action_id"] == "action-000001"
    assert incomplete["attempt_id"] == "attempt-000001"
    assert incomplete["details"]["boundary"] == "download_action"


def test_capture_observation_uses_same_action_linkage(tmp_path, profiles):
    def collector(context):
        attempt_id = begin_attempt(context, Route.EXPORT)
        action = context.journal.record_action(
            "open_settings",
            status="success",
            attempt_id=attempt_id,
            context={"screen": "settings"},
        )
        context.capture_observation(
            "settings", attempt_id=attempt_id, action=action
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
        Route.EXPORT,
        {},
        collector,
        run_id="run-observation",
    )

    records = read_manifest(result.run_dir)["artifacts"]
    assert [record["kind"] for record in records] == [
        "screen_image",
        "ui_hierarchy",
    ]
    assert {record["action_id"] for record in records} == {"action-000001"}
    assert {record["observation_id"] for record in records} == {
        "observation-000001"
    }


@pytest.mark.parametrize('permanent', [False, True])
def test_capture_recovery_journal_is_scoped_and_preserves_evidence_or_failure(tmp_path, profiles, permanent):
    import base64
    import hashlib
    from io import BytesIO
    from PIL import Image
    from aura.device import AndroidDevice
    from aura.journal import EventJournal

    buffer = BytesIO()
    Image.new('RGB', (32, 32), 'white').save(buffer, format='JPEG')
    source = buffer.getvalue()
    captures = []
    def capture(*args):
        captures.append(args)
        return base64.b64encode(source[:-10] if permanent or len(captures) == 1 else source).decode('ascii')
    device = AndroidDevice(SimpleNamespace(jsonrpc=SimpleNamespace(takeScreenshot=capture),
        dump_hierarchy=lambda **kw: '<hierarchy/>'), object(), serial=None)
    device.shell = FakeDevice().shell
    device.installed_packages = lambda: ()
    previous = EventJournal(tmp_path / 'outside.jsonl')
    device.capture_journal = previous

    def collect(context):
        attempt = begin_attempt(context, Route.EXPORT)
        action = context.journal.record_action('observe', status='success', attempt_id=attempt)
        context.capture_observation('screen', attempt_id=attempt, action=action)
        context.finish_attempt(attempt, ProcedureStatus.COMPLETED, AcquisitionStatus.ACQUIRED)
        return Outcome(OutcomeStatus.COMPLETE)

    runtime = AcquisitionRuntime(tmp_path, device)
    if permanent:
        with pytest.raises(AcquisitionRunError, match='truncated'):
            runtime.run(profiles.app, profiles.system_ui, Route.EXPORT, {}, collect, run_id='capture')
    else:
        runtime.run(profiles.app, profiles.system_ui, Route.EXPORT, {}, collect, run_id='capture')
    assert device.capture_journal is previous
    assert previous.path.read_text() == ''
    document = read_manifest(tmp_path / 'capture')
    events = [json.loads(s) for s in (tmp_path / 'capture/events.jsonl').read_text().splitlines()]
    capture_events = [e for e in events if e['event_type'] == 'screen_capture']
    assert [e['status'] for e in capture_events] == (['retrying', 'retrying', 'failed'] if permanent else ['retrying', 'recovered'])
    assert all(e['action_id'] == 'action-000001' for e in capture_events)
    assert document['outcome']['status'] == ('failed' if permanent else 'complete')
    assert len(document['artifacts']) == (0 if permanent else 2)
    for record in document['artifacts']:
        raw = (tmp_path / 'capture' / record['relative_path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record['sha256']
        assert record['action_id'] == 'action-000001'


def test_unexpected_exception_still_writes_failed_manifest(tmp_path, profiles):
    def collector(context):
        raise RuntimeError("boom")

    runtime = AcquisitionRuntime(tmp_path, FakeDevice())

    with pytest.raises(AcquisitionRunError) as raised:
        runtime.run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {},
            collector,
            run_id="run-failed",
        )

    manifest = read_manifest(raised.value.run_dir)
    assert manifest["outcome"]["status"] == "failed"
    assert manifest["outcome"]["action"] == "collector_exception"
    assert manifest["outcomes"] == []


def test_rejects_unsafe_run_identifier_before_creating_directory(tmp_path, profiles):
    runtime = AcquisitionRuntime(tmp_path, FakeDevice())

    with pytest.raises(ValueError, match="run_id"):
        runtime.run(
            profiles.app,
            profiles.system_ui,
            Route.EXPORT,
            {},
            lambda context: Outcome(OutcomeStatus.COMPLETE),
            run_id="../escape",
        )

    assert list(tmp_path.iterdir()) == []


def test_invalid_collector_outcome_still_writes_failed_manifest(tmp_path, profiles):
    runtime = AcquisitionRuntime(tmp_path, FakeDevice())

    with pytest.raises(AcquisitionRunError) as raised:
        runtime.run(
            profiles.app,
            profiles.system_ui,
            Route.EXPORT,
            {},
            lambda context: Outcome("not-a-status"),
            run_id="run-invalid-outcome",
        )

    assert read_manifest(raised.value.run_dir)["outcome"]["status"] == "failed"


def test_suppression_failure_does_not_invoke_collector(tmp_path, profiles):
    device = FakeDndDevice(refuse_hide_all=True)
    called = False

    def collector(context):
        nonlocal called
        called = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(AcquisitionRunError) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {"notifications": "suppress_all"},
            collector,
            run_id="run-prepare-failed",
        )

    assert called is False
    assert read_manifest(raised.value.run_dir)["outcome"]["action"] == (
        "system_ui_prepare_exception"
    )


def test_unreadable_zen_mode_does_not_invoke_collector(tmp_path, profiles):
    device = FakeDndDevice(zen_mode_output="null")
    called = False

    def collector(context):
        nonlocal called
        called = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(AcquisitionRunError) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {"notifications": "suppress_all"},
            collector,
            run_id="run-unreadable-dnd",
        )

    assert called is False
    assert read_manifest(raised.value.run_dir)["outcome"]["action"] == (
        "system_ui_prepare_exception"
    )


def test_collector_exception_keeps_suppression(tmp_path, profiles):
    device = FakeDndDevice()

    def collector(context):
        attempt_id = begin_attempt(context, Route.MATERIALIZE)
        action = context.journal.record_action(
            "materialize",
            status="success",
            attempt_id=attempt_id,
        )
        context.artifacts.write_bytes(
            "materialized.bin",
            b"retained",
            kind="materialized_data",
            attempt_id=attempt_id,
            action=action,
        )
        raise RuntimeError("boom")

    with pytest.raises(AcquisitionRunError) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {"notifications": "suppress_all"},
            collector,
            run_id="run-collector-failed",
        )

    assert device.dnd_enabled is True
    assert device.hide_all is True
    assert read_manifest(raised.value.run_dir)["artifacts"][0][
        "relative_path"
    ] == "artifacts/materialized.bin"


def test_no_restore_preserves_success_and_collected_artifact(tmp_path, profiles):
    device = FakeDndDevice(fail_restore=True)

    def collector(context):
        attempt_id = begin_attempt(context, Route.EXPORT)
        action = context.journal.record_action(
            "export",
            status="success",
            attempt_id=attempt_id,
        )
        context.artifacts.write_bytes(
            "export.zip",
            b"zip",
            kind="export",
            attempt_id=attempt_id,
            action=action,
        )
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.ACQUIRED,
        )
        return Outcome(OutcomeStatus.COMPLETE)

    result = AcquisitionRuntime(tmp_path, device).run(
        profiles.app,
        profiles.system_ui,
        Route.EXPORT,
        {"notifications": "suppress_all"},
        collector,
        run_id="run-no-restore",
    )

    manifest = read_manifest(result.run_dir)
    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest["outcome"]["status"] == "complete"
    assert [artifact["relative_path"] for artifact in manifest["artifacts"]] == [
        "artifacts/export.zip"
    ]
    assert not any(
        event.get("action") == "system_ui_restore_exception"
        and event["status"] == "failed"
        for event in events
    )
    assert device.dnd_enabled is True
    assert device.hide_all is True


def test_collector_error_does_not_attempt_restore(
    tmp_path,
    profiles,
):
    device = FakeDndDevice(fail_restore=True)

    with pytest.raises(AcquisitionRunError) as raised:
        AcquisitionRuntime(tmp_path, device).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {"notifications": "suppress_all"},
            lambda context: (_ for _ in ()).throw(RuntimeError("boom")),
            run_id="run-both-failed",
        )

    manifest = read_manifest(raised.value.run_dir)
    events = [
        json.loads(line)
        for line in (raised.value.run_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest["outcome"]["action"] == "collector_exception"
    assert not any(
        event.get("action") == "system_ui_restore_exception"
        for event in events
    )


def test_profile_without_required_extension_fails_before_collector(
    tmp_path,
    profiles,
):
    unsupported = SystemUIProfile(
        name="other",
        description="No verified suppression extension.",
        manufacturer_match=("other",),
        operations={"dnd": {"strategy": "plain_dnd"}},
    )
    called = False

    def collector(context):
        nonlocal called
        called = True
        return Outcome(OutcomeStatus.COMPLETE)

    with pytest.raises(AcquisitionRunError):
        AcquisitionRuntime(tmp_path, FakeDndDevice()).run(
            profiles.app,
            unsupported,
            Route.MATERIALIZE,
            {"notifications": "suppress_all"},
            collector,
            run_id="run-unsupported",
        )

    assert called is False


def test_rejects_unknown_notification_condition_before_run_directory(
    tmp_path,
    profiles,
):
    with pytest.raises(ValueError, match="condition.notifications"):
        AcquisitionRuntime(tmp_path, FakeDndDevice()).run(
            profiles.app,
            profiles.system_ui,
            Route.MATERIALIZE,
            {"notifications": "dnd_only"},
            lambda context: Outcome(OutcomeStatus.COMPLETE),
            run_id="run-invalid-condition",
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bluetooth",
    [None, {}, {"target_name": ""}, {"target_name": 7}],
)
def test_rejects_invalid_bluetooth_condition_before_run_directory(
    tmp_path,
    profiles,
    bluetooth,
):
    with pytest.raises(ValueError, match="condition.bluetooth"):
        AcquisitionRuntime(tmp_path, FakeBluetoothDevice()).run(
            profiles.app,
            profiles.system_ui,
            Route.EXPORT,
            {"bluetooth": bluetooth},
            lambda context: Outcome(OutcomeStatus.COMPLETE),
            run_id="run-invalid-bluetooth",
        )

    assert list(tmp_path.iterdir()) == []


def test_unbound_partial_result_records_its_own_decision_action(tmp_path, profiles):
    def collector(context):
        attempt = begin_attempt(context, Route.MATERIALIZE)
        observation_action = context.linked_action('observe_page', attempt)
        context.capture_observation('page', attempt_id=attempt, action=observation_action)
        context.journal.record_action('unrelated_navigation', status='success')
        context.interrupt_open_attempts('content_unavailable')
        return Outcome(OutcomeStatus.PARTIAL, 'content_unavailable')

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        profiles.app, profiles.system_ui, Route.MATERIALIZE, {}, collector,
        run_id='result-decision-action')
    manifest = read_manifest(result.run_dir)
    outcome = manifest['outcomes'][0]
    assert outcome['action_id'] is not None
    events = [json.loads(line) for line in (result.run_dir / 'events.jsonl').read_text().splitlines()]
    action = next(a for a in events if a.get('action_id') == outcome['action_id'] and a['event_type'] == 'action')
    assert action['action'] == 'record_acquisition_outcome'
    assert action['attempt_id'] == outcome['attempt_id']
    assert action['details']['reason'] == 'content_unavailable'
    assert 'source_action_id' not in action['context']


@pytest.mark.parametrize("package_ready, stable_at, expected, elapsed", [
    (35, 35, True, 36), (0, 35, True, 36), (0, 0, True, 1),
    (None, None, False, 120),
])
def test_start_app_waits_for_slow_device_without_fixed_delay(
    tmp_path, profiles, monkeypatch, package_ready, stable_at, expected, elapsed
):
    from aura.ui import UiRuntime
    now = [0.0]
    def sleep(seconds):
        now[0] += seconds
    class SlowDevice(FakeDevice):
        def exists(self, selector):
            return package_ready is not None and now[0] >= package_ready
        def hierarchy(self):
            generation = min(now[0], stable_at)
            return f'<hierarchy><node package="notion.id" text="{float(generation)}"/></hierarchy>'
    device = SlowDevice()
    profiles.app.timings["application_start_settle"] = 0
    monkeypatch.setattr(time, "sleep", sleep)
    ui = UiRuntime(device, SimpleNamespace(), default_timeout=5, poll_interval=1,
                   monotonic=lambda: now[0], sleep=sleep)
    context = RunContext(run_dir=tmp_path, base_context={}, device=device,
        journal=SimpleNamespace(), artifacts=SimpleNamespace(), ui=ui,
        app_profile=profiles.app, system_ui_profile=profiles.system_ui)
    assert context.start_app() is expected
    assert now[0] == elapsed
    assert len(device.started) == (1 if expected else 2)
    assert ui.default_timeout == 5
