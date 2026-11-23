from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .apps.registry import APPS, collectors
from .device import AndroidDevice, InstalledPackage
from .authorization import authorization_record, authorize_plan, grant_for, profile_digest
from .configuration import authorization_from_configuration, configuration_record, load_configuration, validate_configuration_link
from .journal import EventJournal
from .models import Route
from .packaging import PackageError, package_run
from .profiles import (
    AppProfile,
    ProfileError,
    ProfileStore,
    SystemUIProfile,
)
from .runtime import AcquisitionInterrupted, AcquisitionRunError, AcquisitionRuntime
from .session import DeviceSession, SessionError, SessionStore, capture_environment
from .system_ui import DndSnapshot, SystemUIRuntime


APP_PACKAGES = {name: spec["package_name"] for name, spec in APPS.items()}
COLLECTORS = collectors()


class CliError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileResolution:
    app_id: str | None
    package_name: str
    version_name: str | None
    status: str
    profile: AppProfile | None


def _app_id(value: str) -> str:
    return "samsung_browser" if value == "sbrowser" else value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aura")
    device_options = argparse.ArgumentParser(add_help=False)
    device_options.add_argument("--serial", help="target ADB serial from adb devices; required when multiple devices are connected")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", parents=[device_options])
    for command, output in (("configure", "session-config.json"), ("authorize", "case-authority.json")):
        configure = commands.add_parser(command, parents=[device_options], help=("create a session configuration using installed app profiles"
                                                      if command == "configure" else "legacy authority-file creation"))
        configure.add_argument("apps", nargs="+", type=_app_id, choices=tuple(APP_PACKAGES), help="app IDs; sbrowser is an alias for samsung_browser")
        configure.add_argument("--environment", choices=("device_only", "controlled_online", "all"), default="all", help="filter the scopes offered for interactive selection (default: all supported environments)")
        configure.add_argument("--method", choices=("materialize", "export", "all"), default="all", help="filter the scopes offered for interactive selection (default: all supported methods)")
        configure.add_argument("--output", type=Path, default=Path(output), help="new JSON file; existing files are not overwritten")
    acquire = commands.add_parser("acquire", parents=[device_options])
    acquire.add_argument("apps", nargs="+", type=_app_id, choices=tuple(APP_PACKAGES), help="app IDs; sbrowser is an alias for samsung_browser")
    config = acquire.add_mutually_exclusive_group(required=True)
    config.add_argument("--config", type=Path, help="session configuration JSON")
    config.add_argument("--authorization", type=Path, help="legacy input option; accepts an authority or session configuration JSON")
    return parser


def _single_serial(serials: list[str]) -> str:
    if len(serials) != 1:
        raise CliError("exactly one ADB device is required without --serial; use --serial to select a connected device")
    return serials[0]


def _installed_version(device, package_name: str) -> str:
    output = device.shell(f"dumpsys package {package_name}")
    match = re.search(r"^\s*versionName=(\S+)\s*$", output, re.MULTILINE)
    if match is None:
        raise CliError(f"installed version is unavailable: {package_name}")
    return match.group(1)


def _numeric_version(value: str) -> tuple[int, ...] | None:
    if re.fullmatch(r"\d+(?:\.\d+)*", value) is None:
        return None
    return tuple(int(part) for part in value.split("."))


def _resolve_app_profile(
    store: ProfileStore,
    profiles_root: Path,
    device,
    app_id: str,
) -> ProfileResolution:
    package_name = APP_PACKAGES[app_id]
    version = _installed_version(device, package_name)
    return _select_app_profile(
        store, profiles_root, app_id, package_name, version,
        device.shell("getprop ro.build.version.release").strip(),
    )


def _select_app_profile(
    store: ProfileStore,
    profiles_root: Path,
    app_id: str,
    package_name: str,
    version: str,
    android_version: str | None = None,
) -> ProfileResolution:
    app_root = profiles_root / "apps" / app_id
    ProfileStore._safe_segments(app_id, version)
    if (app_root / f"{version}.json").is_file():
        exact = store.load_app(app_id, version)
        if exact.supports_android(android_version):
            return ProfileResolution(app_id, package_name, version, "exact", exact)
    installed = _numeric_version(version)
    if installed is None:
        return ProfileResolution(
            app_id, package_name, version, "no_profile", None
        )
    if not app_root.is_dir():
        return ProfileResolution(
            app_id, package_name, version, "no_profile", None
        )
    versions = [
        (profile, parsed)
        for path in sorted(app_root.glob("*.json"))
        if (parsed := _numeric_version(path.stem)) is not None
        if (profile := store.load_app(app_id, path.stem)).supports_android(android_version)
    ]
    if not versions:
        return ProfileResolution(
            app_id, package_name, version, "no_profile", None
        )

    def rank(item: tuple[AppProfile, tuple[int, ...]]):
        _, candidate = item
        width = max(len(installed), len(candidate))
        installed_parts = installed + (0,) * (width - len(installed))
        candidate_parts = candidate + (0,) * (width - len(candidate))
        distance = tuple(
            abs(left - right)
            for left, right in zip(installed_parts, candidate_parts)
        )
        return distance, tuple(-part for part in candidate_parts)

    selected, _ = min(versions, key=rank)
    return ProfileResolution(
        app_id,
        package_name,
        version,
        "nearest",
        selected,
    )


def _match_installed_apps(
    store: ProfileStore,
    profiles_root: Path,
    packages: tuple[InstalledPackage, ...],
    android_version: str | None = None,
) -> tuple[ProfileResolution, ...]:
    app_ids = {package_name: app_id for app_id, package_name in APP_PACKAGES.items()}
    matches = []
    for package in packages:
        app_id = app_ids.get(package.package_name)
        if app_id is None or package.version_name is None:
            matches.append(
                ProfileResolution(
                    app_id,
                    package.package_name,
                    package.version_name,
                    "no_profile",
                    None,
                )
            )
            continue
        matches.append(
            _select_app_profile(
                store,
                profiles_root,
                app_id,
                package.package_name,
                package.version_name,
                android_version,
            )
        )
    return tuple(matches)


def _resolution_document(resolution: ProfileResolution) -> dict[str, object]:
    profile = resolution.profile
    return {
        "app_id": resolution.app_id,
        "package_name": resolution.package_name,
        "version_name": resolution.version_name,
        "profile_status": resolution.status,
        "profile_app_version": (
            None if profile is None else profile.app_version
        ),
    }


def _build_execution_plan(
    session_id: str,
    selected_apps: tuple[str, ...],
    resolutions: tuple[ProfileResolution, ...],
) -> tuple[dict[str, object], ...]:
    available = {
        resolution.app_id: resolution
        for resolution in resolutions
        if resolution.app_id is not None and resolution.profile is not None
    }
    plan = []
    for acquisition_environment in ("device_only", "controlled_online"):
        for app_id in selected_apps:
            resolution = available.get(app_id)
            if resolution is None:
                continue
            profile = resolution.profile
            assert profile is not None
            for method in profile.routes:
                targets = tuple(
                    target.target_id
                    for target in profile.targets
                    if target.method is method and acquisition_environment in target.acquisition_environments
                )
                if not targets:
                    continue
                plan.append({
                    "app_id": app_id,
                    "package_name": resolution.package_name,
                    "installed_app_version": resolution.version_name,
                    "profile_app_version": profile.app_version,
                    "profile_sha256": profile_digest(profile),
                    "profile_status": resolution.status,
                    "method": method.value,
                    "acquisition_environment": acquisition_environment,
                    "target_ids": targets,
                    "excluded_target_ids": tuple(
                        target.target_id for target in profile.targets
                        if target.method is method and acquisition_environment not in target.acquisition_environments
                    ),
                    "status": "planned",
                })
    # Keep extra Bluetooth preparation after other work in each environment.
    plan.sort(key=lambda entry: (
        entry["acquisition_environment"] != "device_only",
        entry["app_id"] == "whatsapp" and entry["method"] == "export",
    ))
    for order, entry in enumerate(plan, 1):
        entry["order"] = order
        environment_name = entry["acquisition_environment"].replace("_", "-")
        entry["run_id"] = f"{session_id}-{order:03d}-{entry['app_id']}-{entry['method']}-{environment_name}"
    return tuple(plan)


def _execute_configure(args) -> tuple[dict[str, object], int]:
    if args.output.exists():
        raise CliError(f"session configuration file already exists: {args.output}; use --output with a new filename")
    serial, device = _connected_device(automation=False, expected_serial=args.serial)
    profiles_root = Path.cwd() / "profiles"
    store = ProfileStore(profiles_root)
    app_ids = tuple(dict.fromkeys(args.apps))
    resolutions = tuple(_resolve_app_profile(store, profiles_root, device, app_id) for app_id in app_ids)
    for resolution in resolutions:
        if resolution.status != "exact":
            raise CliError(f"exact profile required for {resolution.app_id} {resolution.version_name}: {resolution.status}")
    plan = tuple(
        entry for entry in _build_execution_plan("configuration", app_ids, resolutions)
        if (args.environment == "all" or entry["acquisition_environment"] == args.environment)
        and (args.method in {None, "all"} or entry["method"] == args.method)
    )
    missing = set(app_ids) - {entry["app_id"] for entry in plan}
    if missing:
        raise CliError(f"no executions match the selected method/environment for: {', '.join(sorted(missing))}")
    print("Available acquisition scopes:")
    for index, entry in enumerate(plan, 1):
        print(f"  {index}. {entry['app_id']} {entry['profile_app_version']} / {entry['method']} / {entry['acquisition_environment']}")
    selection = input("Select scope numbers (e.g. 1,3), or all; blank cancels: ").strip().lower()
    if not selection:
        return {"status": "cancelled", "reason": "no_scopes_selected"}, 1
    if selection != "all":
        try:
            indices = {int(value) for value in re.split(r"[,\s]+", selection)}
        except ValueError:
            raise CliError("scope selection must contain numbers or all") from None
        if not indices or not indices <= set(range(1, len(plan) + 1)):
            raise CliError("scope selection contains an unavailable number")
        plan = tuple(entry for index, entry in enumerate(plan, 1) if index in indices)
    grant_fields = ("app_id", "profile_app_version", "profile_sha256", "method", "acquisition_environment", "target_ids")
    grants = [
        {**{key: entry[key] for key in grant_fields}, "target_ids": list(entry["target_ids"])}
        for entry in plan
    ]
    print(json.dumps({"serial": serial, "proposed_execution_scopes": grants}, ensure_ascii=False, indent=2))
    online = any(entry["acquisition_environment"] == "controlled_online" for entry in plan)
    document = {
        "basis_type": input("Authority basis (e.g. research_device_owner_instruction, warrant, consent): ").strip(),
        "reference": input("Authority reference / research instruction: ").strip(),
        "scope": f"Device {serial}; full profile targets for: " + "; ".join(
            f"{entry['app_id']} {entry['profile_app_version']} / {entry['method']} / {entry['acquisition_environment']}"
            for entry in plan
        ),
        "confirmed_by": input("Confirmed by: ").strip(),
        "remote_acquisition": "allowed" if online else "denied",
        "grants": grants,
        "network_control": {"external_traffic_restriction": "not_reported", "reference": ""},
    }
    if online:
        print("The selected scope includes remote data. AURA does not enforce or verify external traffic restrictions.")
        control = document["network_control"]
        control["external_traffic_restriction"] = input("Traffic restriction (not_applied / configured / not_reported) [not_reported]: ").strip() or "not_reported"
        if control["external_traffic_restriction"] == "configured":
            control["reference"] = input("External traffic restriction reference: ").strip()
    if input("Confirm session scope and authority cover every target and environment shown above and save? [y/N]: ").strip().lower() not in {"y", "yes"}:
        return {"status": "cancelled", "reason": "authority_not_confirmed"}, 1
    document["confirmed_at"] = _now()
    for grant in grants:
        grant["scope_matches_profile"] = True
    legacy = args.command == "authorize"
    if not legacy:
        document = {
            "schema_version": 1,
            "execution_scopes": document.pop("grants"),
            "network_control": document.pop("network_control"),
            "authorization": document,
        }
    record = configuration_record(document)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(record["document"], stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"status": "created", "authorization" if legacy else "configuration": str(args.output.resolve()),
            "grant_count" if legacy else "execution_scope_count": len(grants),
            "document_sha256": record["document_sha256"]}, 0


def _execute_inspect(serial: str | None = None) -> tuple[dict[str, object], int]:
    serial, device = _connected_device(automation=False, expected_serial=serial)
    profiles_root = Path.cwd() / "profiles"
    store = ProfileStore(profiles_root)
    system_ui_profile = _resolve_system_ui_profile(
        store, profiles_root, device
    )
    resolutions = _match_installed_apps(
        store, profiles_root, device.installed_packages(),
        device.shell("getprop ro.build.version.release").strip(),
    )
    return {
        "status": "inspected",
        "serial": serial,
        "system_ui_profile": system_ui_profile.profile_id,
        "applications": [
            _resolution_document(resolution) for resolution in resolutions
        ],
    }, 0


def _resolve_system_ui_profile(
    store: ProfileStore,
    profiles_root: Path,
    device,
) -> SystemUIProfile:
    manufacturer = device.shell(
        "getprop ro.product.manufacturer"
    ).strip()
    matches = [
        profile
        for path in sorted((profiles_root / "system_ui").glob("*.json"))
        if (
            profile := store.load_system_ui(path.stem)
        ).supports(manufacturer)
    ]
    if len(matches) != 1:
        raise CliError(
            "expected one System UI Profile for "
            f"{manufacturer!r}, found {len(matches)}"
        )
    return matches[0]


def _condition(
    app_id: str,
    route: Route,
    acquisition_environment: str,
    *,
    host_name: str | None = None,
    home: Path | None = None,
) -> dict[str, object]:
    if acquisition_environment not in {"device_only", "controlled_online"}:
        raise CliError(
            f"unknown acquisition environment: {acquisition_environment!r}"
        )
    condition: dict[str, object] = {
        "target": dict(APPS[app_id]["target"]),
        "notifications": "suppress_all",
        "airplane_mode": "enabled",
        "acquisition_environment": acquisition_environment,
    }
    if app_id == "whatsapp" and route is Route.EXPORT:
        receiver = (host_name if host_name is not None else platform.node()).strip()
        receiver_home = home or Path.home()
        documents = receiver_home / "Documents"
        directories = [path for path in (
            documents, receiver_home / "Downloads", documents / "Bluetooth Exchange Folder"
        ) if path.is_dir()]
        if not receiver:
            raise CliError("Bluetooth host name is unavailable")
        if not directories:
            raise CliError(
                f"Bluetooth receive directories are unavailable under: {receiver_home}"
            )
        condition["bluetooth"] = {
            "target_name": receiver,
            "receiver_label": receiver,
            "receive_dir": str(directories[0]),
        }
        if len(directories) > 1:
            condition["bluetooth"]["receive_dirs"] = [str(path) for path in directories]
    return condition


def _connected_device(*, automation=True, expected_serial=None):
    import adbutils

    serials = [
        device.serial
        for device in adbutils.adb.device_list()
        if device.serial
    ]
    if expected_serial is None:
        serial = _single_serial(serials)
    elif expected_serial in serials:
        serial = expected_serial
    else:
        raise CliError(f"requested ADB device is not connected and authorized: {expected_serial!r}")
    if not automation:
        return serial, AndroidDevice(None, adbutils.adb.device(serial=serial), serial=serial)
    try:
        return serial, AndroidDevice.connect(serial)
    except (Exception, KeyboardInterrupt) as error:
        evidence = getattr(error, "automation_evidence", None)
        if evidence is not None:
            path = Path.cwd() / "runs" / f"connection-failed-{_stamp()}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                json.dump({"serial": serial, **evidence}, stream, ensure_ascii=False, indent=2)
            path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
        if isinstance(error, KeyboardInterrupt):
            raise
        raise CliError(f"device connection failed: {error}") from error


def _automation_snapshot(device):
    try:
        return device.automation_snapshot()
    except (Exception, KeyboardInterrupt) as error:
        return {"status": "unavailable", "queried_at": _now(), "error_type": type(error).__name__,
                "error": f"{type(error).__name__}: {error}"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timing(started: float, scope: str) -> dict[str, object]:
    return {
        "elapsed_seconds": perf_counter() - started,
        "timing_clock": "perf_counter",
        "timing_scope": scope,
    }


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")


def _active_session(
    store: SessionStore,
    serial: str,
    system_ui_profile: SystemUIProfile,
    *,
    for_collection: bool,
) -> DeviceSession:
    session = store.load()
    if session.serial != serial:
        raise CliError("active session belongs to a different device")
    if session.system_ui_profile != system_ui_profile.profile_id:
        raise CliError("active session uses a different System UI Profile")
    allowed = (
        {"prepared", "planned", "acquiring"}
        if for_collection
        else {
            "preparing",
            "prepared",
            "planned",
            "acquiring",
            "interrupted",
        }
    )
    if session.status not in allowed:
        raise CliError(
            f"active session status is not usable: {session.status}"
        )
    return session


def _execute_prepare(authorization=None, connection=None, session_id=None, *, configuration=None) -> tuple[dict[str, object], int]:
    preparation_started = perf_counter()
    root = Path.cwd()
    runs_root = root / "runs"
    session_store = SessionStore(runs_root)
    if session_store.active_path.exists():
        raise CliError("an active device session already exists")
    serial, device = connection or _connected_device()
    profiles_root = root / "profiles"
    store = ProfileStore(profiles_root)
    system_ui_profile = _resolve_system_ui_profile(
        store,
        profiles_root,
        device,
    )
    session_id = session_id or f"session-{_stamp()}"
    journal = EventJournal(
        runs_root / "sessions" / f"{session_id}-prepare.events.jsonl",
        {
            "device_session_id": session_id,
            "serial": serial,
            "system_ui_profile": system_ui_profile.profile_id,
        },
    )
    system_ui = SystemUIRuntime(device, system_ui_profile, journal)
    journal.record_action("automation_connection", status="recorded", details=getattr(
        device, "connection_evidence", {"status": "unavailable", "reason": "device_adapter_does_not_expose_connection_evidence"}
    ))
    try:
        device_model = device.shell("getprop ro.product.model").strip() or None
        android_version = device.shell("getprop ro.build.version.release").strip() or None
        snapshot = system_ui.snapshot_environment()
        initial_state = capture_environment(lambda: snapshot)
    except (Exception, KeyboardInterrupt) as error:
        reason = str(error) or type(error).__name__
        journal.record_action(
            "device_initial_query_exception", status="failed",
            details={"error_type": type(error).__name__, "error": reason},
        )
        if isinstance(error, KeyboardInterrupt):
            raise
        raise CliError(f"device initial query failed: {reason}") from error
    session = DeviceSession(
        session_id=session_id,
        serial=serial,
        system_ui_profile=system_ui_profile.profile_id,
        prepared_at=_now(),
        status="preparing",
        initial=snapshot,
        device_model=device_model,
        android_version=android_version,
        last_confirmed_state=initial_state,
        authorization=authorization,
        configuration=configuration,
        automation={"connection": getattr(device, "connection_evidence", {"status": "unavailable", "reason": "device_adapter_does_not_expose_connection_evidence"})},
        network_control={"poc_enforces_traffic_restriction": False, "external_control_verified_by_aura": False,
                         **((authorization or {}).get("document", {}).get("network_control")
                            or {"external_traffic_restriction": "not_reported", "reference": ""})},
    )
    session_store.create(session)
    try:
        system_ui.prepare_environment(snapshot)
        observed = system_ui.verify_prepared_environment()
    except (Exception, KeyboardInterrupt) as error:
        reason = str(error) or type(error).__name__
        journal.record_action(
            "device_prepare_exception",
            status="failed",
            details={"error_type": type(error).__name__, "error": reason},
        )
        preparation_timing = _timing(preparation_started, "session_preparation")
        wifi_restoration = _restore_session_wifi(system_ui, session, journal)
        restoration = _restore_session_notifications(system_ui, session, journal)
        final_state = capture_environment(system_ui.snapshot_environment, session.last_confirmed_state)
        final_automation = _automation_snapshot(device)
        interruption_reason = (
            reason if isinstance(error, KeyboardInterrupt)
            else wifi_restoration["error"] if wifi_restoration.get("error_type") == "KeyboardInterrupt"
            else restoration["error"] if restoration.get("error_type") == "KeyboardInterrupt"
            else final_state["error"] if final_state.get("error_type") == "KeyboardInterrupt"
            else final_automation["error"] if final_automation.get("error_type") == "KeyboardInterrupt"
            else None
        )
        session_store.archive(
            session.with_status(
                "interrupted" if interruption_reason is not None else "prepare_failed",
                finalized_at=_now(),
                preparation={"status": "failed", "at": _now(), "error": reason,
                             **preparation_timing},
                final_state=final_state,
                notification_restoration=restoration,
                wifi_restoration=wifi_restoration,
                last_confirmed_state=(final_state if final_state["status"] == "observed" else session.last_confirmed_state),
                interruption={"at": _now(), "reason": interruption_reason} if interruption_reason is not None else None,
                error=reason,
                automation={**(session.automation or {}), "final": final_automation},
            )
        )
        if isinstance(error, KeyboardInterrupt):
            raise
        if interruption_reason is not None:
            raise KeyboardInterrupt(interruption_reason) from error
        raise CliError(f"device preparation failed: {reason}") from error

    verified = capture_environment(lambda: observed)
    prepared = session.with_status(
        "prepared", applied_at=_now(),
        preparation={**verified, "status": "verified",
                     **_timing(preparation_started, "session_preparation")},
        last_confirmed_state=verified,
    )
    session_store.save(prepared)
    return {
        "status": "prepared",
        "session_id": prepared.session_id,
        "serial": prepared.serial,
        "system_ui_profile": prepared.system_ui_profile,
        "initial_state": asdict(prepared.initial),
    }, 0


def _execution_grant(session, resolution, app_id, route, acquisition_environment, run_id):
    app_profile = resolution.profile
    installed_app_version = resolution.version_name
    if app_profile is None:
        raise CliError("authorization profile is unavailable")
    try:
        if not session.authorization or authorization_record(session.authorization.get("document")) != session.authorization:
            raise ValueError("missing or altered authorization record")
    except (ValueError, TypeError) as error:
        raise CliError(f"invalid active session authorization: {error}") from error
    if session.configuration is not None:
        try:
            validate_configuration_link(session.configuration, session.authorization)
        except (ValueError, TypeError) as error:
            raise CliError(f"invalid active session configuration: {error}") from error
    candidate = {"app_id": app_id, "profile_app_version": app_profile.app_version, "profile_sha256": profile_digest(app_profile),
                 "method": route.value, "acquisition_environment": acquisition_environment,
                 "target_ids": [target.target_id for target in app_profile.targets
                                if target.method is route and acquisition_environment in target.acquisition_environments]}
    grant = grant_for(candidate, session.authorization) if session.authorization else None
    if grant is None or installed_app_version != app_profile.app_version:
        raise CliError("authorization no longer matches the installed app, profile or full execution scope")
    if session.execution_plan:
        planned = next((entry for entry in session.execution_plan if entry["run_id"] == run_id), None)
        if planned is None or planned["status"] not in {"planned", "running"} or planned.get("authorization_grant") != grant:
            raise CliError("authorization does not match the active execution plan")
        if session.configuration is not None or "configuration_document_sha256" in planned:
            if session.configuration is None or planned.get("configuration_document_sha256") != session.configuration["document_sha256"]:
                raise CliError("configuration does not match the active execution plan")
    return grant


def _acquisition_summary(manifest: dict, target_ids: list[str]) -> dict:
    """Summarize recorded item outcomes, separately from unfinished target traversal."""
    latest = {attempt["acquisition_item_id"]: attempt for attempt in manifest.get("attempts", [])}
    attempts_by_id = {attempt["attempt_id"]: attempt for attempt in manifest.get("attempts", [])}
    counts = Counter()
    type_counts = {}
    issues = []
    collection_bound_count = 0
    for item in manifest.get("items", []):
        if item["target_id"] not in target_ids:
            continue
        attempt = latest.get(item["acquisition_item_id"], {})
        source = item.get("source", {})
        collection_bound = not attempt and "collection_attempt_id" in source
        if collection_bound:
            collection_bound_count += 1
            attempt = attempts_by_id.get(source["collection_attempt_id"], {})
        procedure = attempt.get("procedure_status") if attempt else "not_attempted"
        if collection_bound and not attempt:
            procedure = None
        status = attempt.get("acquisition_status") or ("not_attempted" if procedure == "not_attempted" else "unknown")
        counts[status] += 1
        type_counts.setdefault(item["item_type"], Counter())[status] += 1
        if status != "acquired" or procedure != "completed":
            issues.append({
                "acquisition_item_id": item["acquisition_item_id"], "target_id": item["target_id"],
                "item_type": item["item_type"], "source": item.get("source", {}),
                "attempt_id": attempt.get("attempt_id"), "procedure_status": procedure,
                "acquisition_status": status,
                "status_basis": "referenced_collection_attempt" if collection_bound else "direct_attempt",
                "reason": attempt.get("reason") or (
                    "collection_attempt_reference_unresolved" if collection_bound and not attempt else "attempt_result_unavailable"),
                "details": attempt.get("details", {}),
            })
    identifications = {record["target_id"]: record for record in manifest.get("identifications", [])}
    incomplete = []
    for target_id in target_ids:
        record = identifications.get(target_id, {})
        if record.get("traversal_complete") is not True:
            incomplete.append({
                "target_id": target_id, "identification_status": record.get("identification_status", "unknown"),
                "identified_item_count": record.get("identified_item_count"),
                "reason": record.get("reason") or "target_traversal_not_confirmed",
            })
    # Keep the console preview small; acquisition_record retains every item and attempt.
    summary = {
        "counting_basis": "latest_direct_attempt_or_referenced_collection_attempt", "item_counts": dict(counts),
        "collection_bound_item_count": collection_bound_count,
        "item_type_counts": {kind: dict(values) for kind, values in type_counts.items()},
        "incomplete_targets": incomplete, "unidentified_item_count": None if incomplete else 0,
        "issue_count": len(issues), "issues": issues[:10], "issues_omitted": max(0, len(issues) - 10),
    }
    outcome = manifest.get("outcome", {})
    if outcome.get("status") != "complete":
        summary["run_issue"] = {
            key: outcome[key] for key in ("reason", "action", "action_id", "context") if key in outcome
        }
        summary["run_issue"]["details"] = {
            key: value for key, value in outcome.get("details", {}).items()
            if not isinstance(value, (dict, list))
        }
    return summary


def _execute_collect(
    app_id: str,
    route_value: str,
    acquisition_environment: str,
    *,
    run_id: str | None = None,
    connection=None,
) -> tuple[dict[str, object], int]:
    route = Route(route_value)
    serial, device = connection or _connected_device()
    root = Path.cwd()
    runs_root = root / "runs"
    profiles_root = root / "profiles"
    store = ProfileStore(profiles_root)
    system_ui_profile = _resolve_system_ui_profile(
        store,
        profiles_root,
        device,
    )
    session = _active_session(
        SessionStore(runs_root),
        serial,
        system_ui_profile,
        for_collection=True,
    )
    resolution = _resolve_app_profile(
        store,
        profiles_root,
        device,
        app_id,
    )
    app_profile = resolution.profile
    if app_profile is None:
        raise CliError(
            f"no applicable Profile for {app_id} {resolution.version_name}"
        )
    installed_app_version = resolution.version_name
    if installed_app_version is None:
        raise CliError(f"installed version is unavailable: {app_id}")
    if route not in app_profile.routes:
        raise CliError(
            f"{app_id} does not declare Route {route.value!r}"
        )
    grant = _execution_grant(session, resolution, app_id, route, acquisition_environment, run_id)
    condition = _condition(app_id, route, acquisition_environment)
    condition["authorization"] = {"document_sha256": session.authorization["document_sha256"], "grant": grant}
    condition["network_control"] = session.network_control
    if session.configuration is not None:
        condition["configuration_document_sha256"] = session.configuration["document_sha256"]
    pending_downloads = [
        {**request, "run_id": execution["run_id"]}
        for execution in session.executions if execution.get("app_id") == app_id
        for request in execution.get("pending_downloads", [])
    ]
    if pending_downloads:
        condition["pending_downloads"] = pending_downloads
    profile_context = {
        "installed_app_version": installed_app_version,
        "profile_app_version": app_profile.app_version,
        "app_profile_exact_match": resolution.status == "exact",
    }
    resolved_run_id = run_id or f"{app_id}-{route.value}-{_stamp()}"
    try:
        result = AcquisitionRuntime(runs_root, device).run(
            app_profile,
            system_ui_profile,
            route,
            condition,
            COLLECTORS[app_id],
            run_id=resolved_run_id,
            prepared_session_id=session.session_id,
            installed_app_version=installed_app_version,
        )
        run_dir = result.run_dir
        status = result.outcome.status.value
        reason = result.outcome.reason
    except (AcquisitionRunError, AcquisitionInterrupted) as error:
        run_dir = error.run_dir
        try:
            outcome = json.loads(
                (run_dir / "acquisition.json").read_text(encoding="utf-8")
            )["outcome"]
            status = outcome["status"]
            reason = outcome.get("reason")
        except (
            OSError,
            KeyError,
            TypeError,
            json.JSONDecodeError,
        ) as record_error:
            raise CliError("failed acquisition record is unreadable") from record_error

    manifest = json.loads((run_dir / "acquisition.json").read_text(encoding="utf-8"))
    pending = manifest.get("outcome", {}).get("details", {}).get("pending_downloads", [])
    final_state = manifest["run"].get("final_state")
    result_details = {
        "acquisition_record": str(run_dir / "acquisition.json"),
        "acquisition_summary": _acquisition_summary(manifest, grant["target_ids"]),
    }
    try:
        package = package_run(run_dir)
    except PackageError as error:
        return {
            "status": "failed",
            "reason": "packaging_failed",
            "error": str(error),
            "session_id": session.session_id,
            "run_dir": str(run_dir),
            "final_state": final_state,
            **result_details,
            **profile_context,
        }, 2
    return {
        "status": status,
        "reason": reason,
        "session_id": session.session_id,
        "run_id": resolved_run_id,
        "run_dir": str(run_dir),
        "archive_path": str(package.archive_path),
        "hash_path": str(package.hash_path),
        "archive_size": package.size,
        "sha256": package.sha256,
        "final_state": final_state,
        **result_details,
        **({"pending_downloads": pending} if pending else {}),
        **profile_context,
    }, 0 if status == "complete" else 1


def _restore_session_wifi(system_ui, session, journal) -> dict[str, object]:
    started = perf_counter()
    target = {"wifi_enabled": session.initial.wifi_enabled}
    result = {"target": target, "started_at": _now(), "observed": None}
    try:
        observed = system_ui.restore_wifi(session.initial.wifi_enabled)
        result["observed"] = {"wifi_enabled": observed}
        if result["observed"] != target:
            raise CliError("Wi-Fi restoration did not match initial state")
        result["status"] = "restored"
    except (Exception, KeyboardInterrupt) as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error) or type(error).__name__)
    result.update(completed_at=_now(), **_timing(started, "wifi_restoration"))
    journal.record_action("session_wifi_restoration", status=result["status"], details=result)
    return result


def _restore_session_notifications(system_ui, session, journal) -> dict[str, object]:
    started = perf_counter()
    target = {"dnd_enabled": session.initial.dnd_enabled, "hide_all": session.initial.hide_all}
    result = {"target": target, "started_at": _now(), "observed": None}
    try:
        system_ui.restore_notification_suppression(DndSnapshot(session.initial.dnd_enabled, session.initial.hide_all))
        observed = system_ui.snapshot_notification_suppression()
        result["observed"] = {"dnd_enabled": observed.enabled, "hide_all": observed.hide_all}
        if result["observed"] != target:
            raise CliError("notification restoration did not match initial state")
        result["status"] = "restored"
    except (Exception, KeyboardInterrupt) as error:
        result.update(status="failed", error_type=type(error).__name__, error=str(error) or type(error).__name__)
    result.update(completed_at=_now(), **_timing(started, "notification_restoration"))
    journal.record_action("session_notification_restoration", status=result["status"], details=result)
    return result


def _execute_finalize(connection=None) -> tuple[dict[str, object], int]:
    root = Path.cwd()
    session_store = SessionStore(root / "runs")
    session = session_store.load()
    journal = EventJournal(
        session_store.archive_root / f"{session.session_id}-finalize-{_stamp()}.events.jsonl",
        {"device_session_id": session.session_id, "serial": session.serial},
    )

    restoration = None
    wifi_restoration = None

    def query():
        nonlocal restoration, wifi_restoration
        serial, device = connection or _connected_device()
        if serial != session.serial:
            raise CliError("active session belongs to a different device")
        profiles_root = root / "profiles"
        profile = _resolve_system_ui_profile(ProfileStore(profiles_root), profiles_root, device)
        if profile.profile_id != session.system_ui_profile:
            raise CliError("active session uses a different System UI Profile")
        system_ui = SystemUIRuntime(device, profile, journal)
        wifi_restoration = _restore_session_wifi(system_ui, session, journal)
        restoration = _restore_session_notifications(system_ui, session, journal)
        return system_ui.snapshot_environment()

    final_state = capture_environment(query, session.last_confirmed_state)
    if wifi_restoration is None:
        wifi_restoration = {
            "status": "not_performed", "at": _now(), "target": {"wifi_enabled": session.initial.wifi_enabled},
            "error_type": final_state.get("error_type"), "error": final_state.get("error"),
        }
        journal.record_action("session_wifi_restoration", status="not_performed", details=wifi_restoration)
    if restoration is None:
        restoration = {
            "status": "not_performed", "at": _now(),
            "target": {"dnd_enabled": session.initial.dnd_enabled, "hide_all": session.initial.hide_all},
            "error_type": final_state.get("error_type"), "error": final_state.get("error"),
        }
        journal.record_action("session_notification_restoration", status="not_performed", details=restoration)
    automation = dict(session.automation or {})
    final_automation = {}
    if connection is not None:
        final_automation = automation["final"] = _automation_snapshot(connection[1])
    journal.record_action("device_final_state", status=final_state["status"], details=final_state)
    if wifi_restoration.get("error_type") == "KeyboardInterrupt":
        session = replace(session, interruption=session.interruption or {
            "at": _now(), "reason": "KeyboardInterrupt", "stage": "wifi_restoration",
        })
    elif restoration.get("error_type") == "KeyboardInterrupt":
        session = replace(session, interruption=session.interruption or {
            "at": _now(), "reason": "KeyboardInterrupt", "stage": "notification_restoration",
        })
    elif final_state.get("error_type") == "KeyboardInterrupt":
        session = replace(session, interruption=session.interruption or {
            "at": final_state["query_failed_at"], "reason": "KeyboardInterrupt",
        })
    elif final_automation.get("error_type") == "KeyboardInterrupt":
        session = replace(session, interruption=session.interruption or {
            "at": final_automation["queried_at"], "reason": final_automation["error"],
        })
    execution_summary = dict(Counter(str(run.get("status", "unknown")) for run in session.executions))
    final_status = (
        "interrupted" if session.interruption is not None else
        "partial" if any(status != "complete" for status in execution_summary) else "complete"
    )
    finalized = session.with_status(
        final_status,
        finalized_at=_now(),
        final_state=final_state,
        notification_restoration=restoration,
        wifi_restoration=wifi_restoration,
        automation=automation or None,
        last_confirmed_state=(
            final_state if final_state["status"] == "observed" else session.last_confirmed_state
        ),
    )
    archive_path = session_store.archive(finalized)
    return {
        "status": final_status,
        "execution_summary": execution_summary,
        "executions": [
            {key: run[key] for key in (
                "run_id", "app_id", "method", "acquisition_environment", "status", "reason", "error",
                "acquisition_summary", "acquisition_record", "archive_path",
            ) if key in run}
            for run in session.executions
        ],
        "session_id": finalized.session_id,
        "serial": finalized.serial,
        "final_state": final_state,
        "notification_restoration": restoration,
        "wifi_restoration": wifi_restoration,
        "session_record": str(archive_path),
        "session_hash": str(archive_path.parent / "session.sha256"),
    }, 0 if (final_state["status"] == "observed" and final_status == "complete"
             and restoration["status"] == "restored" and wifi_restoration["status"] == "restored"
             and final_state["observed"]["wifi_enabled"] == session.initial.wifi_enabled) else 1


def _set_plan_status(
    plan: tuple[dict[str, object], ...],
    run_id: str,
    status: str,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {**entry, "status": status} if entry["run_id"] == run_id else entry
        for entry in plan
    )


def _execute_acquire(
    app_ids: tuple[str, ...],
    authorization: dict | None = None,
    *, configuration: dict | None = None, serial: str | None = None,
) -> tuple[dict[str, object], int]:
    if len(app_ids) != len(set(app_ids)):
        raise CliError("acquisition applications must not contain duplicates")
    try:
        if not isinstance(authorization, dict) or authorization_record(authorization.get("document")) != authorization:
            raise ValueError("authorization record is missing or invalid")
    except (ValueError, TypeError) as error:
        raise CliError(f"invalid authorization: {error}") from error
    if configuration is not None:
        try:
            validate_configuration_link(configuration, authorization)
        except (ValueError, TypeError) as error:
            raise CliError(f"invalid session configuration: {error}") from error
    root = Path.cwd()
    runs_root = root / "runs"
    store = SessionStore(runs_root)
    if store.active_path.exists():
        raise CliError("an active device session already exists")
    serial, preflight_device = _connected_device(automation=False, expected_serial=serial)
    profiles_root = root / "profiles"
    profile_store = ProfileStore(profiles_root)
    resolutions = _match_installed_apps(profile_store, profiles_root, preflight_device.installed_packages(),
                                       preflight_device.shell("getprop ro.build.version.release").strip())
    available = {resolution.app_id for resolution in resolutions if resolution.profile is not None}
    if any(app_id not in available for app_id in app_ids):
        raise CliError("no applicable Profile for selected applications")
    session_id = f"session-{_stamp()}"
    plan = authorize_plan(_build_execution_plan(session_id, app_ids, resolutions), authorization)
    if configuration is not None:
        plan = tuple({**entry, "configuration_document_sha256": configuration["document_sha256"]} for entry in plan)
    if not plan:
        raise CliError("acquisition plan is empty")
    if not any(entry["status"] == "planned" for entry in plan):
        decision = {"record_kind": "authorization_preflight", "status": "not_performed", "at": _now(),
                    "serial": serial, "authorization": authorization, "execution_plan": plan}
        if configuration is not None:
            decision["configuration"] = configuration
        path = runs_root / f"{session_id}-not-performed.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            json.dump(decision, stream, ensure_ascii=False, indent=2)
        path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
        return {"status": "not_performed", "reason": "no_authorized_executions", "decision_record": str(path)}, 1
    connection = _connected_device(expected_serial=serial)
    _, prepare_code = _execute_prepare(authorization, connection, session_id, configuration=configuration)
    if prepare_code:
        raise CliError("device preparation failed")
    session = store.load()
    operation_error: BaseException | None = None
    interrupted = False
    acquisition_environment = None
    system_ui = None
    transition_started = None
    try:
        serial, device = connection
        if serial != session.serial:
            raise CliError("prepared session belongs to a different device")
        session = replace(session, installed_apps=tuple(_resolution_document(resolution) for resolution in resolutions))
        session = session.with_status(
            "planned",
            selected_apps=app_ids,
            execution_plan=plan,
        )
        store.save(session)

        system_ui = SystemUIRuntime(
            device,
            _resolve_system_ui_profile(profile_store, profiles_root, device),
            EventJournal(
                store.archive_root / f"{session.session_id}-environment.events.jsonl",
                {"device_session_id": session.session_id, "serial": serial},
            ),
        )
        for acquisition_environment in ("device_only", "controlled_online"):
            environment_entries = [
                entry for entry in plan if entry["acquisition_environment"] == acquisition_environment and entry["status"] == "planned"
            ]
            if not environment_entries:
                continue
            for entry in environment_entries:
                current = _resolve_app_profile(profile_store, profiles_root, device, str(entry["app_id"]))
                _execution_grant(session, current, str(entry["app_id"]), Route(entry["method"]), acquisition_environment, entry["run_id"])
            transition_started = perf_counter()
            if acquisition_environment == "controlled_online":
                system_ui.verify_prepared_environment(None)
            system_ui.apply_acquisition_environment(acquisition_environment)
            observed = system_ui.verify_prepared_environment(acquisition_environment)
            state = capture_environment(lambda: observed)
            transition_timing = _timing(transition_started, "environment_transition")
            transition_started = None
            session = session.with_status(
                "acquiring",
                last_confirmed_state=state,
                environment_transitions=(
                    *session.environment_transitions,
                    {
                        "acquisition_environment": acquisition_environment,
                        "status": "started",
                        "at": _now(),
                        "state": state,
                        **transition_timing,
                    },
                ),
            )
            store.save(session)
            for entry in environment_entries:
                run_id = str(entry["run_id"])
                execution_started = perf_counter()
                started = {
                    "order": entry["order"],
                    "run_id": run_id,
                    "app_id": entry["app_id"],
                    "method": entry["method"],
                    "acquisition_environment": acquisition_environment,
                    "status": "running",
                    "started_at": _now(),
                }
                session = replace(
                    session,
                    execution_plan=_set_plan_status(
                        session.execution_plan, run_id, "running"
                    ),
                    executions=(*session.executions, started),
                )
                store.save(session)
                try:
                    payload, exit_code = _execute_collect(
                        str(entry["app_id"]),
                        str(entry["method"]),
                        acquisition_environment,
                        run_id=run_id,
                        connection=connection,
                    )
                except (Exception, KeyboardInterrupt) as error:
                    operation_error = error
                    payload = {
                        "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                        "reason": str(error) or type(error).__name__,
                    }
                    exit_code = 2
                run_status = str(payload["status"])
                finished = {
                    **started,
                    "status": run_status,
                    "completed_at": _now(),
                    **_timing(execution_started, "execution_including_packaging"),
                }
                for name in (
                    "archive_path",
                    "archive_size",
                    "sha256",
                    "reason",
                    "error",
                    "acquisition_summary",
                    "acquisition_record",
                    "pending_downloads",
                ):
                    if name in payload:
                        finished[name] = payload[name]
                session = replace(
                    session,
                    execution_plan=_set_plan_status(
                        session.execution_plan, run_id, run_status
                    ),
                    executions=(*session.executions[:-1], finished),
                )
                run_state = payload.get("final_state")
                if isinstance(run_state, dict):
                    confirmed = run_state if run_state.get("status") == "observed" else run_state.get("last_confirmed")
                    if confirmed is not None:
                        session = replace(session, last_confirmed_state=confirmed)
                if run_status in {"failed", "interrupted"} or exit_code == 2:
                    remaining = tuple(
                        str(candidate["run_id"])
                        for candidate in session.execution_plan
                        if candidate["status"] == "planned"
                    )
                    session = session.with_status(
                        "interrupted",
                        interruption={
                            "at": _now(),
                            "acquisition_environment": acquisition_environment,
                            "run_id": run_id,
                            "reason": payload.get("reason")
                            or payload.get("error")
                            or "acquisition run failed",
                            "remaining_run_ids": remaining,
                        },
                    )
                    interrupted = True
                store.save(session)
                if interrupted:
                    break
            # Reuse the last run's observation and its original timestamp.
            # Packaging does not change the device environment.
            state = run_state if isinstance(run_state, dict) else capture_environment(
                system_ui.snapshot_environment, session.last_confirmed_state
            )
            if state.get("error_type") == "KeyboardInterrupt":
                raise KeyboardInterrupt()
            session = replace(
                session,
                last_confirmed_state=state if state["status"] == "observed" else session.last_confirmed_state,
                environment_transitions=(
                    *session.environment_transitions,
                    {
                        "acquisition_environment": acquisition_environment,
                        "status": "interrupted" if interrupted else "completed",
                        "at": _now(),
                        "state": state,
                    },
                ),
            )
            store.save(session)
            if interrupted:
                break
    except (Exception, KeyboardInterrupt) as error:
        operation_error = error
        if system_ui is not None and acquisition_environment is not None:
            state = capture_environment(system_ui.snapshot_environment, session.last_confirmed_state)
            session = replace(
                session,
                last_confirmed_state=state if state["status"] == "observed" else session.last_confirmed_state,
                environment_transitions=(*session.environment_transitions, {
                    "acquisition_environment": acquisition_environment,
                    "status": "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                    "at": _now(), "state": state,
                    "error": str(error) or type(error).__name__,
                    **(_timing(transition_started, "environment_transition")
                       if transition_started is not None else {}),
                }),
            )
        session = session.with_status(
            "interrupted",
            interruption={
                "at": _now(),
                "acquisition_environment": acquisition_environment,
                "run_id": None,
                "reason": str(error) or type(error).__name__,
                "remaining_run_ids": tuple(
                    str(entry["run_id"])
                    for entry in session.execution_plan
                    if entry["status"] == "planned"
                ),
            },
        )
        store.save(session)
    finally:
        final_payload, final_code = _execute_finalize(connection)

    if operation_error is not None:
        final_payload["error"] = str(operation_error) or type(operation_error).__name__
    if interrupted and final_code == 0:
        final_payload["status"] = "interrupted"
    acquisition_code = 1 if interrupted or operation_error else 0
    return final_payload, max(acquisition_code, final_code)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            payload, exit_code = _execute_inspect(args.serial)
        elif args.command in {"configure", "authorize"}:
            try:
                payload, exit_code = _execute_configure(args)
            except (EOFError, KeyboardInterrupt):
                payload, exit_code = {"status": "cancelled", "reason": "operator_interrupted"}, 1
            except (OSError, UnicodeError, ValueError) as error:
                raise CliError(f"session configuration creation failed: {error}") from error
        else:
            try:
                config = load_configuration(args.config or args.authorization)
                authority = authorization_record(authorization_from_configuration(config["document"]))
            except (OSError, UnicodeError, ValueError) as error:
                raise CliError(f"invalid session configuration / authorization: {error}") from error
            if "authorization" in config["document"]:
                payload, exit_code = _execute_acquire(tuple(args.apps), authority, configuration=config, serial=args.serial)
            else:
                payload, exit_code = _execute_acquire(tuple(args.apps), authority, serial=args.serial)
    except (CliError, ProfileError, SessionError, PackageError) as error:
        payload = {
            "status": "failed",
            "command": args.command,
            "error": str(error),
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code
