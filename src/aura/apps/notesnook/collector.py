"""Notesnook 3.4.5 local-workspace export."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

from .adapter import DOCUMENTS_PACKAGE, PACKAGE


_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_SELECTION_COUNT = re.compile(r"^(\d+) selected$")
_EXPORTED_COUNT = re.compile(r"^(\d+) notes exported$")
_EXPORTED_FILE = re.compile(r"^Notes exported as (.+\.zip) successfully$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HISTORY_LABEL = re.compile(
    r"^\d{2}-\d{2}-\d{4} .+ -  .+, .+$"
)
_ATTACHMENT_ROW = re.compile(
    r"^([^,]+), (.+), File size: (.+)$"
)
_MIME_TYPE = re.compile(r"^[^/\s]+/[^/\s]+$")
_DISPLAYED_SIZE = re.compile(r"^\d+(?:\.\d+)? [KMGT]?B$")
_NOTE_COUNT = re.compile(r"^\d+ notes?$")
_HASH_FRAGMENT = re.compile(r"^[0-9a-fA-F]{8,}$")
_WORD_COUNT = re.compile(r"^(\d+) words?$", re.IGNORECASE)


class NotesnookCollectorError(RuntimeError):
    pass


def _sync_state(
    root: ET.Element,
    acquisition_environment: str,
) -> dict[str, object]:
    if acquisition_environment not in {"device_only", "controlled_online"}:
        raise NotesnookCollectorError("acquisition environment is invalid")
    statuses = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip().startswith(
            ("Synced ", "Syncing", "Sync failed ")
        )
    ]
    if len(statuses) != 1:
        raise NotesnookCollectorError("account sync state is unavailable")
    status = statuses[0]
    passed = (
        acquisition_environment == "device_only"
        and status.startswith("Sync failed ")
        and "(Offline)" in status
    ) or (
        acquisition_environment == "controlled_online"
        and status.startswith("Synced ")
    )
    return {"sync_label": status, "sync_gate_passed": passed}


def _account_record(root: ET.Element) -> dict[str, str]:
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
        if label.startswith(("Synced ", "Syncing", "Sync failed "))
    ]
    if len(emails) != 1 or len(sync_labels) != 1:
        raise NotesnookCollectorError("account context is unavailable")
    return {
        "account_email": emails[0],
        "sync_label": sync_labels[0],
    }


def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        raise NotesnookCollectorError("element bounds are invalid")
    bounds = tuple(map(int, match.groups()))
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise NotesnookCollectorError("element bounds are invalid")
    return bounds


def _shown(node: ET.Element) -> bool:
    return (
        node.get("package") == PACKAGE
        and node.get("visible-to-user") == "true"
    )


def _contains(
    outer: tuple[int, int, int, int],
    inner: tuple[int, int, int, int],
) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


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
        and node.get("resource-id") == "buttons.add"
        and node.get("clickable") == "true"
    ]
    cards = []
    for card in lists[0].iter():
        resource_id = card.get("resource-id", "")
        content_description = card.get("content-desc", "").strip()
        if (
            not resource_id.startswith("note-item-")
            or not content_description
            or not _shown(card)
            or card.get("clickable") != "true"
        ):
            continue
        menus = [
            node
            for node in card.iter()
            if node.get("resource-id") == "listitem.menu"
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


def _history_rows(root: ET.Element) -> tuple[dict[str, object], ...]:
    rows = sorted(
        (
            node
            for node in root.iter()
            if _shown(node)
            and node.get("class") == "android.view.ViewGroup"
            and node.get("clickable") == "true"
            and _HISTORY_LABEL.fullmatch(
                node.get("content-desc", "").strip()
            )
        ),
        key=lambda node: (_bounds(node)[1], _bounds(node)[0]),
    )
    return tuple(
        {
            "version_ref": f"version-{index:06d}",
            "label": node.get("content-desc", "").strip(),
            "bounds": _bounds(node),
        }
        for index, node in enumerate(rows, 1)
    )


def _history_version(root: ET.Element) -> dict[str, object]:
    values = []
    prohibited = {"Restore", "Delete permanently"}
    for node in root.iter():
        value = node.get("text", "").strip()
        if (
            _shown(node)
            and node.get("class")
            in {
                "android.view.View",
                "android.widget.TextView",
                "android.widget.EditText",
            }
            and value
            and value not in prohibited
            and value not in values
        ):
            values.append(value)
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
        "restore_control_observed": "Restore" in controls,
        "delete_control_observed": "Delete permanently" in controls,
    }


def _current_note_record(root: ET.Element) -> dict[str, object] | None:
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
        and (match := _WORD_COUNT.fullmatch(
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
        "content_status": (
            "complete" if body or word_count == 0 else "partial"
        ),
    }


def _note_menu_record(root: ET.Element) -> dict[str, str]:
    values = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
    ]
    try:
        created = values.index("Created at")
        edited = values.index("Last edited at")
        record = {
            "title": values[0],
            "created_at": values[created + 1],
            "last_edited_at": values[edited + 1],
        }
    except (IndexError, ValueError):
        raise NotesnookCollectorError("note metadata is unavailable") from None
    if created == 0 or edited <= created + 1:
        raise NotesnookCollectorError("note metadata is unavailable")
    return record


def _attachment_rows(root: ET.Element) -> tuple[dict[str, object], ...]:
    rows = []
    for node in root.iter():
        match = _ATTACHMENT_ROW.fullmatch(
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
    changed = tuple(item for item in after if item not in before)
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


def _trash_metadata(root: ET.Element) -> dict[str, object]:
    values = [
        node.get("text", "").strip()
        for node in root.iter()
        if _shown(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
    ]
    try:
        created = values.index("Created at")
        deleted = values.index("Deleted at")
        edited = values.index("Last edited at")
        record = {
            "title": values[0],
            "created_at": values[created + 1],
            "deleted_at": values[deleted + 1],
            "last_edited_at": values[edited + 1],
        }
    except (IndexError, ValueError):
        raise NotesnookCollectorError("trash metadata is unavailable") from None
    if created == 0 or deleted <= created + 1 or edited <= deleted + 1:
        raise NotesnookCollectorError("trash metadata is unavailable")
    controls = {
        node.get("content-desc", "").strip()
        for node in root.iter()
        if _shown(node) and node.get("clickable") == "true"
    }
    return {
        **record,
        "restore_control_observed": "Restore" in controls,
        "delete_control_observed": "Delete" in controls,
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
    match = _SELECTION_COUNT.fullmatch(headers[0].get("content-desc", ""))
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


def _export_success(root: ET.Element) -> tuple[int, str]:
    labels = [
        node.get("text", "")
        for node in root.iter()
        if _shown(node) and node.get("class") == "android.widget.TextView"
    ]
    counts = [
        match
        for label in labels
        if (match := _EXPORTED_COUNT.fullmatch(label)) is not None
    ]
    files = [
        match
        for label in labels
        if (match := _EXPORTED_FILE.fullmatch(label)) is not None
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
    if len(matches) != 1:
        raise NotesnookCollectorError(f"{element_id} is unavailable")
    return _bounds(matches[0])


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


def collect_account_context(
    device,
    outputs,
    target_ref: str,
    acquisition_environment: str,
) -> dict[str, object]:
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
        if recovery == 4 or not any(node.get("package") in {PACKAGE, DOCUMENTS_PACKAGE}
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
        record["sync_gate_passed"] = False
        return record

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
    account_record = _account_record(account_tree)
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
            acquisition_environment,
        )
        if not sync_state["sync_label"].startswith("Syncing"):
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
    return record


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
    rows = _history_rows(_tree(device, history_observation_id))
    outputs.write_record(
        f"materialize/notes/{note_ref}/history/history.json",
        {
            "target_ref": target_ref,
            "note_ref": note_ref,
            "versions": [
                {
                    "version_ref": row["version_ref"],
                    "label": row["label"],
                }
                for row in rows
            ],
        },
        action_id=f"action-open-{note_ref}-history",
        observation_id=history_observation_id,
        include_observation=True,
    )

    for index, expected in enumerate(rows):
        current_rows = _history_rows(_tree(device, history_observation_id))
        if (
            len(current_rows) != len(rows)
            or current_rows[index]["label"] != expected["label"]
        ):
            raise NotesnookCollectorError("history version is unavailable")
        version_ref = expected["version_ref"]
        if hasattr(outputs, "begin_item"):
            outputs.begin_item(f"revision:{note_ref}:{version_ref}", "notesnook.notes", "revision", note_ref=note_ref, version_ref=version_ref)
        open_action_id = f"action-open-{note_ref}-{version_ref}"
        device.click_bounds(
            open_action_id,
            current_rows[index]["bounds"],
            history_observation_id,
        )
        version_observation_id = f"observation-{note_ref}-{version_ref}"
        version = device.observe(version_observation_id)
        version_observation_id = version["observation_id"]
        record = {
            "target_ref": target_ref,
            "note_ref": note_ref,
            "version_ref": version_ref,
            "label": expected["label"],
            **_history_version(_tree(device, version_observation_id)),
        }
        outputs.write_record(
            f"materialize/notes/{note_ref}/history/{version_ref}.json",
            record,
            action_id=open_action_id,
            observation_id=version_observation_id,
            include_observation=True,
        )
        device.back(
            f"action-return-{note_ref}-{version_ref}",
            version_observation_id,
        )
        returned_history = device.observe(
            f"observation-{note_ref}-history-after-{version_ref}"
        )
        history_observation_id = returned_history["observation_id"]

    device.back(f"action-close-{note_ref}-history", history_observation_id)
    returned_id = (
        f"observation-materialize-notes-after-{note_ref}-history"
    )
    returned = device.observe(returned_id)
    if _workspace_state(
        device, _tree(device, returned["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("note list return is unavailable")
    return len(rows), returned["observation_id"]


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
    seen_pages = set()
    seen_rows = {}
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
        rows = _attachment_rows(root)
        signature = tuple(
            (
                row["kind_label"],
                row["filename"],
                row["displayed_size"],
            )
            for row in rows
        )
        if signature in seen_pages:
            break
        seen_pages.add(signature)

        for row in rows:
            key = (
                row["kind_label"],
                row["filename"],
                row["displayed_size"],
            )
            if key in seen_rows:
                continue
            attachment_ref = f"attachment-{len(seen_rows) + 1:06d}"
            if hasattr(outputs, "begin_item"):
                outputs.begin_item(f"attachment:{note_ref}:{attachment_ref}", "notesnook.attachments", "attachment", note_ref=note_ref, attachment_ref=attachment_ref)
            expected = {**row, "attachment_ref": attachment_ref}
            seen_rows[key] = expected
            current_rows = _attachment_rows(
                _tree(device, list_observation_id)
            )
            current = [
                item
                for item in current_rows
                if (
                    item["kind_label"],
                    item["filename"],
                    item["displayed_size"],
                ) == key
            ]
            if len(current) != 1:
                raise NotesnookCollectorError(
                    "attachment row is unavailable"
                )
            row_action_id = f"action-open-{note_ref}-{attachment_ref}"
            device.click_bounds(
                row_action_id,
                current[0]["bounds"],
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
                for row in seen_rows.values()
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
    device.click_bounds(
        open_action_id,
        card_bounds,
        notes_observation_id,
    )
    record = None
    observation_id = ""
    for attempt in range(1, 7):
        suffix = "" if attempt == 1 else f"-retry-{attempt:06d}"
        current = device.observe(f"observation-{note_ref}-current{suffix}")
        observation_id = current["observation_id"]
        record = _current_note_record(_tree(device, observation_id))
        if record is not None:
            break
    if record is None:
        raise NotesnookCollectorError("current note is unavailable")
    outputs.write_record(
        f"materialize/notes/{note_ref}/current.json",
        {
            "target_ref": target_ref,
            "note_ref": note_ref,
            **record,
        },
        action_id=open_action_id,
        observation_id=observation_id,
        include_observation=True,
    )
    device.back(f"action-close-{note_ref}-current", observation_id)
    returned = device.observe(
        f"observation-materialize-notes-after-{note_ref}-current"
    )
    if _workspace_state(
        device, _tree(device, returned["observation_id"])
    ) != "authenticated":
        raise NotesnookCollectorError("note list return is unavailable")
    return (
        returned["observation_id"],
        record["content_status"] == "partial",
    )


def _rewind_note_list(device) -> str:
    seen = set()
    page = 1
    while True:
        observation = device.observe(
            f"observation-materialize-notes-rewind-{page:06d}"
        )
        observation_id = observation["observation_id"]
        root = _tree(device, observation_id)
        if _workspace_state(device, root) != "authenticated":
            raise NotesnookCollectorError(
                "authenticated workspace is unavailable"
            )
        note_list = next(
            node
            for node in device.matching_elements(
                root, "notesnook.notes-list"
            )
            if _shown(node)
        )
        signature = tuple(
            (node.get("resource-id"), node.get("content-desc", ""))
            for node in note_list.iter()
            if _shown(node)
            and node.get("resource-id", "").startswith("note-item-")
            and node.get("content-desc", "")
        )
        if signature in seen:
            return observation_id
        seen.add(signature)
        device.swipe(
            f"action-rewind-materialize-notes-{page:06d}",
            "down",
            observation_id,
        )
        page += 1


def _materialize_notes(
    device,
    outputs,
    target_ref: str,
) -> tuple[int, int, int, int, str]:
    page = 1
    observation_id = _rewind_note_list(device)
    seen_pages = set()
    seen_notes = set()
    note_count = history_count = attachment_count = current_partial_count = 0
    while True:
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
        signature = tuple(card["source_key"] for card in cards)
        if signature in seen_pages:
            return (
                note_count,
                history_count,
                attachment_count,
                current_partial_count,
                observation_id,
            )
        seen_pages.add(signature)
        if not cards:
            return (
                note_count,
                history_count,
                attachment_count,
                current_partial_count,
                observation_id,
            )

        for expected in cards:
            if expected["source_key"] in seen_notes:
                continue
            current = next(
                (
                    card
                    for card in _note_cards(
                        device, _tree(device, observation_id)
                    )
                    if card["source_key"] == expected["source_key"]
                ),
                None,
            )
            if current is None:
                raise NotesnookCollectorError("note card is unavailable")
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
            current = next(
                (
                    card
                    for card in _note_cards(
                        device, _tree(device, observation_id)
                    )
                    if card["source_key"] == expected["source_key"]
                ),
                None,
            )
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
                **_note_menu_record(_tree(device, menu_observation_id)),
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
            current = next(
                (
                    card
                    for card in _note_cards(
                        device, _tree(device, observation_id)
                    )
                    if card["source_key"] == expected["source_key"]
                ),
                None,
            )
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

        device.swipe(
            f"action-scroll-materialize-notes-page-{page:06d}",
            "up",
            observation_id,
        )
        page += 1
        observation = device.observe(
            f"observation-materialize-notes-page-{page:06d}"
        )
        observation_id = observation["observation_id"]


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
    while True:
        cards = _trash_cards(device, _tree(device, observation_id))
        signature = tuple(card["source_key"] for card in cards)
        if signature in seen_pages:
            break
        seen_pages.add(signature)
        if not cards:
            break
        for expected in cards:
            if expected["source_key"] in seen_items:
                continue
            current = next(
                (
                    card
                    for card in _trash_cards(
                        device, _tree(device, observation_id)
                    )
                    if card["source_key"] == expected["source_key"]
                ),
                None,
            )
            if current is None:
                raise NotesnookCollectorError("trash card is unavailable")
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
                **_trash_metadata(_tree(device, menu_observation_id)),
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

        device.swipe(
            f"action-scroll-trash-page-{page:06d}",
            "up",
            observation_id,
        )
        page += 1
        trash = device.observe(f"observation-trash-page-{page:06d}")
        observation_id = trash["observation_id"]

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


def materialize(device, outputs, target_ref: str) -> dict[str, object]:
    if hasattr(outputs, "context"):
        outputs.context.begin_identification("notesnook.notes")
        outputs.context.begin_identification("notesnook.attachments")
    try:
        notes, versions, attachments, current_partial, notes_observation_id = (
            _materialize_notes(device, outputs, target_ref)
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
    report = {
        "status": "partial" if current_partial else "complete",
        "reason_code": (
            "notesnook_current_note_partial"
            if current_partial
            else None
        ),
        "target_ref": target_ref,
        "route": "materialize",
        "note_count": notes,
        "history_version_count": versions,
        "attachment_count": attachments,
        "trash_count": trash,
        "current_note_partial_count": current_partial,
    }
    outputs.write_record(
        "materialize/summary.json",
        report,
        action_id="action-open-trash",
    )
    if hasattr(outputs, "context"):
        for target_id in ("notesnook.notes", "notesnook.attachments"):
            outputs.context.finish_identification(target_id, completion_condition="notes_and_trash_exhausted")
    return report


def collect(device, outputs, target_ref: str) -> dict[str, object]:
    if hasattr(outputs, "begin_item"):
        outputs.context.begin_identification("notesnook.export")
        outputs.begin_item(f"export:{target_ref}", "notesnook.export", "note_export")
    ordinary = device.observe("observation-ordinary-list")
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

    returned = device.observe("observation-returned")
    try:
        returned_count, reported_filename = _export_success(
            _tree(device, returned["observation_id"])
        )
    except NotesnookCollectorError:
        return _partial("application_return_unverified", target_ref)
    if returned_count != selected_count:
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
