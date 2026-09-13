"""WhatsApp 2.26.27.85 Export flow up to Bluetooth identification."""

from __future__ import annotations

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path

from ...models import AcquisitionStatus, ActionRef
from ...profiles import AppProfile
from ...windows_bluetooth import (
    finish_windows_bluetooth_receive,
    prepare_windows_bluetooth_receiver,
)


PACKAGE = "com.whatsapp"
_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_UNSAFE_PATH = re.compile(r"[/\\\x00-\x1f]")
_ATTRIBUTES = {
    "class_name": "class",
    "content_description": "content-desc",
    "resource_id": "resource-id",
    "text": "text",
    "checked": "checked",
    "clickable": "clickable",
    "focusable": "focusable",
    "scrollable": "scrollable",
    "selected": "selected",
}


class WhatsAppCollectorError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        message: str | None = None,
        *,
        action: ActionRef | None = None,
    ):
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.action = action


def acquisition_item_attempt(
    context,
    linked: Mapping[str, object],
    *,
    document: Mapping[str, object] | None = None,
) -> str:
    document = document or {}
    record_kind = str(document.get("record_kind", ""))
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


def _bounds(node: ET.Element) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        raise WhatsAppCollectorError(
            "whatsapp_ui_bounds_invalid",
            "UI bounds are invalid",
        )
    result = tuple(map(int, match.groups()))
    if result[0] >= result[2] or result[1] >= result[3]:
        raise WhatsAppCollectorError(
            "whatsapp_ui_bounds_invalid",
            "UI bounds are invalid",
        )
    return result


def _visible(node: ET.Element) -> bool:
    if node.get("visible-to-user") == "false":
        return False
    try:
        left, top, right, bottom = _bounds(node)
    except WhatsAppCollectorError:
        return False
    return right > 0 and bottom > 0 and left < 1080 and top < 2400


def _selector_spec(
    profile: AppProfile,
    element_id: str,
) -> tuple[str, dict[str, str]]:
    try:
        entry = profile.selectors[element_id]
        selector = entry["selector"]
        owner = selector["owner_package"]
        expected = {
            _ATTRIBUTES[selector["kind"]]: _xml_value(selector["value"])
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
        expected.update({
            _ATTRIBUTES[key]: _xml_value(value)
            for key, value in constraints.items()
        })
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
    return tuple(
        node
        for node in root.iter()
        if _visible(node)
        and node.get("package") == owner
        and all(node.get(key) == value for key, value in expected.items())
    )


def _unique_bounds(
    root: ET.Element,
    profile: AppProfile,
    element_id: str,
    reason_code: str,
    label: str,
) -> tuple[int, int, int, int]:
    matches = _matching(root, profile, element_id)
    if len(matches) != 1:
        qualifier = "unavailable" if not matches else "ambiguous"
        raise WhatsAppCollectorError(
            reason_code,
            f"{label} is {qualifier}",
        )
    return _bounds(matches[0])


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


def _normalized_label(value: str) -> str:
    return " ".join(
        "".join(
            character
            for character in value
            if unicodedata.category(character) != "Cf"
        ).split()
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
        _visible(node)
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
        if _visible(node)
        and node.get("package") in owners
        and node.get("resource-id") in resource_ids
        and _normalized_label(node.get("text", "")) == expected
    ]
    if len(matches) != 1:
        qualifier = "unavailable" if not matches else "ambiguous"
        raise WhatsAppCollectorError(
            f"whatsapp_bluetooth_receiver_{qualifier}",
            f"Bluetooth receiver is {qualifier}",
        )
    parents = {
        child: parent
        for parent in root.iter()
        for child in parent
    }
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
        or fallback.get("swipe_direction") not in {"left", "right"}
        or not isinstance(fallback.get("more_labels"), list)
        or not fallback["more_labels"]
        or not all(
            isinstance(value, str) and value
            for value in fallback["more_labels"]
        )
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        )
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
        if _visible(node)
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
        _visible(node) and node.get("package") in owners
        for node in root.iter()
    ):
        raise WhatsAppCollectorError(
            "whatsapp_share_sheet_unavailable",
            "Android share sheet is unavailable",
        )
    return True


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
            timeout=context.app_profile.timings.get(
                "share_target_wait", 1.0
            ),
        )
    except WhatsAppCollectorError as error:
        if error.reason_code != "whatsapp_bluetooth_target_unavailable":
            raise

    _, owners, resource_ids, _, fallback = _share_sheet_spec(operation)
    try:
        context.ui.swipe(
            "scan_whatsapp_share_targets",
            fallback["swipe_direction"],
            expect_change=True,
            context=linked,
        )
    except Exception as swipe_error:
        raise WhatsAppCollectorError(
            "whatsapp_share_fallback_unavailable",
            str(swipe_error),
        ) from swipe_error

    swiped_state = _settled_root(
        context,
        lambda candidate: _share_sheet_marker(candidate, operation),
        "whatsapp_share_sheet_unavailable",
    )
    root, hierarchy, _ = swiped_state
    try:
        return root, hierarchy, bluetooth_target_bounds(root, operation)
    except WhatsAppCollectorError as bluetooth_error:
        if (
            bluetooth_error.reason_code
            != "whatsapp_bluetooth_target_unavailable"
        ):
            raise

    more_bounds = _share_target_bounds(
        root,
        owners,
        resource_ids,
        set(fallback["more_labels"]),
        "whatsapp_share_more",
        "share-sheet More target",
    )
    _, expanded_state = _click_and_wait(
        context,
        "expand_whatsapp_share_targets",
        more_bounds,
        lambda candidate: bluetooth_target_bounds(candidate, operation),
        "whatsapp_bluetooth_target_unavailable",
        linked,
    )
    root, hierarchy, bluetooth_bounds = expanded_state
    return root, hierarchy, bluetooth_bounds


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
) -> tuple[str, Path]:
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
    directory = Path(receive_dir).expanduser()
    try:
        resolved = directory.resolve(strict=True)
    except OSError:
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_condition_invalid"
        ) from None
    if (
        not directory.is_absolute()
        or not resolved.is_dir()
        or resolved == Path(resolved.anchor)
        or resolved == Path.home().resolve()
    ):
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_condition_invalid"
        )
    return receiver_label.strip(), resolved


def _file_inventory(directory: Path) -> dict[Path, tuple[int, int]]:
    result = {}
    for path in directory.iterdir():
        if path.name.startswith(".") or not path.is_file():
            continue
        stat = path.stat()
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def _wait_for_received_file(
    directory: Path,
    baseline: Mapping[Path, tuple[int, int]],
    *,
    timeout: float,
    poll_interval: float,
) -> Path:
    deadline = time.monotonic() + max(0.0, timeout)
    previous = None
    stable = 0
    while True:
        changed = {
            path: signature
            for path, signature in _file_inventory(directory).items()
            if baseline.get(path) != signature
        }
        if len(changed) > 1:
            raise WhatsAppCollectorError(
                "whatsapp_export_receipt_ambiguous"
            )
        if len(changed) == 1:
            current = next(iter(changed.items()))
            stable = stable + 1 if current == previous else 1
            previous = current
            if stable >= 2:
                return current[0]
        else:
            previous = None
            stable = 0
        if time.monotonic() >= deadline:
            raise WhatsAppCollectorError("whatsapp_export_receipt_missing")
        time.sleep(max(0.0, poll_interval))


def _settled_root(
    context,
    parser,
    reason_code: str,
    *,
    timeout: float | None = None,
):
    settle = context.app_profile.timings.get("transition_settle", 0.5)
    if settle:
        time.sleep(settle)
    previous = None
    stable = 0
    resolved = None
    last_error = None

    def ready() -> bool:
        nonlocal previous, stable, resolved, last_error
        value = context.device.hierarchy()
        try:
            root = ET.fromstring(value)
            parsed = parser(root)
        except (ET.ParseError, TypeError, WhatsAppCollectorError) as error:
            previous = None
            stable = 0
            last_error = error
            return False
        stable = stable + 1 if value == previous else 1
        previous = value
        if stable >= 2:
            resolved = (root, value, parsed)
            return True
        return False

    if not context.ui.wait_until(ready, timeout=timeout):
        if isinstance(last_error, WhatsAppCollectorError):
            raise last_error
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
    more_options = chat_state[2][1]
    _, menu_state = _click_and_wait(
        context,
        "open_whatsapp_more_options",
        more_options,
        lambda root: _unique_bounds(
            root,
            context.app_profile,
            "whatsapp.more",
            "whatsapp_more_menu_unavailable",
            "More",
        ),
        "whatsapp_more_menu_unavailable",
        linked,
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
        f"whatsapp/{_safe_component(target['ref'])}/export/"
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
    acquisition_item_attempt(context, {"target_ref": target["ref"], "display_name": target["display_name"]})
    if context.base_context["condition"].get("target", {}).get("kind") == "chat":
        context.finish_identification("whatsapp.chat_export", completion_condition="export_target_identified")
    receiver_label, receive_dir = export_bluetooth_condition(
        context.base_context["condition"]
    )
    host = prepare_windows_bluetooth_receiver(
        context.app_profile.timings.get("windows_receiver_timeout", 10.0)
    )
    host_skipped = host["reason"] == "windows_receiver_unsupported"
    host_action = context.journal.record_action(
        "prepare_windows_bluetooth_receiver",
        status="success" if host["ok"] or host_skipped else "failed",
        context={
            "target_ref": target["ref"],
            "display_name": target["display_name"],
        },
        details={**host, "skipped": host_skipped},
    )
    if not host["ok"] and not host_skipped:
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_host_receiver_unavailable",
            action=host_action,
        )

    report = identify_bluetooth(
        context,
        target,
        already_open=already_open,
    )
    linked = {
        "target_ref": target["ref"],
        "display_name": target["display_name"],
    }
    try:
        share_operation = context.system_ui_profile.operations["share_sheet"]
    except KeyError:
        raise WhatsAppCollectorError(
            "whatsapp_share_profile_invalid",
            "Bluetooth share Profile is invalid",
        ) from None
    bluetooth_action, picker_state = _click_and_wait(
        context,
        "open_whatsapp_bluetooth_receiver_picker",
        report["bluetooth_bounds"],
        lambda root: receiver_bounds(
            root,
            share_operation,
            receiver_label,
        ),
        "whatsapp_bluetooth_receiver_unavailable",
        linked,
    )
    root, hierarchy, selected_receiver_bounds = picker_state
    attempt_id = acquisition_item_attempt(context, linked)
    retained = retention_action(
        context, attempt_id, bluetooth_action, linked
    )
    prefix = (
        f"whatsapp/{_safe_component(target['ref'])}/export/"
        "receiver-picker-screen"
    )
    context.retain_observation(
        prefix,
        context.device.screenshot(),
        ET.tostring(root, encoding="utf-8", xml_declaration=True)
        if not hierarchy.startswith("<?xml")
        else hierarchy.encode("utf-8"),
        attempt_id=attempt_id,
        action=retained,
        context=dict(linked),
    )

    baseline = _file_inventory(receive_dir)
    context.ui.click_bounds(
        "select_whatsapp_bluetooth_receiver",
        selected_receiver_bounds,
        context={**linked, "receiver_label": receiver_label},
    )
    receipt_timeout = context.app_profile.timings.get(
        "export_receipt_timeout", 90.0
    )
    receipt_poll = context.app_profile.timings.get(
        "export_receipt_poll_interval", 0.2
    )
    host_finish = finish_windows_bluetooth_receive(
        timeout_sec=receipt_timeout,
        poll_interval=receipt_poll,
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
        context={**linked, "receiver_label": receiver_label},
        details={**host_finish, "skipped": finish_skipped},
    )
    if not host_finish["ok"] and not finish_skipped:
        raise WhatsAppCollectorError(
            "whatsapp_bluetooth_host_receive_unconfirmed",
            action=finish_action,
        )
    received = _wait_for_received_file(
        receive_dir,
        baseline,
        timeout=receipt_timeout,
        poll_interval=receipt_poll,
    )
    receipt_action = context.journal.record_action(
        "receive_whatsapp_export",
        status="success",
        context={**linked, "receiver_label": receiver_label},
        details={"filename": received.name},
    )
    attempt_id = acquisition_item_attempt(context, linked)
    retained = retention_action(
        context, attempt_id, receipt_action, linked
    )
    artifact = context.artifacts.retain_file(
        received,
        (
            f"whatsapp/{_safe_component(target['ref'])}/export/"
            f"{received.name}"
        ),
        kind="app_export",
        attempt_id=attempt_id,
        action=retained,
        context={**linked, "receiver_label": receiver_label},
    )
    context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
    return {
        "action": receipt_action,
        "artifact_id": artifact.artifact_id,
        "receiver_label": receiver_label,
        "received_filename": received.name,
    }


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
