"""WhatsApp 2.26.27.85 Export flow up to Bluetooth identification."""

from __future__ import annotations
from ...collection import (
    stability_probe,
    selector_conditions,
    one_candidate,
    poll_samples,
    first_stable,
    matching_nodes,
)
from ...parsing import parse_bounds

from ...artifacts import changed_inventory


import math
import hashlib
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO, TextIOWrapper
from pathlib import Path
from zipfile import BadZipFile, ZipFile
from zlib import error as ZlibError

from ...models import AcquisitionStatus, ActionRef, ProcedureStatus
from ...profiles import AppProfile
from ...ui import hierarchy_signature
from ...windows_bluetooth import (
    close_waiting_windows_bluetooth_receiver,
    finish_windows_bluetooth_receive,
    normalize_bluetooth_label as _normalized_label,
    prepare_windows_bluetooth_receiver,
)


PACKAGE = "com.whatsapp"
_UNSAFE_PATH = re.compile(r"[/\\\x00-\x1f]")
_TRANSCRIPT_LINE = re.compile(
    r"^\[?\d{1,4}[./-]\s?\d{1,2}[./-]\s?\d{1,4}[.,]?\s+"
    r"[^\r\n]*?\d{1,2}:\d{2}(?::\d{2})?[^\r\n]*?(?:\]\s+| - )\S"
)


class WhatsAppCollectorError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        message: str | None = None,
        *,
        action: ActionRef | None = None,
        retryable: bool = False,
    ):
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.action = action
        self.retryable = retryable
        self.retry_reason = "not_transfer_failure"


def acquisition_item_attempt(
    context,
    linked: Mapping[str, object],
    *,
    document: Mapping[str, object] | None = None,
) -> str:
    document = document or {}
    record_kind = str(document.get("record_kind", ""))
    if record_kind in {"whatsapp_active_chat_traversal", "whatsapp_active_chat_export_traversal"}:
        target_id = "whatsapp.export_summary" if context.base_context.get("route") == "export" else "whatsapp.conversations"
        return context.item_attempt(
            "whatsapp:list-summary", target_id, "conversation_collection",
            source=dict(linked), context=dict(linked), retention=True,
        )
    if "attachment_ref" in linked or record_kind == "whatsapp_attachment":
        kind = str(
            document.get("kind", linked.get("attachment_kind", "file"))
        )
        item_type = "file" if kind == "document" else kind
        if item_type not in {"photo", "video", "file"}:
            item_type = "file"
        target_id = "whatsapp.attachments"
        ref = linked.get("attachment_ref", "attachment")
    elif "message_ref" in linked or record_kind == "whatsapp_message":
        target_id, item_type = "whatsapp.conversations", "message"
        ref = linked.get("message_ref", "message")
    elif (
        "export" in record_kind
        or context.base_context.get("route") == "export"
    ):
        target_id = "whatsapp.chat_export"
        item_type = "conversation_export"
        ref = linked.get("chat_ref", linked.get("target_ref", "export"))
    else:
        target_id, item_type = "whatsapp.conversations", "conversation"
        ref = linked.get("chat_ref", linked.get("target_ref", "conversation"))
    source = dict(linked)
    key_base = linked.get("logical_chatroom_id", linked.get("target_ref", ""))
    return context.item_attempt(
        f"{target_id}:{key_base}:{ref}",
        target_id,
        item_type,
        source=source,
        context=source,
    )


def retention_action(
    context,
    attempt_id: str,
    action: ActionRef,
    linked: Mapping[str, object],
) -> ActionRef:
    return context.linked_action(
        "retain_whatsapp_output",
        attempt_id,
        source_action=action,
        context=dict(linked),
    )


def _xml_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, str):
        return value
    raise WhatsAppCollectorError(
        "whatsapp_profile_invalid",
        "WhatsApp selector is invalid",
    )


def _bounds(node):
    try:
        return parse_bounds(node.get('bounds', ''), allow_negative=False)
    except ValueError:
        raise WhatsAppCollectorError("whatsapp_ui_bounds_invalid", "UI bounds are invalid") from None


def _visible(node: ET.Element, root: ET.Element) -> bool:
    if node.get("visible-to-user") == "false":
        return False
    try:
        left, top, right, bottom = _bounds(node)
    except WhatsAppCollectorError:
        return False
    for window in [root] if root.get("bounds") else root:
        try:
            viewport = _bounds(window)
        except WhatsAppCollectorError:
            continue
        if (
            window.get("visible-to-user") != "false"
            and left < viewport[2] and top < viewport[3]
            and right > viewport[0] and bottom > viewport[1]
        ):
            return True
    return False


def _selector_spec(
    profile: AppProfile,
    element_id: str,
) -> tuple[str, dict[str, str]]:
    try:
        entry = profile.selectors[element_id]
        selector = entry["selector"]
        owner = selector["owner_package"]
        expected = {
            **selector_conditions({selector["kind"]: selector["value"]}, backend="xml", transform=_xml_value)
        }
        constraints = entry.get("constraints", {})
    except (KeyError, TypeError):
        raise WhatsAppCollectorError(
            "whatsapp_profile_invalid",
            f"unknown WhatsApp element: {element_id}",
        ) from None
    if owner != PACKAGE or not isinstance(constraints, Mapping):
        raise WhatsAppCollectorError(
            "whatsapp_profile_invalid",
            "WhatsApp selector is invalid",
        )
    try:
        expected.update(selector_conditions(constraints, backend="xml", transform=_xml_value))
    except KeyError:
        raise WhatsAppCollectorError(
            "whatsapp_profile_invalid",
            "WhatsApp selector constraint is invalid",
        ) from None
    return owner, expected


def _matching(
    root: ET.Element,
    profile: AppProfile,
    element_id: str,
) -> tuple[ET.Element, ...]:
    owner, expected = _selector_spec(profile, element_id)
    return matching_nodes(root, owner, expected, visible=_visible)


def _unique_bounds(
    root: ET.Element,
    profile: AppProfile,
    element_id: str,
    reason_code: str,
    label: str,
) -> tuple[int, int, int, int]:
    matches = _matching(root, profile, element_id)
    try:
        node = one_candidate(matches)
    except ValueError as error:
        raise WhatsAppCollectorError(reason_code, f"{label} is {error}") from None
    return _bounds(node)


def chat_row_bounds(
    root: ET.Element,
    profile: AppProfile,
    display_name: str,
) -> tuple[int, int, int, int]:
    names = [
        node
        for node in _matching(root, profile, "whatsapp.chat-name")
        if node.get("text", "").strip() == display_name
    ]
    if len(names) != 1:
        qualifier = "unavailable" if not names else "ambiguous"
        raise WhatsAppCollectorError(
            f"whatsapp_chat_target_{qualifier}",
            f"chat target is {qualifier}",
        )
    parents = {
        child: parent
        for parent in root.iter()
        for child in parent
    }
    rows = set(_matching(root, profile, "whatsapp.chat-row"))
    current = parents.get(names[0])
    while current is not None and current not in rows:
        current = parents.get(current)
    if current is None:
        raise WhatsAppCollectorError(
            "whatsapp_chat_target_unavailable",
            "chat target is unavailable",
        )
    return _bounds(current)


def bluetooth_target_bounds(
    root: ET.Element,
    operation: Mapping[str, object],
) -> tuple[int, int, int, int]:
    _, owners, resource_ids, labels, _ = _share_sheet_spec(operation)
    return _share_target_bounds(
        root,
        owners,
        resource_ids,
        labels,
        "whatsapp_bluetooth_target",
        "Bluetooth target",
    )


def receiver_bounds(
    root: ET.Element,
    operation: Mapping[str, object],
    receiver_label: str,
) -> tuple[int, int, int, int]:
    try:
        picker = operation["receiver_picker"]
        owners = set(picker["owner_packages"])
        title_labels = set(picker["title_labels"])
        resource_ids = set(picker["receiver_resource_ids"])
    except (KeyError, TypeError):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth receiver Profile is invalid",
        ) from None
    if (
        not isinstance(picker, Mapping)
        or not owners
        or not title_labels
        or not resource_ids
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth receiver Profile is invalid",
        )
    if not any(
        _visible(node, root)
        and node.get("package") in owners
        and node.get("text", "").strip() in title_labels
        for node in root.iter()
    ):
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_receiver_unavailable",
            "Bluetooth receiver picker is unavailable",
        )
    expected = _normalized_label(receiver_label)
    matches = [
        node
        for node in root.iter()
        if _visible(node, root)
        and node.get("package") in owners
        and node.get("resource-id") in resource_ids
        and _normalized_label(node.get("text", "")) == expected
    ]
    if not matches:
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_receiver_unavailable",
            "Bluetooth receiver is unavailable",
        )
    parents = {
        child: parent
        for parent in root.iter()
        for child in parent
    }
    # ponytail: PoC uses the first matching UI row; identity-based selection can replace it.
    current = matches[0]
    while current is not None and current.get("clickable") != "true":
        current = parents.get(current)
    if current is None:
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_receiver_unavailable",
            "Bluetooth receiver is unavailable",
        )
    return _bounds(current)


def _share_sheet_spec(operation: Mapping[str, object]):
    try:
        strategy = operation["strategy"]
        owners = set(operation["owner_packages"])
        resource_ids = set(operation["resource_ids"])
        labels = set(operation["bluetooth_labels"])
    except (KeyError, TypeError):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        ) from None
    if (
        strategy != "visible_then_expand"
        or not owners
        or not resource_ids
        or not labels
        or not all(isinstance(value, str) and value for value in owners)
        or not all(isinstance(value, str) and value for value in resource_ids)
        or not all(isinstance(value, str) and value for value in labels)
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        )
    fallback = operation.get("fallback")
    if (
        not isinstance(fallback, Mapping)
        or fallback.get("swipe_direction") not in {"left", "right", "up", "down"}
        or type(fallback.get("max_swipes")) is not int
        or not 1 <= fallback["max_swipes"] <= 20
        or not isinstance(fallback.get("swipe_container_resource_ids"), list)
        or not fallback["swipe_container_resource_ids"]
        or not all(isinstance(value, str) and value for value in fallback["swipe_container_resource_ids"])
        or not isinstance(fallback.get("more_labels"), list)
        or not all(
            isinstance(value, str) and value
            for value in fallback["more_labels"]
        )
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        )
    overrides = fallback.get("swipe_direction_overrides", {})
    if not isinstance(overrides, Mapping) or any(
        resource_id not in fallback["swipe_container_resource_ids"]
        or not isinstance(direction, str) or direction not in {"left", "right", "up", "down"}
        for resource_id, direction in overrides.items()
    ):
        raise WhatsAppCollectorError("whatsapp_share_profile_invalid", "Invalid share-list swipe direction")
    return strategy, owners, resource_ids, labels, fallback


def _share_target_bounds(
    root: ET.Element,
    owners: set[str],
    resource_ids: set[str],
    labels: set[str],
    reason_prefix: str,
    label: str,
) -> tuple[int, int, int, int]:
    matches = [
        node
        for node in root.iter()
        if _visible(node, root)
        and node.get("package") in owners
        and node.get("resource-id") in resource_ids
        and node.get("text", "").strip() in labels
    ]
    if len(matches) != 1:
        qualifier = "unavailable" if not matches else "ambiguous"
        raise WhatsAppCollectorError(
            f"{reason_prefix}_{qualifier}",
            f"{label} is {qualifier}",
        )
    return _bounds(matches[0])


def _share_sheet_marker(
    root: ET.Element,
    operation: Mapping[str, object],
) -> bool:
    _, owners, _, _, _ = _share_sheet_spec(operation)
    if not any(
        _visible(node, root) and node.get("package") in owners
        for node in root.iter()
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_sheet_unavailable",
            "Android share sheet is unavailable",
        )
    return True


def _share_scroll_container(root, owners, fallback):
    containers = [
        node for node in root.iter()
        if _visible(node, root)
        and node.get("package") in owners
        and node.get("resource-id") in fallback["swipe_container_resource_ids"]
    ]
    if len(containers) > 1:
        # S8 switches from the outer ScrollView to the inner list after expansion.
        containers = [node for node in containers if node.get("scrollable") == "true"]
    if len(containers) != 1:
        raise WhatsAppCollectorError(
            "whatsapp_share_fallback_unavailable",
            "Expected one visible share-app scrolling container",
        )
    return containers[0]


def _bluetooth_share_state(context, operation, state, linked):
    root, hierarchy, _ = state
    try:
        return root, hierarchy, bluetooth_target_bounds(root, operation)
    except WhatsAppCollectorError as error:
        if error.reason_code != "whatsapp_bluetooth_target_unavailable":
            raise
    try:
        return _settled_root(
            context,
            lambda candidate: bluetooth_target_bounds(
                candidate,
                operation,
            ),
            "whatsapp_bluetooth_target_unavailable",
            timeout=context.app_profile.timings["share_target_wait"],
        )
    except WhatsAppCollectorError as error:
        if error.reason_code != "whatsapp_bluetooth_target_unavailable":
            raise

    _, owners, resource_ids, _, fallback = _share_sheet_spec(operation)
    root, hierarchy, _ = _settled_root(
        context, lambda candidate: _share_sheet_marker(candidate, operation),
        "whatsapp_share_sheet_unavailable",
    )
    container = _share_scroll_container(root, owners, fallback)
    first = fallback.get("swipe_direction_overrides", {}).get(container.get("resource-id"), fallback["swipe_direction"])
    opposite = {"left": "right", "right": "left", "up": "down", "down": "up"}
    for direction in (first, opposite[first]):
        for step in range(fallback["max_swipes"] + 1):
            try:
                return root, hierarchy, bluetooth_target_bounds(root, operation)
            except WhatsAppCollectorError as error:
                if error.reason_code != "whatsapp_bluetooth_target_unavailable":
                    raise
            container = _share_scroll_container(root, owners, fallback)
            try:
                more_bounds = _share_target_bounds(
                    container, owners, resource_ids, set(fallback["more_labels"]),
                    "whatsapp_share_more", "share-sheet More target",
                )
            except WhatsAppCollectorError as error:
                if error.reason_code != "whatsapp_share_more_unavailable":
                    raise
            else:
                _, expanded_state = _click_and_wait(
                    context, "expand_whatsapp_share_targets", more_bounds,
                    lambda candidate: bluetooth_target_bounds(candidate, operation),
                    "whatsapp_bluetooth_target_unavailable", linked,
                )
                return expanded_state
            if step == fallback["max_swipes"]:
                break
            before = hierarchy_signature(container)
            try:
                context.ui.swipe_bounds(
                    "scan_whatsapp_share_targets", _bounds(container), direction,
                    context=linked,
                )
            except Exception as error:
                raise WhatsAppCollectorError("whatsapp_share_fallback_unavailable", str(error)) from error
            root, hierarchy, _ = _settled_root(
                context, lambda candidate: _share_sheet_marker(candidate, operation),
                "whatsapp_share_sheet_unavailable",
            )
            if hierarchy_signature(_share_scroll_container(root, owners, fallback)) == before:
                break
    raise WhatsAppCollectorError(
        "whatsapp_share_fallback_unavailable", "Bluetooth and More were not found in the share-app list",
    )


def _receiver_picker_state(context, operation, receiver_label, linked):
    picker = operation["receiver_picker"]
    discovery = picker.get("discovery", {})
    if not isinstance(discovery, Mapping):
        raise WhatsAppCollectorError("whatsapp_share_profile_invalid", "Invalid Bluetooth discovery settings")
    retries = discovery.get("scan_retries", 0)
    labels = discovery.get("scan_labels", [])
    if (type(retries) is not int or not 0 <= retries <= 10
            or not isinstance(labels, list) or not all(isinstance(label, str) and label for label in labels)):
        raise WhatsAppCollectorError("whatsapp_share_profile_invalid", "Invalid Bluetooth discovery settings")
    for key in ("wait_sec", "scan_wait_sec"):
        value = discovery.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise WhatsAppCollectorError("whatsapp_share_profile_invalid", "Invalid Bluetooth discovery timeout")
    for attempt in range(retries + 1):
        try:
            return _settled_root(
                context, lambda root: receiver_bounds(root, operation, receiver_label),
                "whatsapp_bluetooth_receiver_unavailable",
                timeout=discovery.get("wait_sec" if attempt == 0 else "scan_wait_sec"),
            )
        except WhatsAppCollectorError as error:
            if error.reason_code != "whatsapp_bluetooth_receiver_unavailable" or attempt == retries:
                raise
            root = ET.fromstring(context.device.hierarchy())
            if not any(_visible(node, root) and node.get("package") in picker["owner_packages"]
                       and node.get("text", "").strip() in picker["title_labels"] for node in root.iter()):
                raise
            scans = [node for node in root.iter()
                     if _visible(node, root) and node.get("package") in picker["owner_packages"]
                     and node.get("enabled") != "false"
                     and (node.get("text", "").strip() in labels or node.get("content-desc", "").strip() in labels)]
            if len(scans) != 1:
                raise
            context.ui.click_bounds("refresh_whatsapp_bluetooth_receivers", _bounds(scans[0]),
                                    context={**linked, "scan_attempt": attempt + 1})


def _select_bluetooth_receiver(context, operation, receiver_label, linked):
    picker = operation["receiver_picker"]
    stop_labels = picker.get("discovery", {}).get("stop_labels", [])
    if not isinstance(stop_labels, list) or not all(isinstance(label, str) and label for label in stop_labels):
        raise WhatsAppCollectorError("whatsapp_share_profile_invalid", "Invalid Bluetooth stop labels")

    def stop_buttons(root):
        return [node for node in root.iter() if _visible(node, root)
                and node.get("package") in picker["owner_packages"]
                and node.get("clickable") == "true" and node.get("enabled") != "false"
                and (node.get("text", "").strip() in stop_labels
                     or node.get("content-desc", "").strip() in stop_labels)]

    # Evidence capture can outlive a discovery animation; read the target again.
    root, _, _ = _receiver_picker_state(context, operation, receiver_label, linked)
    stops = stop_buttons(root)
    if len(stops) > 1:
        raise WhatsAppCollectorError("whatsapp_bluetooth_receiver_unavailable", "Bluetooth Stop is ambiguous")
    if stops:
        context.ui.click_bounds("stop_whatsapp_bluetooth_discovery", _bounds(stops[0]), context=linked)

    def ready(root):
        if stop_buttons(root):
            raise WhatsAppCollectorError("whatsapp_bluetooth_receiver_unavailable", "Bluetooth discovery is still active")
        return receiver_bounds(root, operation, receiver_label)

    _, _, bounds = _settled_root(context, ready, "whatsapp_bluetooth_receiver_unavailable",
        stable_for=context.app_profile.timings["transition_settle"],
        signature=lambda root: (hierarchy_signature(root), receiver_bounds(root, operation, receiver_label)))

    def closed(root):
        visible = [node for node in root.iter() if _visible(node, root)]
        if (not any(node.get("package") not in {None, "com.android.systemui"} for node in visible)
                or any(node.get("package") in picker["owner_packages"]
                       and node.get("text", "").strip() in picker["title_labels"] for node in visible)):
            raise WhatsAppCollectorError("whatsapp_bluetooth_receiver_selection_unconfirmed")
        return True

    linked = {**linked, "receiver_label": receiver_label}
    try:
        _click_and_wait(context, "select_whatsapp_bluetooth_receiver", bounds, closed,
                        "whatsapp_bluetooth_receiver_selection_unconfirmed", linked)
    except WhatsAppCollectorError:
        context.journal.record_action("confirm_whatsapp_bluetooth_receiver_selection", status="failed", context=linked)
        raise
    context.journal.record_action("confirm_whatsapp_bluetooth_receiver_selection", status="success", context=linked)


def _safe_component(value: str) -> str:
    result = " ".join(_UNSAFE_PATH.sub("_", value).split())[:120]
    if not result or result in {".", ".."}:
        raise WhatsAppCollectorError(
            "whatsapp_target_invalid",
            "target reference is invalid",
        )
    return result


def export_bluetooth_condition(
    condition: Mapping[str, object],
) -> tuple[str, tuple[Path, ...]]:
    bluetooth = condition.get("bluetooth")
    if not isinstance(bluetooth, Mapping):
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_condition_invalid"
        )
    receiver_label = bluetooth.get("receiver_label")
    receive_dir = bluetooth.get("receive_dir")
    if (
        not isinstance(receiver_label, str)
        or not receiver_label.strip()
        or not isinstance(receive_dir, str)
        or not receive_dir.strip()
    ):
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_condition_invalid"
        )
    candidates = bluetooth.get("receive_dirs", [receive_dir])
    if not isinstance(candidates, list) or not candidates or not all(isinstance(path, str) and path.strip() for path in candidates):
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_condition_invalid"
        )
    directories = []
    for candidate in (receive_dir, *candidates):
        directory = Path(candidate).expanduser()
        try:
            resolved = directory.resolve(strict=True)
        except (OSError, ValueError):
            raise WhatsAppCollectorError("whatsapp_bluetooth_condition_invalid") from None
        if (not directory.is_absolute() or not resolved.is_dir()
                or resolved == Path(resolved.anchor) or resolved == Path.home().resolve()):
            raise WhatsAppCollectorError("whatsapp_bluetooth_condition_invalid")
        if resolved not in directories:
            directories.append(resolved)
    return receiver_label.strip(), tuple(directories)


def _file_inventory(directory: Path | tuple[Path, ...]) -> dict[Path, tuple[int, int]]:
    result = {}
    directories = (directory,) if isinstance(directory, Path) else directory
    for root in directories:
        for path in root.iterdir():
            if path.name.startswith(".") or not path.is_file():
                continue
            stat = path.stat()
            result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def _wait_for_received_file(
    directory: Path | tuple[Path, ...],
    baseline: Mapping[Path, tuple[int, int]],
    *,
    timeout: float,
    poll_interval: float,
    expected_names: list[str] | None = None,
) -> Path:
    names = {_normalized_label(name).casefold() for name in (expected_names or [])}
    def candidate():
        changed = {path: signature for path, signature in changed_inventory(
            baseline.items(), _file_inventory(directory).items(), signature=lambda row: row[1])
            if not names or _normalized_label(path.name).casefold() in names}
        if len(changed) > 1:
            raise WhatsAppCollectorError("whatsapp_export_receipt_ambiguous")
        return next(iter(changed.items()), None)
    selected = first_stable(poll_samples(candidate, timeout=max(0.0, timeout),
                                        interval=max(0.0, poll_interval)))
    if selected is not None:
        return selected[0]
    raise WhatsAppCollectorError("whatsapp_export_receipt_missing")


def _export_receipt_error(path: Path) -> str | None:
    if path.suffix.casefold() != ".zip":
        return "whatsapp_export_zip_invalid"
    try:
        with ZipFile(path) as archive:
            if archive.testzip() is not None:
                return "whatsapp_export_zip_corrupt"
            for member in archive.infolist():
                if member.is_dir() or not member.filename.casefold().endswith(".txt"):
                    continue
                try:
                    with TextIOWrapper(archive.open(member), encoding="utf-8-sig") as text:
                        # ponytail: timestamp structure covers messages and system rows;
                        # extend locale shapes only with a captured export.
                        if any(_TRANSCRIPT_LINE.match(unicodedata.normalize(
                            "NFKC", line.lstrip("\u200e\u200f")
                        )) for line in text):
                            return None
                except UnicodeDecodeError:
                    continue
    except (BadZipFile, OSError, RuntimeError, NotImplementedError, ZlibError):
        return "whatsapp_export_zip_invalid"
    return "whatsapp_export_transcript_missing"


def _settled_root(context, parser, reason_code: str, *, timeout: float | None = None,
                  stable_for: float = 0.0, signature=hierarchy_signature):
    settle = context.app_profile.timings['transition_settle']
    if settle:
        time.sleep(settle)
    resolved = None
    def key(value):
        nonlocal resolved
        root = ET.fromstring(value)
        parsed = parser(root)
        resolved = (root, value, parsed)
        return signature(root)
    ready, state = stability_probe(context.device.hierarchy, key=key,
        stable_for=stable_for,
        retry_errors=(ET.ParseError, TypeError, WhatsAppCollectorError))
    if not context.ui.wait_until(ready, timeout=timeout):
        if isinstance(state['error'], WhatsAppCollectorError):
            raise state['error']
        raise WhatsAppCollectorError(reason_code)
    return resolved


def _click_and_wait(
    context,
    action_name: str,
    bounds: tuple[int, int, int, int],
    parser,
    reason_code: str,
    linked: dict[str, object],
):
    action = context.ui.click_bounds(
        action_name,
        bounds,
        context=linked,
    )
    return action, _settled_root(context, parser, reason_code)


def identify_bluetooth(
    context,
    target: Mapping[str, str],
    *,
    already_open: bool = False,
) -> dict[str, object]:
    linked = {
        "target_ref": target["ref"],
        "display_name": target["display_name"],
    }
    chat_parser = lambda root: (
        _unique_bounds(
            root,
            context.app_profile,
            "whatsapp.chat-screen",
            "whatsapp_chat_screen_unavailable",
            "chat screen",
        ),
        _unique_bounds(
            root,
            context.app_profile,
            "whatsapp.more-options",
            "whatsapp_more_options_unavailable",
            "More options",
        ),
    )
    if already_open:
        chat_state = _settled_root(
            context,
            chat_parser,
            "whatsapp_chat_screen_unavailable",
        )
    else:
        _, _, row = _settled_root(
            context,
            lambda root: chat_row_bounds(
                root,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_chat_list_unavailable",
        )
        _, chat_state = _click_and_wait(
            context,
            "open_whatsapp_chat",
            row,
            chat_parser,
            "whatsapp_chat_screen_unavailable",
            linked,
        )
    header = _matching(chat_state[0], context.app_profile, "whatsapp.chat-screen")[0]
    if _normalized_label(header.get("text", "")) != _normalized_label(target["display_name"]):
        reason = "whatsapp_export_chat_mismatch"
        attempt_id = acquisition_item_attempt(context, linked)
        action = context.journal.record_action(
            "verify_whatsapp_export_chat", status="failed", attempt_id=attempt_id,
            context=linked, details={"reason": reason, "observed_name": header.get("text", "")})
        try:
            retained = retention_action(context, attempt_id, action, linked)
            context.retain_observation(
                f"whatsapp/{_safe_component(target['ref'])}/export/{attempt_id}/chat-mismatch-screen",
                context.device.screenshot(), chat_state[1].encode("utf-8"),
                attempt_id=attempt_id, action=retained, context=dict(linked))
        except Exception as error:
            context.journal.record_action(
                "retain_whatsapp_chat_mismatch", status="failed", attempt_id=attempt_id,
                context=linked, details={"error_type": type(error).__name__, "error": str(error)})
        raise WhatsAppCollectorError(reason, action=action)
    more_options = chat_state[2][1]
    menu_parser = lambda root: _unique_bounds(
        root, context.app_profile, "whatsapp.more", "whatsapp_more_menu_unavailable", "More",
    )
    try:
        _, menu_state = _click_and_wait(
            context, "open_whatsapp_more_options", more_options, menu_parser,
            "whatsapp_more_menu_unavailable", linked,
        )
    except WhatsAppCollectorError as error:
        if error.reason_code != "whatsapp_more_menu_unavailable":
            raise
        context.journal.record_action("open_whatsapp_more_options", status="retrying",
            context=linked, details={"retry": 1, "reason": error.reason_code})

        def retry_state(root):
            if _matching(root, context.app_profile, "whatsapp.more"):
                return "menu", menu_parser(root)
            _, bounds = chat_parser(root)
            header = _matching(root, context.app_profile, "whatsapp.chat-screen")[0]
            if _normalized_label(header.get("text", "")) != _normalized_label(target["display_name"]):
                raise WhatsAppCollectorError("whatsapp_export_chat_mismatch")
            return "chat", bounds

        root, hierarchy, (state, bounds) = _settled_root(
            context, retry_state, "whatsapp_more_menu_unavailable",
        )
        if state == "menu":
            menu_state = root, hierarchy, bounds
        else:
            _, menu_state = _click_and_wait(
                context, "open_whatsapp_more_options", bounds, menu_parser,
                "whatsapp_more_menu_unavailable", linked,
            )
    _, more_state = _click_and_wait(
        context,
        "open_whatsapp_more_menu",
        menu_state[2],
        lambda root: _unique_bounds(
            root,
            context.app_profile,
            "whatsapp.export-chat",
            "whatsapp_export_action_unavailable",
            "Export chat",
        ),
        "whatsapp_export_action_unavailable",
        linked,
    )
    _, export_state = _click_and_wait(
        context,
        "open_whatsapp_export_dialog",
        more_state[2],
        lambda root: _unique_bounds(
            root,
            context.app_profile,
            "whatsapp.include-media",
            "whatsapp_include_media_unavailable",
            "Include media",
        ),
        "whatsapp_include_media_unavailable",
        linked,
    )
    try:
        share_operation = context.system_ui_profile.operations["share_sheet"]
    except KeyError:
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        ) from None
    _, share_state = _click_and_wait(
        context,
        "include_whatsapp_export_media",
        export_state[2],
        lambda root: _share_sheet_marker(root, share_operation),
        "whatsapp_share_sheet_unavailable",
        linked,
    )
    root, hierarchy, bluetooth_bounds = _bluetooth_share_state(
        context,
        share_operation,
        share_state,
        linked,
    )
    action = context.journal.record_action(
        "identify_whatsapp_bluetooth_share_target",
        status="success",
        context=linked,
        details={
            "bounds": bluetooth_bounds,
            "system_ui_profile": context.system_ui_profile.profile_id,
            "selected": False,
        },
    )
    attempt_id = acquisition_item_attempt(context, linked)
    retained = retention_action(context, attempt_id, action, linked)
    prefix = (
        f"whatsapp/{_safe_component(target['ref'])}/export/{attempt_id}/"
        "share-sheet-screen"
    )
    screen, tree = context.retain_observation(
        prefix,
        context.device.screenshot(),
        ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if not hierarchy.startswith("<?xml")
        else hierarchy.encode("utf-8"),
        attempt_id=attempt_id,
        action=retained,
        context=dict(linked),
    )
    return {
        "action": action,
        "bluetooth_bounds": bluetooth_bounds,
        "screen_artifact_id": screen.artifact_id,
        "ui_tree_artifact_id": tree.artifact_id,
    }


def collect_export(
    context,
    target: Mapping[str, str],
    *,
    already_open: bool = False,
) -> dict[str, object]:
    context.begin_identification("whatsapp.chat_export")
    linked = {"target_ref": target["ref"], "display_name": target["display_name"]}
    previous_attempt = None
    limit = context.app_profile.parameters.get("export_attempts", 2)
    if type(limit) is not int or not 1 <= limit <= 5:
        raise WhatsAppCollectorError("whatsapp_export_attempt_limit_invalid")
    for number in range(1, limit + 1):
        attempt_context = {**linked, "transfer_attempt": number, "max_transfer_attempts": limit}
        if previous_attempt:
            attempt_context["previous_attempt_id"] = previous_attempt
        attempt_id = acquisition_item_attempt(context, attempt_context)
        if number == 1 and context.base_context["condition"].get("target", {}).get("kind") == "chat":
            context.finish_identification("whatsapp.chat_export", completion_condition="export_target_identified")
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        failure = None
        try:
            _, receive_dirs = export_bluetooth_condition(context.base_context["condition"])
            initial_inventory = _file_inventory(receive_dirs)
            report = _collect_export_once(context, target, attempt_id=attempt_id,
                                          linked=attempt_context, already_open=already_open)
        except (Exception, KeyboardInterrupt) as error:
            failure = error
        timing = {"started_at": started_at, "completed_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed_seconds": max(0.0, time.perf_counter() - started),
                  "timing_clock": "perf_counter", "timing_scope": "whatsapp_export_transfer_attempt"}
        source_action = getattr(failure, "action", None) if failure else report["action"]
        action = context.journal.record_action(
            "whatsapp_export_transfer_attempt", status="interrupted" if isinstance(failure, KeyboardInterrupt) else "failed" if failure else "success",
            attempt_id=attempt_id, context={**attempt_context, **({"source_action_id": source_action.action_id} if source_action else {})},
            details={**timing, "reason": getattr(failure, "reason_code", None),
                     "detail": str(failure) if failure else None,
                     "retry_candidate": getattr(failure, "retryable", False),
                     "retry_check": getattr(failure, "retry_reason", None)},
        )
        if failure is None:
            context.finish_attempt(attempt_id, ProcedureStatus.COMPLETED, AcquisitionStatus.ACQUIRED,
                                   action=action, details=timing)
            return report
        if number == limit or not isinstance(failure, WhatsAppCollectorError) or not failure.retryable:
            if isinstance(failure, WhatsAppCollectorError) and failure.action is None:
                failure.action = action
            raise failure
        try:
            already_open = _restore_export_chat(context, target, attempt_context, allow_list=not already_open)
            time.sleep(context.app_profile.timings["transition_settle"])
            try:
                changed = any(initial_inventory.get(path) != signature
                              for path, signature in _file_inventory(receive_dirs).items())
            except OSError as error:
                raise WhatsAppCollectorError("whatsapp_retry_receive_directory_unavailable", str(error)) from error
            if changed:
                raise WhatsAppCollectorError("whatsapp_retry_receive_files_changed")
        except WhatsAppCollectorError as recovery_error:
            recovery_error.action = context.journal.record_action(
                "retry_whatsapp_export", status="failed", attempt_id=attempt_id, context=attempt_context,
                details={"reason": recovery_error.reason_code, "source_action_id": action.action_id},
            )
            raise
        context.journal.record_action("retry_whatsapp_export", status="success", attempt_id=attempt_id,
                                      context=attempt_context, details={"next_transfer_attempt": number + 1, "source_action_id": action.action_id})
        context.finish_attempt(attempt_id, ProcedureStatus.INTERRUPTED, AcquisitionStatus.NOT_ACQUIRED,
                               reason=failure.reason_code, action=action, details=timing)
        previous_attempt = attempt_id
        print(f"AURA: Retrying Bluetooth transfer (1/1) for {target['display_name']}: {failure}", file=sys.stderr, flush=True)


def _restore_export_chat(context, target, linked, *, allow_list):
    def parse(root):
        chats = _matching(root, context.app_profile, "whatsapp.chat-screen")
        if len(chats) == 1:
            if _normalized_label(chats[0].get("text", "")) != _normalized_label(target["display_name"]):
                raise WhatsAppCollectorError("whatsapp_retry_chat_mismatch")
            return True
        if _matching(root, context.app_profile, "whatsapp.chat-list"):
            if not allow_list:
                raise WhatsAppCollectorError("whatsapp_retry_chat_list_unexpected")
            chat_row_bounds(root, context.app_profile, target["display_name"])
            return False
        raise WhatsAppCollectorError("whatsapp_retry_chat_unavailable")

    # Bound recovery to the receiver picker/share overlays; never reopen an ordinal chat by name.
    for step in range(4):
        try:
            return _settled_root(context, parse, "whatsapp_retry_chat_unavailable")[2]
        except WhatsAppCollectorError as error:
            if error.reason_code != "whatsapp_retry_chat_unavailable" or step == 3:
                raise
            context.ui.back("restore_whatsapp_export_chat", context={**linked, "back_attempt": step + 1})


def _move_preserved_receipt(context, source, artifact, baseline, source_stat, attempt_id, linked):
    destination = context.run_dir / artifact.relative_path
    details = {"source_path": str(source), "destination_path": str(destination),
               "artifact_id": artifact.artifact_id, "sha256": artifact.sha256, "source_removed": False}
    try:
        if source in baseline or source.is_symlink():
            raise ValueError("Refusing to remove a pre-existing or symbolic-link source")
        for path in (source, destination):
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != artifact.sha256:
                    raise ValueError("Receipt hash differs from the preserved artifact")
        current_stat = source.stat()
        if any(getattr(current_stat, name) != getattr(source_stat, name)
               for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
            raise ValueError("Receipt changed during preservation")
        source.unlink()
        details["source_removed"] = True
    except (OSError, ValueError) as error:
        action = context.journal.record_action("move_whatsapp_export_receipt", status="failed", attempt_id=attempt_id,
            context=linked, details={**details, "error": str(error)})
        raise WhatsAppCollectorError("whatsapp_export_source_cleanup_failed", str(error), action=action) from error
    context.journal.record_action("move_whatsapp_export_receipt", status="success", attempt_id=attempt_id,
        context=linked, details=details)


def _collect_export_once(context, target, *, attempt_id, linked, already_open):
    prefix = f"whatsapp/{_safe_component(target['ref'])}/export/{attempt_id}"

    captured_stages = set()

    def capture_host_window(window, stage):
        if stage in captured_stages:
            return
        captured_stages.add(stage)
        capture_context = {**linked, "host_platform": "Windows", "stage": stage}
        try:
            capture_context["receiver_handle"] = int(window.handle)
            image = window.capture_as_image()
            with BytesIO() as stream:
                image.save(stream, format="PNG")
                png = stream.getvalue()
            action = context.linked_action("capture_windows_bluetooth_receiver", attempt_id,
                                           context=capture_context)
            context.artifacts.write_bytes(
                f"{prefix}/host-bluetooth-{stage}.png",
                png, kind="host_screen_image", attempt_id=attempt_id, action=action,
                context=capture_context,
            )
        except Exception as error:
            context.journal.record_action(
                "capture_windows_bluetooth_receiver", status="failed", attempt_id=attempt_id,
                context=capture_context, details={"error_type": type(error).__name__, "detail": str(error)},
            )

    receiver_label, receive_dirs = export_bluetooth_condition(
        context.base_context["condition"]
    )
    host = prepare_windows_bluetooth_receiver(
        context.app_profile.timings["windows_receiver_timeout"],
        capture_window=capture_host_window,
    )
    host_skipped = host["reason"] == "windows_receiver_unsupported"
    host_action = context.journal.record_action(
        "prepare_windows_bluetooth_receiver",
        status="success" if host["ok"] or host_skipped else "failed",
        attempt_id=attempt_id,
        context=linked,
        details={**host, "skipped": host_skipped,
                 "receive_directories": [str(path) for path in receive_dirs]},
    )
    failure = None
    try:
        if not host["ok"] and not host_skipped:
            raise WhatsAppCollectorError(
                "whatsapp_bluetooth_host_receiver_unavailable",
                f"{host['reason']}: {host.get('detail', '')}",
                action=host_action,
                retryable=host["reason"] in {"receive_failed", "receiver_waiting_screen_not_found", "receiver_ui_error"},
            )
        report = identify_bluetooth(
            context,
            target,
            already_open=already_open,
        )
        try:
            share_operation = context.system_ui_profile.operations["share_sheet"]
        except KeyError:
            raise WhatsAppCollectorError(
                "whatsapp_share_profile_invalid",
                "Bluetooth share Profile is invalid",
            ) from None
        bluetooth_action = context.ui.click_bounds(
            "open_whatsapp_bluetooth_receiver_picker", report["bluetooth_bounds"], context=linked,
        )
        picker_state = _receiver_picker_state(context, share_operation, receiver_label, linked)
        root, hierarchy, _ = picker_state
        attempt_id = acquisition_item_attempt(context, linked)
        retained = retention_action(
            context, attempt_id, bluetooth_action, linked
        )
        context.retain_observation(
            f"{prefix}/receiver-picker-screen",
            context.device.screenshot(),
            ET.tostring(root, encoding="utf-8", xml_declaration=True)
            if not hierarchy.startswith("<?xml")
            else hierarchy.encode("utf-8"),
            attempt_id=attempt_id,
            action=retained,
            context=dict(linked),
        )

        baseline = _file_inventory(receive_dirs)
        _select_bluetooth_receiver(context, share_operation, receiver_label, linked)
        receipt_timeout = context.app_profile.timings["export_receipt_timeout"]
        receipt_poll = context.app_profile.timings["export_receipt_poll_interval"]
        host_finish = finish_windows_bluetooth_receive(
            timeout_sec=receipt_timeout,
            poll_interval=receipt_poll,
            receiver_handle=host.get("receiver_handle"),
            capture_window=capture_host_window,
            existing_names=tuple(path.name for path in _file_inventory(receive_dirs)),
        )
        finish_skipped = (
            host_finish["reason"] == "windows_receiver_unsupported"
        )
        finish_action = context.journal.record_action(
            "finish_windows_bluetooth_receive",
            status=(
                "success"
                if host_finish["ok"] or finish_skipped
                else "failed"
            ),
            attempt_id=attempt_id,
            context={**linked, "receiver_label": receiver_label},
            details={**host_finish, "skipped": finish_skipped},
        )
        if not host_finish["ok"] and not finish_skipped:
            raise WhatsAppCollectorError(
                "whatsapp_bluetooth_host_receive_unconfirmed",
                f"{host_finish['reason']}: {host_finish.get('detail', '')}",
                action=finish_action,
                retryable=host_finish["reason"] in {"receive_failed", "receiver_ui_error", "receive_completion_not_found"},
            )
        receipt_details = {"receive_directories": [str(path) for path in receive_dirs],
                           "expected_file_names": host_finish.get("file_names", [])}
        context.journal.record_action("wait_whatsapp_export_file", status="started", context=linked,
                                      details=receipt_details)
        print("AURA: Checking received files after Finish: " + ", ".join(receipt_details["receive_directories"]),
              file=sys.stderr, flush=True)
        try:
            received = _wait_for_received_file(
                receive_dirs, baseline, timeout=receipt_timeout, poll_interval=receipt_poll,
                expected_names=host_finish.get("file_names"),
            )
        except WhatsAppCollectorError as error:
            context.journal.record_action("wait_whatsapp_export_file", status="failed", context=linked,
                                          details={**receipt_details, "reason": error.reason_code})
            raise
        source_stat = received.stat()
        receipt_error = _export_receipt_error(received)
        receipt_action = context.journal.record_action(
            "receive_whatsapp_export",
            status="failed" if receipt_error else "success",
            context={**linked, "receiver_label": receiver_label},
            details={"filename": received.name, "source_path": str(received),
                     "validation_error": receipt_error},
        )
        attempt_id = acquisition_item_attempt(context, linked)
        retained = retention_action(
            context, attempt_id, receipt_action, linked
        )
        artifact = context.artifacts.retain_file(
            received,
            (
                f"{prefix}/"
                f"{received.name}"
            ),
            kind="diagnostic" if receipt_error else "app_export",
            attempt_id=attempt_id,
            action=retained,
            context={**linked, "receiver_label": receiver_label},
        )
        if receipt_error:
            raise WhatsAppCollectorError(receipt_error, action=receipt_action)
        if not host_skipped:
            _move_preserved_receipt(context, received, artifact, baseline, source_stat, attempt_id, linked)
        print(f"AURA: Received file verified and preserved: {received.name}. Continuing to the next step.",
              file=sys.stderr, flush=True)
        return {
            "action": receipt_action,
            "artifact_id": artifact.artifact_id,
            "receiver_label": receiver_label,
            "received_filename": received.name,
        }
    except WhatsAppCollectorError as error:
        failure = error
        raise
    finally:
        cleanup = {"ok": False, "reason": "receiver_handle_unavailable"}
        if host.get("receiver_handle") is not None:
            cleanup = close_waiting_windows_bluetooth_receiver(host["receiver_handle"])
            context.journal.record_action(
                "cleanup_windows_bluetooth_receiver",
                status="success" if cleanup["ok"] else "failed",
                attempt_id=attempt_id,
                context={"target_ref": target["ref"], "display_name": target["display_name"]},
                details={**cleanup, "receiver_handle": host["receiver_handle"]},
            )
        if failure is not None and failure.retryable:
            failure.retryable = cleanup["ok"] and cleanup["reason"] in {
                "idle_receiver_closed", "failed_receiver_closed", "receiver_already_closed",
            }
            failure.retry_reason = "receiver_closed" if failure.retryable else "receiver_not_confirmed_closed"


__all__ = [
    "WhatsAppCollectorError",
    "acquisition_item_attempt",
    "bluetooth_target_bounds",
    "chat_row_bounds",
    "collect_export",
    "export_bluetooth_condition",
    "identify_bluetooth",
    "receiver_bounds",
    "retention_action",
]
