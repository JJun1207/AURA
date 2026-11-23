from fakes import browser_profile
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from aura import cli
from aura.device import AndroidDevice, InstalledPackage
from aura.models import Outcome, OutcomeStatus, Route
from aura.profiles import (
    AcquisitionTarget,
    AppProfile,
    ProfileStore,
    SystemUIProfile,
)
from aura.runtime import AcquisitionRunError
from aura.session import DeviceSession, EnvironmentSnapshot, SessionStore
from aura.system_ui import DndSnapshot
from fakes import FakeEnvironmentDevice, target_rules, synthetic_authorization


class ShellDevice:
    def __init__(self, responses):
        self.responses = {"getprop ro.build.version.release": "16\n", **responses}

    def shell(self, command):
        return self.responses[command]


@pytest.mark.parametrize("exit_kind", ["complete", "partial", "offline_only", "interrupt", "online_interrupt", "failure", "package_error", "disconnect", "transition_failure", "pending_download"])
@pytest.mark.parametrize("initial_wifi", [False, True])
def test_real_session_restores_notifications_and_wifi(tmp_path, monkeypatch, exit_kind, initial_wifi):
    clock = [100.0]
    monkeypatch.setattr(cli, "perf_counter", lambda: clock[0], raising=False)
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    device = FakeEnvironmentDevice(wifi_enabled=initial_wifi, wifi_connected=exit_kind != "transition_failure")
    resolutions = tuple(
        cli.ProfileResolution(app, package, "1.0", "exact", _materialize_profile(app, package))
        for app, package in (("whatsapp", "com.whatsapp"), ("telegram", "org.telegram.messenger"))
    )
    disconnected = False

    def connected(**kwargs):
        if disconnected:
            raise cli.CliError("device disconnected")
        return "SERIAL", device

    monkeypatch.setattr(cli, "_connected_device", connected)
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli, "_match_installed_apps", lambda *args: resolutions)
    monkeypatch.setattr(cli, "_resolve_app_profile", lambda *args: next(r for r in resolutions if r.app_id == args[-1]))
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    calls = []

    def collect(context):
        nonlocal disconnected
        clock[0] += 3.0
        environment = context.base_context["condition"]["acquisition_environment"]
        calls.append((context.app_profile.app_id, environment))
        if exit_kind == "pending_download":
            pending = context.base_context["condition"].get("pending_downloads", [])
            assert len(pending) == (1 if len(calls) == 3 else 0)
            if pending:
                assert pending[0]["displayed_name"] == "report.txt"
                assert pending[0]["run_id"].endswith("whatsapp-materialize-device-only")
        assert device.airplane_enabled and device.dnd_enabled
        assert device.wifi_enabled == (environment == "controlled_online")
        attempt = context.item_attempt("record", context.app_profile.targets[0].target_id, "record")
        action = context.journal.record_action("retain", status="success", attempt_id=attempt)
        context.artifacts.write_bytes("record.bin", b"evidence", kind="data", attempt_id=attempt, action=action)
        if len(calls) == 2 and exit_kind in {"interrupt", "failure"}:
            raise KeyboardInterrupt() if exit_kind == "interrupt" else RuntimeError("collector failed")
        if len(calls) == 4 and exit_kind == "online_interrupt":
            raise KeyboardInterrupt()
        context.complete_open_attempts("acquired")
        if len(calls) == 4 and exit_kind == "disconnect":
            disconnected = True
            monkeypatch.setattr(device, "shell", lambda command: (_ for _ in ()).throw(RuntimeError("device disconnected")))
        if exit_kind == "pending_download" and len(calls) == 1:
            return Outcome(OutcomeStatus.PARTIAL, "download_timeout", details={
                "pending_downloads": [{"displayed_name": "report.txt", "attempt_id": attempt}]})
        return (Outcome(OutcomeStatus.PARTIAL, reason="notion_page_close_failed", action=action,
                        details={"failure": "page did not return to its parent"})
                if exit_kind == "partial" and len(calls) == 1 else Outcome(OutcomeStatus.COMPLETE))

    monkeypatch.setitem(cli.COLLECTORS, "whatsapp", collect)
    monkeypatch.setitem(cli.COLLECTORS, "telegram", collect)
    original_package = cli.package_run

    def package(path):
        clock[0] += 2.0
        if exit_kind == "package_error" and len(calls) == 2:
            raise cli.PackageError("package failed")
        return original_package(path)

    monkeypatch.setattr(cli, "package_run", package)
    authority = synthetic_authorization([r.profile for r in resolutions], remote="denied" if exit_kind == "offline_only" else "allowed")
    payload, code = cli._execute_acquire(("whatsapp", "telegram"), authority)
    document = json.loads(Path(payload["session_record"]).read_text())
    expected = [("whatsapp", "device_only"), ("telegram", "device_only")]
    if exit_kind in {"complete", "partial", "disconnect", "online_interrupt", "pending_download"}:
        expected += [("whatsapp", "controlled_online"), ("telegram", "controlled_online")]
    assert calls == expected
    if exit_kind in {"partial", "pending_download"}:
        assert payload["status"] == document["status"] == "partial"
        assert payload["execution_summary"] == {"partial": 1, "complete": 3}
        assert payload["executions"][0]["reason"] == ("download_timeout" if exit_kind == "pending_download" else "notion_page_close_failed")
        assert payload["executions"][0]["acquisition_environment"] == "device_only"
        summary = payload["executions"][0]["acquisition_summary"]
        assert summary == document["executions"][0]["acquisition_summary"]
        # This fixture writes kind=data, which does not satisfy structured_data.
        assert summary["item_counts"] == {"not_acquired": 1}
        assert summary["issues"][0]["target_id"] == resolutions[0].profile.targets[0].target_id
        if exit_kind == "partial":
            assert summary["run_issue"]["details"]["failure"] == "page did not return to its parent"
            assert summary["run_issue"]["action"] == "retain"
        assert Path(payload["executions"][0]["acquisition_record"]).is_file()
    assert code == (0 if exit_kind in {"complete", "offline_only"} else 1)
    if exit_kind == "offline_only":
        assert [row["status"] for row in document["execution_plan"]] == ["complete", "complete", "excluded", "excluded"]
        assert all(row["acquisition_environment"] == "device_only" for row in document["environment_transitions"])
        assert all(row["exclusion_reason"] == "remote_acquisition_denied" for row in document["execution_plan"][2:])
    assert document["device_model"] == "Fake phone"
    assert document["android_version"] == "16"
    assert document["preparation"]["status"] == "verified"
    assert document["preparation"]["observed"]["wifi_enabled"] is False
    assert document["preparation"]["observed_at"]
    assert "restoration" not in document and "restored_at" not in document
    assert device.airplane_enabled
    assert device.dnd_enabled == (exit_kind == "disconnect")
    # The disconnect double fails shell reads; its UI remains available.
    assert device.hide_all is False
    assert document["notification_restoration"]["status"] == ("failed" if exit_kind == "disconnect" else "restored")
    assert device.wifi_enabled == (True if exit_kind == "disconnect" else initial_wifi)
    assert document["wifi_restoration"]["status"] == ("failed" if exit_kind == "disconnect" else "restored")
    assert document["wifi_restoration"]["target"] == {"wifi_enabled": initial_wifi}
    assert payload["wifi_restoration"] == document["wifi_restoration"]
    assert document["executions"][0]["archive_path"]
    if exit_kind == "package_error":
        assert payload["executions"][-1]["error"] == "package failed"
        assert payload["executions"][-1]["acquisition_summary"]["item_counts"] == {"not_acquired": 1}
    for execution in document["executions"]:
        assert execution["elapsed_seconds"] == 5.0
        assert execution["timing_clock"] == "perf_counter"
        assert execution["timing_scope"] == "execution_including_packaging"
    assert document["preparation"]["elapsed_seconds"] == 0.0
    if exit_kind == "complete":
        # Initial snapshot, after enabling DND, and after restoring DND.
        assert device.hide_navigation_attempts == 3
        for transition in document["environment_transitions"]:
            if transition["status"] != "completed":
                continue
            last_run = next(
                row for row in reversed(document["executions"])
                if row["acquisition_environment"] == transition["acquisition_environment"]
            )
            with ZipFile(last_run["archive_path"]) as package:
                acquisition = json.loads(package.read("acquisition.json"))
            assert transition["state"] == acquisition["run"]["final_state"]
    for transition in document["environment_transitions"]:
        if transition["status"] == "started":
            assert transition["elapsed_seconds"] == 0.0
    if exit_kind == "disconnect":
        assert document["final_state"]["status"] == "unavailable"
        assert document["final_state"]["observed"] is None
        assert document["final_state"]["last_confirmed"]["observed"]["wifi_enabled"] is True
        assert "disconnected" in document["final_state"]["error"]
    else:
        assert document["final_state"]["observed"]["wifi_enabled"] == device.wifi_enabled
    for transition in document["environment_transitions"]:
        assert "conditions" not in transition
        assert transition["state"]["observed_at"] or transition["state"]["status"] == "unavailable"
    if exit_kind in {"interrupt", "online_interrupt"}:
        assert document["status"] == "interrupted"
        assert document["executions"][-1]["status"] == "interrupted"
        assert document["executions"][-1]["archive_path"]
    if exit_kind == "transition_failure":
        assert document["environment_transitions"][-1]["status"] == "failed"
        assert document["environment_transitions"][-1]["acquisition_environment"] == "controlled_online"
        assert document["environment_transitions"][-1]["state"]["observed"]["wifi_enabled"] is True
        assert "not connected" in document["environment_transitions"][-1]["error"]


@pytest.mark.parametrize("failure", ["wifi", "dnd", "verification", "interrupt"])
def test_real_prepare_failure_restores_initial_wifi_and_notifications(tmp_path, monkeypatch, failure):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)

    class Device(FakeEnvironmentDevice):
        def shell(self, command):
            if failure == "wifi" and command in {
                "svc wifi disable", "cmd wifi set-wifi-enabled disabled", "settings put global wifi_on 0"
            }:
                raise RuntimeError("Wi-Fi setting failed")
            return super().shell(command)

    device = Device(wifi_enabled=True, refuse_hide_all=failure == "dnd")
    monkeypatch.setattr(cli, "_connected_device", lambda **kwargs: ("SERIAL", device))
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    if failure in {"verification", "interrupt"}:
        def verify(self):
            raise KeyboardInterrupt() if failure == "interrupt" else RuntimeError("verification failed")
        monkeypatch.setattr(cli.SystemUIRuntime, "verify_prepared_environment", verify)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else cli.CliError):
        cli._execute_prepare()
    records = list((tmp_path / "runs" / "sessions").glob("*/session.json"))
    assert len(records) == 1
    document = json.loads(records[0].read_text())
    assert document["status"] == ("interrupted" if failure == "interrupt" else "prepare_failed")
    assert document["preparation"]["status"] == "failed"
    assert document["error"]
    assert document["final_state"]["observed"]["airplane_enabled"] is True
    assert device.airplane_enabled is True
    assert device.wifi_enabled is True
    assert device.dnd_enabled is False
    assert device.hide_all is False
    assert document["notification_restoration"]["status"] == "restored"
    assert document["wifi_restoration"]["status"] == "restored"
    assert not (tmp_path / "runs" / "session.json").exists()


@pytest.mark.parametrize("failed_command", ["getprop ro.product.model", "getprop ro.build.version.release", "settings get global zen_mode"])
def test_prepare_initial_query_failure_records_reason_without_inventing_state(tmp_path, monkeypatch, failed_command):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)

    class Device(FakeEnvironmentDevice):
        def shell(self, command):
            if command == failed_command:
                raise RuntimeError("query failed: " + command)
            return super().shell(command)

    device = Device(wifi_enabled=True)
    monkeypatch.setattr(cli, "_connected_device", lambda **kwargs: ("SERIAL", device))
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    with pytest.raises(cli.CliError, match="query failed"):
        cli._execute_prepare()
    events = [json.loads(line) for path in (tmp_path / "runs" / "sessions").glob("*.events.jsonl") for line in path.read_text().splitlines()]
    assert any(event.get("action") == "device_initial_query_exception" and failed_command in event["details"]["error"] for event in events)
    assert device.wifi_enabled and not device.airplane_enabled and not device.dnd_enabled
    assert not (tmp_path / "runs" / "session.json").exists()


def test_finalize_query_interrupt_retains_interrupted_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    store.create(_session())
    monkeypatch.setattr(cli, "_connected_device", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    payload, code = cli._execute_finalize()
    document = json.loads(Path(payload["session_record"]).read_text())
    assert code == 1
    assert document["status"] == "interrupted"
    assert document["interruption"]["reason"] == "KeyboardInterrupt"


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, RuntimeError])
@pytest.mark.parametrize("already_interrupted", [False, True])
def test_finalize_automation_query_failure_archives_evidence(
    tmp_path, monkeypatch, error_type, already_interrupted
):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    archive = tmp_path / "acquisition.zip"
    with ZipFile(archive, "w") as package:
        package.writestr("evidence.txt", "already acquired")
    original_archive = archive.read_bytes()
    execution = {"run_id": "run-1", "status": "complete", "archive_path": str(archive),
                 "sha256": hashlib.sha256(original_archive).hexdigest()}
    interruption = {"at": "2026-07-31T00:00:02+00:00", "reason": "collector interrupted"} if already_interrupted else None
    store.create(_session().with_status(
        "interrupted" if already_interrupted else "acquiring",
        executions=(execution,), interruption=interruption,
    ))

    class Device(FakeEnvironmentDevice):
        def automation_snapshot(self):
            raise error_type("final automation query failed")

    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    try:
        payload, code = cli._execute_finalize(("SERIAL", Device(wifi_enabled=True)))
    except KeyboardInterrupt:
        pytest.fail("final automation query interruption escaped before session archival")

    session_record = Path(payload["session_record"])
    document = json.loads(session_record.read_text())
    assert not store.active_path.exists()
    assert Path(payload["session_hash"]).read_text() == hashlib.sha256(session_record.read_bytes()).hexdigest() + "  session.json\n"
    assert archive.read_bytes() == original_archive
    assert document["executions"] == [execution]
    assert document["final_state"]["status"] == "observed"
    assert document["final_state"]["observed"]["wifi_enabled"] is True
    assert document["last_confirmed_state"] == document["final_state"]
    final_automation = document["automation"]["final"]
    assert final_automation["status"] == "unavailable"
    assert final_automation["error_type"] == error_type.__name__
    assert "final automation query failed" in final_automation["error"]
    assert final_automation["queried_at"]
    assert final_automation.get("observations") is None
    assert final_automation.get("observed_at") is None
    expected_status = "interrupted" if already_interrupted or error_type is KeyboardInterrupt else "complete"
    assert payload["status"] == document["status"] == expected_status
    assert code == (1 if expected_status == "interrupted" else 0)
    if already_interrupted:
        assert document["interruption"] == interruption
    elif error_type is KeyboardInterrupt:
        assert "final automation query failed" in document["interruption"]["reason"]
        assert document["interruption"]["at"] == final_automation["queried_at"]
    else:
        assert document["interruption"] is None


@pytest.mark.parametrize("query", ["environment", "automation"])
def test_prepare_error_then_final_query_interrupt_preserves_both_causes(tmp_path, monkeypatch, query):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)

    class Device(FakeEnvironmentDevice):
        preparation_failed = False

        def shell(self, command):
            if command == "svc wifi disable":
                self.preparation_failed = True
                raise RuntimeError("Wi-Fi preparation failed")
            if query == "environment" and self.preparation_failed and command == "settings get global zen_mode":
                raise KeyboardInterrupt()
            return super().shell(command)

    device = Device(wifi_enabled=True)
    if query == "automation":
        def interrupt_automation():
            raise KeyboardInterrupt("final automation query interrupted")
        monkeypatch.setattr(device, "automation_snapshot", interrupt_automation, raising=False)
    monkeypatch.setattr(cli, "_connected_device", lambda **kwargs: ("SERIAL", device))
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    with pytest.raises(KeyboardInterrupt) as raised:
        cli._execute_prepare()
    document = json.loads(next((tmp_path / "runs" / "sessions").glob("*/session.json")).read_text())
    assert document["status"] == "interrupted"
    assert document["error"] == document["preparation"]["error"] == "Wi-Fi preparation failed"
    assert str(raised.value.__cause__) == "Wi-Fi preparation failed"
    if query == "environment":
        assert document["interruption"]["reason"] == "KeyboardInterrupt"
        assert document["final_state"]["error_type"] == "KeyboardInterrupt"
        assert document["final_state"]["observed"] is None
        assert document["final_state"]["last_confirmed"]["observed"]["airplane_enabled"] is False
    else:
        assert "final automation query interrupted" in document["interruption"]["reason"]
        assert document["automation"]["final"]["status"] == "unavailable"
        assert document["automation"]["final"]["error_type"] == "KeyboardInterrupt"
        assert document["final_state"]["status"] == "observed"
        assert document["final_state"]["observed"]["airplane_enabled"] is True
    record = next((tmp_path / "runs" / "sessions").glob("*/session.json"))
    assert record.with_suffix(".sha256").read_text() == hashlib.sha256(record.read_bytes()).hexdigest() + "  session.json\n"
    assert device.airplane_enabled and device.wifi_enabled
    assert not (tmp_path / "runs" / "session.json").exists()


def _write_app_profile(root, version):
    app_dir = root / "apps" / "whatsapp"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / f"{version}.json").write_text(
        json.dumps({
            "app_id": "whatsapp",
            "package_name": "com.whatsapp",
            "app_version": version,
            "acquisition_methods": ["export", "materialize"],
            "targets": [
                {
                    "target_id": "whatsapp.conversations",
                    "item_types": ["conversation", "message"],
                    "method": "materialize",
                    "acquisition_environments": ["device_only", "controlled_online"],
                    "expected_artifacts": ["structured_data"],
                    "rules": target_rules(("conversation", "message"), boundaries=("chat_history_exhausted",)),
                },
                {
                    "target_id": "whatsapp.chat_export",
                    "item_types": ["conversation_export"],
                    "method": "export",
                    "acquisition_environments": ["device_only"],
                    "expected_artifacts": ["export_file"],
                    "rules": target_rules(("conversation_export",), ("export_file",), ("export_target_identified",)),
                },
            ],
            "selectors": {"chat-list": {"resourceId": "android:id/list"}},
            "timings": browser_profile('whatsapp', '2.26.43.74').timings,
        }),
        encoding="utf-8",
    )


def _write_profiles(root):
    _write_app_profile(root, "2.26.27.85")
    system_ui_dir = root / "system_ui"
    system_ui_dir.mkdir(parents=True)
    (system_ui_dir / "samsung.json").write_text(
        json.dumps({
            "name": "samsung",
            "description": "Samsung",
            "manufacturer_match": ["samsung"],
            "operations": {"dnd": {"strategy": "switch"}},
        }),
        encoding="utf-8",
    )


def _session(serial="SERIAL"):
    return DeviceSession(
        session_id="session-1",
        serial=serial,
        system_ui_profile="samsung",
        prepared_at="2026-07-31T00:00:00+00:00",
        status="prepared",
        initial=EnvironmentSnapshot(False, False, False, True),
        applied_at="2026-07-31T00:00:01+00:00",
        authorization=synthetic_authorization([_app_profile()]),
    )


def _app_profile():
    return AppProfile(
        app_id="whatsapp",
        package_name="com.whatsapp",
        app_version="2.26.27.85",
        routes=(Route.MATERIALIZE, Route.EXPORT),
        targets=(
            AcquisitionTarget(
                target_id="whatsapp.conversations",
                item_types=("conversation", "message"),
                method=Route.MATERIALIZE,
                acquisition_environments=("device_only", "controlled_online"),
                expected_artifacts=("structured_data",),
                rules=target_rules(("conversation", "message"), boundaries=("chat_history_exhausted",)),
            ),
        ),
        selectors={},
        timings={**dict.fromkeys(('default_timeout', 'poll_interval', 'transition_settle', 'application_start_settle'), 0), },
        parameters={},
    )


def _system_profile():
    return SystemUIProfile(
        name="samsung",
        description="Samsung",
        manufacturer_match=("samsung",),
        operations={"dnd": {"strategy": "switch"}},
    )


def _materialize_profile(app_id, package_name):
    return AppProfile(
        app_id=app_id,
        package_name=package_name,
        app_version="1.0",
        routes=(Route.MATERIALIZE,),
        targets=(
            AcquisitionTarget(
                target_id=next(iter(cli.APPS[app_id]["targets"])),
                item_types=("record",),
                method=Route.MATERIALIZE,
                acquisition_environments=("device_only", "controlled_online"),
                expected_artifacts=("structured_data",),
                rules=target_rules(("record",), boundaries=tuple(next(iter(cli.APPS[app_id]["targets"].values()))["boundaries"])),
            ),
        ),
        selectors={},
        timings={**dict.fromkeys(('default_timeout', 'poll_interval', 'transition_settle', 'application_start_settle'), 0), },
        parameters={},
    )


def test_parser_accepts_inspect_and_multi_app_acquire_commands():
    inspect = cli._parser().parse_args(["inspect"])
    acquire = cli._parser().parse_args(
        ["acquire", "whatsapp", "telegram", "--authorization", "authority.json"]
    )

    assert inspect.command == "inspect"
    assert (acquire.command, acquire.apps) == (
        "acquire",
        ["whatsapp", "telegram"],
    )


def test_inspect_reports_profiles_without_creating_session(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    _write_profiles(tmp_path / "profiles")
    device = SimpleNamespace(shell=lambda command: "16\n", installed_packages=lambda: (
        InstalledPackage("com.whatsapp", "2.26.27.85"),
        InstalledPackage("example.unsupported", "1.0"),
    ))
    monkeypatch.setattr(
        cli, "_connected_device", lambda **kwargs: ("SERIAL", device)
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    payload, exit_code = cli._execute_inspect()

    assert exit_code == 0
    assert payload == {
        "status": "inspected",
        "serial": "SERIAL",
        "system_ui_profile": "samsung",
        "applications": [
            {
                "app_id": "whatsapp",
                "package_name": "com.whatsapp",
                "version_name": "2.26.27.85",
                "profile_status": "exact",
                "profile_app_version": "2.26.27.85",
            },
            {
                "app_id": None,
                "package_name": "example.unsupported",
                "version_name": "1.0",
                "profile_status": "no_profile",
                "profile_app_version": None,
            },
        ],
    }
    assert not (tmp_path / "runs" / "session.json").exists()


def test_execution_plan_contains_only_selected_profiled_apps(tmp_path):
    _write_profiles(tmp_path)
    whatsapp = cli.ProfileResolution(
        "whatsapp",
        "com.whatsapp",
        "2.26.27.85",
        "exact",
        ProfileStore(tmp_path).load_app(
            "whatsapp", "2.26.27.85"
        ),
    )
    unsupported = cli.ProfileResolution(
        "telegram",
        "org.telegram.messenger",
        "1.0",
        "no_profile",
        None,
    )

    plan = cli._build_execution_plan(
        "session-1",
        ("whatsapp", "telegram"),
        (whatsapp, unsupported),
    )

    assert [
        (entry["order"], entry["app_id"], entry["method"], entry["acquisition_environment"])
        for entry in plan
    ] == [
        (1, "whatsapp", "materialize", "device_only"),
        (2, "whatsapp", "export", "device_only"),
        (3, "whatsapp", "materialize", "controlled_online"),
    ]
    assert all(entry["status"] == "planned" for entry in plan)
    assert all(entry["profile_status"] == "exact" for entry in plan)
    assert plan[0]["run_id"] == "session-1-001-whatsapp-materialize-device-only"
    assert plan[-1]["run_id"] == "session-1-003-whatsapp-materialize-controlled-online"
    assert all("phase" not in entry and "profile_version" not in entry for entry in plan)


def test_whatsapp_export_runs_last_in_both_environments():
    profiles = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    resolutions = tuple(
        cli.ProfileResolution(app, package, version, "exact", profiles.load_app(app, version))
        for app, package, version in (
            ("whatsapp", "com.whatsapp", "2.26.43.74"),
            ("telegram", "org.telegram.messenger", "13.1.1"),
            ("notesnook", "com.streetwriters.notesnook", "3.4.15"),
        )
    )
    plan = cli._build_execution_plan("session-1", ("whatsapp", "telegram", "notesnook"), resolutions)
    offline = [p for p in plan if p["acquisition_environment"] == "device_only"]
    online = [p for p in plan if p["acquisition_environment"] == "controlled_online"]
    assert (offline[-1]["app_id"], offline[-1]["method"]) == ("whatsapp", "export")
    assert [(p["app_id"], p["method"]) for p in offline[:-1]] == [
        ("whatsapp", "materialize"), ("telegram", "materialize"),
        ("notesnook", "materialize"), ("notesnook", "export"),
    ]
    assert [(p["app_id"], p["method"]) for p in online] == [(p["app_id"], p["method"]) for p in offline]
    assert [p["order"] for p in plan] == list(range(1, len(plan) + 1))
    assert all(f'-{p["order"]:03d}-' in p["run_id"] for p in plan)
    assert plan == tuple(offline + online)


def test_single_serial_requires_exactly_one_device():
    assert cli._single_serial(["TEST-SERIAL-01"]) == "TEST-SERIAL-01"

    with pytest.raises(cli.CliError, match="exactly one"):
        cli._single_serial([])
    with pytest.raises(cli.CliError, match="exactly one"):
        cli._single_serial(["A", "B"])


@pytest.mark.parametrize(("installed", "release", "status", "selected"), [
    ("138.0.7204.179", "9", "exact", "138.0.7204.179"),
    ("150.0.7871.124", "10", "exact", "150.0.7871.124"),
    ("150.0.7871.124", "9", "nearest", "138.0.7204.179"),
    ("138.0.7204.179", "16", "nearest", "150.0.7871.124"),
    ("149.0", "9", "nearest", "138.0.7204.179"),
    ("139.0", "10", "nearest", "150.0.7871.124"),
    ("150.0.7871.124", "8", "no_profile", None),
    ("150.0.7871.124", None, "no_profile", None),
    ("150.0.7871.124", "Q", "no_profile", None),
])
def test_profile_resolution_filters_incompatible_android_versions(installed, release, status, selected):
    root = Path(__file__).resolve().parents[1] / "profiles"
    result = cli._select_app_profile(
        ProfileStore(root), root, "chrome", "com.android.chrome", installed,
        android_version=release,
    )
    assert result.version_name == installed
    assert result.status == status
    assert (result.profile.app_version if result.profile else None) == selected


def test_device_and_inventory_resolution_use_android_release():
    root = Path(__file__).resolve().parents[1] / "profiles"
    store = ProfileStore(root)
    device = ShellDevice({
        "dumpsys package com.android.chrome": "versionName=150.0.7871.124\n",
        "getprop ro.build.version.release": "9\n",
    })
    resolved = cli._resolve_app_profile(store, root, device, "chrome")
    inventory, = cli._match_installed_apps(
        store, root, (InstalledPackage("com.android.chrome", "150.0.7871.124"),), "9",
    )
    assert resolved == inventory
    assert resolved.profile.app_version == "138.0.7204.179"
    assert resolved.version_name == "150.0.7871.124"


def test_installed_version_is_read_from_dumpsys():
    device = ShellDevice({
        "dumpsys package com.whatsapp": "  versionName=2.26.27.85\n",
    })

    assert cli._installed_version(
        device,
        "com.whatsapp",
    ) == "2.26.27.85"


def test_android_device_reads_installed_packages_with_versions():
    package_dump = """Packages:
  Package [org.telegram.messenger] (one):
    codePath=/data/app/telegram
    versionName=12.9.2
    User 0: installed=true hidden=false
  Package [com.whatsapp] (two):
    codePath=/data/app/whatsapp
    versionName=2.26.27.85
    User 0: installed=true hidden=false
  Package [com.whatsapp] (three):
    codePath=/product/app/WhatsApp
    versionName=2.20.0
    User 0: installed=true hidden=false
"""
    adb = SimpleNamespace(
        shell=lambda command: (
            package_dump
            if command == "dumpsys package packages"
            else (
                "package:/data/app/telegram/base.apk="
                "org.telegram.messenger\n"
                "package:/data/app/whatsapp/base.apk=com.whatsapp\n"
            )
            if command == "pm list packages -f --user 0"
            else "0"
            if command == "am get-current-user"
            else pytest.fail(command)
        )
    )

    packages = AndroidDevice(
        object(), adb, serial="SERIAL"
    ).installed_packages()

    assert packages == (
        InstalledPackage("com.whatsapp", "2.26.27.85"),
        InstalledPackage("org.telegram.messenger", "12.9.2"),
    )


def test_resolves_exact_installed_app_and_manufacturer_profiles(tmp_path):
    _write_profiles(tmp_path)
    store = ProfileStore(tmp_path)
    device = ShellDevice({
        "dumpsys package com.whatsapp": "versionName=2.26.27.85\n",
        "getprop ro.product.manufacturer": "samsung\n",
    })

    resolution = cli._resolve_app_profile(
        store,
        tmp_path,
        device,
        "whatsapp",
    )
    system_ui = cli._resolve_system_ui_profile(
        store,
        tmp_path,
        device,
    )

    assert resolution.status == "exact"
    assert resolution.profile.app_version == "2.26.27.85"
    assert resolution.version_name == "2.26.27.85"
    assert resolution.profile.routes == (Route.EXPORT, Route.MATERIALIZE)
    assert system_ui.profile_id == "samsung"


def test_app_profile_fallback_selects_nearest_numeric_version(tmp_path):
    _write_profiles(tmp_path)
    _write_app_profile(tmp_path, "12.9.0")
    device = ShellDevice({
        "dumpsys package com.whatsapp": "versionName=12.7.2\n",
    })

    resolution = cli._resolve_app_profile(
        ProfileStore(tmp_path),
        tmp_path,
        device,
        "whatsapp",
    )

    assert resolution.status == "nearest"
    assert resolution.version_name == "12.7.2"
    assert resolution.profile.app_version == "12.9.0"


def test_app_profile_fallback_prefers_higher_version_on_equal_distance(
    tmp_path,
):
    _write_app_profile(tmp_path, "12.6.2")
    _write_app_profile(tmp_path, "12.8.2")
    device = ShellDevice({
        "dumpsys package com.whatsapp": "versionName=12.7.2\n",
    })

    resolution = cli._resolve_app_profile(
        ProfileStore(tmp_path),
        tmp_path,
        device,
        "whatsapp",
    )

    assert resolution.status == "nearest"
    assert resolution.profile.app_version == "12.8.2"


def test_app_profile_fallback_marks_non_numeric_versions_without_exact_profile(
    tmp_path,
):
    _write_app_profile(tmp_path, "legacy")
    device = ShellDevice({
        "dumpsys package com.whatsapp": "versionName=12.7.2\n",
    })

    resolution = cli._resolve_app_profile(
        ProfileStore(tmp_path), tmp_path, device, "whatsapp"
    )

    assert resolution.status == "no_profile"
    assert resolution.profile is None


def test_app_profile_fallback_marks_missing_profile_root(tmp_path):
    device = ShellDevice({
        "dumpsys package com.whatsapp": "versionName=12.7.2\n",
    })

    resolution = cli._resolve_app_profile(
        ProfileStore(tmp_path), tmp_path, device, "whatsapp"
    )

    assert resolution.status == "no_profile"
    assert resolution.profile is None


def test_matches_installed_apps_as_exact_or_without_profile(tmp_path):
    _write_profiles(tmp_path)

    matches = cli._match_installed_apps(
        ProfileStore(tmp_path),
        tmp_path,
        (
            InstalledPackage("com.whatsapp", "2.26.27.85"),
            InstalledPackage("example.unsupported", "1.0"),
        ),
    )

    assert [(match.package_name, match.status) for match in matches] == [
        ("com.whatsapp", "exact"),
        ("example.unsupported", "no_profile"),
    ]
    assert matches[0].profile is not None
    assert matches[1].profile is None


@pytest.mark.parametrize(
    ("app", "route", "target"),
    [
        (
            "telegram",
            Route.MATERIALIZE,
            {"kind": "account", "ref": "account.telegram.all"},
        ),
        (
            "notion",
            Route.MATERIALIZE,
            {"kind": "account", "ref": "account.notion.all"},
        ),
        (
            "notion",
            Route.EXPORT,
            {"kind": "account", "ref": "account.notion.all"},
        ),
        (
            "whatsapp",
            Route.MATERIALIZE,
            {
                "kind": "chat_list",
                "ref": "chat-list.whatsapp.active",
            },
        ),
        (
            "notesnook",
            Route.MATERIALIZE,
            {"kind": "container", "ref": "notes.notesnook.all"},
        ),
        (
            "notesnook",
            Route.EXPORT,
            {"kind": "container", "ref": "notes.notesnook.all"},
        ),
    ],
)
def test_condition_uses_whole_application_target(app, route, target):
    condition = cli._condition(app, route, "device_only")

    assert condition["target"] == target
    assert condition["notifications"] == "suppress_all"
    assert condition["airplane_mode"] == "enabled"
    assert condition["acquisition_environment"] == "device_only"
    assert "bluetooth" not in condition


def test_whatsapp_export_uses_running_host_and_documents(tmp_path):
    documents = tmp_path / "Documents"
    documents.mkdir()

    condition = cli._condition(
        "whatsapp",
        Route.EXPORT,
        "controlled_online",
        host_name="DESKTOP-TESTLAB",
        home=tmp_path,
    )

    assert condition == {
        "target": {
            "kind": "chat_list",
            "ref": "chat-list.whatsapp.active",
        },
        "notifications": "suppress_all",
        "airplane_mode": "enabled",
        "acquisition_environment": "controlled_online",
        "bluetooth": {
            "target_name": "DESKTOP-TESTLAB",
            "receiver_label": "DESKTOP-TESTLAB",
            "receive_dir": str(documents),
        },
    }


def test_whatsapp_export_accepts_downloads_without_documents(tmp_path):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    condition = cli._condition("whatsapp", Route.EXPORT, "device_only", host_name="Receiver", home=tmp_path)
    assert condition["bluetooth"]["receive_dir"] == str(downloads)


def test_prepare_persists_snapshot_before_mutating_device(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    snapshot = EnvironmentSnapshot(False, False, False, True)
    observed = []
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", FakeEnvironmentDevice()), raising=False
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    class SystemUI:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot_environment(self):
            return snapshot

        def prepare_environment(self, value):
            observed.append(SessionStore(tmp_path / "runs").load().status)

        def verify_prepared_environment(self):
            observed.append("verified")
            return snapshot

    monkeypatch.setattr(cli, "SystemUIRuntime", SystemUI, raising=False)

    payload, exit_code = cli._execute_prepare()

    active = SessionStore(tmp_path / "runs").load()
    assert (exit_code, payload["status"]) == (0, "prepared")
    assert active.status == "prepared"
    assert active.initial == snapshot
    assert observed == ["preparing", "verified"]


def test_prepare_failure_archives_after_wifi_and_notification_restoration(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    snapshot = EnvironmentSnapshot(False, False, False, True)
    restored = []
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", FakeEnvironmentDevice()), raising=False
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    class SystemUI:
        def __init__(self, *args, **kwargs):
            pass

        def snapshot_environment(self):
            return snapshot

        def prepare_environment(self, value):
            raise RuntimeError("prepare failed")

        def restore_wifi(self, enabled):
            assert enabled is True
            return enabled

        def restore_notification_suppression(self, value):
            restored.append(value)

        def snapshot_notification_suppression(self):
            return DndSnapshot(False, False)

    monkeypatch.setattr(cli, "SystemUIRuntime", SystemUI)

    with pytest.raises(cli.CliError, match="prepare failed"):
        cli._execute_prepare()

    store = SessionStore(tmp_path / "runs")
    archives = list(store.archive_root.glob("session-*/session.json"))
    assert restored == [DndSnapshot(False, False)]
    assert not store.active_path.exists()
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["status"] == (
        "prepare_failed"
    )


def test_acquisition_summary_reports_items_and_unfinished_scope_without_counting_retries():
    manifest = {
        "outcome": {"status": "partial", "reason": "notesnook_materialize_failed",
                    "action": "scroll-note-17-history", "action_id": "action-701",
                    "details": {"failure": "history overlap is ambiguous or unavailable"}},
        "items": [
            {"acquisition_item_id": "note", "target_id": "notes", "item_type": "note"},
            {"acquisition_item_id": "revision", "target_id": "notes", "item_type": "revision",
             "source": {"note_ref": "note-17", "version_ref": "version-2"}},
            {"acquisition_item_id": "attachment", "target_id": "attachments", "item_type": "attachment"},
        ],
        "attempts": [
            {"acquisition_item_id": "note", "attempt_id": "a1", "procedure_status": "completed",
             "acquisition_status": "not_acquired", "reason": "retryable"},
            {"acquisition_item_id": "note", "attempt_id": "a2", "procedure_status": "completed",
             "acquisition_status": "acquired"},
            {"acquisition_item_id": "revision", "attempt_id": "a3", "procedure_status": "interrupted",
             "acquisition_status": "partial", "reason": "history overlap is ambiguous or unavailable",
             "details": {"observation_id": "screen-1"}},
            {"acquisition_item_id": "attachment", "attempt_id": "a4", "procedure_status": "not_attempted",
             "acquisition_status": None, "reason": "procedure stopped"},
        ],
        "identifications": [
            {"target_id": "notes", "identification_status": "interrupted", "identified_item_count": 2,
             "traversal_complete": False, "reason": "collector_returned_before_traversal_completion"},
            {"target_id": "attachments", "identification_status": "completed", "identified_item_count": 1,
             "traversal_complete": True},
            {"target_id": "export", "identification_status": "not_attempted", "traversal_complete": False},
        ],
    }
    summary = cli._acquisition_summary(manifest, ["notes", "attachments"])
    assert summary["counting_basis"] == "latest_direct_attempt_or_referenced_collection_attempt"
    assert summary["item_counts"] == {"acquired": 1, "partial": 1, "not_attempted": 1}
    assert summary["item_type_counts"]["revision"] == {"partial": 1}
    assert summary["run_issue"]["action"] == "scroll-note-17-history"
    assert summary["run_issue"]["details"]["failure"] == "history overlap is ambiguous or unavailable"
    assert summary["issue_count"] == 2
    assert [i["attempt_id"] for i in summary["issues"]] == ["a3", "a4"]
    assert summary["issues"][0]["source"] == {"note_ref": "note-17", "version_ref": "version-2"}
    assert summary["issues"][0]["details"] == {"observation_id": "screen-1"}
    assert [i["target_id"] for i in summary["incomplete_targets"]] == ["notes"]
    assert summary["unidentified_item_count"] is None
    assert summary["issues_omitted"] == 0


def test_acquisition_summary_bounds_preview_and_distinguishes_unknown_from_empty():
    manifest = {"outcome": {"status": "complete"}, "identifications": [
        {"target_id": "files", "identification_status": "completed", "traversal_complete": True,
         "identified_item_count": 0}]}
    empty = cli._acquisition_summary(manifest, ["files"])
    assert empty["item_counts"] == {} and empty["issue_count"] == 0
    assert empty["unidentified_item_count"] == 0
    assert "run_issue" not in empty
    manifest["identifications"] = []
    manifest["items"] = [{"acquisition_item_id": f"f{i}", "target_id": "files", "item_type": "file"}
                         for i in range(12)]
    unknown = cli._acquisition_summary(manifest, ["files"])
    assert unknown["item_counts"] == {"not_attempted": 12}
    assert unknown["issue_count"] == 12 and len(unknown["issues"]) == 10
    assert unknown["issues_omitted"] == 2
    assert unknown["unidentified_item_count"] is None
    assert unknown["incomplete_targets"][0]["identification_status"] == "unknown"


@pytest.mark.parametrize("status", ["acquired", "partial"])
def test_summary_resolves_collection_bound_messages_and_browser_rows(status):
    manifest = {
        "outcome": {"status": "partial"},
        "items": [
            {"acquisition_item_id": "parent", "target_id": "scope", "item_type": "collection"},
            {"acquisition_item_id": "message", "target_id": "scope", "item_type": "message",
             "source": {"collection_attempt_id": "a1", "artifact_id": "message-json"}},
            {"acquisition_item_id": "visit", "target_id": "scope", "item_type": "visit",
             "source": {"collection_attempt_id": "a1", "artifact_id": "history-json", "record_index": 0}},
            {"acquisition_item_id": "broken", "target_id": "scope", "item_type": "message",
             "source": {"collection_attempt_id": "missing", "artifact_id": "missing-json"}},
        ],
        "attempts": [{"attempt_id": "a1", "acquisition_item_id": "parent",
                      "procedure_status": "completed" if status == "acquired" else "interrupted",
                      "acquisition_status": status, "reason": None if status == "acquired" else "list_changed_after_return"}],
    }
    summary = cli._acquisition_summary(manifest, ["scope"])
    assert summary["item_counts"] == {status: 3, "unknown": 1}
    assert summary["collection_bound_item_count"] == 3
    assert summary["issue_count"] == (1 if status == "acquired" else 4)
    if status == "partial":
        message = next(row for row in summary["issues"] if row["acquisition_item_id"] == "message")
        assert message["attempt_id"] == "a1"
        assert message["status_basis"] == "referenced_collection_attempt"
        assert message["reason"] == "list_changed_after_return"
    broken = summary["issues"][-1]
    assert broken["acquisition_status"] == "unknown"
    assert broken["reason"] == "collection_attempt_reference_unresolved"


def test_collect_uses_active_session_and_packages_run(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    SessionStore(tmp_path / "runs").create(_session())
    captured = {}
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", FakeEnvironmentDevice()), raising=False
    )
    monkeypatch.setattr(
        cli,
        "_resolve_app_profile",
        lambda *args: cli.ProfileResolution(
            "whatsapp",
            "com.whatsapp",
            "2.26.27.85",
            "exact",
            _app_profile(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    class Runtime:
        def __init__(self, runs_root, device):
            self.runs_root = runs_root

        def run(self, *args, **kwargs):
            captured.update(kwargs)
            run_dir = self.runs_root / "run-1"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
            (run_dir / "acquisition.json").write_text(
                '{"run":{},"outcome":{"status":"complete","reason":null}}\n',
                encoding="utf-8",
            )
            return SimpleNamespace(
                run_dir=run_dir,
                outcome=Outcome(OutcomeStatus.COMPLETE),
            )

    monkeypatch.setattr(cli, "AcquisitionRuntime", Runtime)

    payload, exit_code = cli._execute_collect(
        "whatsapp", "materialize", "device_only"
    )

    assert exit_code == 0
    assert payload["status"] == "complete"
    assert captured["prepared_session_id"] == "session-1"
    assert captured["installed_app_version"] == "2.26.27.85"
    assert payload["installed_app_version"] == "2.26.27.85"
    assert payload["profile_app_version"] == "2.26.27.85"
    assert payload["app_profile_exact_match"] is True
    assert (tmp_path / payload["archive_path"]).is_file()
    assert (tmp_path / payload["hash_path"]).is_file()
    assert payload["archive_size"] == (
        tmp_path / payload["archive_path"]
    ).stat().st_size


def test_collect_packages_initialized_failed_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    SessionStore(tmp_path / "runs").create(_session())
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", FakeEnvironmentDevice()), raising=False
    )
    monkeypatch.setattr(
        cli,
        "_resolve_app_profile",
        lambda *args: cli.ProfileResolution(
            "whatsapp",
            "com.whatsapp",
            "2.26.27.85",
            "exact",
            _app_profile(),
        ),
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    class Runtime:
        def __init__(self, runs_root, device):
            self.runs_root = runs_root

        def run(self, *args, **kwargs):
            run_dir = self.runs_root / "run-failed"
            run_dir.mkdir()
            (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")
            (run_dir / "acquisition.json").write_text(
                '{"run":{},"outcome":{"status":"failed","reason":"boom"}}\n',
                encoding="utf-8",
            )
            raise AcquisitionRunError(run_dir, "boom")

    monkeypatch.setattr(cli, "AcquisitionRuntime", Runtime)

    payload, exit_code = cli._execute_collect(
        "whatsapp", "materialize", "controlled_online"
    )

    assert (exit_code, payload["status"], payload["reason"]) == (
        1,
        "failed",
        "boom",
    )
    assert payload["installed_app_version"] == "2.26.27.85"
    assert payload["profile_app_version"] == "2.26.27.85"
    assert payload["app_profile_exact_match"] is True
    assert (tmp_path / payload["archive_path"]).is_file()


def test_finalize_archives_unavailable_state_for_session_review(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    store.create(_session())
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", FakeEnvironmentDevice()), raising=False
    )
    monkeypatch.setattr(
        cli,
        "_resolve_system_ui_profile",
        lambda *args: _system_profile(),
    )

    class FailingSystemUI:
        def __init__(self, *args, **kwargs):
            pass

        def restore_environment(self, snapshot):
            raise AssertionError("finalization must not restore network settings")

        def restore_wifi(self, enabled):
            raise RuntimeError("Wi-Fi unavailable")

        def restore_notification_suppression(self, snapshot):
            raise RuntimeError("notification restoration unavailable")

        def snapshot_environment(self):
            raise RuntimeError("observation failed")

    monkeypatch.setattr(
        cli, "SystemUIRuntime", FailingSystemUI, raising=False
    )
    payload, exit_code = cli._execute_finalize()

    assert (exit_code, payload["status"]) == (1, "complete")
    assert not store.active_path.exists()
    session_record = store.archive_root / "session-1" / "session.json"
    assert session_record.is_file()
    document = json.loads(session_record.read_text(encoding="utf-8"))
    assert document["final_state"]["status"] == "unavailable"
    assert "observation failed" in document["final_state"]["error"]
    assert document["final_state"]["observed"] is None


def test_finalize_observed_state_does_not_replace_acquisition_status(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    store.create(_session().with_status("prepared", initial=EnvironmentSnapshot(False, False, False, False)))
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", object())
    )
    monkeypatch.setattr(
        cli, "_resolve_system_ui_profile", lambda *args: _system_profile()
    )

    class SystemUI:
        def __init__(self, *args, **kwargs):
            pass

        def restore_notification_suppression(self, snapshot):
            pass

        def restore_wifi(self, enabled):
            assert enabled is False
            return enabled

        def snapshot_notification_suppression(self):
            return DndSnapshot(False, False)

        def snapshot_environment(self):
            return EnvironmentSnapshot(False, False, False, False, False)

    monkeypatch.setattr(cli, "SystemUIRuntime", SystemUI)

    payload, exit_code = cli._execute_finalize()

    assert (exit_code, payload["status"]) == (0, "complete")
    assert payload["final_state"]["observed"]["wifi_enabled"] is False
    document = json.loads(
        Path(payload["session_record"]).read_text(encoding="utf-8")
    )
    assert document["status"] == "complete"
    assert document["final_state"]["status"] == "observed"


def test_finalize_accepts_preparing_session_for_recovery(tmp_path):
    store = SessionStore(tmp_path)
    session = _session().with_status("preparing")
    store.create(session)

    resolved = cli._active_session(
        store,
        "SERIAL",
        _system_profile(),
        for_collection=False,
    )

    assert resolved == session


@pytest.mark.parametrize("initial_dnd,initial_hide", [(False, False), (False, True), (True, False), (True, True)])
def test_finalize_restores_initial_notifications_and_wifi(tmp_path, monkeypatch, initial_dnd, initial_hide):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    initial = EnvironmentSnapshot(initial_dnd, initial_hide, False, True)
    store = SessionStore(tmp_path / "runs")
    store.create(_session().with_status("prepared", initial=initial))
    device = FakeEnvironmentDevice(dnd_enabled=True, hide_all=True, airplane_enabled=True, wifi_enabled=False, wifi_connected=False)
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    result, code = cli._execute_finalize(("SERIAL", device))
    assert code == 0
    assert (device.dnd_enabled, device.hide_all) == (initial_dnd, initial_hide)
    assert (device.airplane_enabled, device.wifi_enabled) == (True, True)
    record = json.loads(Path(result["session_record"]).read_text())
    assert record["notification_restoration"]["status"] == "restored"
    assert record["notification_restoration"]["target"] == {"dnd_enabled": initial_dnd, "hide_all": initial_hide}


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_finalize_wifi_restoration_failure_still_restores_notifications(tmp_path, monkeypatch, error_type):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    store.create(_session().with_status("prepared", initial=EnvironmentSnapshot(False, False, False, False)))

    class Device(FakeEnvironmentDevice):
        def shell(self, command):
            if command == "svc wifi disable":
                raise error_type("Wi-Fi restoration failed")
            return super().shell(command)

    device = Device(dnd_enabled=True, hide_all=True, airplane_enabled=True, wifi_enabled=True)
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    result, code = cli._execute_finalize(("SERIAL", device))
    record = json.loads(Path(result["session_record"]).read_text())
    assert code == 1
    assert result["wifi_restoration"] == record["wifi_restoration"]
    assert record["wifi_restoration"]["status"] == "failed"
    assert record["wifi_restoration"]["error_type"] == error_type.__name__
    assert record["notification_restoration"]["status"] == "restored"
    assert device.airplane_enabled and device.wifi_enabled
    assert not device.dnd_enabled and not device.hide_all
    assert record["final_state"]["observed"]["wifi_enabled"] is True
    assert result["status"] == ("interrupted" if error_type is KeyboardInterrupt else "complete")
    if error_type is KeyboardInterrupt:
        assert record["interruption"]["stage"] == "wifi_restoration"
    assert not store.active_path.exists()


def test_finalize_preserves_restoration_failure_and_still_observes_final_state(tmp_path, monkeypatch):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    store = SessionStore(tmp_path / "runs")
    store.create(_session())
    device = FakeEnvironmentDevice(dnd_enabled=True, hide_all=True, airplane_enabled=True, wifi_enabled=False, fail_restore=True)
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    runtime = cli.SystemUIRuntime
    monkeypatch.setattr(cli, "SystemUIRuntime", lambda *args: runtime(*args, timeout=0))
    result, code = cli._execute_finalize(("SERIAL", device))
    assert code == 1
    record = json.loads(Path(result["session_record"]).read_text())
    assert record["notification_restoration"]["status"] == "failed"
    assert record["notification_restoration"]["error"]
    assert record["wifi_restoration"]["status"] == "restored"
    assert device.wifi_enabled is True
    assert record["final_state"]["observed"]["hide_all"] is True
    assert device.dnd_enabled is False
    assert not store.active_path.exists()


def test_main_prints_result_and_maps_status_to_exit_code(
    monkeypatch,
    capsys,
    tmp_path,
):
    monkeypatch.setattr(cli, "_execute_acquire", lambda *args, **kwargs: ({
        "status": "complete",
        "session_record": "runs/sessions/session-1/session.json",
    }, 0))

    path = tmp_path / "authority.json"
    path.write_text(json.dumps(synthetic_authorization([_app_profile()])["document"]))
    exit_code = cli.main(["acquire", "whatsapp", "--authorization", str(path)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {
        "status": "complete",
        "session_record": "runs/sessions/session-1/session.json",
    }
