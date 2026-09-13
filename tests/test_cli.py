import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

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
from fakes import FakeEnvironmentDevice, target_rules


class ShellDevice:
    def __init__(self, responses):
        self.responses = {"getprop ro.build.version.release": "16\n", **responses}

    def shell(self, command):
        return self.responses[command]


@pytest.mark.parametrize("exit_kind", ["complete", "interrupt", "online_interrupt", "failure", "package_error", "disconnect", "transition_failure"])
def test_real_session_packages_and_preserves_last_environment(tmp_path, monkeypatch, exit_kind):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)
    device = FakeEnvironmentDevice(wifi_enabled=True, wifi_connected=exit_kind != "transition_failure")
    resolutions = tuple(
        cli.ProfileResolution(app, package, "1.0", "exact", _materialize_profile(app, package))
        for app, package in (("whatsapp", "com.whatsapp"), ("telegram", "org.telegram.messenger"))
    )
    disconnected = False

    def connected():
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
        environment = context.base_context["condition"]["acquisition_environment"]
        calls.append((context.app_profile.app_id, environment))
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
        return Outcome(OutcomeStatus.COMPLETE)

    monkeypatch.setitem(cli.COLLECTORS, "whatsapp", collect)
    monkeypatch.setitem(cli.COLLECTORS, "telegram", collect)
    original_package = cli.package_run

    def package(path):
        if exit_kind == "package_error" and len(calls) == 2:
            raise cli.PackageError("package failed")
        return original_package(path)

    monkeypatch.setattr(cli, "package_run", package)
    payload, code = cli._execute_acquire(("whatsapp", "telegram"))
    document = json.loads(Path(payload["session_record"]).read_text())
    expected = [("whatsapp", "device_only"), ("telegram", "device_only")]
    if exit_kind in {"complete", "disconnect", "online_interrupt"}:
        expected += [("whatsapp", "controlled_online"), ("telegram", "controlled_online")]
    assert calls == expected
    assert code == (0 if exit_kind == "complete" else 1)
    assert document["device_model"] == "Fake phone"
    assert document["android_version"] == "16"
    assert document["preparation"]["status"] == "verified"
    assert document["preparation"]["observed"]["wifi_enabled"] is False
    assert document["preparation"]["observed_at"]
    assert "restoration" not in document and "restored_at" not in document
    assert device.airplane_enabled and device.dnd_enabled
    assert device.wifi_enabled == (exit_kind in {"complete", "disconnect", "online_interrupt", "transition_failure"})
    assert document["executions"][0]["archive_path"]
    if exit_kind == "disconnect":
        assert document["final_state"]["status"] == "unavailable"
        assert document["final_state"]["observed"] is None
        assert document["final_state"]["last_confirmed"]["observed"]["wifi_enabled"] is True
        assert "disconnected" in document["final_state"]["error"]
    else:
        assert document["final_state"]["observed"]["wifi_enabled"] == device.wifi_enabled
    for transition in document["environment_transitions"]:
        assert "conditions" not in transition
        assert transition["state"]["observed_at"]
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
def test_real_prepare_failure_archives_confirmed_state_without_rollback(tmp_path, monkeypatch, failure):
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
    monkeypatch.setattr(cli, "_connected_device", lambda: ("SERIAL", device))
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
    assert device.wifi_enabled == (failure == "wifi")
    assert device.dnd_enabled == (failure != "wifi")
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
    monkeypatch.setattr(cli, "_connected_device", lambda: ("SERIAL", device))
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


def test_prepare_error_then_final_query_interrupt_preserves_both_causes(tmp_path, monkeypatch):
    profile = ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_system_ui("samsung")
    monkeypatch.chdir(tmp_path)

    class Device(FakeEnvironmentDevice):
        preparation_failed = False

        def shell(self, command):
            if command == "svc wifi disable":
                self.preparation_failed = True
                raise RuntimeError("Wi-Fi preparation failed")
            if self.preparation_failed and command == "settings get global zen_mode":
                raise KeyboardInterrupt()
            return super().shell(command)

    device = Device(wifi_enabled=True)
    monkeypatch.setattr(cli, "_connected_device", lambda: ("SERIAL", device))
    monkeypatch.setattr(cli, "_resolve_system_ui_profile", lambda *args: profile)
    monkeypatch.setattr(cli.SystemUIRuntime, "_settle_transition", lambda self: None)
    with pytest.raises(KeyboardInterrupt) as raised:
        cli._execute_prepare()
    document = json.loads(next((tmp_path / "runs" / "sessions").glob("*/session.json")).read_text())
    assert document["status"] == "interrupted"
    assert document["interruption"]["reason"] == "KeyboardInterrupt"
    assert document["error"] == document["preparation"]["error"] == "Wi-Fi preparation failed"
    assert str(raised.value.__cause__) == "Wi-Fi preparation failed"
    assert document["final_state"]["error_type"] == "KeyboardInterrupt"
    assert document["final_state"]["observed"] is None
    assert document["final_state"]["last_confirmed"]["observed"]["airplane_enabled"] is False
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
            "timings": {"default_timeout": 5},
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
        timings={},
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
                target_id=f"{app_id}.content",
                item_types=("record",),
                method=Route.MATERIALIZE,
                acquisition_environments=("device_only", "controlled_online"),
                expected_artifacts=("structured_data",),
                rules=target_rules(("record",)),
            ),
        ),
        selectors={},
        timings={},
        parameters={},
    )


def test_parser_accepts_inspect_and_multi_app_acquire_commands():
    inspect = cli._parser().parse_args(["inspect"])
    acquire = cli._parser().parse_args(
        ["acquire", "whatsapp", "telegram"]
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
        cli, "_connected_device", lambda: ("SERIAL", device)
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
        (1, "whatsapp", "export", "device_only"),
        (2, "whatsapp", "materialize", "device_only"),
        (3, "whatsapp", "materialize", "controlled_online"),
    ]
    assert all(entry["status"] == "planned" for entry in plan)
    assert all(entry["profile_status"] == "exact" for entry in plan)
    assert plan[0]["run_id"] == "session-1-001-whatsapp-export-device-only"
    assert plan[-1]["run_id"] == "session-1-003-whatsapp-materialize-controlled-online"
    assert all("phase" not in entry and "profile_version" not in entry for entry in plan)


def test_single_serial_requires_exactly_one_device():
    assert cli._single_serial(["TESTSERIAL"]) == "TESTSERIAL"

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


def test_whatsapp_export_uses_running_host_and_documents(tmp_path):
    documents = tmp_path / "Documents"
    documents.mkdir()

    condition = cli._condition(
        "whatsapp",
        Route.EXPORT,
        "controlled_online",
        host_name="DESKTOP-81NCFKT",
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
            "target_name": "DESKTOP-81NCFKT",
            "receiver_label": "DESKTOP-81NCFKT",
            "receive_dir": str(documents),
        },
    }


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


def test_prepare_failure_archives_without_restoring(
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

        def restore_environment(self, value):
            restored.append(value)

    monkeypatch.setattr(cli, "SystemUIRuntime", SystemUI)

    with pytest.raises(cli.CliError, match="prepare failed"):
        cli._execute_prepare()

    store = SessionStore(tmp_path / "runs")
    archives = list(store.archive_root.glob("session-*/session.json"))
    assert restored == []
    assert not store.active_path.exists()
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["status"] == (
        "prepare_failed"
    )


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
            "2.26.26.1",
            "nearest",
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
    assert captured["installed_app_version"] == "2.26.26.1"
    assert payload["installed_app_version"] == "2.26.26.1"
    assert payload["profile_app_version"] == "2.26.27.85"
    assert payload["app_profile_exact_match"] is False
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
            raise AssertionError("finalization must not restore")

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
    store.create(_session())
    monkeypatch.setattr(
        cli, "_connected_device", lambda: ("SERIAL", object())
    )
    monkeypatch.setattr(
        cli, "_resolve_system_ui_profile", lambda *args: _system_profile()
    )

    class SystemUI:
        def __init__(self, *args, **kwargs):
            pass

        def restore_environment(self, snapshot):
            pass

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


def test_main_prints_result_and_maps_status_to_exit_code(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(cli, "_execute_acquire", lambda *args: ({
        "status": "complete",
        "session_record": "runs/sessions/session-1/session.json",
    }, 0))

    exit_code = cli.main(["acquire", "whatsapp"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {
        "status": "complete",
        "session_record": "runs/sessions/session-1/session.json",
    }
