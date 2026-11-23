"""Google Drive UI acquisition."""

from __future__ import annotations
from ...collection import (
    retain_ui_observation,
    seek_window,
    matching_candidates,
    ordered_candidate,
    window_overlap,
    visit_scope,
    field_id,
    poll_samples,
    first_stable,
    walk_hierarchy,
    ui_selector,
    iter_windows,
    display_pattern,
    display_text,
)
from ...artifacts import select_inventory_file, parse_inventory
from ...parsing import (
    field_value,
    row_values,
    read_rows,
    parsing_spec,
    parse_bounds,
    node_values,
    overlaps_bounds as _overlaps,
)






import hashlib
import json
import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from ...ui import hierarchy_signature

from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
)


PREFIX = "com.google.android.apps.docs:id/"
_UI_KEYS = {
    "resource_id": "resourceId",
    "text": "text",
    "content_description": "description",
    "class_name": "className",
}


def _id(node: ET.Element) -> str:
    return node.get("resource-id", "").removeprefix(PREFIX)


def _text(node: ET.Element) -> str:
    return node.get("text", "").strip()


def _label(node: ET.Element) -> str:
    return (_text(node) or node.get("content-desc", "").strip())


def _value(root, resource_id):
    return field_value(root, {'resource_id': resource_id, 'scope': 'descendants', 'attribute': ['text', 'content-desc']}, ui_fields={}, resource_prefix=PREFIX)





def _bounds(node):
    try:
        return parse_bounds(node.get('bounds', ''), allow_negative=False, positive_area=False)
    except ValueError:
        raise ValueError("Google Drive row bounds are unavailable") from None



@dataclass(frozen=True)
class DriveRow:
    name: str
    kind: str
    displayed_type: str
    displayed_modified: str
    row_bounds: tuple[int, int, int, int]
    menu_bounds: tuple[int, int, int, int]
    resource_id: str = ""

    def as_record(self, parent_path: tuple[str, ...]) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "drive_path": [*parent_path, self.name],
            "displayed_type": self.displayed_type,
            "displayed_modified": self.displayed_modified,
        }



def parse_listing(xml: str, *, include_obscured: bool = False, profile) -> list[DriveRow]:
    root = ET.fromstring(xml)
    blockers = [
        _bounds(node)
        for node in root.iter()
        if _id(node) in {field_id(profile, key) for key in ('scanner_fab', 'fab_compose_view', 'bottom_navigation')}
    ]
    rows = []
    for node in root.iter():
        if _id(node) == field_id(profile, "more_actions_button"):
            continue
        menu = next(
            (item for item in node.iter() if _id(item) == field_id(profile, "more_actions_button")),
            None,
        )
        if node.get("clickable") != "true" or menu is None:
            continue
        menu_prefix = display_text(profile, 'more_actions_prefix')
        menu_label = next(iter(node_values(menu.iter(), 'content-desc', prefix=menu_prefix)), '')
        if not menu_label:
            continue
        menu_bounds = _bounds(menu)
        if not include_obscured and any(_overlaps(menu_bounds, blocker) for blocker in blockers):
            continue
        name = menu_label.removeprefix(menu_prefix)
        labels = node_values(node.iter(), 'content-desc', exclude={name, menu_label})
        displayed_type = labels[0] if labels else ""
        texts = node_values(node.iter())
        modified = ""
        modified_label = display_text(profile, 'modified_label')
        if modified_label in texts:
            index = texts.index(modified_label)
            if index + 1 < len(texts):
                modified = texts[index + 1]
        rows.append(
            DriveRow(
                name=name,
                kind="folder" if displayed_type == display_text(profile, "folder_type") else "file",
                displayed_type=displayed_type,
                displayed_modified=modified,
                row_bounds=_bounds(node),
                menu_bounds=menu_bounds,
                resource_id=node.get("resource-id", ""),
            )
        )
    return rows


def _row_key(row: DriveRow) -> tuple[str, str, str]:
    return row.name, row.kind, row.displayed_modified


def _row_candidates(rows, row):
    candidates = list(matching_candidates(rows, row, key=lambda value: (value.name, value.kind)))
    # A unique name/kind survives date-label rollover. Without an ordered
    # frame, equal names still need matching metadata; never pick the first.
    return candidates if len(candidates) <= 1 else list(matching_candidates(candidates, row, key=_row_key))


def _reveal_modified_date(context, row: DriveRow, hierarchy: str, parent_path):
    root = ET.fromstring(hierarchy)
    listing = next((node for node in root.iter() if _id(node) == field_id(context.app_profile, "scrollList") and node.get("bounds")), None)
    top, bottom = (_bounds(listing)[1], _bounds(listing)[3]) if listing is not None else (
        0, max(_bounds(node)[3] for node in root.iter() if node.get("bounds")))
    direction = "down" if sum(row.row_bounds[1::2]) < top + bottom else "up"
    return context.ui.swipe("reveal_google_drive_modified_date", direction,
                            context={"drive_path": [*parent_path, row.name], "item_kind": row.kind})


def _refresh_row(context, row: DriveRow, parent_path: tuple[str, ...] = (), *, row_scope=None) -> DriveRow:
    current = _locate_row(context, row, parent_path, row_scope=row_scope)

    def settled():
        nonlocal current
        # A visible, correctly named row can still be moving after folder return.
        time.sleep(context.app_profile.timings["transition_settle"])
        observed = _locate_row(context, row, parent_path, row_scope=row_scope)
        stable = (observed.row_bounds, observed.menu_bounds) == (current.row_bounds, current.menu_bounds)
        current = observed
        return stable

    if not context.ui.wait_until(settled):
        raise RuntimeError("google_drive_row_layout_not_stable")
    return current


def _locate_row(context, row: DriveRow, parent_path: tuple[str, ...] = (), *, row_scope=None) -> DriveRow:
    if row_scope is not None:
        expected, index = row_scope
        current = parse_listing(context.device.hierarchy(), profile=context.app_profile)
        try:
            return ordered_candidate(expected, current, index,
                key=lambda value: (value.name, value.kind), allow_removed_prefix=True)
        except ValueError:
            # A unique row can still be revealed after a snackbar obscures it.
            # Equal rows require the ordered frame; never guess between them.
            if sum((value.name, value.kind) == (row.name, row.kind)
                   for value in expected) != 1:
                raise RuntimeError("google_drive_listing_changed_after_return") from None

    recheck_count = 0
    for recovery in range(2):
        hierarchy = context.device.hierarchy()
        all_rows = parse_listing(hierarchy, include_obscured=True, profile=context.app_profile)
        obscured = _row_candidates(all_rows, row)
        missing_date = [current for current in all_rows
                        if (current.name, current.kind) == (row.name, row.kind) and not current.displayed_modified]
        if not obscured and not missing_date:
            def row_loaded():
                nonlocal hierarchy, obscured, missing_date, recheck_count
                recheck_count += 1
                hierarchy = context.device.hierarchy()
                all_rows = parse_listing(hierarchy, include_obscured=True, profile=context.app_profile)
                obscured = _row_candidates(all_rows, row)
                missing_date = [current for current in all_rows
                                if (current.name, current.kind) == (row.name, row.kind) and not current.displayed_modified]
                return bool(obscured or missing_date)

            context.ui.wait_until(row_loaded)
        if missing_date:
            if recovery or len(missing_date) != 1:
                break
            _reveal_modified_date(context, missing_date[0], hierarchy, parent_path)
            time.sleep(context.app_profile.timings["transition_settle"])
            continue
        matches = [current for current in parse_listing(hierarchy, profile=context.app_profile)
                   if any(_row_key(current) == _row_key(candidate) for candidate in obscured)]
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
        time.sleep(context.app_profile.timings["transition_settle"])
    raise RuntimeError("google_drive_row_not_uniquely_visible")


def parse_information(xml: str, profile) -> dict[str, str]:
    root = ET.fromstring(xml)
    toolbar = next((node for node in root.iter() if _id(node) == field_id(profile, "toolbar")), None)
    description = "" if toolbar is None else toolbar.get("content-desc", "").strip()
    prefix = display_text(profile, 'information_prefix')
    values = {
        "name": description.removeprefix(prefix) if description.startswith(prefix) else "",
        **row_values(root, parsing_spec(profile, 'information')['fields'], ui_fields=profile.parameters['ui_fields'], resource_prefix=PREFIX),
    }
    private = next((node for node in root.iter() if _id(node) == field_id(profile, "private_acl")), None)
    if private is not None:
        values["access"] = next(
            (_text(node) for node in private.iter() if _text(node)),
            "",
        )
    return {key: value for key, value in values.items() if value}


def detail_metadata_complete(metadata: dict[str, str]) -> bool:
    return {"type", "location", "created", "modified"} <= metadata.keys()


def parse_activity(xml: str, profile) -> tuple[list[dict[str, str]], str]:
    root = ET.fromstring(xml)
    records = [record for record, _ in read_rows(root, parsing_spec(profile, 'activity'),
        ui_fields=profile.parameters['ui_fields'], resource_prefix=PREFIX)]
    return records, _value(root, field_id(profile, 'recents_status'))


def activity_complete(status: str, profile) -> bool:
    return status.startswith(display_text(profile, "activity_end_prefix"))


def select_download(
    before: tuple[tuple[str, int, int], ...],
    after: tuple[tuple[str, int, int], ...],
    expected_name: str,
) -> tuple[str, int, int]:
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
    def matches(row):
        name = PurePosixPath(row[0]).name
        return name == expected.name or any(pattern.fullmatch(name) for pattern in duplicate_names)
    try:
        return select_inventory_file(before, after, matches=matches)[0]
    except ValueError:
        raise RuntimeError('google_drive_download_inventory_ambiguous') from None


def parse_file_inventory(output: str, download_root: str) -> tuple[tuple[str, int, int], ...]:
    try:
        return parse_inventory(output, (download_root,), format='nul_triples')
    except ValueError:
        raise RuntimeError('google_drive_download_inventory_invalid') from None


def _selector(context, name):
    try:
        return ui_selector(context.app_profile, name)
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


def _observe(context, attempt_id: str, prefix: str, source_action: ActionRef, linked: dict[str, object], *, hierarchy: str | None = None, screen: bytes | None = None) -> tuple[str, ActionRef]:
    if hierarchy is None:
        hierarchy = context.device.hierarchy()
    if screen is None:
        action, _ = retain_ui_observation(context, attempt_id, prefix, hierarchy,
            name='observe_google_drive_ui', source_action=source_action, linked=linked)
    else:
        action = _linked(context, 'observe_google_drive_ui', attempt_id, source_action, linked)
        context.retain_observation(prefix, screen, hierarchy.encode('utf-8'),
            attempt_id=attempt_id, action=action, context=linked)
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


def _empty_folder(hierarchy, profile):
    return display_text(profile, "empty_folder") in node_values(
        node for node in ET.fromstring(hierarchy).iter()
        if node.get("package") == profile.package_name
        and node.get("class") == "android.widget.TextView"
        and node.get("visible-to-user") != "false"
    )


def _open_root(context, attempt_id: str) -> ActionRef:
    navigation = _selector(context, "google_drive.navigation.files")
    seen = set()
    source_action = None
    while not context.device.exists(navigation):
        hierarchy = context.device.hierarchy()
        signature = hierarchy_signature(hierarchy)
        if signature in seen:
            raise RuntimeError("google_drive_root_unavailable")
        seen.add(signature)
        source_action = context.ui.back("return_to_google_drive_navigation")
        time.sleep(context.app_profile.timings["transition_settle"])
    opened = context.ui.click(
        "open_google_drive_files",
        navigation,
    )
    if not context.ui.wait_until(lambda:
        context.device.exists(_selector(context, "google_drive.screen.list"))
        or _empty_folder(context.device.hierarchy(), context.app_profile)
    ):
        raise RuntimeError("google_drive_root_unavailable")
    time.sleep(context.app_profile.timings["transition_settle"])
    return _linked(
        context,
        "google_drive_root_opened",
        attempt_id,
        opened,
        {"drive_path": [context.app_profile.parameters["root_label"]]},
    )


def _scroll_until(context, selector: dict[str, object], *, surface: str):
    action = None
    def advance():
        nonlocal action
        action = context.ui.swipe(f'scroll_google_drive_{surface}', 'up',
            distance_ratio=context.app_profile.parameters['menu_scroll_ratio'], context={'surface': surface})
        time.sleep(context.app_profile.timings['transition_settle'])
    found, _ = seek_window(lambda: context.device.exists(selector), advance,
        lambda found: None if found else hierarchy_signature(context.device.hierarchy()),
        lambda found: True if found else None)
    if found is None:
        raise RuntimeError(f'google_drive_{surface}_control_unavailable')
    return action


def _open_menu(context, row: DriveRow, linked: dict[str, object], *, row_scope=None):
    row = _refresh_row(context, row, tuple(linked["drive_path"][:-1]), row_scope=row_scope)
    action = context.ui.click(
        "open_google_drive_item_menu",
        _row_selector(context, row, menu=True),
        context=linked,
    )
    time.sleep(context.app_profile.timings["transition_settle"])
    return action


def _row_selector(context, row: DriveRow, *, menu: bool):
    # Huawei can return equal dumps with stale descendant bounds. Resolve the
    # validated occurrence through Android's native selector at touch time.
    rows = parse_listing(context.device.hierarchy(), include_obscured=True,
                         profile=context.app_profile)
    selector = {"packageName": context.app_profile.package_name}
    if menu:
        matches = [current for current in rows if current.name == row.name]
        selector["description"] = display_text(context.app_profile, 'more_actions_prefix') + row.name
    else:
        if not row.resource_id:
            raise RuntimeError("google_drive_native_row_identity_unavailable")
        matches = [current for current in rows if current.resource_id == row.resource_id]
        selector.update(resourceId=row.resource_id, clickable=True)
    if matches.count(row) != 1:
        raise RuntimeError("google_drive_listing_changed_before_click")
    if context.device.count(selector) != len(matches):
        raise RuntimeError("google_drive_native_row_count_mismatch")
    if len(matches) > 1:
        selector["instance"] = matches.index(row)
    return selector


def _collect_activity(context, row, parent_path, prefix, linked, source_action, *, initial_observation=None):
    attempt_id = context.item_attempt(
        "google-drive:activity:" + prefix, "google_drive.activity", "activity_history",
        source=row.as_record(parent_path), context=linked)
    action = _linked(context, "google_drive_activity_started", attempt_id, source_action, linked)
    activities, activity_status = [], ""
    error_details = {}
    try:
        first_linked, capture = linked, {}
        if initial_observation is not None:
            hierarchy, observation = initial_observation
            screen = next(artifact for artifact in context.artifacts.records
                          if artifact.artifact_id == observation.screen_artifact_id)
            capture = {"hierarchy": hierarchy, "screen": (context.run_dir / screen.relative_path).read_bytes()}
            first_linked = {**linked, "source_observation_id": observation.observation_id, "capture_reused": True}
        hierarchy, action = _observe(context, attempt_id, f"{prefix}/activity-screen", action, first_linked, **capture)
        activities, activity_status = parse_activity(hierarchy, profile=context.app_profile)
        activity_windows = 0
        seen = {hierarchy_signature(hierarchy)}
        while not activity_complete(activity_status, context.app_profile):
            if display_pattern(context.app_profile, 'activity_error', flags=re.IGNORECASE).search(activity_status):
                break
            swipe = context.ui.swipe(
                "scroll_google_drive_activity",
                "up",
                context=linked,
            )
            time.sleep(context.app_profile.timings["transition_settle"])
            current = context.device.hierarchy()
            signature = hierarchy_signature(current)
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
                hierarchy=current,
            )
            window_records, activity_status = parse_activity(current, profile=context.app_profile)
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
        activity_collection_status = (
            "complete" if activity_complete(activity_status, context.app_profile)
            else "partial"
        )
    except Exception as error:
        activity_collection_status = "partial"
        error_details = {"error_type": type(error).__name__, "error": str(error)}
    document = {
        **row.as_record(parent_path), "activity": activities,
        "activity_status": activity_status or "visible_records_collected",
        "activity_collection_status": activity_collection_status,
        **error_details,
    }
    action = _linked(context, "retain_google_drive_activity", attempt_id, action, linked)
    _write_json(context, f"{prefix}/activity.json", document,
                attempt_id=attempt_id, action=action, linked=linked)
    complete = activity_collection_status == "complete"
    context.finish_attempt(attempt_id,
        ProcedureStatus.COMPLETED if complete else ProcedureStatus.INTERRUPTED,
        AcquisitionStatus.ACQUIRED if complete else AcquisitionStatus.PARTIAL,
        reason=None if complete else "google_drive_activity_incomplete", action=action,
        details={"activity_count": len(activities), **document})
    return document, action


def _collect_information(
    context,
    row: DriveRow,
    parent_path: tuple[str, ...],
    acquisition_environment: str,
    sequence: int,
    *, row_scope=None,
) -> tuple[dict[str, object], ActionRef | None, bool]:
    path = (*parent_path, row.name)
    linked = {
        "drive_path": list(path),
        "item_kind": row.kind,
        "acquisition_environment": acquisition_environment,
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
        menu_action = _open_menu(context, row, linked, row_scope=row_scope)
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
        time.sleep(context.app_profile.timings["transition_settle"])
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
        metadata = parse_information(hierarchy, profile=context.app_profile)
        if metadata.get("name") and metadata["name"] != row.name:
            raise RuntimeError("google_drive_information_identity_mismatch")
        detail_status = (
            "acquired" if detail_metadata_complete(metadata) else "listing_only"
        )
        acquired = detail_status == "acquired"
        document = {
            **row.as_record(parent_path),
            "metadata": metadata,
            "detail_metadata_status": detail_status,
        }
        action = _linked(
            context,
            "retain_google_drive_metadata",
            attempt_id,
            action,
            linked,
        )
        _write_json(
            context,
            f"{prefix}/metadata.json",
            document,
            attempt_id=attempt_id,
            action=action,
            linked=linked,
        )
        if context.target_enabled("google_drive.activity"):
            activity, activity_action = _collect_activity(
                context, row, parent_path, prefix, linked, action,
                initial_observation=(hierarchy, context.observations[-1]))
            document.update({key: activity[key] for key in (
                "activity", "activity_status", "activity_collection_status")})
            action = _linked(context, "google_drive_activity_returned", attempt_id, activity_action, linked)
        else:
            document.update(activity=[], activity_collection_status="not_attempted",
                            activity_status="profile_environment_excluded")
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
                if acquired
                else AcquisitionStatus.PARTIAL
            ),
            reason=(
                None if detail_status == "acquired"
                else "google_drive_detail_metadata_limited"
            ),
            action=action,
            details={
                "metadata_fields": sorted(metadata),
                "detail_metadata_status": detail_status,
            },
        )
        return document, action, acquired
    except Exception as error:
        reason = ("google_drive_information_identity_mismatch" if str(error) == "google_drive_information_identity_mismatch"
                  else "google_drive_metadata_unavailable")
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.INTERRUPTED,
            AcquisitionStatus.PARTIAL if context.result_status(attempt_id) is not AcquisitionStatus.NOT_ACQUIRED else AcquisitionStatus.NOT_ACQUIRED,
            reason=reason,
            action=action,
            details={"error_type": type(error).__name__, "error": str(error)},
        )
        return {
            **row.as_record(parent_path),
            "metadata": {},
            "activity": [],
            "activity_status": "unavailable",
            "reason": reason,
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
    def candidate():
        current = _scan_downloads(context)
        try:
            return select_download(baseline, current, expected_name)
        except RuntimeError:
            return None
    selected = first_stable(poll_samples(candidate, probes=probes, interval=interval))
    if selected is not None:
        return selected
    raise RuntimeError("google_drive_download_did_not_settle")


def _download_file(
    context,
    row: DriveRow,
    parent_path: tuple[str, ...],
    sequence: int,
    prior_downloads=(),
    *, row_scope=None,
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
    requested = False
    try:
        baseline = _scan_downloads(context)
        menu_action = _open_menu(context, row, linked, row_scope=row_scope)
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
        if context.device.exists({**_selector(context, "google_drive.menu.download"), "enabled": False}):
            raise RuntimeError("google_drive_download_control_disabled")
        # A transport error can follow a dispatched tap, so keep this conservative.
        requested = True
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
        possible_prior = []
        previous_requests = [*context.base_context.get("condition", {}).get("pending_downloads", []),
                             *(item for item in prior_downloads if item.get("request_may_be_pending"))]
        for previous in previous_requests:
            try:
                select_download((), ((remote_path, expected_size, mtime),), previous["displayed_name"])
            except RuntimeError:
                continue
            possible_prior.append({key: previous[key] for key in ("displayed_name", "attempt_id", "run_id") if key in previous})
        reason = "google_drive_download_source_ambiguous" if possible_prior else None
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
            "status": "partial" if possible_prior else "acquired",
            "reason": reason,
            "attempt_id": attempt_id,
            "possible_prior_requests": possible_prior,
            "request_may_be_pending": bool(possible_prior),
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
            AcquisitionStatus.PARTIAL if possible_prior else AcquisitionStatus.ACQUIRED,
            reason=reason,
            action=action,
            details={
                "retained_path": artifact.relative_path,
                "size": artifact.size,
                "sha256": artifact.sha256,
                "possible_prior_requests": possible_prior,
            },
        )
        return record, action, not possible_prior
    except Exception as error:
        if isinstance(error, RuntimeError) and str(error) == "google_drive_row_not_uniquely_visible":
            raise
        recovery_error = None
        try:
            _, action = _observe(
                context, attempt_id,
                f"google-drive/files/{sequence:06d}-{_path_key(path)}/download-unavailable-screen",
                action, linked,
            )
            if menu_open or not context.device.exists(_selector(context, "google_drive.screen.list")):
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
            details={"error_type": type(error).__name__, "error": str(error), "menu_recovery_error": recovery_error,
                     "request_may_be_pending": requested},
        )
        return {
            "drive_path": list(path),
            "displayed_name": row.name,
            "status": "not_acquired",
            "reason": "google_drive_download_failed",
            "error": str(error),
            "attempt_id": attempt_id,
            "request_may_be_pending": requested,
        }, action, False


def _walk_folder(
    context,
    parent_path: tuple[str, ...],
    inventory_attempt: str,
    acquisition_environment: str,
    source_action: ActionRef,
    records: list[dict[str, object]],
    downloads: list[dict[str, object]],
    counters: dict[str, int],
) -> ActionRef:
    return walk_hierarchy((context, parent_path, inventory_attempt, acquisition_environment, source_action,
                           records, downloads, counters), _folder_level)


def _folder_level(task):
    (context, parent_path, inventory_attempt, acquisition_environment, source_action,
     records, downloads, counters) = task
    profile = context.app_profile
    previous_rows = []
    window = 0
    action = source_action
    listing_key = f"{counters['items']:06d}-{_path_key(parent_path)}"
    def read():
        nonlocal window, action
        window += 1
        hierarchy, action = _observe(
            context,
            inventory_attempt,
            f"google-drive/listings/{listing_key}/window-{window:04d}",
            action,
            {"drive_path": list(parent_path), "window": window},
        )
        return hierarchy
    def advance():
        nonlocal action
        swipe = context.ui.swipe("scroll_google_drive_listing", "up",
            context={"drive_path": list(parent_path), "window": window})
        time.sleep(context.app_profile.timings["transition_settle"])
        action = _linked(context, "google_drive_listing_scrolled", inventory_attempt,
                         swipe, {"drive_path": list(parent_path), "window": window})

    for hierarchy in iter_windows(read, advance, hierarchy_signature, adjacent=True):
        visible_rows = parse_listing(hierarchy, profile=profile)
        if not visible_rows and _empty_folder(hierarchy, profile):
            return action
        # Relative dates are evidence, not identity. Keep ordered occurrences
        # when comparing adjacent windows, including repeated equal names.
        overlap = window_overlap(
            [dict(name=row.name, kind=row.kind, displayed_type=row.displayed_type) for row in previous_rows],
            [dict(name=row.name, kind=row.kind, displayed_type=row.displayed_type) for row in visible_rows])
        previous_rows = visible_rows
        for index, row in enumerate(visible_rows[overlap:], overlap):
            row_scope = (visible_rows, index)
            if index > overlap:
                read()  # Preserve the returned listing before the next visit.
            counters["items"] += 1
            if row.kind == "folder":
                counters["folders"] += 1
            else:
                counters["files"] += 1
            record, row_action, acquired = _collect_information(
                context,
                row,
                parent_path,
                acquisition_environment,
                counters["items"],
                row_scope=row_scope,
            )
            records.append(record)
            counters["metadata_failures"] += 0 if acquired else 1
            counters["activity_failures"] += int(record.get("activity_collection_status") == "partial")
            if row_action is not None:
                action = row_action
            if record.get("reason") == "google_drive_information_identity_mismatch":
                raise RuntimeError(record["reason"])
            if not acquired and not context.device.exists(_selector(context, "google_drive.screen.list")):
                raise RuntimeError("google_drive_listing_not_restored_after_metadata")
            # The list can be visible while the information-return animation still moves its rows.
            time.sleep(context.app_profile.timings["transition_settle"])
            if row.kind == "folder":
                row = _refresh_row(context, row, parent_path, row_scope=row_scope)
            if row.kind == "file" and context.target_enabled("google_drive.files"):
                downloaded, row_action, acquired = _download_file(
                    context,
                    row,
                    parent_path,
                    counters["items"],
                    downloads,
                    row_scope=row_scope,
                )
                time.sleep(context.app_profile.timings["transition_settle"])
                downloads.append(downloaded)
                counters["download_failures"] += 0 if acquired else 1
                if row_action is not None:
                    action = row_action
                if not acquired and not context.device.exists(_selector(context, "google_drive.screen.list")):
                    raise RuntimeError("google_drive_listing_not_restored_after_download")
            if row.kind != "folder":
                continue
            def enter():
                nonlocal action
                opened = context.ui.click(
                    "open_google_drive_folder",
                    _row_selector(context, row, menu=False),
                    context={"drive_path": [*parent_path, row.name]},
                )
                time.sleep(context.app_profile.timings["transition_settle"])
                if not context.device.exists(
                    _selector(context, "google_drive.screen.list")
                ):
                    hierarchy = context.device.hierarchy()
                    if _empty_folder(hierarchy, profile):
                        record.update(children_status="complete", child_count=0,
                                      children_completion_condition="explicit_empty_folder")
                        _, action = _observe(
                            context, inventory_attempt,
                            f"google-drive/items/{counters['items']:06d}-{_path_key((*parent_path, row.name))}/children-empty-screen",
                            opened, {"drive_path": [*parent_path, row.name],
                                     "completion_condition": "explicit_empty_folder"},
                            hierarchy=hierarchy,
                        )
                        return False
                    record["children_status"] = "unavailable"
                    record["children_reason"] = "google_drive_child_folder_unavailable"
                    counters["folder_failures"] += 1
                    _, action = _observe(
                        context, inventory_attempt,
                        f"google-drive/items/{counters['items']:06d}-{_path_key((*parent_path, row.name))}/children-unavailable-screen",
                        opened,
                        {"drive_path": [*parent_path, row.name], "reason": record["children_reason"]},
                        hierarchy=hierarchy,
                    )
                    return False
                action = _linked(
                    context,
                    "google_drive_folder_opened",
                    inventory_attempt,
                    opened,
                    {"drive_path": [*parent_path, row.name]},
                )
                return True

            def restore():
                nonlocal action
                # The folder tap can navigate even when its child list cannot load.
                returned = context.ui.back(
                    "return_from_google_drive_folder",
                    expected=_selector(context, "google_drive.screen.list"),
                    context={"drive_path": list(parent_path)},
                )
                time.sleep(context.app_profile.timings["transition_settle"])
                action = _linked(
                    context,
                    "google_drive_folder_closed",
                    inventory_attempt,
                    returned,
                    {"drive_path": list(parent_path)},
                )
                if not entered:
                    _refresh_row(context, row, parent_path, row_scope=row_scope)
            with visit_scope(enter, restore) as entered:
                if entered:
                    action = yield (
                        context,
                        (*parent_path, row.name),
                        inventory_attempt,
                        acquisition_environment,
                        action,
                        records,
                        downloads,
                        counters,
                    )
            continue

    return action


def _collect_drive(context, acquisition_environment: str) -> Outcome:
    root_label = context.app_profile.parameters.get("root_label")
    if not isinstance(root_label, str) or not root_label:
        return Outcome(OutcomeStatus.FAILED, "google_drive_profile_invalid")
    context.begin_identification("google_drive.inventory")
    for target_id in ("google_drive.activity", "google_drive.files"):
        if context.target_enabled(target_id):
            context.begin_identification(target_id)
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
        "activity_failures": 0,
        "download_failures": 0,
        "folder_failures": 0,
    }
    traversal_error = None
    failure_action = None
    failure_observation_id = None
    failure_details = {}
    try:
        action = _walk_folder(context, (root_label,), inventory_attempt, acquisition_environment, action, records, downloads, counters)
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
        AcquisitionStatus.PARTIAL if traversal_error or counters["folder_failures"] else AcquisitionStatus.ACQUIRED,
        reason=((str(traversal_error) or type(traversal_error).__name__) if traversal_error else
                "google_drive_child_folder_unavailable" if counters["folder_failures"] else None),
        action=failure_action or action,
        observation_id=failure_observation_id,
        details={"item_count": len(records), **counters, **failure_details},
    )
    for target_id in ("google_drive.inventory", "google_drive.activity", "google_drive.files"):
        if not context.target_enabled(target_id):
            continue
        complete = traversal_error is None and counters["folder_failures"] == 0
        context.finish_identification(target_id, completion_condition="recursive_listing_exhausted" if complete else None,
                                      reason=None if complete else
                                      str(traversal_error) if traversal_error else "google_drive_child_folder_unavailable")
    if traversal_error:
        raise traversal_error
    failures = sum(
        counters[key]
        for key in (
            "metadata_failures",
            "activity_failures",
            "download_failures",
            "folder_failures",
        )
    )
    return Outcome(
        OutcomeStatus.PARTIAL if failures else OutcomeStatus.COMPLETE,
        "google_drive_items_incomplete" if failures else None,
        action=action,
        context={"target_ref": "drive.google.my-drive", "acquisition_environment": acquisition_environment},
        details={**counters, "download_count": len(downloads), "pending_downloads": [
            {key: item[key] for key in ("displayed_name", "attempt_id")}
            for item in downloads if item.get("request_may_be_pending")
        ]},
    )


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules({
        'google_drive.inventory': ('google_drive.parse_listing_and_information', {'identity_fields': ['drive_path', 'kind'], 'item_kinds': ['folder', 'file'], 'include_activity': False}),
        'google_drive.activity': ('google_drive.parse_activity', {'identity_fields': ['drive_path', 'kind']}),
        'google_drive.files': ('google_drive.listed_file_download', {'identity_fields': ['drive_path', 'name'], 'selection': 'changed_download_inventory'}),
    })
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if target != {
        "kind": "cloud_storage",
        "ref": "drive.google.my-drive",
    }:
        return Outcome(OutcomeStatus.FAILED, "google_drive_target_invalid")
    acquisition_environment = condition.get("acquisition_environment")
    if not isinstance(acquisition_environment, str) or acquisition_environment not in {"device_only", "controlled_online"}:
        return Outcome(OutcomeStatus.FAILED, "google_drive_acquisition_environment_invalid")
    if not context.start_app():
        return Outcome(OutcomeStatus.FAILED, "google_drive_app_start_not_verified")
    return _collect_drive(context, acquisition_environment)


__all__ = [
    "DriveRow",
    "activity_complete",
    "collect",
    "parse_activity",
    "detail_metadata_complete",
    "parse_information",
    "parse_file_inventory",
    "parse_listing",
    "select_download",
]
