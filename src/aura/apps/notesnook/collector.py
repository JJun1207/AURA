"""Notesnook 3.4.5 local-workspace export."""

from __future__ import annotations
from ...collection import (
    seek_window,
    poll_samples,
    one_candidate,
    matching_candidates,
    pending_candidates,
    iter_windows,
    visit_candidates,
    visit_item,
    content_signature,
    display_pattern,
    device_profile,
    display_text,
    field_id,
    overlap_candidates,
    ordered_candidate,
)
from ...parsing import (
    labelled_values,
    parse_bounds,
    node_values,
    read_rows,
    parsing_spec,
    contains_bounds as _contains,
    overlaps_bounds as _overlaps,
)

from ...artifacts import changed_inventory


from operator import itemgetter

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .adapter import ANDROID_DOCUMENTS_PACKAGE, DOCUMENTS_PACKAGE, PACKAGE, PACKAGES


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIME_TYPE = re.compile(r"^[^/\s]+/[^/\s]+$")
_DISPLAYED_SIZE = re.compile(r"^\d+(?:\.\d+)? [KMGT]?B$")
_NOTE_COUNT = re.compile(r"^\d+ notes?$")
_HASH_FRAGMENT = re.compile(r"^[0-9a-fA-F]{8,}$")


class NotesnookCollectorError(RuntimeError):
    pass


def _sync_state(
    root: ET.Element,
    acquisition_environment: str,
    profile,
) -> dict[str, object]:
    if acquisition_environment not in {"device_only", "controlled_online"}:
        raise NotesnookCollectorError("acquisition environment is invalid")
    statuses = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip().startswith(
            tuple(display_text(profile, key) for key in ("synced", "syncing", "sync_failed"))
        )
    ]
    if len(statuses) != 1:
        raise NotesnookCollectorError("account sync state is unavailable")
    status = statuses[0]
    passed = (
        acquisition_environment == "device_only"
        and status.startswith(display_text(profile, 'sync_failed'))
        and display_text(profile, 'offline') in status
    ) or (
        acquisition_environment == "controlled_online"
        and status.startswith(display_text(profile, 'synced'))
    )
    return {"sync_label": status, "sync_label_matches_environment": passed}


def _account_record(root: ET.Element, profile) -> dict[str, str]:
    labels = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
    ]
    emails = [label for label in labels if _EMAIL.fullmatch(label)]
    sync_labels = [
        label
        for label in labels
        if label.startswith(tuple(display_text(profile, key) for key in ("synced", "syncing", "sync_failed")))
    ]
    if len(emails) != 1 or len(sync_labels) != 1:
        raise NotesnookCollectorError("account context is unavailable")
    return {
        "account_email": emails[0],
        "sync_label": sync_labels[0],
    }


def _bounds(node):
    try:
        return parse_bounds(node.get('bounds', ''), allow_negative=False)
    except ValueError:
        raise NotesnookCollectorError("element bounds are invalid") from None


def _shown(node: ET.Element) -> bool:
    return (
        node.get("package") == PACKAGE
        and node.get("visible-to-user") == "true"
    )




def _workspace_state(device, root: ET.Element) -> str:
    headers = [
        node
        for node in device.matching_elements(
            root, "notesnook.workspace.header"
        )
        if _shown(node)
    ]
    lists = [
        node
        for node in device.matching_elements(root, "notesnook.notes-list")
        if _shown(node)
    ]
    if len(headers) != 1 or len(lists) != 1:
        raise NotesnookCollectorError("workspace is unavailable")
    logged_out = [
        node
        for node in device.matching_elements(
            root, "notesnook.workspace.logged-out"
        )
        if _shown(node)
    ]
    if len(logged_out) > 1:
        raise NotesnookCollectorError("workspace authentication state is ambiguous")
    return "local_only" if logged_out else "authenticated"


def _topmost_note_bounds(
    device,
    root: ET.Element,
) -> tuple[int, int, int, int]:
    lists = [
        node
        for node in device.matching_elements(root, "notesnook.notes-list")
        if _shown(node)
    ]
    menus = [
        node
        for node in device.matching_elements(root, "notesnook.note-menu")
        if _shown(node)
    ]
    if len(lists) != 1 or not menus:
        raise NotesnookCollectorError("note card is unavailable")
    list_bounds = _bounds(lists[0])
    parents = {child: parent for parent in root.iter() for child in parent}
    candidates = []
    for menu in menus:
        card = parents.get(menu)
        if (
            card is None
            or not _shown(card)
            or card.get("clickable") != "true"
            or menu.get("clickable") != "true"
        ):
            continue
        card_bounds = _bounds(card)
        if (
            _contains(list_bounds, card_bounds)
            and _contains(card_bounds, _bounds(menu))
        ):
            candidates.append(card_bounds)
    if not candidates:
        raise NotesnookCollectorError("note card is unavailable")
    top = min(item[1] for item in candidates)
    topmost = [item for item in candidates if item[1] == top]
    if len(topmost) != 1:
        raise NotesnookCollectorError("note card is ambiguous")
    return topmost[0]


def _note_cards(
    device,
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    lists = [
        node
        for node in device.matching_elements(root, "notesnook.notes-list")
        if _shown(node)
    ]
    if len(lists) != 1:
        raise NotesnookCollectorError("note list is unavailable")
    list_bounds = _bounds(lists[0])
    add_buttons = [
        _bounds(node)
        for node in root.iter()
        if _shown(node)
        and node.get("resource-id") == field_id(device_profile(device), "buttons.add")
        and node.get("clickable") == "true"
    ]
    cards = []
    profile = device_profile(device)
    for values, card in read_rows(lists[0], parsing_spec(profile, 'cards'), ui_fields=profile.parameters['ui_fields']):
        resource_id = values['resource_id']
        content_description = values['content_description']
        if (
            not resource_id.startswith(display_text(device_profile(device), 'note_item_prefix'))
            or not content_description
            or not _shown(card)
            or card.get("clickable") != "true"
        ):
            continue
        menus = [
            node
            for node in card.iter()
            if node.get("resource-id") == field_id(device_profile(device), "listitem.menu")
            and _shown(node)
            and node.get("clickable") == "true"
        ]
        card_bounds = _bounds(card)
        if len(menus) != 1:
            continue
        menu_bounds = _bounds(menus[0])
        if (
            not _contains(list_bounds, card_bounds)
            or not _contains(card_bounds, menu_bounds)
        ):
            continue
        if any(_overlaps(menu_bounds, button) for button in add_buttons):
            continue
        cards.append({
            "source_key": (resource_id, content_description),
            "resource_id": resource_id,
            "content_description": content_description,
            "card_bounds": card_bounds,
            "menu_bounds": menu_bounds,
        })
    return tuple(sorted(
        cards,
        key=lambda item: (
            item["card_bounds"][1],
            item["card_bounds"][0],
        ),
    ))


def _history_rows(root: ET.Element, profile) -> tuple[dict[str, object], ...]:
    rows = sorted(
        (
            (values, node)
            for values, node in read_rows(root, parsing_spec(profile, 'history'), ui_fields=profile.parameters['ui_fields'])
            if _shown(node) and display_pattern(profile, 'history_label').fullmatch(values['label'])
        ),
        key=lambda item: (_bounds(item[1])[1], _bounds(item[1])[0]),
    )
    return tuple(
        {
            "version_ref": f"version-{index:06d}",
            "label": values['label'],
            "bounds": _bounds(node),
        }
        for index, (values, node) in enumerate(rows, 1)
    )


def _history_overlap(before, after):
    previous = [row["label"] for row in before]
    current = [row["label"] for row in after]
    overlaps = overlap_candidates(previous, current)
    if not overlaps:
        raise NotesnookCollectorError("history overlap is unavailable")
    return max(overlaps)


def _checkbox_record(root: ET.Element) -> dict[str, object]:
    # UI text does not encode checklist state. Preserve only explicit accessible controls.
    controls = [node for node in root.iter() if _shown(node)
                and node.get("class") == "android.widget.CheckBox"]
    return {
        "checkbox_state_status": "observed" if controls else "not_exposed",
        "checkboxes": [{"label": node.get("text") or node.get("content-desc", ""),
                        "checked": {"true": True, "false": False}.get(node.get("checked")),
                        "bounds": _bounds(node)} for node in controls],
    }


def _history_version(root: ET.Element, profile) -> dict[str, object]:
    restore = display_text(profile, 'history_restore')
    delete = display_text(profile, 'history_delete')
    values = node_values(
        (node for node in root.iter() if _shown(node) and node.get('class') in {
            'android.view.View', 'android.widget.TextView', 'android.widget.EditText'}),
        exclude={restore, delete}, unique=True)
    if not values:
        raise NotesnookCollectorError("history version is unavailable")
    controls = {
        node.get("content-desc", "").strip()
        for node in root.iter()
        if _shown(node) and node.get("clickable") == "true"
    }
    return {
        "title": values[0],
        "visible_content": values[1:],
        "restore_control_observed": restore in controls,
        "delete_control_observed": delete in controls,
        **_checkbox_record(root),
    }


def _current_note_record(root: ET.Element, profile) -> dict[str, object] | None:
    titles = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("resource-id") == "editor-title"
        and node.get("text", "").strip()
    ]
    word_counts = [
        match
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and (match := display_pattern(profile, 'word_count', flags=re.IGNORECASE).fullmatch(
            node.get("text", "").strip()
        ))
    ]
    if len(titles) != 1 or len(word_counts) != 1:
        return None
    body = []
    for node in root.iter():
        value = node.get("text", "").strip()
        if (
            _shown(node)
            and node.get("class") == "android.widget.EditText"
            and node.get("resource-id") != "editor-title"
            and value
            and value not in body
        ):
            body.append(value)
    word_count = int(word_counts[0].group(1))
    return {
        "title": titles[0],
        "body_text": "\n".join(body),
        "word_count": word_count,
        **_checkbox_record(root),
        "content_status": (
            "complete" if body or word_count == 0 else "partial"
        ),
    }


def _note_menu_record(root: ET.Element, profile) -> dict[str, str]:
    values = node_values(node for node in root.iter() if _shown(node) and node.get('class') == 'android.widget.TextView')
    try:
        metadata, positions = labelled_values(values, {key: display_text(profile, key) for key in ('created_at', 'last_edited_at')})
        record = {'title': values[0], **metadata}
    except (IndexError, ValueError):
        raise NotesnookCollectorError("note metadata is unavailable") from None
    if positions['created_at'] == 0 or positions['last_edited_at'] <= positions['created_at'] + 1:
        raise NotesnookCollectorError("note metadata is unavailable")
    return record


def _attachment_rows(root: ET.Element, profile) -> tuple[dict[str, object], ...]:
    rows = []
    for node in root.iter():
        match = display_pattern(profile, 'attachment_row').fullmatch(
            node.get("content-desc", "").strip()
        )
        if (
            not _shown(node)
            or node.get("clickable") != "true"
            or match is None
        ):
            continue
        kind, filename, displayed_size = match.groups()
        if PurePosixPath(filename).name != filename:
            raise NotesnookCollectorError("attachment filename is invalid")
        rows.append((node, kind, filename, displayed_size))
    rows.sort(key=lambda item: (_bounds(item[0])[1], _bounds(item[0])[0]))
    return tuple(
        {
            "attachment_ref": f"attachment-{index:06d}",
            "kind_label": kind,
            "filename": filename,
            "displayed_size": displayed_size,
            "bounds": _bounds(node),
        }
        for index, (node, kind, filename, displayed_size) in enumerate(
            rows, 1
        )
    )


def _attachment_detail(root: ET.Element) -> dict[str, object]:
    values = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
    ]
    downloads = [
        node
        for node in root.iter()
        if _shown(node)
        and node.get("clickable") == "true"
        and any(
            _shown(child)
            and (
                child.get("text", "").strip() == "Download"
                or child.get("content-desc", "").strip() == "Download"
            )
            for child in node.iter()
        )
    ]
    try:
        created = values.index("Created at")
        modified = values.index("Last modified at")
        filename = values[0]
        record = {
            "filename": filename,
            "mime_type": next(value for value in values if _MIME_TYPE.fullmatch(value)),
            "displayed_size": next(
                value for value in values if _DISPLAYED_SIZE.fullmatch(value)
            ),
            "note_count_label": next(
                value for value in values if _NOTE_COUNT.fullmatch(value)
            ),
            "hash_fragment": next(
                value for value in values if _HASH_FRAGMENT.fullmatch(value)
            ),
            "created_at": values[created + 1],
            "last_modified_at": values[modified + 1],
            "download_bounds": _bounds(downloads[0]),
        }
    except (IndexError, StopIteration, ValueError):
        raise NotesnookCollectorError("attachment detail is unavailable") from None
    if (
        PurePosixPath(filename).name != filename
        or len(downloads) != 1
        or created == 0
        or modified <= created + 1
    ):
        raise NotesnookCollectorError("attachment detail is unavailable")
    return record


def _changed_files(
    before: tuple[tuple[str, int, int], ...],
    after: tuple[tuple[str, int, int], ...],
) -> tuple[tuple[str, int, int], ...]:
    changed = changed_inventory(before, after, key=lambda row: row, signature=lambda row: True)
    if len(changed) != 1:
        raise NotesnookCollectorError("single changed file is unavailable")
    return changed


def _trash_cards(
    device,
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "trash_ref": f"trash-{index:06d}",
            "source_key": card["source_key"],
            "content_description": card["content_description"],
            "menu_bounds": card["menu_bounds"],
        }
        for index, card in enumerate(_note_cards(device, root), 1)
    )


def _trash_metadata(root: ET.Element, profile) -> dict[str, object]:
    values = node_values(node for node in root.iter() if _shown(node) and node.get('class') == 'android.widget.TextView')
    try:
        metadata, positions = labelled_values(values, {key: display_text(profile, key) for key in ('created_at', 'deleted_at', 'last_edited_at')})
        record = {'title': values[0], **metadata}
    except (IndexError, ValueError):
        raise NotesnookCollectorError("trash metadata is unavailable") from None
    if positions['created_at'] == 0 or positions['deleted_at'] <= positions['created_at'] + 1 or positions['last_edited_at'] <= positions['deleted_at'] + 1:
        raise NotesnookCollectorError("trash metadata is unavailable")
    controls = {
        node.get("content-desc", "").strip()
        for node in root.iter()
        if _shown(node) and node.get("clickable") == "true"
    }
    return {
        **record,
        "restore_control_observed": display_text(profile, 'trash_restore') in controls,
        "delete_control_observed": display_text(profile, 'trash_delete') in controls,
    }


def _selection_state(
    device,
    root: ET.Element,
) -> tuple[tuple[int, int, int, int], int]:
    headers = [
        node
        for node in device.matching_elements(
            root, "notesnook.selection.header"
        )
        if _shown(node)
    ]
    exports = [
        node
        for node in device.matching_elements(
            root, "notesnook.selection.export"
        )
        if _shown(node)
    ]
    if len(headers) != 1 or len(exports) != 1:
        raise NotesnookCollectorError("selection state is unavailable")
    match = display_pattern(device_profile(device), 'selection_count').fullmatch(headers[0].get("content-desc", ""))
    if match is None:
        raise NotesnookCollectorError("selection count is unavailable")
    parents = {child: parent for parent in root.iter() for child in parent}
    toolbar = parents.get(exports[0])
    if toolbar is None or not _shown(toolbar):
        raise NotesnookCollectorError("select-all boundary is unavailable")
    toolbar_bounds = _bounds(toolbar)
    export_bounds = _bounds(exports[0])
    candidates = [
        _bounds(node)
        for node in toolbar
        if _shown(node)
        and node.get("clickable") == "true"
        and not node.get("resource-id")
        and _contains(toolbar_bounds, _bounds(node))
        and _bounds(node)[2] <= export_bounds[0]
    ]
    if len(candidates) != 1:
        raise NotesnookCollectorError("select-all boundary is ambiguous")
    return candidates[0], int(match.group(1))


def _tree(device, observation_id: str) -> ET.Element:
    try:
        return ET.fromstring(device.read_observation(
            observation_id, "ui_tree"
        ))
    except (ET.ParseError, KeyError, TypeError, ValueError):
        raise NotesnookCollectorError("observation is invalid") from None


def _export_success(root: ET.Element, profile) -> tuple[int, str]:
    labels = [
        node.get("text", "")
        for node in root.iter()
        if _shown(node) and node.get("class") == "android.widget.TextView"
    ]
    counts = [
        match
        for label in labels
        if (match := display_pattern(profile, 'exported_count').fullmatch(label)) is not None
    ]
    files = [
        match
        for label in labels
        if (match := display_pattern(profile, 'exported_file').fullmatch(label)) is not None
    ]
    if len(counts) != 1 or len(files) != 1:
        raise NotesnookCollectorError("export success is unavailable")
    filename = files[0].group(1)
    if PurePosixPath(filename).name != filename:
        raise NotesnookCollectorError("export filename is invalid")
    return int(counts[0].group(1)), filename


def _unique_clickable(
    device,
    root: ET.Element,
    element_id: str,
) -> tuple[int, int, int, int]:
    matches = [
        node
        for node in device.matching_elements(root, element_id)
        if node.get("visible-to-user") == "true"
        and node.get("clickable") == "true"
    ]
    try:
        node = one_candidate(matches)
    except ValueError:
        raise NotesnookCollectorError(f"{element_id} is unavailable") from None
    return _bounds(node)


def _partial(reason: str, target_ref: str) -> dict[str, object]:
    return {
        "status": "partial",
        "reason_code": reason,
        "target_ref": target_ref,
    }


def _select_aura_directory(
    device,
    documents_observation_id: str,
    suffix: str = "",
) -> bool:
    documents_root = _tree(device, documents_observation_id)
    if any(node.get("package") == ANDROID_DOCUMENTS_PACKAGE
           for node in documents_root.iter()):
        return _select_android_directory(device, documents_observation_id, suffix)
    current_aura = device.matching_elements(
        documents_root, "documentsui.current-aura"
    )
    if len(current_aura) != 1:
        download_headers = [
            node
            for node in documents_root.iter()
            if node.get("package") == DOCUMENTS_PACKAGE
            and node.get("class") == "android.widget.TextView"
            and node.get("text") == "Files in Download"
            and node.get("visible-to-user") == "true"
        ]
        if download_headers:
            parents = {
                child: parent
                for parent in documents_root.iter()
                for child in parent
            }
            targets = []
            for label in documents_root.iter():
                if (
                    label.get("package") != DOCUMENTS_PACKAGE
                    or label.get("class") != "android.widget.TextView"
                    or label.get("text") != "AURA"
                    or label.get("visible-to-user") != "true"
                ):
                    continue
                target = parents.get(label)
                while (
                    target is not None
                    and target.get("clickable") != "true"
                ):
                    target = parents.get(target)
                if target is not None:
                    targets.append(_bounds(target))
            if len(download_headers) != 1 or len(targets) != 1:
                return False
            device.click_bounds(
                f"action-open-aura-directory{suffix}",
                targets[0],
                documents_observation_id,
            )
        else:
            try:
                _unique_clickable(
                    device,
                    documents_root,
                    "documentsui.download-root",
                )
            except NotesnookCollectorError:
                # DocumentsUI grid labels are not clickable; their folder rows are.
                parents = {child: parent for parent in documents_root.iter() for child in parent}
                targets = []
                selector = device.element_selector("documentsui.download-root")
                for label in documents_root.iter():
                    if (label.get("package") != selector["owner_package"]
                            or label.get("text") != selector["value"]
                            or label.get("visible-to-user") != "true"):
                        continue
                    # DocumentsUI breadcrumbs accept taps despite clickable=false.
                    if label.get("resource-id") == f"{DOCUMENTS_PACKAGE}:id/breadcrumb_text":
                        targets.append(_bounds(label))
                        continue
                    row = parents.get(label)
                    while row is not None and row.get("clickable") != "true":
                        row = parents.get(row)
                    if row is not None and row.get("package") == DOCUMENTS_PACKAGE:
                        targets.append(_bounds(row))
                if len(targets) != 1:
                    return False
                device.click_bounds(f"action-open-download{suffix}", targets[0], documents_observation_id)
            else:
                device.click(
                    f"action-open-download{suffix}",
                    device.element_selector("documentsui.download-root"),
                    documents_observation_id,
                )
            download = device.observe(
                f"observation-documents-download{suffix}"
            )
            # Reuse the Download-folder path, including non-clickable AURA labels.
            if any(node.get("text") == "Files in Download" and node.get("package") == DOCUMENTS_PACKAGE
                   for node in _tree(device, download["observation_id"]).iter()):
                return _select_aura_directory(device, download["observation_id"], suffix)
            try:
                _unique_clickable(
                    device,
                    _tree(device, download["observation_id"]),
                    "documentsui.aura-directory",
                )
            except NotesnookCollectorError:
                return False
            device.click(
                f"action-open-aura-directory{suffix}",
                device.element_selector("documentsui.aura-directory"),
                download["observation_id"],
            )
        documents = device.observe(f"observation-documents-aura{suffix}")
        documents_observation_id = documents["observation_id"]
        documents_root = _tree(device, documents_observation_id)
        current_aura = device.matching_elements(
            documents_root, "documentsui.current-aura"
        )
    if len(current_aura) != 1:
        return False
    try:
        _unique_clickable(
            device, documents_root, "documentsui.use-folder"
        )
    except NotesnookCollectorError:
        return False
    device.click(
        f"action-use-aura-directory{suffix}",
        device.element_selector("documentsui.use-folder"),
        documents_observation_id,
    )
    confirmation = device.observe(f"observation-documents-confirm{suffix}")
    try:
        _unique_clickable(
            device,
            _tree(device, confirmation["observation_id"]),
            "documentsui.confirm-folder",
        )
    except NotesnookCollectorError:
        return False
    device.click(
        f"action-confirm-aura-directory{suffix}",
        device.element_selector("documentsui.confirm-folder"),
        confirmation["observation_id"],
    )
    return True


def _select_android_directory(device, observation_id, suffix):
    """Android 9 DocumentsUI can initially show Recent, without a folder row."""
    for step in ("show-roots", "download-root", "aura-directory", "use-folder"):
        root = _tree(device, observation_id)
        if step == "use-folder":
            toolbars = device.matching_elements(root, "documentsui.android.toolbar")
            if len(toolbars) != 1 or len(device.matching_elements(
                    toolbars[0], "documentsui.android.current-aura")) != 1:
                return False
        try:
            node = one_candidate(node for node in device.matching_elements(
                root, f"documentsui.android.{step}")
                if node.get("visible-to-user") == "true")
        except ValueError:
            return False
        # Android 9 folder labels receive taps through their RecyclerView rows,
        # although neither the label nor row advertises clickable=true.
        device.click_bounds(f"action-documents-android-{step}{suffix}",
                            _bounds(node), observation_id)
        timeout = device_profile(device).timings["export_completion_timeout"] if step == "use-folder" else None
        observation_id = device.observe(
            f"observation-documents-android-{step}{suffix}", timeout=timeout)["observation_id"]
    return any(_shown(node) for node in _tree(device, observation_id).iter())


def collect_account_context(
    device,
    outputs,
    target_ref: str,
    acquisition_environment: str,
) -> tuple[dict[str, object], str]:
    if hasattr(outputs, "begin_item"):
        outputs.begin_item(f"account:{target_ref}", "notesnook.notes", "account")
    ordinary = device.observe("observation-account-notes")
    ordinary_tree = _tree(device, ordinary["observation_id"])
    for recovery in range(5):
        try:
            _workspace_state(device, ordinary_tree)
            break
        except NotesnookCollectorError:
            if len(device.matching_elements(ordinary_tree, "notesnook.side-menu.open")) == 1:
                break
        if recovery == 4 or not any(node.get("package") in PACKAGES
                                     for node in ordinary_tree.iter()):
            raise NotesnookCollectorError("notesnook entry screen is unavailable")
        device.back(f"action-recover-notes-entry-{recovery + 1}", ordinary["observation_id"])
        ordinary = device.observe(f"observation-account-notes-recovery-{recovery + 1}")
        ordinary_tree = _tree(device, ordinary["observation_id"])
    try:
        workspace_state = _workspace_state(device, ordinary_tree)
    except NotesnookCollectorError:
        _unique_clickable(
            device, ordinary_tree, "notesnook.side-menu.open"
        )
        device.click(
            "action-open-entry-side-menu",
            device.element_selector("notesnook.side-menu.open"),
            ordinary["observation_id"],
        )
        side_menu = device.observe("observation-account-entry-side-menu")
        _unique_clickable(
            device,
            _tree(device, side_menu["observation_id"]),
            "notesnook.side-menu.notes",
        )
        device.click(
            "action-open-notes",
            device.element_selector("notesnook.side-menu.notes"),
            side_menu["observation_id"],
        )
        ordinary = device.observe("observation-account-notes-ready")
        ordinary_tree = _tree(device, ordinary["observation_id"])
        workspace_state = _workspace_state(device, ordinary_tree)
    record = {
        "target_ref": target_ref,
        "workspace_state": workspace_state,
        "acquisition_environment": acquisition_environment,
    }
    if workspace_state == "local_only":
        record["sync_label_matches_environment"] = False
        return record, ordinary["observation_id"]

    device.click(
        "action-open-side-menu",
        device.element_selector("notesnook.side-menu.open"),
        ordinary["observation_id"],
    )
    side_menu = device.observe("observation-account-side-menu")
    try:
        _unique_clickable(
            device,
            _tree(device, side_menu["observation_id"]),
            "notesnook.side-menu.settings",
        )
    except NotesnookCollectorError:
        side_menu = device.observe(
            "observation-account-side-menu-recheck"
        )
        _unique_clickable(
            device,
            _tree(device, side_menu["observation_id"]),
            "notesnook.side-menu.settings",
        )
    device.click(
        "action-open-account",
        device.element_selector("notesnook.side-menu.settings"),
        side_menu["observation_id"],
    )
    account = device.observe("observation-account-sheet")
    account_tree = _tree(device, account["observation_id"])
    if len(device.matching_elements(
        account_tree, "notesnook.account.sync-now"
    )) != 1:
        raise NotesnookCollectorError("account context is unavailable")
    account_record = _account_record(account_tree, profile=device_profile(device))
    device.click(
        "action-sync-now",
        device.element_selector("notesnook.account.sync-now"),
        account["observation_id"],
    )
    for attempt in range(1, 6):
        account = device.observe(
            f"observation-account-sheet-sync-{attempt:06d}"
        )
        sync_state = _sync_state(
            _tree(device, account["observation_id"]),
            acquisition_environment, profile=device_profile(device),
        )
        if not sync_state["sync_label"].startswith(display_text(device_profile(device), 'syncing')):
            break
    record.update({
        "account_email": account_record["account_email"],
        **sync_state,
    })
    outputs.write_account(
        record=record,
        action_id="action-sync-now",
        observation_id=account["observation_id"],
    )

    device.back("action-close-account", account["observation_id"])
    returned_side_menu = device.observe("observation-account-returned-side-menu")
    _unique_clickable(
        device,
        _tree(device, returned_side_menu["observation_id"]),
        "notesnook.side-menu.settings",
    )
    device.back(
        "action-close-side-menu",
        returned_side_menu["observation_id"],
    )
    returned_notes = device.observe("observation-account-returned-notes")
    if _workspace_state(
        device, _tree(device, returned_notes["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("account return is unavailable")
    return record, returned_notes["observation_id"]


def _history_content_bounds(root):
    # WebView accessibility may report scrollable=false even for a long diff.
    nodes = [n for n in root.iter() if _shown(n) and n.get("class") == "android.webkit.WebView"]
    if not nodes:
        nodes = [n for n in root.iter() if _shown(n) and n.get("scrollable") == "true"]
    rectangles = {_bounds(n) for n in nodes}
    if len(rectangles) > 1:
        raise NotesnookCollectorError("history content viewport is ambiguous")
    return next(iter(rectangles), None)


def _collect_history_content(device, outputs, target_ref, note_ref, version_ref, label,
                             open_action_id, first_observation_id):
    root = _tree(device, first_observation_id)
    first = _history_version(root, device_profile(device))
    identity = {"target_ref": target_ref, "note_ref": note_ref, "version_ref": version_ref, "label": label}
    prefix = f"materialize/notes/{note_ref}/history/{version_ref}"
    observation_id, action_id, page = first_observation_id, open_action_id, 1
    windows = []
    complete = False
    try:
        viewport = _history_content_bounds(root)
        if viewport is None:
            complete = True
        else:
            def observe():
                current_root = _tree(device, observation_id)
                current = _history_version(current_root, device_profile(device))
                if current["title"] != first["title"]:
                    raise NotesnookCollectorError("history version changed while scrolling")
                signature = content_signature(ET.tostring(current_root), PACKAGE)
                return current, signature

            def advance():
                nonlocal observation_id, action_id, page
                if page >= getattr(device, "history_max_pages", 1000):
                    raise NotesnookCollectorError("history content traversal limit reached")
                action_id = f"action-scroll-{note_ref}-{version_ref}-content-{page:06d}"
                device.swipe_history(action_id, viewport, observation_id)
                page += 1
                observation_id = device.observe(f"observation-{note_ref}-{version_ref}-content-{page:06d}")["observation_id"]

            for current, _ in iter_windows(observe, advance, lambda value: value[1], include_boundary=True, adjacent=True):
                path = f"{prefix}/window-{page:06d}.json"
                outputs.write_record(path, {**identity, **current, "window_number": page},
                                     action_id=action_id, observation_id=observation_id, include_observation=True)
                windows.append(path)
            complete = True
    finally:
        outputs.write_record(f"{prefix}.json", {
            **identity, **first, "content_status": "complete" if complete else "partial",
            "visible_content_scope": "initial_window", "content_windows": windows,
            "body_traversal": "unchanged_accessible_content_window" if complete and windows else
                              "no_scroll_container" if complete else "incomplete",
        }, action_id=open_action_id, observation_id=first_observation_id, include_observation=True)
    return observation_id


def _ready_history(device, observation_id):
    profile = device_profile(device)
    initial_id = observation_id
    for probe, _ in enumerate(poll_samples(lambda: None, timeout=profile.timings['default_timeout'],
                                           interval=profile.timings['poll_interval']), 1):
        if probe > 1:
            observation_id = device.observe(f"{initial_id}-ready-{probe:06d}")["observation_id"]
        root = _tree(device, observation_id)
        rows = _history_rows(root, profile=profile)
        labels = {node.get("text", "").strip() for node in root.iter()
                  if _shown(node) and node.get("package") == PACKAGE
                  and node.get("class") == "android.widget.TextView"}
        if (display_text(profile, "history_title") in labels
                and (rows or display_text(profile, "history_empty") in labels)):
            return observation_id, root, rows
    raise NotesnookCollectorError("history surface or rows are unavailable")


def _collect_history(
    device,
    outputs,
    target_ref: str,
    note_ref: str,
    menu_observation_id: str,
) -> tuple[int, str]:
    device.click(
        f"action-open-{note_ref}-history",
        device.element_selector("notesnook.history.open"),
        menu_observation_id,
    )
    history_observation_id = f"observation-{note_ref}-history"
    history = device.observe(history_observation_id)
    history_observation_id = history["observation_id"]
    versions = []
    history_observation_id, _, rows = _ready_history(device, history_observation_id)
    skip = 0
    action_id = f"action-open-{note_ref}-history"
    # ponytail: bounded traversal; increase the profile limit for unusually long histories.
    limit = getattr(device, "history_max_pages", 1000)
    page = 1
    completion_condition = None
    def advance_history():
        nonlocal rows, history_observation_id, action_id, skip, page, completion_condition
        if not rows:
            completion_condition = "explicit_empty_history"
            return False
        root = _tree(device, history_observation_id)
        containers = [node for node in root.iter() if _shown(node)
                      and node.get("package") == "com.streetwriters.notesnook"
                      and node.get("class") == "android.widget.ScrollView"
                      and node.get("scrollable") == "true"]
        if not containers:
            completion_condition = "no_scroll_container"
            return False
        if len(containers) != 1:
            raise NotesnookCollectorError("history scroll container is ambiguous")
        previous = rows
        viewport = _bounds(containers[0])
        action_id = f"action-scroll-{note_ref}-history-{page:06d}"
        device.swipe_history(action_id, viewport, history_observation_id)
        history_observation_id = device.observe(f"observation-{note_ref}-history-window-{page+1:06d}")["observation_id"]
        history_observation_id, after_root, rows = _ready_history(device, history_observation_id)
        if content_signature(ET.tostring(root), PACKAGE) == content_signature(
            ET.tostring(after_root), PACKAGE
        ):
            completion_condition = "unchanged_history_xml"
            return False
        skip = _history_overlap(previous, rows)
        if page >= limit:
            raise NotesnookCollectorError("history traversal limit reached")
        page += 1
    for _ in iter_windows(lambda: rows, advance_history, lambda _: None):
        root = _tree(device, history_observation_id)
        outputs.write_record(
            f"materialize/notes/{note_ref}/history/window-{page:06d}.json",
            {"target_ref": target_ref, "note_ref": note_ref,
             "visible_versions": [{"label": row["label"], "bounds": row["bounds"]} for row in rows]},
            action_id=action_id, observation_id=history_observation_id, include_observation=True,
        )
        for expected in visit_candidates(range(skip, len(rows)), lambda index: rows[index]):
            version_ref = f"version-{len(versions) + 1:06d}"
            versions.append({"version_ref": version_ref, "label": expected["label"]})
            if hasattr(outputs, "begin_item"):
                outputs.begin_item(f"revision:{note_ref}:{version_ref}", "notesnook.notes", "revision", note_ref=note_ref, version_ref=version_ref)
            open_action_id = f"action-open-{note_ref}-{version_ref}"
            device.click_bounds(
                open_action_id,
                expected["bounds"],
                history_observation_id,
            )
            version_observation_id = f"observation-{note_ref}-{version_ref}"
            version = device.observe(version_observation_id)
            version_observation_id = version["observation_id"]
            version_observation_id = _collect_history_content(
                device, outputs, target_ref, note_ref, version_ref, expected["label"],
                open_action_id, version_observation_id,
            )
            device.back(
                f"action-return-{note_ref}-{version_ref}",
                version_observation_id,
            )
            returned_history = device.observe(
                f"observation-{note_ref}-history-after-{version_ref}"
            )
            history_observation_id = returned_history["observation_id"]
            history_observation_id, _, returned_rows = _ready_history(device, history_observation_id)
            if [r["label"] for r in returned_rows] != [r["label"] for r in rows]:
                raise NotesnookCollectorError("history window changed after returning from version")
            rows = returned_rows
    outputs.write_record(
        f"materialize/notes/{note_ref}/history/history.json",
        {"target_ref": target_ref, "note_ref": note_ref, "versions": versions,
         "traversal_status": "complete", "completion_condition": completion_condition},
        action_id=action_id, observation_id=history_observation_id, include_observation=True,
    )

    device.back(f"action-close-{note_ref}-history", history_observation_id)
    returned_id = (
        f"observation-materialize-notes-after-{note_ref}-history"
    )
    returned = device.observe(returned_id)
    if _workspace_state(
        device, _tree(device, returned["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("note list return is unavailable")
    return len(versions), returned["observation_id"]


def _collect_attachments(
    device,
    outputs,
    target_ref: str,
    note_ref: str,
    menu_observation_id: str,
) -> tuple[int, str]:
    open_action_id = f"action-open-{note_ref}-attachments"
    device.click(
        open_action_id,
        device.element_selector("notesnook.attachments.open"),
        menu_observation_id,
    )
    list_observation_id = f"observation-{note_ref}-attachments"
    attachment_list = device.observe(list_observation_id)
    list_observation_id = attachment_list["observation_id"]
    seen_rows = []
    previous = None
    page = 1
    while True:
        root = _tree(device, list_observation_id)
        all_files = [
            node
            for node in root.iter()
            if _shown(node)
            and node.get("class") == "android.widget.TextView"
            and node.get("text", "").strip() == "All files"
        ]
        if len(all_files) != 1:
            raise NotesnookCollectorError("All files is unavailable")
        rows = _attachment_rows(root, profile=device_profile(device))
        signature = tuple(
            (
                row["kind_label"],
                row["filename"],
                row["displayed_size"],
            )
            for row in rows
        )
        if signature == previous:
            break

        overlap = max(overlap_candidates(previous or (), signature), default=0)
        previous = signature
        for index, row in enumerate(rows[overlap:], overlap):
            attachment_ref = f"attachment-{len(seen_rows) + 1:06d}"
            if hasattr(outputs, "begin_item"):
                outputs.begin_item(f"attachment:{note_ref}:{attachment_ref}", "notesnook.attachments", "attachment", note_ref=note_ref, attachment_ref=attachment_ref, filename=row["filename"])
            expected = {**row, "attachment_ref": attachment_ref}
            seen_rows.append(expected)
            current_rows = _attachment_rows(_tree(device, list_observation_id), profile=device_profile(device))
            try:
                current = ordered_candidate(rows, current_rows, index,
                    key=itemgetter("kind_label", "filename", "displayed_size"))
            except ValueError:
                raise NotesnookCollectorError("attachment row is unavailable") from None
            row_action_id = f"action-open-{note_ref}-{attachment_ref}"
            device.click_bounds(
                row_action_id,
                current["bounds"],
                list_observation_id,
            )
            detail_observation_id = (
                f"observation-{note_ref}-{attachment_ref}-detail"
            )
            detail = device.observe(detail_observation_id)
            detail_observation_id = detail["observation_id"]
            detail_record = _attachment_detail(
                _tree(device, detail_observation_id)
            )
            if detail_record["filename"] != expected["filename"]:
                raise NotesnookCollectorError(
                    "attachment filename changed"
                )

            baseline = device.snapshot_files(
                f"action-snapshot-before-{note_ref}-{attachment_ref}",
                detail_observation_id,
            )
            device.click_bounds(
                f"action-download-{note_ref}-{attachment_ref}",
                detail_record["download_bounds"],
                detail_observation_id,
            )
            documents = device.observe(
                f"observation-{note_ref}-{attachment_ref}-documents"
            )
            suffix = f"-{note_ref}-{attachment_ref}"
            if not _select_aura_directory(
                device, documents["observation_id"], suffix
            ):
                raise NotesnookCollectorError(
                    "attachment directory selection failed"
                )
            returned = device.observe(
                f"observation-{note_ref}-attachments-after-{attachment_ref}"
            )
            list_observation_id = returned["observation_id"]
            after = device.snapshot_files(
                f"action-snapshot-after-{note_ref}-{attachment_ref}",
                list_observation_id,
                baseline,
            )
            if after == baseline:
                returned = device.observe(f"observation-unavailable-return{suffix}")
                list_observation_id = returned["observation_id"]
                root = _tree(device, list_observation_id)
                labels = [node for node in root.iter() if _shown(node)
                          and node.get("class") == "android.widget.TextView"
                          and node.get("text", "").strip() == "All files"]
                try:
                    one_candidate(labels)
                    ordered_candidate(rows, _attachment_rows(root, profile=device_profile(device)), index,
                                      key=itemgetter("kind_label", "filename", "displayed_size"))
                except ValueError:
                    raise NotesnookCollectorError("attachment list return is unavailable") from None
                outputs.preserve_failure("attachment_file_not_observed", completed=True)
                continue
            remote_path, size, _ = _changed_files(baseline, after)[0]
            if PurePosixPath(remote_path).name != expected["filename"]:
                raise NotesnookCollectorError(
                    "attachment filename changed"
                )
            pull_action_id = f"action-pull-{note_ref}-{attachment_ref}"
            payload = device.pull_file(
                pull_action_id,
                remote_path,
                list_observation_id,
            )
            if len(payload) != size:
                raise NotesnookCollectorError("attachment size changed")
            record = {
                "target_ref": target_ref,
                "note_ref": note_ref,
                "attachment_ref": attachment_ref,
                "kind_label": expected["kind_label"],
                "list_displayed_size": expected["displayed_size"],
                **{
                    key: value
                    for key, value in detail_record.items()
                    if key != "download_bounds"
                },
                "device_path": remote_path,
                "size": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
            outputs.write_attachment(
                note_ref=note_ref,
                attachment_ref=attachment_ref,
                filename=expected["filename"],
                payload=payload,
                record=record,
                action_id=pull_action_id,
                observation_id=detail_observation_id,
            )

        device.swipe(
            f"action-scroll-{note_ref}-attachments-page-{page:06d}",
            "up",
            list_observation_id,
        )
        page += 1
        attachment_list = device.observe(
            f"observation-{note_ref}-attachments-page-{page:06d}"
        )
        list_observation_id = attachment_list["observation_id"]

    outputs.write_record(
        f"materialize/notes/{note_ref}/attachments/attachments.json",
        {
            "target_ref": target_ref,
            "note_ref": note_ref,
            "attachments": [
                {
                    key: row[key]
                    for key in (
                        "attachment_ref",
                        "kind_label",
                        "filename",
                        "displayed_size",
                    )
                }
                for row in seen_rows
            ],
        },
        action_id=open_action_id,
        observation_id=list_observation_id,
        include_observation=True,
    )

    device.back(
        f"action-close-{note_ref}-attachments",
        list_observation_id,
    )
    returned_id = (
        f"observation-materialize-notes-after-{note_ref}-attachments"
    )
    returned = device.observe(returned_id)
    if _workspace_state(
        device, _tree(device, returned["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("note list return is unavailable")
    return len(seen_rows), returned["observation_id"]


def _collect_current_note(
    device,
    outputs,
    target_ref: str,
    note_ref: str,
    card_bounds: tuple[int, int, int, int],
    notes_observation_id: str,
) -> tuple[str, bool]:
    open_action_id = f"action-open-{note_ref}-current"
    observation_id = ""
    def enter():
        device.click_bounds(
            open_action_id,
            card_bounds,
            notes_observation_id,
        )
    def read():
        nonlocal observation_id
        record = None
        observation_id = ""
        profile = device_profile(device)
        current_action_id = open_action_id
        for attempt, _ in enumerate(poll_samples(lambda: None,
                probes=profile.parameters['note_open_probes'],
                interval=profile.timings['poll_interval']), 1):
            suffix = "" if attempt == 1 else f"-retry-{attempt:06d}"
            current = device.observe(f"observation-{note_ref}-current{suffix}")
            observation_id = current["observation_id"]
            root = _tree(device, observation_id)
            record = _current_note_record(root, profile=profile)
            if record is not None:
                break
            editors = [node for node in root.iter() if _shown(node)
                       and node.get('class') == 'android.widget.EditText']
            if (editors and not any(node.get('resource-id') == 'editor-title' for node in editors)
                    and attempt < profile.parameters['note_open_probes']):
                current_action_id = f'action-rewind-{note_ref}-current-{attempt:06d}'
                device.swipe(current_action_id, 'down', observation_id)
        if record is None:
            raise NotesnookCollectorError("current note is unavailable")
        outputs.write_record(
            f"materialize/notes/{note_ref}/current.json",
            {
                "target_ref": target_ref,
                "note_ref": note_ref,
                **record,
            },
            action_id=current_action_id,
            observation_id=observation_id,
            include_observation=True,
        )
        return record["content_status"] == "partial"
    def restore():
        device.back(f"action-close-{note_ref}-current", observation_id)
        returned = device.observe(
            f"observation-materialize-notes-after-{note_ref}-current"
        )
        if _workspace_state(
            device, _tree(device, returned["observation_id"])
        ) != "authenticated":
            raise NotesnookCollectorError("note list return is unavailable")
        return returned["observation_id"]
    partial, parent = visit_item(enter, read, restore)
    return parent, partial


def _rewind_note_list(device, *, initial_observation_id: str | None = None) -> str:
    page = 1
    observation_id = None
    def observe():
        nonlocal observation_id
        if page == 1 and initial_observation_id is not None:
            observation_id = initial_observation_id
        else:
            observation = device.observe(f'observation-materialize-notes-rewind-{page:06d}')
            observation_id = observation['observation_id']
        root = _tree(device, observation_id)
        if _workspace_state(device, root) != 'authenticated':
            raise NotesnookCollectorError('authenticated workspace is unavailable')
        note_list = next(node for node in device.matching_elements(root, 'notesnook.notes-list') if _shown(node))
        signature = tuple((node.get('resource-id'), node.get('content-desc', ''))
            for node in note_list.iter() if _shown(node)
            and node.get('resource-id', '').startswith(display_text(device_profile(device), 'note_item_prefix'))
            and node.get('content-desc', ''))
        return observation_id, signature
    def advance():
        nonlocal page
        device.swipe(f'action-rewind-materialize-notes-{page:06d}', 'down', observation_id)
        page += 1
    _, last = seek_window(observe, advance, lambda value: value[1], lambda value: None)
    return last[0]


def _materialize_notes(
    device,
    outputs,
    target_ref: str,
    *, initial_observation_id: str | None = None,
) -> tuple[int, int, int, int, str]:
    page = 1
    observation_id = _rewind_note_list(device, initial_observation_id=initial_observation_id)
    seen_pages = set()
    seen_notes = set()
    note_count = history_count = attachment_count = current_partial_count = 0
    def read():
        nonlocal observation_id
        root = _tree(device, observation_id)
        if _workspace_state(device, root) != "authenticated":
            raise NotesnookCollectorError(
                "authenticated workspace is unavailable"
            )
        cards = _note_cards(device, root)
        if not cards and not seen_notes:
            for retry in range(1, 4):
                refreshed = device.observe(
                    f"observation-materialize-notes-ready-{retry:06d}"
                )
                observation_id = refreshed["observation_id"]
                cards = _note_cards(
                    device, _tree(device, observation_id)
                )
                if cards:
                    break
        return cards
    def advance():
        nonlocal page, observation_id
        device.swipe(
            f"action-scroll-materialize-notes-page-{page:06d}",
            "up",
            observation_id,
            distance_ratio=device_profile(device).parameters["note_list_scroll_ratio"],
        )
        page += 1
        observation = device.observe(
            f"observation-materialize-notes-page-{page:06d}"
        )
        observation_id = observation["observation_id"]
    for cards in iter_windows(read, advance, lambda cards: tuple(card["source_key"] for card in cards)):
        if not cards:
            break
        def reacquire(expected):
            current = next(matching_candidates(_note_cards(device, _tree(device, observation_id)), expected, key=itemgetter("source_key")), None)
            if current is None:
                raise NotesnookCollectorError("note card is unavailable")
            return expected, current
        pending = pending_candidates(cards, seen_notes, key=itemgetter("source_key"))
        for expected, current in visit_candidates(pending, reacquire):
            seen_notes.add(expected["source_key"])
            note_count += 1
            note_ref = f"note-{note_count:06d}"
            if hasattr(outputs, "begin_item"):
                outputs.begin_item(f"note:{note_ref}:{note_ref}", "notesnook.notes", "note", note_ref=note_ref)
            observation_id, current_partial = _collect_current_note(
                device,
                outputs,
                target_ref,
                note_ref,
                current["card_bounds"],
                observation_id,
            )
            current_partial_count += int(current_partial)
            current = next(matching_candidates(
                _note_cards(device, _tree(device, observation_id)), expected,
                key=itemgetter("source_key")), None)
            if current is None:
                raise NotesnookCollectorError("note card is unavailable")
            open_action_id = f"action-open-{note_ref}-menu"
            device.click_bounds(
                open_action_id,
                current["menu_bounds"],
                observation_id,
            )
            menu = device.observe(f"observation-{note_ref}-menu")
            menu_observation_id = menu["observation_id"]
            record = {
                "target_ref": target_ref,
                "note_ref": note_ref,
                "source_key": list(expected["source_key"]),
                "content_description": expected["content_description"],
                **_note_menu_record(_tree(device, menu_observation_id), device_profile(device)),
            }
            outputs.write_record(
                f"materialize/notes/{note_ref}/note.json",
                record,
                action_id=open_action_id,
                observation_id=menu_observation_id,
                include_observation=True,
            )
            versions, observation_id = _collect_history(
                device,
                outputs,
                target_ref,
                note_ref,
                menu_observation_id,
            )
            current = next(matching_candidates(
                _note_cards(device, _tree(device, observation_id)), expected,
                key=itemgetter("source_key")), None)
            if current is None:
                raise NotesnookCollectorError("note card is unavailable")
            reopen_action_id = (
                f"action-open-{note_ref}-menu-for-attachments"
            )
            device.click_bounds(
                reopen_action_id,
                current["menu_bounds"],
                observation_id,
            )
            menu = device.observe(
                f"observation-{note_ref}-menu-for-attachments"
            )
            menu_observation_id = menu["observation_id"]
            attachments, menu_observation_id = _collect_attachments(
                device,
                outputs,
                target_ref,
                note_ref,
                menu_observation_id,
            )
            history_count += versions
            attachment_count += attachments
            observation_id = menu_observation_id

    return note_count, history_count, attachment_count, current_partial_count, observation_id


def _materialize_trash(
    device,
    outputs,
    target_ref: str,
    notes_observation_id: str,
) -> int:
    device.click(
        "action-open-materialize-side-menu",
        device.element_selector("notesnook.side-menu.open"),
        notes_observation_id,
    )
    side_menu = device.observe("observation-materialize-side-menu")
    _unique_clickable(
        device,
        _tree(device, side_menu["observation_id"]),
        "notesnook.side-menu.trash",
    )
    device.click(
        "action-open-trash",
        device.element_selector("notesnook.side-menu.trash"),
        side_menu["observation_id"],
    )
    page = 1
    trash = device.observe(f"observation-trash-page-{page:06d}")
    observation_id = trash["observation_id"]
    initial_observation_id = observation_id
    _unique_clickable(
        device,
        _tree(device, observation_id),
        "notesnook.trash.header",
    )

    seen_pages = set()
    seen_items = set()
    records = []
    def read():
        nonlocal observation_id
        cards = _trash_cards(device, _tree(device, observation_id))
        return cards
    def advance():
        nonlocal page, observation_id
        device.swipe(
            f"action-scroll-trash-page-{page:06d}",
            "up",
            observation_id,
        )
        page += 1
        trash = device.observe(f"observation-trash-page-{page:06d}")
        observation_id = trash["observation_id"]
    for cards in iter_windows(read, advance, lambda cards: tuple(card["source_key"] for card in cards)):
        if not cards:
            break
        def reacquire(expected):
            current = next(matching_candidates(_trash_cards(device, _tree(device, observation_id)), expected, key=itemgetter("source_key")), None)
            if current is None:
                raise NotesnookCollectorError("trash card is unavailable")
            return expected, current
        pending = pending_candidates(cards, seen_items, key=itemgetter("source_key"))
        for expected, current in visit_candidates(pending, reacquire):
            seen_items.add(expected["source_key"])
            trash_ref = f"trash-{len(records) + 1:06d}"
            if hasattr(outputs, "begin_item"):
                outputs.begin_item(f"trash_item::{trash_ref}", "notesnook.notes", "trash_item", trash_ref=trash_ref)
            open_action_id = f"action-open-{trash_ref}-menu"
            device.click_bounds(
                open_action_id,
                current["menu_bounds"],
                observation_id,
            )
            menu = device.observe(f"observation-{trash_ref}-menu")
            menu_observation_id = menu["observation_id"]
            record = {
                "target_ref": target_ref,
                "trash_ref": trash_ref,
                "source_key": list(expected["source_key"]),
                "content_description": expected["content_description"],
                **_trash_metadata(_tree(device, menu_observation_id), device_profile(device)),
            }
            outputs.write_record(
                f"materialize/trash/{trash_ref}.json",
                record,
                action_id=open_action_id,
                observation_id=menu_observation_id,
                include_observation=True,
            )
            records.append(record)
            device.back(
                f"action-close-{trash_ref}-menu",
                menu_observation_id,
            )
            returned = device.observe(
                f"observation-trash-after-{trash_ref}"
            )
            observation_id = returned["observation_id"]


    outputs.write_record(
        "materialize/trash/trash.json",
        {
            "target_ref": target_ref,
            "trash_count": len(records),
            "items": [
                {
                    "trash_ref": record["trash_ref"],
                    "content_description": record["content_description"],
                }
                for record in records
            ],
        },
        action_id="action-open-trash",
        observation_id=initial_observation_id,
        include_observation=True,
    )
    device.click(
        "action-open-trash-side-menu",
        device.element_selector("notesnook.side-menu.open"),
        observation_id,
    )
    side_menu = device.observe("observation-trash-side-menu")
    _unique_clickable(
        device,
        _tree(device, side_menu["observation_id"]),
        "notesnook.side-menu.notes",
    )
    device.click(
        "action-return-to-notes",
        device.element_selector("notesnook.side-menu.notes"),
        side_menu["observation_id"],
    )
    notes = device.observe("observation-trash-returned-notes")
    if _workspace_state(
        device, _tree(device, notes["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("note list return is unavailable")
    return len(records)


def materialize(device, outputs, target_ref: str, *, initial_observation_id: str | None = None) -> dict[str, object]:
    if hasattr(outputs, "context"):
        outputs.context.begin_identification("notesnook.notes")
        outputs.context.begin_identification("notesnook.attachments")
    try:
        notes, versions, attachments, current_partial, notes_observation_id = (
            _materialize_notes(device, outputs, target_ref, initial_observation_id=initial_observation_id)
        )
        trash = _materialize_trash(
            device,
            outputs,
            target_ref,
            notes_observation_id,
        )
    except Exception as error:
        if hasattr(outputs, "preserve_failure"):
            outputs.preserve_failure(str(error))
        return {
            **_partial("notesnook_materialize_failed", target_ref),
            "route": "materialize",
            "failure": str(error),
        }
    attachment_failures = getattr(outputs, "attachment_failures", 0)
    report = {
        "status": "partial" if current_partial or attachment_failures else "complete",
        "reason_code": (
            "notesnook_current_note_partial"
            if current_partial
            else "notesnook_attachments_not_acquired" if attachment_failures else None
        ),
        "target_ref": target_ref,
        "route": "materialize",
        "note_count": notes,
        "history_version_count": versions,
        "attachment_count": attachments,
        "trash_count": trash,
        "current_note_partial_count": current_partial,
    }
    if attachment_failures:
        report.update(attachment_not_acquired_count=attachment_failures,
                      attachment_acquired_count=attachments - attachment_failures)
    outputs.write_record(
        "materialize/summary.json",
        report,
        action_id="action-open-trash",
    )
    if hasattr(outputs, "context"):
        for target_id in ("notesnook.notes", "notesnook.attachments"):
            outputs.context.finish_identification(target_id, completion_condition="notes_and_trash_exhausted")
    return report


def collect(device, outputs, target_ref: str, *, initial_observation_id: str | None = None) -> dict[str, object]:
    if hasattr(outputs, "begin_item"):
        outputs.context.begin_identification("notesnook.export")
        outputs.begin_item(f"export:{target_ref}", "notesnook.export", "note_export")
    ordinary = ({"observation_id": initial_observation_id} if initial_observation_id is not None
                else device.observe("observation-ordinary-list"))
    try:
        ordinary_tree = _tree(device, ordinary["observation_id"])
        workspace_state = _workspace_state(device, ordinary_tree)
        note_bounds = _topmost_note_bounds(device, ordinary_tree)
    except NotesnookCollectorError:
        return _partial("workspace_selection_entry_unresolved", target_ref)

    device.long_click_bounds(
        "action-enter-selection-mode",
        note_bounds,
        ordinary["observation_id"],
    )
    one_selected = device.observe("observation-one-selected")
    try:
        select_all_bounds, before_count = _selection_state(
            device, _tree(device, one_selected["observation_id"])
        )
    except NotesnookCollectorError:
        return _partial("whole_container_selection_failed", target_ref)
    if before_count != 1:
        return _partial("whole_container_selection_failed", target_ref)

    device.click_bounds(
        "action-select-all",
        select_all_bounds,
        one_selected["observation_id"],
    )
    selected = device.observe("observation-container-selected")
    try:
        _, selected_count = _selection_state(
            device, _tree(device, selected["observation_id"])
        )
    except NotesnookCollectorError:
        return _partial("whole_container_selection_failed", target_ref)
    if selected_count <= before_count:
        return _partial("whole_container_selection_failed", target_ref)
    if hasattr(outputs, "context"):
        outputs.context.finish_identification("notesnook.export", completion_condition="export_selection_verified")

    device.click(
        "action-open-export-options",
        device.element_selector("notesnook.selection.export"),
        selected["observation_id"],
    )
    options = device.observe("observation-export-options")
    try:
        export_bounds = _unique_clickable(
            device,
            _tree(device, options["observation_id"]),
            "notesnook.export.markdown-frontmatter",
        )
    except NotesnookCollectorError:
        return _partial("export_option_unresolved", target_ref)
    baseline = device.snapshot_files(
        "action-snapshot-before",
        options["observation_id"],
    )
    device.click_bounds(
        "action-export-frontmatter",
        export_bounds,
        options["observation_id"],
    )

    documents = device.observe("observation-documents")
    if not _select_aura_directory(device, documents["observation_id"]):
        return _partial("export_directory_selection_failed", target_ref)

    profile = device_profile(device)
    for probe, _ in enumerate(poll_samples(lambda: None,
            timeout=profile.timings['export_completion_timeout'],
            interval=profile.timings['poll_interval']), 1):
        suffix = "" if probe == 1 else f"-retry-{probe:06d}"
        returned = device.observe(f"observation-returned{suffix}")
        try:
            returned_count, reported_filename = _export_success(
                _tree(device, returned["observation_id"]), profile=profile)
        except NotesnookCollectorError:
            continue
        if returned_count != selected_count:
            return _partial("export_count_mismatch", target_ref)
        break
    else:
        return _partial("application_return_unverified", target_ref)

    after = device.snapshot_files(
        "action-snapshot-after",
        returned["observation_id"],
        baseline,
    )
    created = tuple(
        item
        for item in after
        if item not in baseline and item[0].casefold().endswith(".zip")
    )
    if len(created) != 1:
        return _partial("single_zip_export_not_observed", target_ref)
    export_path, export_size, _ = created[0]
    if PurePosixPath(export_path).name != reported_filename:
        return _partial("export_filename_mismatch", target_ref)
    payload = device.pull_file(
        "action-pull-export",
        export_path,
        returned["observation_id"],
    )
    if len(payload) != export_size:
        return _partial("export_size_mismatch", target_ref)

    record = {
        "target_ref": target_ref,
        "route": "export",
        "export_format": "markdown_frontmatter",
        "workspace_state": workspace_state,
        "selected_document_count": selected_count,
        "retained_filename": "export.zip",
        "export_size": export_size,
        "export_sha256": hashlib.sha256(payload).hexdigest(),
    }
    outputs.write_export(
        payload=payload,
        record=record,
        audit={**record, "device_path": export_path},
        action_id="action-pull-export",
        observation_id=returned["observation_id"],
    )
    device.back(
        "action-close-export-result",
        returned["observation_id"],
    )
    selected_return = device.observe(
        "observation-export-selection-return"
    )
    try:
        _selection_state(
            device,
            _tree(device, selected_return["observation_id"]),
        )
    except NotesnookCollectorError:
        return _partial("export_notes_return_failed", target_ref)
    device.back(
        "action-close-export-selection",
        selected_return["observation_id"],
    )
    notes_return = device.observe("observation-export-notes-return")
    try:
        _workspace_state(
            device,
            _tree(device, notes_return["observation_id"]),
        )
    except NotesnookCollectorError:
        return _partial("export_notes_return_failed", target_ref)
    return {
        "status": "complete",
        "reason_code": None,
        "export_path": export_path,
        **record,
    }
