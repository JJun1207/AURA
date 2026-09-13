"""Google Drive UI acquisition."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
)


PREFIX = "com.google.android.apps.docs:id/"
_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_UI_KEYS = {
    "resource_id": "resourceId",
    "text": "text",
    "content_description": "description",
    "class_name": "className",
}
_LISTING_BLOCKERS = {"scanner_fab", "fab_compose_view", "bottom_navigation"}


def _id(node: ET.Element) -> str:
    return node.get("resource-id", "").removeprefix(PREFIX)


def _text(node: ET.Element) -> str:
    return node.get("text", "").strip()


def _label(node: ET.Element) -> str:
    return (_text(node) or node.get("content-desc", "").strip())


def _value(root: ET.Element, resource_id: str) -> str:
    node = next((item for item in root.iter() if _id(item) == resource_id), None)
    return "" if node is None else _label(node)


def _direct_value(root: ET.Element, resource_id: str) -> str:
    node = next((item for item in root if _id(item) == resource_id), None)
    return "" if node is None else _label(node)


def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        raise ValueError("Google Drive row bounds are unavailable")
    return tuple(map(int, match.groups()))


def _overlaps(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> bool:
    return (
        first[0] < second[2]
        and first[2] > second[0]
        and first[1] < second[3]
        and first[3] > second[1]
    )


@dataclass(frozen=True)
class DriveRow:
    name: str
    kind: str
    displayed_type: str
    displayed_modified: str
    row_bounds: tuple[int, int, int, int]
    menu_bounds: tuple[int, int, int, int]

    def as_record(self, parent_path: tuple[str, ...]) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "drive_path": [*parent_path, self.name],
            "displayed_type": self.displayed_type,
            "displayed_modified": self.displayed_modified,
        }


@dataclass(frozen=True)
class CollectionScope:
    collect_activity: bool
    download_files: bool


def collection_scope(acquisition_environment: str) -> CollectionScope:
    if acquisition_environment == "device_only":
        return CollectionScope(False, False)
    if acquisition_environment == "controlled_online":
        return CollectionScope(True, True)
    raise ValueError(f"unsupported Google Drive acquisition_environment: {acquisition_environment!r}")


def parse_listing(xml: str, *, include_obscured: bool = False) -> list[DriveRow]:
    root = ET.fromstring(xml)
    blockers = [
        _bounds(node)
        for node in root.iter()
        if _id(node) in _LISTING_BLOCKERS
    ]
    rows = []
    for node in root.iter():
        if _id(node) == "more_actions_button":
            continue
        menu = next(
            (item for item in node.iter() if _id(item) == "more_actions_button"),
            None,
        )
        if node.get("clickable") != "true" or menu is None:
            continue
        menu_label = next(
            (
                item.get("content-desc", "").strip()
                for item in menu.iter()
                if item.get("content-desc", "").strip().startswith(
                    "More actions for "
                )
            ),
            "",
        )
        if not menu_label:
            continue
        menu_bounds = _bounds(menu)
        if not include_obscured and any(_overlaps(menu_bounds, blocker) for blocker in blockers):
            continue
        name = menu_label.removeprefix("More actions for ")
        labels = [
            item.get("content-desc", "").strip()
            for item in node.iter()
            if item.get("content-desc", "").strip()
            and item.get("content-desc", "").strip() not in {name, menu_label}
        ]
        displayed_type = labels[0] if labels else ""
        texts = [_text(item) for item in node.iter() if _text(item)]
        modified = ""
        if "Modified" in texts:
            index = texts.index("Modified")
            if index + 1 < len(texts):
                modified = texts[index + 1]
        rows.append(
            DriveRow(
                name=name,
                kind="folder" if displayed_type == "Folder" else "file",
                displayed_type=displayed_type,
                displayed_modified=modified,
                row_bounds=_bounds(node),
                menu_bounds=menu_bounds,
            )
        )
    return rows


def _row_key(row: DriveRow) -> tuple[str, str, str]:
    return row.name, row.kind, row.displayed_modified


def _refresh_row(context, row: DriveRow, parent_path: tuple[str, ...] = ()) -> DriveRow:
    recheck_count = 0
    for recovery in range(2):
        hierarchy = context.device.hierarchy()
        obscured = [current for current in parse_listing(hierarchy, include_obscured=True)
                    if _row_key(current) == _row_key(row)]
        if not obscured:
            def row_loaded():
                nonlocal hierarchy, obscured, recheck_count
                recheck_count += 1
                hierarchy = context.device.hierarchy()
                obscured = [current for current in parse_listing(hierarchy, include_obscured=True)
                            if _row_key(current) == _row_key(row)]
                return bool(obscured)

            context.ui.wait_until(row_loaded)
        matches = [current for current in parse_listing(hierarchy)
                   if _row_key(current) == _row_key(row)]
        if len(matches) == len(obscured) == 1:
            if recheck_count:
                context.journal.record_action("google_drive_row_ready_after_wait", status="success",
                    context={"drive_path": [*parent_path, row.name], "item_kind": row.kind},
                    details={"recheck_count": recheck_count})
            return matches[0]
        if matches or len(obscured) != 1 or recovery:
            break
        # A dismissed download snackbar can move Drive's floating buttons over this row.
        context.ui.swipe("reveal_google_drive_obscured_row", "up",
                         context={"drive_path": [*parent_path, row.name], "item_kind": row.kind})
        time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
    raise RuntimeError("google_drive_row_not_uniquely_visible")


def parse_information(xml: str) -> dict[str, str]:
    root = ET.fromstring(xml)
    toolbar = next((node for node in root.iter() if _id(node) == "toolbar"), None)
    description = "" if toolbar is None else toolbar.get("content-desc", "").strip()
    prefix = "Showing item properties for "
    values = {
        "name": description.removeprefix(prefix) if description.startswith(prefix) else "",
        "type": _value(root, "kind_text"),
        "location": _value(root, "location_text"),
        "size": _value(root, "size_text"),
        "storage_used": _value(root, "quota_text"),
        "created": _value(root, "created_text"),
        "modified": _value(root, "modified_text"),
    }
    private = next((node for node in root.iter() if _id(node) == "private_acl"), None)
    if private is not None:
        values["access"] = next(
            (_text(node) for node in private.iter() if _text(node)),
            "",
        )
    return {key: value for key, value in values.items() if value}


def detail_metadata_complete(metadata: dict[str, str]) -> bool:
    return {"type", "location", "created", "modified"} <= metadata.keys()


def parse_activity(xml: str) -> tuple[list[dict[str, str]], str]:
    root = ET.fromstring(xml)
    records = []
    for node in root.iter():
        actor = _direct_value(node, "recent_event_username")
        event = _direct_value(node, "recent_event_eventType")
        if actor and event:
            records.append(
                {
                    "actor": actor,
                    "displayed_time": _direct_value(
                        node, "recent_event_timestamp"
                    ),
                    "event": event,
                }
            )
    return records, _value(root, "recents_status")


def activity_complete(status: str) -> bool:
    return status.startswith("No activity recorded before ")


def select_download(
    before: tuple[tuple[str, int, int], ...],
    after: tuple[tuple[str, int, int], ...],
    expected_name: str,
) -> tuple[str, int, int]:
    previous = {path: (size, mtime) for path, size, mtime in before}
    expected = PurePosixPath(expected_name)
    duplicate_names = (
        re.compile(
            rf"^{re.escape(expected.stem)} \(\d+\)"
            rf"{re.escape(expected.suffix)}$"
        ),
        re.compile(rf"^{re.escape(expected.name)} \(\d+\)$"),
        re.compile(
            rf"^{re.escape(expected.name)}(?: \(\d+\))?\.txt$"
        ),
    )
    candidates = [
        row
        for row in after
        if previous.get(row[0]) != row[1:]
            and (
                PurePosixPath(row[0]).name == expected.name
                or any(
                    pattern.fullmatch(PurePosixPath(row[0]).name)
                    for pattern in duplicate_names
                )
            )
    ]
    if len(candidates) != 1:
        raise RuntimeError("google_drive_download_inventory_ambiguous")
    return candidates[0]


def parse_file_inventory(
    output: str, download_root: str
) -> tuple[tuple[str, int, int], ...]:
    fields = output.split("\0")
    if fields[-1:] != [""]:
        raise RuntimeError("google_drive_download_inventory_invalid")
    fields.pop()
    if len(fields) % 3:
        raise RuntimeError("google_drive_download_inventory_invalid")
    root = PurePosixPath(download_root)
    records = []
    for index in range(0, len(fields), 3):
        path = PurePosixPath(fields[index])
        try:
            size = int(fields[index + 1])
            mtime = int(fields[index + 2])
        except ValueError:
            raise RuntimeError(
                "google_drive_download_inventory_invalid"
            ) from None
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not path.is_relative_to(root)
            or size < 0
            or mtime < 0
        ):
            raise RuntimeError("google_drive_download_inventory_invalid")
        records.append((path.as_posix(), size, mtime))
    if len({row[0] for row in records}) != len(records):
        raise RuntimeError("google_drive_download_inventory_invalid")
    return tuple(sorted(records))


def _selector(context, name: str) -> dict[str, object]:
    try:
        entry = context.app_profile.selectors[name]
        spec = entry["selector"]
        selector = {
            _UI_KEYS[spec["kind"]]: spec["value"],
            "packageName": spec["owner_package"],
        }
        selector.update(
            {
                _UI_KEYS[key]: value
                for key, value in entry.get("constraints", {}).items()
            }
        )
        return selector
    except (KeyError, TypeError):
        raise RuntimeError(f"invalid Google Drive selector: {name}") from None


def _linked(
    context,
    name: str,
    attempt_id: str,
    source_action: ActionRef | None,
    linked: dict[str, object],
) -> ActionRef:
    return context.linked_action(
        name,
        attempt_id,
        source_action=source_action,
        context=linked,
    )


def _observe(
    context,
    attempt_id: str,
    prefix: str,
    source_action: ActionRef,
    linked: dict[str, object],
) -> tuple[str, ActionRef]:
    hierarchy = context.device.hierarchy()
    action = _linked(
        context,
        "observe_google_drive_ui",
        attempt_id,
        source_action,
        linked,
    )
    context.retain_observation(
        prefix,
        context.device.screenshot(),
        hierarchy.encode("utf-8"),
        attempt_id=attempt_id,
        action=action,
        context=linked,
    )
    return hierarchy, action


def _path_key(path: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join(path).encode("utf-8")).hexdigest()[:16]


def _write_json(
    context,
    relative_path: str,
    document: dict[str, object],
    *,
    attempt_id: str,
    action: ActionRef,
    linked: dict[str, object],
):
    return context.artifacts.write_bytes(
        relative_path,
        (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode(),
        kind="structured_data",
        attempt_id=attempt_id,
        action=action,
        context=linked,
    )


def _open_root(context, attempt_id: str) -> ActionRef:
    navigation = _selector(context, "google_drive.navigation.files")
    seen = set()
    source_action = None
    while not context.device.exists(navigation):
        hierarchy = context.device.hierarchy()
        signature = hashlib.sha256(hierarchy.encode()).hexdigest()
        if signature in seen:
            raise RuntimeError("google_drive_root_unavailable")
        seen.add(signature)
        source_action = context.ui.back("return_to_google_drive_navigation")
        time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
    opened = context.ui.click(
        "open_google_drive_files",
        navigation,
        expected=_selector(context, "google_drive.screen.list"),
    )
    return _linked(
        context,
        "google_drive_root_opened",
        attempt_id,
        opened,
        {"drive_path": [context.app_profile.parameters["root_label"]]},
    )


def _scroll_until(context, selector: dict[str, object], *, surface: str):
    seen = set()
    action = None
    while not context.device.exists(selector):
        hierarchy = context.device.hierarchy()
        signature = hashlib.sha256(hierarchy.encode()).hexdigest()
        if signature in seen:
            raise RuntimeError(f"google_drive_{surface}_control_unavailable")
        seen.add(signature)
        action = context.ui.swipe(
            f"scroll_google_drive_{surface}",
            "up",
            context={"surface": surface},
        )
        time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
    return action


def _open_menu(context, row: DriveRow, linked: dict[str, object]):
    action = context.ui.click_bounds(
        "open_google_drive_item_menu",
        row.menu_bounds,
        context=linked,
    )
    time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
    return action


def _collect_information(
    context,
    row: DriveRow,
    parent_path: tuple[str, ...],
    scope: CollectionScope,
    sequence: int,
) -> tuple[dict[str, object], ActionRef | None, bool]:
    path = (*parent_path, row.name)
    linked = {
        "drive_path": list(path),
        "item_kind": row.kind,
        "acquisition_environment": (
            "controlled_online" if scope.collect_activity else "device_only"
        ),
    }
    attempt_id = context.item_attempt(
        f"google-drive:metadata:{sequence:06d}",
        "google_drive.inventory",
        f"{row.kind}_metadata",
        source=row.as_record(parent_path),
        context=linked,
    )
    action = None
    try:
        menu_action = _open_menu(context, row, linked)
        _scroll_until(
            context,
            _selector(context, "google_drive.menu.view-information"),
            surface="item_menu",
        )
        opened = context.ui.click(
            "open_google_drive_information",
            _selector(context, "google_drive.menu.view-information"),
            expected=_selector(context, "google_drive.screen.information"),
            context=linked,
        )
        action = _linked(
            context,
            "google_drive_information_opened",
            attempt_id,
            opened,
            {**linked, "menu_action": menu_action.action_id},
        )
        prefix = f"google-drive/items/{sequence:06d}-{_path_key(path)}"
        hierarchy, action = _observe(
            context,
            attempt_id,
            f"{prefix}/information-screen",
            action,
            linked,
        )
        metadata = parse_information(hierarchy)
        activities, activity_status = parse_activity(hierarchy)
        activity_windows = 0
        seen = {hashlib.sha256(hierarchy.encode()).hexdigest()}
        while scope.collect_activity and not activity_complete(activity_status):
            swipe = context.ui.swipe(
                "scroll_google_drive_activity",
                "up",
                context=linked,
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
            current = context.device.hierarchy()
            signature = hashlib.sha256(current.encode()).hexdigest()
            if signature in seen:
                break
            seen.add(signature)
            activity_windows += 1
            current, action = _observe(
                context,
                attempt_id,
                f"{prefix}/activity-window-{activity_windows:04d}",
                _linked(
                    context,
                    "google_drive_activity_scrolled",
                    attempt_id,
                    swipe,
                    linked,
                ),
                linked,
            )
            window_records, activity_status = parse_activity(current)
            known = {
                (item["actor"], item["displayed_time"], item["event"])
                for item in activities
            }
            activities.extend(
                item
                for item in window_records
                if (item["actor"], item["displayed_time"], item["event"])
                not in known
            )
        if not scope.collect_activity:
            activities = []
            activity_status = "not_attempted_in_device_only"
        detail_status = (
            "acquired" if detail_metadata_complete(metadata) else "listing_only"
        )
        document = {
            **row.as_record(parent_path),
            "metadata": metadata,
            "detail_metadata_status": detail_status,
            "activity": activities,
            "activity_status": activity_status or "visible_records_collected",
        }
        action = _linked(
            context,
            "retain_google_drive_metadata",
            attempt_id,
            action,
            {**linked, "activity_count": len(activities)},
        )
        _write_json(
            context,
            f"{prefix}/metadata.json",
            document,
            attempt_id=attempt_id,
            action=action,
            linked=linked,
        )
        returned = context.ui.click(
            "return_from_google_drive_information",
            _selector(context, "google_drive.information.back"),
            expected=_selector(context, "google_drive.screen.list"),
            context=linked,
        )
        action = _linked(
            context,
            "google_drive_information_closed",
            attempt_id,
            returned,
            linked,
        )
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            (
                AcquisitionStatus.ACQUIRED
                if detail_status == "acquired"
                else AcquisitionStatus.PARTIAL
            ),
            reason=(
                None
                if detail_status == "acquired"
                else "google_drive_detail_metadata_limited"
            ),
            action=action,
            details={
                "metadata_fields": sorted(metadata),
                "detail_metadata_status": detail_status,
                "activity_count": len(activities),
                "activity_status": activity_status,
            },
        )
        return document, action, detail_status == "acquired"
    except Exception as error:
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.INTERRUPTED,
            AcquisitionStatus.PARTIAL if context.result_status(attempt_id) is not AcquisitionStatus.NOT_ACQUIRED else AcquisitionStatus.NOT_ACQUIRED,
            reason="google_drive_metadata_unavailable",
            action=action,
            details={"error_type": type(error).__name__, "error": str(error)},
        )
        return {
            **row.as_record(parent_path),
            "metadata": {},
            "activity": [],
            "activity_status": "unavailable",
            "reason": "google_drive_metadata_unavailable",
        }, action, False


def _inventory_command(root: str) -> str:
    emit = (
        'stat -c "%s %Y" "$@" | '
        'while IFS=" " read -r size mtime; do '
        'file=$1; shift; '
        'printf "%s\\000%s\\000%s\\000" "$file" "$size" "$mtime"; '
        "done"
    )
    quoted = shlex.quote(root)
    return (
        f'[ ! -d {quoted} ] || find {quoted} -mindepth 1 -type f '
        f"-exec sh -c {shlex.quote(emit)} sh {{}} +"
    )


def _scan_downloads(context) -> tuple[tuple[str, int, int], ...]:
    root = context.app_profile.parameters["download_root"]
    return parse_file_inventory(
        context.device.shell(_inventory_command(root)), root
    )


def _await_download(context, baseline, expected_name):
    probes = int(context.app_profile.parameters["inventory_probes"])
    interval = context.app_profile.timings["inventory_probe_interval"]
    previous = None
    for probe in range(probes):
        if probe:
            time.sleep(interval)
        try:
            candidate = select_download(
                baseline, _scan_downloads(context), expected_name
            )
        except RuntimeError:
            candidate = None
        if candidate is not None and candidate == previous:
            return candidate
        previous = candidate
    raise RuntimeError("google_drive_download_did_not_settle")


def _download_file(
    context,
    row: DriveRow,
    parent_path: tuple[str, ...],
    sequence: int,
) -> tuple[dict[str, object], ActionRef | None, bool]:
    path = (*parent_path, row.name)
    linked = {"drive_path": list(path), "item_kind": "file"}
    attempt_id = context.item_attempt(
        f"google-drive:file:{sequence:06d}",
        "google_drive.files",
        "file_content",
        source=row.as_record(parent_path),
        context=linked,
    )
    action = None
    menu_open = False
    try:
        baseline = _scan_downloads(context)
        menu_action = _open_menu(context, row, linked)
        menu_open = True
        _scroll_until(
            context,
            _selector(context, "google_drive.menu.download"),
            surface="download_menu",
        )
        action = _linked(
            context,
            "observe_google_drive_download_control",
            attempt_id,
            menu_action,
            linked,
        )
        context.retain_observation(
            f"google-drive/files/{sequence:06d}-{_path_key(path)}/download-menu-screen",
            context.device.screenshot(),
            context.device.hierarchy().encode("utf-8"),
            attempt_id=attempt_id,
            action=action,
            context=linked,
        )
        clicked = context.ui.click(
            "download_google_drive_file",
            _selector(context, "google_drive.menu.download"),
            context=linked,
        )
        menu_open = False
        action = _linked(
            context,
            "google_drive_download_started",
            attempt_id,
            clicked,
            linked,
        )
        remote_path, expected_size, mtime = _await_download(
            context, baseline, row.name
        )
        original_name = PurePosixPath(remote_path).name
        with TemporaryDirectory() as directory:
            local = Path(directory) / "artifact"
            context.device.pull(remote_path, local)
            if local.stat().st_size != expected_size:
                raise RuntimeError("google_drive_download_size_changed")
            action = _linked(
                context,
                "retain_google_drive_download",
                attempt_id,
                action,
                {**linked, "device_source_path": remote_path},
            )
            artifact = context.artifacts.retain_file(
                local,
                f"google-drive/files/{sequence:06d}-{_path_key(path)}/{original_name}",
                kind="acquired_file",
                attempt_id=attempt_id,
                action=action,
                context={**linked, "device_source_path": remote_path},
            )
        record = {
            "drive_path": list(path),
            "displayed_name": row.name,
            "device_source_path": remote_path,
            "retained_path": artifact.relative_path,
            "size": artifact.size,
            "sha256": artifact.sha256,
            "device_mtime": mtime,
            "status": "acquired",
        }
        _write_json(
            context,
            f"google-drive/files/{sequence:06d}-{_path_key(path)}/download.json",
            record,
            attempt_id=attempt_id,
            action=action,
            linked=linked,
        )
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            AcquisitionStatus.ACQUIRED,
            action=action,
            details={
                "retained_path": artifact.relative_path,
                "size": artifact.size,
                "sha256": artifact.sha256,
            },
        )
        return record, action, True
    except Exception as error:
        recovery_error = None
        if menu_open:
            try:
                _, action = _observe(
                    context, attempt_id,
                    f"google-drive/files/{sequence:06d}-{_path_key(path)}/download-unavailable-screen",
                    action, linked,
                )
                returned = context.ui.back(
                    "dismiss_google_drive_download_menu",
                    expected=_selector(context, "google_drive.screen.list"),
                    context=linked,
                )
                action = _linked(context, "google_drive_download_menu_closed", attempt_id, returned, linked)
            except Exception as failure:
                recovery_error = str(failure) or type(failure).__name__
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.INTERRUPTED,
            AcquisitionStatus.PARTIAL if context.result_status(attempt_id) is not AcquisitionStatus.NOT_ACQUIRED else AcquisitionStatus.NOT_ACQUIRED,
            reason="google_drive_download_failed",
            action=action,
            details={"error_type": type(error).__name__, "error": str(error), "menu_recovery_error": recovery_error},
        )
        return {
            "drive_path": list(path),
            "displayed_name": row.name,
            "status": "not_acquired",
            "reason": "google_drive_download_failed",
        }, action, False


def _walk_folder(
    context,
    parent_path: tuple[str, ...],
    inventory_attempt: str,
    scope: CollectionScope,
    source_action: ActionRef,
    records: list[dict[str, object]],
    downloads: list[dict[str, object]],
    counters: dict[str, int],
) -> ActionRef:
    seen_hierarchies = set()
    processed = set()
    window = 0
    action = source_action
    while True:
        window += 1
        hierarchy, action = _observe(
            context,
            inventory_attempt,
            f"google-drive/listings/{_path_key(parent_path)}/window-{window:04d}",
            action,
            {"drive_path": list(parent_path), "window": window},
        )
        row = next(
            (
                row
                for row in parse_listing(hierarchy)
                if _row_key(row) not in processed
            ),
            None,
        )
        if row is not None:
            processed.add(_row_key(row))
            counters["items"] += 1
            if row.kind == "folder":
                counters["folders"] += 1
            else:
                counters["files"] += 1
            record, row_action, acquired = _collect_information(
                context,
                row,
                parent_path,
                scope,
                counters["items"],
            )
            records.append(record)
            counters["metadata_failures"] += 0 if acquired else 1
            if row_action is not None:
                action = row_action
            if not acquired and not context.device.exists(_selector(context, "google_drive.screen.list")):
                raise RuntimeError("google_drive_listing_not_restored_after_metadata")
            row = _refresh_row(context, row, parent_path)
            if row.kind == "file" and scope.download_files:
                downloaded, row_action, acquired = _download_file(
                    context,
                    row,
                    parent_path,
                    counters["items"],
                )
                downloads.append(downloaded)
                counters["download_failures"] += 0 if acquired else 1
                if row_action is not None:
                    action = row_action
                if not acquired and not context.device.exists(_selector(context, "google_drive.screen.list")):
                    raise RuntimeError("google_drive_listing_not_restored_after_download")
            if row.kind != "folder":
                continue
            opened = context.ui.click_bounds(
                "open_google_drive_folder",
                row.row_bounds,
                context={"drive_path": [*parent_path, row.name]},
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
            if not context.device.exists(
                _selector(context, "google_drive.screen.list")
            ):
                record["children_status"] = "unavailable"
                counters["folder_failures"] += 1
                continue
            action = _linked(
                context,
                "google_drive_folder_opened",
                inventory_attempt,
                opened,
                {"drive_path": [*parent_path, row.name]},
            )
            action = _walk_folder(
                context,
                (*parent_path, row.name),
                inventory_attempt,
                scope,
                action,
                records,
                downloads,
                counters,
            )
            returned = context.ui.back(
                "return_from_google_drive_folder",
                expected=_selector(context, "google_drive.screen.list"),
                context={"drive_path": list(parent_path)},
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
            action = _linked(
                context,
                "google_drive_folder_closed",
                inventory_attempt,
                returned,
                {"drive_path": list(parent_path)},
            )
            continue

        signature = hashlib.sha256(hierarchy.encode()).hexdigest()
        if signature in seen_hierarchies:
            return action
        seen_hierarchies.add(signature)
        swipe = context.ui.swipe(
            "scroll_google_drive_listing",
            "up",
            context={"drive_path": list(parent_path), "window": window},
        )
        time.sleep(context.app_profile.timings.get("transition_settle", 0.0))
        action = _linked(
            context,
            "google_drive_listing_scrolled",
            inventory_attempt,
            swipe,
            {"drive_path": list(parent_path), "window": window},
        )


def _collect_drive(context, scope: CollectionScope) -> Outcome:
    root_label = context.app_profile.parameters.get("root_label")
    if not isinstance(root_label, str) or not root_label:
        return Outcome(OutcomeStatus.FAILED, "google_drive_profile_invalid")
    context.begin_identification("google_drive.inventory")
    if scope.download_files:
        context.begin_identification("google_drive.files")
    inventory_attempt = context.item_attempt(
        "google-drive:inventory",
        "google_drive.inventory",
        "inventory_collection",
        source={"drive_path": [root_label]},
        context={"drive_path": [root_label]},
    )
    action = _open_root(context, inventory_attempt)
    records: list[dict[str, object]] = []
    downloads: list[dict[str, object]] = []
    counters = {
        "items": 0,
        "folders": 0,
        "files": 0,
        "metadata_failures": 0,
        "download_failures": 0,
        "folder_failures": 0,
    }
    traversal_error = None
    failure_action = None
    failure_observation_id = None
    failure_details = {}
    try:
        action = _walk_folder(context, (root_label,), inventory_attempt, scope, action, records, downloads, counters)
    except (Exception, KeyboardInterrupt) as error:
        traversal_error = error
        source = context.journal.actions[-1] if context.journal.actions else action
        linked = {**source.context, "error_type": type(error).__name__, "error": str(error)}
        try:
            _, failure_action = _observe(context, inventory_attempt,
                "google-drive/traversal-failure-screen", source, linked)
            failure_observation_id = context.observations[-1].observation_id
        except Exception as capture_error:
            failure_details["failure_evidence_error"] = f"{type(capture_error).__name__}: {capture_error}"
    acquisition_environment = "controlled_online" if scope.collect_activity else "device_only"
    inventory = {
        "record_kind": "google_drive_inventory",
        "acquisition_environment": acquisition_environment,
        "root": root_label,
        "item_count": len(records),
        "items": records,
        "downloads": downloads,
    }
    action = _linked(
        context,
        "retain_google_drive_inventory",
        inventory_attempt,
        action,
        {"drive_path": [root_label], "item_count": len(records)},
    )
    _write_json(
        context,
        "google-drive/inventory.json",
        inventory,
        attempt_id=inventory_attempt,
        action=action,
        linked={"drive_path": [root_label]},
    )
    context.finish_attempt(
        inventory_attempt,
        ProcedureStatus.INTERRUPTED if traversal_error else ProcedureStatus.COMPLETED,
        AcquisitionStatus.PARTIAL if traversal_error else AcquisitionStatus.ACQUIRED,
        reason=(str(traversal_error) or type(traversal_error).__name__) if traversal_error else None,
        action=failure_action or action,
        observation_id=failure_observation_id,
        details={"item_count": len(records), **counters, **failure_details},
    )
    for target_id in ("google_drive.inventory", "google_drive.files") if scope.download_files else ("google_drive.inventory",):
        complete = traversal_error is None and counters["folder_failures"] == 0
        context.finish_identification(target_id, completion_condition="recursive_listing_exhausted" if complete else None,
                                      reason=None if complete else str(traversal_error) or "google_drive_child_folder_unavailable")
    if traversal_error:
        raise traversal_error
    failures = sum(
        counters[key]
        for key in (
            "metadata_failures",
            "download_failures",
            "folder_failures",
        )
    )
    return Outcome(
        OutcomeStatus.PARTIAL if failures else OutcomeStatus.COMPLETE,
        "google_drive_items_incomplete" if failures else None,
        action=action,
        context={"target_ref": "drive.google.my-drive", "acquisition_environment": acquisition_environment},
        details={**counters, "download_count": len(downloads)},
    )


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules({
        'google_drive.inventory': ('google_drive.parse_listing_and_information', {'identity_fields': ['drive_path', 'kind'], 'item_kinds': ['folder', 'file'], 'include_activity': 'controlled_online'}),
        'google_drive.files': ('google_drive.listed_file_download', {'identity_fields': ['drive_path', 'name'], 'environment': 'controlled_online', 'selection': 'changed_download_inventory'}),
    })
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if target != {
        "kind": "cloud_storage",
        "ref": "drive.google.my-drive",
    }:
        return Outcome(OutcomeStatus.FAILED, "google_drive_target_invalid")
    acquisition_environment = condition.get("acquisition_environment")
    if not isinstance(acquisition_environment, str):
        return Outcome(OutcomeStatus.FAILED, "google_drive_acquisition_environment_invalid")
    if not context.start_app():
        return Outcome(OutcomeStatus.FAILED, "google_drive_app_start_not_verified")
    return _collect_drive(context, collection_scope(acquisition_environment))


__all__ = [
    "DriveRow",
    "CollectionScope",
    "activity_complete",
    "collection_scope",
    "collect",
    "parse_activity",
    "detail_metadata_complete",
    "parse_information",
    "parse_file_inventory",
    "parse_listing",
    "select_download",
]
