from __future__ import annotations

import argparse
import json
import platform
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .apps.notesnook import collect as collect_notesnook
from .apps.notion import collect as collect_notion
from .apps.chrome import collect as collect_chrome
from .apps.google_drive import collect as collect_google_drive
from .apps.samsung_browser import collect as collect_samsung_browser
from .apps.telegram import collect as collect_telegram
from .apps.whatsapp import collect as collect_whatsapp
from .device import AndroidDevice, InstalledPackage
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
from .system_ui import SystemUIRuntime


APP_PACKAGES = {
    "chrome": "com.android.chrome",
    "google_drive": "com.google.android.apps.docs",
    "samsung_browser": "com.sec.android.app.sbrowser",
    "telegram": "org.telegram.messenger",
    "whatsapp": "com.whatsapp",
    "notion": "notion.id",
    "notesnook": "com.streetwriters.notesnook",
}
COLLECTORS = {
    "chrome": collect_chrome,
    "google_drive": collect_google_drive,
    "samsung_browser": collect_samsung_browser,
    "telegram": collect_telegram,
    "whatsapp": collect_whatsapp,
    "notion": collect_notion,
    "notesnook": collect_notesnook,
}


class CliError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileResolution:
    app_id: str | None
    package_name: str
    version_name: str | None
    status: str
    profile: AppProfile | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m aura")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    acquire = commands.add_parser("acquire")
    acquire.add_argument("apps", nargs="+", choices=tuple(APP_PACKAGES))
    return parser


def _single_serial(serials: list[str]) -> str:
    if len(serials) != 1:
        raise CliError("exactly one ADB device is required")
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
                order = len(plan) + 1
                environment_name = acquisition_environment.replace("_", "-")
                plan.append({
                    "order": order,
                    "run_id": (
                        f"{session_id}-{order:03d}-{app_id}-"
                        f"{method.value}-{environment_name}"
                    ),
                    "app_id": app_id,
                    "package_name": resolution.package_name,
                    "installed_app_version": resolution.version_name,
                    "profile_app_version": profile.app_version,
                    "profile_status": resolution.status,
                    "method": method.value,
                    "acquisition_environment": acquisition_environment,
                    "target_ids": targets,
                    "status": "planned",
                })
    return tuple(plan)


def _execute_inspect() -> tuple[dict[str, object], int]:
    serial, device = _connected_device()
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
    targets = {
        "chrome": {
            "kind": "browser",
            "ref": "browser.chrome.all",
        },
        "google_drive": {
            "kind": "cloud_storage",
            "ref": "drive.google.my-drive",
        },
        "samsung_browser": {
            "kind": "browser",
            "ref": "browser.samsung-browser.all",
        },
        "telegram": {
            "kind": "account",
            "ref": "account.telegram.all",
        },
        "whatsapp": {
            "kind": "chat_list",
            "ref": "chat-list.whatsapp.active",
        },
        "notion": {
            "kind": "account",
            "ref": "account.notion.all",
        },
        "notesnook": {
            "kind": "container",
            "ref": "notes.notesnook.all",
        },
    }
    if acquisition_environment not in {"device_only", "controlled_online"}:
        raise CliError(
            f"unknown acquisition environment: {acquisition_environment!r}"
        )
    condition: dict[str, object] = {
        "target": targets[app_id],
        "notifications": "suppress_all",
        "airplane_mode": "enabled",
        "acquisition_environment": acquisition_environment,
    }
    if app_id == "whatsapp" and route is Route.EXPORT:
        receiver = (host_name if host_name is not None else platform.node()).strip()
        documents = (home or Path.home()) / "Documents"
        if not receiver:
            raise CliError("Bluetooth host name is unavailable")
        if not documents.is_dir():
            raise CliError(
                f"Bluetooth receive directory is unavailable: {documents}"
            )
        condition["bluetooth"] = {
            "target_name": receiver,
            "receiver_label": receiver,
            "receive_dir": str(documents),
        }
    return condition


def _connected_device():
    import adbutils

    serial = _single_serial([
        device.serial
        for device in adbutils.adb.device_list()
        if device.serial
    ])
    return serial, AndroidDevice.connect(serial)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _execute_prepare() -> tuple[dict[str, object], int]:
    root = Path.cwd()
    runs_root = root / "runs"
    session_store = SessionStore(runs_root)
    if session_store.active_path.exists():
        raise CliError("an active device session already exists")
    serial, device = _connected_device()
    profiles_root = root / "profiles"
    store = ProfileStore(profiles_root)
    system_ui_profile = _resolve_system_ui_profile(
        store,
        profiles_root,
        device,
    )
    session_id = f"session-{_stamp()}"
    journal = EventJournal(
        runs_root / "sessions" / f"{session_id}-prepare.events.jsonl",
        {
            "device_session_id": session_id,
            "serial": serial,
            "system_ui_profile": system_ui_profile.profile_id,
        },
    )
    system_ui = SystemUIRuntime(device, system_ui_profile, journal)
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
        final_state = capture_environment(system_ui.snapshot_environment, session.last_confirmed_state)
        interruption_reason = (
            reason if isinstance(error, KeyboardInterrupt)
            else final_state["error"] if final_state.get("error_type") == "KeyboardInterrupt"
            else None
        )
        session_store.archive(
            session.with_status(
                "interrupted" if interruption_reason is not None else "prepare_failed",
                finalized_at=_now(),
                preparation={"status": "failed", "at": _now(), "error": reason},
                final_state=final_state,
                last_confirmed_state=(final_state if final_state["status"] == "observed" else session.last_confirmed_state),
                interruption={"at": _now(), "reason": interruption_reason} if interruption_reason is not None else None,
                error=reason,
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
        preparation={**verified, "status": "verified"},
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


def _execute_collect(
    app_id: str,
    route_value: str,
    acquisition_environment: str,
    *,
    run_id: str | None = None,
) -> tuple[dict[str, object], int]:
    route = Route(route_value)
    serial, device = _connected_device()
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
            _condition(app_id, route, acquisition_environment),
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
    final_state = manifest["run"].get("final_state")
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
        **profile_context,
    }, 0 if status == "complete" else 1


def _execute_finalize() -> tuple[dict[str, object], int]:
    root = Path.cwd()
    session_store = SessionStore(root / "runs")
    session = session_store.load()
    journal = EventJournal(
        session_store.archive_root / f"{session.session_id}-finalize-{_stamp()}.events.jsonl",
        {"device_session_id": session.session_id, "serial": session.serial},
    )

    def query():
        serial, device = _connected_device()
        if serial != session.serial:
            raise CliError("active session belongs to a different device")
        profiles_root = root / "profiles"
        profile = _resolve_system_ui_profile(ProfileStore(profiles_root), profiles_root, device)
        if profile.profile_id != session.system_ui_profile:
            raise CliError("active session uses a different System UI Profile")
        return SystemUIRuntime(device, profile, journal).snapshot_environment()

    final_state = capture_environment(query, session.last_confirmed_state)
    journal.record_action("device_final_state", status=final_state["status"], details=final_state)
    if final_state.get("error_type") == "KeyboardInterrupt":
        session = replace(session, interruption=session.interruption or {
            "at": final_state["query_failed_at"], "reason": "KeyboardInterrupt",
        })
    final_status = "interrupted" if session.interruption is not None else "complete"
    finalized = session.with_status(
        final_status,
        finalized_at=_now(),
        final_state=final_state,
        last_confirmed_state=(
            final_state if final_state["status"] == "observed" else session.last_confirmed_state
        ),
    )
    archive_path = session_store.archive(finalized)
    return {
        "status": final_status,
        "session_id": finalized.session_id,
        "serial": finalized.serial,
        "final_state": final_state,
        "session_record": str(archive_path),
        "session_hash": str(archive_path.parent / "session.sha256"),
    }, 0 if final_state["status"] == "observed" else 1


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
) -> tuple[dict[str, object], int]:
    if len(app_ids) != len(set(app_ids)):
        raise CliError("acquisition applications must not contain duplicates")
    _, prepare_code = _execute_prepare()
    if prepare_code:
        raise CliError("device preparation failed")

    root = Path.cwd()
    runs_root = root / "runs"
    store = SessionStore(runs_root)
    session = store.load()
    operation_error: BaseException | None = None
    interrupted = False
    acquisition_environment = None
    system_ui = None
    try:
        serial, device = _connected_device()
        if serial != session.serial:
            raise CliError("prepared session belongs to a different device")
        profiles_root = root / "profiles"
        profile_store = ProfileStore(profiles_root)
        resolutions = _match_installed_apps(
            profile_store,
            profiles_root,
            device.installed_packages(),
            device.shell("getprop ro.build.version.release").strip(),
        )
        installed = tuple(
            _resolution_document(resolution) for resolution in resolutions
        )
        session = replace(session, installed_apps=installed)
        store.save(session)

        available = {
            resolution.app_id: resolution
            for resolution in resolutions
            if resolution.app_id is not None
            and resolution.profile is not None
        }
        unavailable = [
            app_id for app_id in app_ids if app_id not in available
        ]
        if unavailable:
            raise CliError(
                "no applicable Profile for: " + ", ".join(unavailable)
            )
        plan = _build_execution_plan(session.session_id, app_ids, resolutions)
        if not plan:
            raise CliError("acquisition plan is empty")
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
                entry for entry in plan if entry["acquisition_environment"] == acquisition_environment
            ]
            if not environment_entries:
                continue
            system_ui.verify_prepared_environment(None)
            system_ui.apply_acquisition_environment(acquisition_environment)
            observed = system_ui.verify_prepared_environment(acquisition_environment)
            state = capture_environment(lambda: observed)
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
                    },
                ),
            )
            store.save(session)
            for entry in environment_entries:
                run_id = str(entry["run_id"])
                started = {
                    "order": len(session.executions) + 1,
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
                }
                for name in (
                    "archive_path",
                    "archive_size",
                    "sha256",
                    "reason",
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
            state = capture_environment(system_ui.snapshot_environment, session.last_confirmed_state)
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
        final_payload, final_code = _execute_finalize()

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
            payload, exit_code = _execute_inspect()
        else:
            payload, exit_code = _execute_acquire(tuple(args.apps))
    except (CliError, ProfileError, SessionError, PackageError) as error:
        payload = {
            "status": "failed",
            "command": args.command,
            "error": str(error),
        }
        exit_code = 2
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code
