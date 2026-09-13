"""Shared UI and artifact helpers for the exact-version Notion collector."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping

from ...device import COORDINATE_SELECTOR_KEYS
from ...models import (
    AcquisitionStatus,
    ActionRef,
    ProcedureStatus,
)


PACKAGE = "notion.id"
_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_UNSAFE_LABEL = re.compile(r"[/\\:\x00-\x1f]")
_UI_KEYS = {
    "class_name": "className",
    "content_description": "description",
    "resource_id": "resourceId",
    "text": "text",
    "checked": "checked",
    "clickable": "clickable",
    "focusable": "focusable",
    "scrollable": "scrollable",
    "selected": "selected",
}


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
    if "attachment_ref" in linked or record_kind == "notion_attachment":
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
            "notion.content",
            "workspace",
            linked.get("workspace_ref", prefix),
        )
    else:
        target_id, item_type, ref = (
            "notion.content",
            "account_collection",
            linked.get("target_ref", prefix or "account"),
        )
    source = dict(linked)
    scope = ":".join(
        f"{name}={linked[name]}"
        for name in (
            "target_ref",
            "workspace_ref",
            "parent_page_ref",
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


def bounds(node: ET.Element) -> tuple[int, int, int, int]:
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        raise NotionCollectorError("element bounds are invalid")
    result = tuple(map(int, match.groups()))
    if result[0] >= result[2] or result[1] >= result[3]:
        raise NotionCollectorError("element bounds are invalid")
    return result


def onscreen(node: ET.Element) -> bool:
    if node.get("package") != PACKAGE:
        return False
    try:
        left, top, right, bottom = bounds(node)
    except NotionCollectorError:
        return False
    return right > 0 and bottom > 0 and left < 1080 and top < 2400


def selector(context, element_id: str) -> dict[str, object]:
    try:
        entry = context.app_profile.selectors[element_id]
        spec = entry["selector"]
        result = {
            _UI_KEYS[str(spec["kind"])]: spec["value"],
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
        result.update({
            _UI_KEYS[key]: value for key, value in constraints.items()
        })
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
) -> dict[str, object]:
    time.sleep(context.app_profile.timings.get("transition_settle", 0.5))
    previous = None
    current = None
    parsed = None
    parser_error = None
    stable_polls = 0

    def settled() -> bool:
        nonlocal previous, current, parsed, parser_error, stable_polls
        current = context.device.hierarchy()
        try:
            root = ET.fromstring(current)
        except (ET.ParseError, TypeError):
            raise NotionCollectorError("Notion hierarchy is invalid") from None
        if parser is not None:
            try:
                parsed = parser(root)
                parser_error = None
            except NotionCollectorError as error:
                parsed = None
                parser_error = str(error)
                previous = None
                stable_polls = 0
                return False
        stable_polls = stable_polls + 1 if current == previous else 1
        previous = current
        return stable_polls >= 2

    if not context.ui.wait_until(settled):
        if parser is not None and isinstance(current, str):
            linked = {
                **dict(action.context),
                "observation_id": observation_id,
                "parser_error": parser_error,
            }
            attempt_id = item_attempt(
                context,
                linked,
                prefix=diagnostic_name or observation_id,
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
    context.journal.append(
        "observation_recorded",
        status="success",
        context={"observation_id": observation_id},
        details={"action_event_id": action.event_id},
    )
    result = {
        "observation_id": observation_id,
        "root": root,
        "screen": screen,
        "tree": current.encode("utf-8"),
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
