"""Shared UI and artifact helpers for the exact-version Notion collector."""

from __future__ import annotations
from ...runtime import record_snapshot
from ...collection import stability_probe, selector_conditions, display_text, matching_nodes
from ...parsing import parse_bounds, field_value

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping

from ...device import COORDINATE_SELECTOR_KEYS
from ...ui import hierarchy_signature
from ...models import (
    AcquisitionStatus,
    ActionRef,
    ProcedureStatus,
)


PACKAGE = "notion.id"
_UNSAFE_LABEL = re.compile(r"[/\\:\x00-\x1f]")


class NotionCollectorError(RuntimeError):
    pass


def item_attempt(
    context,
    linked: Mapping[str, object],
    *,
    record_kind: object = None,
    prefix: str = "",
    retention: bool = True,
) -> str:
    linked = dict(linked)
    target_ref = context.base_context.get("condition", {}).get("target", {}).get("ref")
    if target_ref is not None:
        linked.setdefault("target_ref", target_ref)
    if record_kind == "notion_page_listing":
        target_id, item_type, ref = "notion.inventory", "page_listing", linked.get("page_ref", prefix)
    elif record_kind == "notion_workspace_members" or prefix.endswith(("/members", "/members.json")):
        target_id, item_type, ref = "notion.members", "workspace_members", linked.get("workspace_ref", prefix)
    elif "attachment_ref" in linked or record_kind == "notion_attachment":
        target_id, item_type, ref = (
            "notion.attachments",
            "attachment",
            linked.get("attachment_ref", prefix),
        )
    elif "export" in prefix or record_kind in {
        "notion_page_export",
        "notion_exports",
    }:
        item_type = (
            "database_export"
            if linked.get("page_kind") == "database"
            else "page_export"
        )
        target_id, ref = "notion.export", linked.get("page_ref", prefix)
    elif "database_item_ref" in linked or record_kind == "notion_database_item":
        target_id, item_type, ref = (
            "notion.content",
            "database_item",
            linked.get("database_item_ref", prefix),
        )
    elif "page_ref" in linked or record_kind in {
        "notion_general_page",
        "notion_pages",
        "notion_database",
    }:
        target_id, item_type, ref = (
            "notion.content",
            "page",
            linked.get("page_ref", prefix),
        )
    elif "workspace_ref" in linked or record_kind in {
        "notion_workspace",
        "notion_workspaces",
        "notion_workspace_members",
    }:
        target_id, item_type, ref = (
            "notion.inventory",
            "workspace",
            linked.get("workspace_ref", prefix),
        )
    else:
        target_id, item_type, ref = (
            "notion.inventory",
            "account_collection",
            linked.get("target_ref", prefix or "account"),
        )
    source = dict(linked)
    scope = ":".join(
        f"{name}={linked[name]}"
        for name in (
            "target_ref",
            "workspace_ref",
            "page_ref",
            "database_ref",
            "database_item_ref",
        )
        if name in linked
    )
    return context.item_attempt(
        f"{item_type}:{scope}:{ref}",
        target_id,
        item_type,
        source=source,
        context=source,
        retention=retention,
    )


def output_action(
    context,
    attempt_id: str,
    action: ActionRef,
    linked: Mapping[str, object],
) -> ActionRef:
    return context.linked_action(
        "retain_notion_output",
        attempt_id,
        source_action=action,
        context=dict(linked),
    )


def bounds(node):
    try:
        return parse_bounds(node.get('bounds', ''), allow_negative=False)
    except ValueError:
        raise NotionCollectorError("element bounds are invalid") from None


def node_label(node):
    return field_value(node, {"attribute": ["content-desc", "text"]}, ui_fields={})


def page_buttons(root, label):
    return tuple(node for node in matching_nodes(
        root, PACKAGE, {"class": "android.widget.Button"}, visible=onscreen)
        if node_label(node) == label)


def database_control_labels(profile):
    return {display_text(profile, name) for name in ('database_filter_sort', 'database_view_settings')}


def property_text_cells(root):
    nodes = tuple(node for node in matching_nodes(
        root, PACKAGE, {"class": "android.view.View"}, visible=onscreen)
        if node.get("clickable") != "true" and node.get("text", "").strip())
    selected = set(nodes)
    return tuple(node for node in nodes if not any(
        child is not node and child in selected and child.get("text") == node.get("text")
        for child in node.iter()))


def page_actions_bounds(root):
    matches = {bounds(node) for node in page_buttons(root, "Actions")
               if node.get("clickable") == "true"}
    if len(matches) != 1:
        raise NotionCollectorError("notion_page_actions_unavailable")
    return matches.pop()


def onscreen(node: ET.Element, root: ET.Element | None = None) -> bool:
    if node.get("package") != PACKAGE or node.get("visible-to-user") == "false":
        return False
    try:
        left, top, right, bottom = bounds(node)
    except NotionCollectorError:
        return False
    if root is None:
        return right > 0 and bottom > 0
    window_left, window_top, window_right, window_bottom = viewport_bounds(root)
    return (right > window_left and bottom > window_top
            and left < window_right and top < window_bottom)


def viewport_bounds(root: ET.Element) -> tuple[int, int, int, int]:
    """Use the observed window, including the system navigation strip."""
    for node in (root, *root):
        if node.get("bounds") and (
            node is root or (
                node.get("package") == PACKAGE
                and node.get("class") == "android.widget.FrameLayout"
            )
        ):
            return bounds(node)
    navigation = [
        bounds(node) for node in root.iter()
        if node.get("resource-id") == "com.android.systemui:id/navigation_bar_frame"
    ]
    if len(navigation) == 1:
        left, _, right, bottom = navigation[0]
        return left, 0, right, bottom
    visible = [bounds(node) for node in root.iter() if onscreen(node)]
    if not visible:
        raise NotionCollectorError("Notion viewport is unavailable")
    return (min(b[0] for b in visible), min(b[1] for b in visible),
            max(b[2] for b in visible), max(b[3] for b in visible))


def is_page_document(node: ET.Element, root: ET.Element) -> bool:
    return (
        onscreen(node, root)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") != "true"
        and bool(node.get("text", ""))
    )


def page_keyboard_visible(root: ET.Element, profile) -> bool:
    marker = display_text(profile, 'page_keyboard_dismiss')
    return any(onscreen(node, root) and node.get("resource-id") == marker
               for node in root.iter())


def dismiss_page_keyboard(context, sequence, action, observation, *, linked, verify_parent):
    """Dismiss only an observed keyboard, preserving both states on the same page."""
    if not page_keyboard_visible(observation["root"], context.app_profile):
        return action, observation
    prefix = f"notion/diagnostics/keyboard-{action.action_id}"
    retain_pair(context, f"{prefix}-before", observation, action, linked)
    action = context.ui.back(f"notion_dismiss_page_keyboard_{next(sequence):06d}",
                             context=dict(linked))

    observation = observe(context, f"observation-keyboard-{action.action_id}", action)
    retain_pair(context, f"{prefix}-after", observation, action, linked)
    if (page_keyboard_visible(observation["root"], context.app_profile)
            or not verify_parent(observation["root"])):
        raise NotionCollectorError("notion_page_keyboard_restore_failed")
    return action, observation


def selector(context, element_id: str) -> dict[str, object]:
    try:
        entry = context.app_profile.selectors[element_id]
        spec = entry["selector"]
        result = {
            **selector_conditions({str(spec["kind"]): spec["value"]}, backend="uiautomator"),
            "packageName": spec["owner_package"],
        }
        constraints = entry.get("constraints", {})
    except (KeyError, TypeError):
        raise NotionCollectorError(
            f"unknown Notion element: {element_id}"
        ) from None
    if (
        result["packageName"] != PACKAGE
        or not isinstance(spec["value"], str)
        or not isinstance(constraints, Mapping)
    ):
        raise NotionCollectorError("Notion selector is invalid")
    try:
        result.update(selector_conditions(constraints, backend="uiautomator"))
    except KeyError:
        raise NotionCollectorError(
            "Notion selector constraint is invalid"
        ) from None
    if any(
        str(key).strip().casefold() in COORDINATE_SELECTOR_KEYS
        for key in result
    ):
        raise NotionCollectorError("coordinate selector is invalid")
    return result


def observe(
    context,
    observation_id: str,
    action: ActionRef,
    parser: Callable[[ET.Element], object] | None = None,
    diagnostic_name: str | None = None,
    *, record_kind: str | None = None, timeout: float | None = None,
) -> dict[str, object]:
    time.sleep(context.app_profile.timings["transition_settle"])
    parsed = None
    def read():
        current = context.device.hierarchy()
        try:
            return current, ET.fromstring(current)
        except (ET.ParseError, TypeError):
            raise NotionCollectorError("Notion hierarchy is invalid") from None
    def key(value):
        nonlocal parsed
        current, root = value
        if parser is not None:
            parsed = None
            parsed = parser(root)
        return hierarchy_signature(root)
    settled, state = stability_probe(read, key=key,
        retry_errors=(NotionCollectorError,), clear_error_on_success=True)
    completed = context.ui.wait_until(settled, timeout=timeout)
    current = state['value'][0] if state['value'] is not None else None
    parser_error = str(state['error']) if state['error'] is not None else None
    if not completed:
        if (parser is not None or diagnostic_name is not None) and isinstance(current, str):
            linked = {
                **dict(action.context),
                "observation_id": observation_id,
                "parser_error": parser_error,
                "hierarchy_settled": False,
            }
            attempt_id = item_attempt(
                context,
                linked,
                prefix=diagnostic_name or observation_id,
                record_kind=record_kind,
            )
            retained = output_action(context, attempt_id, action, linked)
            context.retain_observation(
                (
                    "notion/diagnostics/"
                    f"{safe_component(diagnostic_name or observation_id)}"
                    "-last"
                ),
                context.device.screenshot(),
                current.encode("utf-8"),
                attempt_id=attempt_id,
                action=retained,
                context=linked,
            )
            if parser is not None:
                context.finish_attempt(
                    attempt_id,
                    ProcedureStatus.INTERRUPTED,
                    AcquisitionStatus.NOT_ACQUIRED,
                    reason=str(parser_error or "notion_hierarchy_unsettled"),
                    action=retained,
                    observation_id=context.observations[-1].observation_id,
                )
        raise NotionCollectorError(
            parser_error or "Notion hierarchy did not settle"
        )
    assert isinstance(current, str)
    root = ET.fromstring(current)
    screen = context.device.screenshot()
    snapshot = record_snapshot(context.journal, observation_id, action, screen, current.encode('utf-8'))
    result = {
        "observation_id": observation_id,
        "root": root,
        "screen": snapshot["screen_image"],
        "tree": snapshot["ui_tree"],
    }
    if parser is not None:
        result["parsed"] = parsed
    return result


def retain_pair(
    context,
    prefix: str,
    observation: Mapping[str, object],
    action: ActionRef,
    linked_context: Mapping[str, object],
) -> dict[str, object]:
    source_observation_id = str(observation["observation_id"])
    artifact_context = {
        **dict(linked_context),
        "source_snapshot_id": f"notion-snapshot:{source_observation_id}",
    }
    attempt_id = item_attempt(context, artifact_context, prefix=prefix)
    retained = output_action(context, attempt_id, action, artifact_context)
    screen, tree = context.retain_observation(
        prefix,
        observation["screen"],
        observation["tree"],
        attempt_id=attempt_id,
        action=retained,
        context=artifact_context,
    )
    return {
        "observation_id": context.observations[-1].observation_id,
        "screen_artifact_id": screen.artifact_id,
        "screen_path": screen.relative_path,
        "ui_tree_artifact_id": tree.artifact_id,
        "ui_tree_path": tree.relative_path,
    }


def _register_summary_results(
    context, document: Mapping[str, object]
) -> list[str]:
    record_kind = document.get("record_kind")
    entry_names = {
        "notion_exports": "exports",
        "notion_pages": "pages",
        "notion_workspaces": "workspaces",
    }
    entry_name = entry_names.get(record_kind)
    if entry_name is None:
        return []
    raw_entries = document.get(entry_name)
    entries = list(raw_entries) if isinstance(raw_entries, list) else []
    failed_workspace_ref = document.get("failed_workspace_ref")
    if record_kind == "notion_workspaces" and failed_workspace_ref and not any(
        isinstance(entry, Mapping)
        and entry.get("workspace_ref") == failed_workspace_ref
        for entry in entries
    ):
        entries.append({
            "workspace_ref": failed_workspace_ref,
            "acquisition_status": "partial",
            "reason_code": document.get("reason_code"),
        })
    represented = {
        entry.get("workspace_ref")
        for entry in entries
        if isinstance(entry, Mapping)
    }
    discovered = document.get("discovered_workspace_refs", ())
    if record_kind == "notion_workspaces" and isinstance(discovered, list):
        entries.extend(
            {
                "workspace_ref": workspace_ref,
                "acquisition_status": "not_attempted",
                "reason_code": "notion_workspace_not_attempted_after_failure",
            }
            for workspace_ref in discovered
            if workspace_ref not in represented
        )
    attempt_ids = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        status = entry.get("acquisition_status")
        post_status = entry.get("post_acquisition_status")
        linked = {
            key: value
            for key, value in {
                "target_ref": document.get("target_ref"),
                "workspace_ref": entry.get("workspace_ref")
                or document.get("workspace_ref"),
                "parent_page_ref": entry.get("parent_page_ref"),
                "page_ref": entry.get("page_ref"),
                "notion_page_id": entry.get("notion_page_id"),
                "page_kind": entry.get("classification"),
            }.items()
            if value is not None
        }
        child_kind = (
            "notion_page_export"
            if record_kind == "notion_exports"
            else "notion_workspace"
            if record_kind == "notion_workspaces"
            else "notion_database"
            if entry.get("classification") == "database"
            else "notion_general_page"
        )
        attempt_id = item_attempt(
            context,
            linked,
            record_kind=child_kind,
        )
        attempt_ids.append(attempt_id)
        attempt = next(
            item for item in context.attempts if item.attempt_id == attempt_id
        )
        if attempt.procedure_status is not None or (
            status not in {"partial", "not_attempted", "out_of_scope"}
            and post_status != "partial"
        ):
            continue
        if post_status == "partial":
            context.finish_attempt(
                attempt_id,
                ProcedureStatus.INTERRUPTED,
                AcquisitionStatus.ACQUIRED,
                reason=str(
                    entry.get("post_acquisition_reason_code")
                    or "notion_post_acquisition_incomplete"
                ),
            )
        elif status in {"not_attempted", "out_of_scope"}:
            retained = any(a.attempt_id == attempt_id for a in context.artifacts.records)
            context.finish_attempt(
                attempt_id,
                ProcedureStatus.COMPLETED if retained else ProcedureStatus.NOT_ATTEMPTED,
                AcquisitionStatus.NOT_ACQUIRED if retained else None,
                reason=str(
                    entry.get("reason_code")
                    or "notion_item_not_attempted_after_failure"
                ),
            )
        else:
            context.finish_attempt(
                attempt_id,
                ProcedureStatus.INTERRUPTED,
                AcquisitionStatus.NOT_ACQUIRED,
                reason=str(entry.get("reason_code") or "notion_item_unavailable"),
            )
    return attempt_ids


def write_json(
    context,
    relative_path: str,
    document: Mapping[str, object],
    action: ActionRef,
    linked_context: Mapping[str, object],
):
    summary = document.get("record_kind") in {"notion_exports", "notion_pages", "notion_workspaces"}
    try:
        value = (
            json.dumps(
                document,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise NotionCollectorError("Notion output JSON is invalid") from None
    record_kind = document.get("record_kind")
    attempt_id = (
        item_attempt(context, {"target_ref": document.get("target_ref")})
        if summary
        else item_attempt(
            context,
            linked_context,
            record_kind=record_kind,
            prefix=relative_path,
        )
    )
    retained = output_action(
        context,
        attempt_id,
        action,
        linked_context,
    )
    artifact = context.artifacts.write_bytes(
        relative_path,
        value,
        kind="ui_record",
        attempt_id=attempt_id,
        action=retained,
        context=dict(linked_context),
    )
    status = document.get("acquisition_status")
    if summary:
        _register_summary_results(context, document)
    if status in {
        "partial",
        "not_materialized",
        "unavailable",
        "out_of_scope",
        "not_attempted",
    } and next(
        record
        for record in context.attempts
        if record.attempt_id == attempt_id
    ).procedure_status is None:
        reason = str(document.get("reason_code") or status)
        observation_id = linked_context.get("observation_id")
        if not any(o.observation_id == observation_id and o.attempt_id == attempt_id for o in context.observations):
            observation_id = None
        context.finish_attempt(
            attempt_id,
            ProcedureStatus.COMPLETED,
            (
                AcquisitionStatus.PARTIAL
                if status == "partial"
                else AcquisitionStatus.NOT_ACQUIRED
            ),
            reason=reason,
            action=retained,
            observation_id=observation_id,
        )
    return artifact


def click(context, action_name: str, element_id: str, **linked):
    return context.ui.click(
        action_name,
        selector(context, element_id),
        context=dict(linked),
    )


def safe_component(label: object) -> str:
    value = " ".join(
        _UNSAFE_LABEL.sub("_", str(label)).split()
    )[:60]
    return value or "untitled"
