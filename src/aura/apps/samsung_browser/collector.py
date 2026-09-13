"""Samsung Browser UI record parsers."""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET

from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
)


PREFIX = "com.sec.android.app.sbrowser:id/"
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


def _value(node: ET.Element, resource_id: str) -> str:
    child = next((item for item in node.iter() if _id(item) == resource_id), None)
    return "" if child is None else _text(child)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


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
        raise RuntimeError(f"invalid Samsung Browser selector: {name}") from None


def _merge_windows(
    collected: list[dict[str, object]],
    visible: list[dict[str, object]],
) -> list[dict[str, object]]:
    overlap = min(len(collected), len(visible))
    while overlap and collected[-overlap:] != visible[:overlap]:
        overlap -= 1
    return [*collected, *visible[overlap:]]


def parse_history(xml: str) -> list[dict[str, str]]:
    root = _root(xml)
    date_group = next(
        (_text(node) for node in root.iter() if _id(node) == "title" and _text(node)),
        "",
    )
    return [
        {
            "date_group": date_group,
            "title": _value(row, "history_title"),
            "displayed_address": _value(row, "history_url"),
            "displayed_time": _value(row, "history_time"),
        }
        for row in root.iter()
        if _id(row) == "history_relative" and _value(row, "history_title")
    ]


def dedupe_history(records: list[dict[str, str]]) -> list[dict[str, str]]:
    result = []
    seen = {}
    for record in records:
        key = (
            record.get("title", ""),
            record.get("displayed_address", ""),
            record.get("displayed_time", ""),
        )
        if key in seen:
            if not seen[key].get("date_group") and record.get("date_group"):
                seen[key]["date_group"] = record["date_group"]
            continue
        retained = dict(record)
        seen[key] = retained
        result.append(retained)
    return result


def parse_downloads(xml: str) -> list[dict[str, str]]:
    return [
        {
            "title": _value(row, "download_item_title"),
            "source": _value(row, "download_item_url"),
            "displayed_size": _value(row, "download_item_total_size"),
        }
        for row in _root(xml).iter()
        if _id(row) == "download_item_parent" and _value(row, "download_item_title")
    ]


def parse_saved_pages(xml: str) -> list[dict[str, str]]:
    return [
        {
            "title": _value(row, "saved_page_list_view_title_text_view"),
            "displayed_address": _value(row, "saved_page_list_view_url_text_view"),
            "description": _value(row, "saved_page_list_view_description_text_view"),
        }
        for row in _root(xml).iter()
        if _id(row) == "saved_page_list_view_layout"
        and _value(row, "saved_page_list_view_title_text_view")
    ]


def parse_bookmarks(xml: str) -> list[dict[str, str]]:
    return [
        {"title": _value(row, "bookmark_folder_title")}
        for row in _root(xml).iter()
        if _id(row) == "displayed_view" and _value(row, "bookmark_folder_title")
    ]


def _return_to_main(context) -> ActionRef | None:
    main = _selector(context, "samsung_browser.main")
    if context.device.exists(main):
        return None
    seen = set()
    action = None
    while not context.device.exists(main):
        hierarchy = context.device.hierarchy()
        signature = hashlib.sha256(hierarchy.encode()).hexdigest()
        if signature in seen:
            raise RuntimeError("Samsung Browser main screen is unavailable")
        seen.add(signature)
        action = context.ui.back("return_to_samsung_browser_main")
        time.sleep(context.app_profile.timings.get("transition_settle", 0.3))
    assert action is not None
    return action


def _open_surface(context, menu_item: str, marker: str) -> ActionRef:
    _return_to_main(context)
    context.ui.click(
        "open_samsung_browser_menu",
        _selector(context, "samsung_browser.menu"),
        expected=_selector(context, "samsung_browser.menu.list"),
    )
    return context.ui.click(
        f"open_{menu_item.replace('.', '_')}",
        _selector(context, menu_item),
        expected=_selector(context, marker),
    )


def _open_bookmarks(context) -> ActionRef:
    _return_to_main(context)
    return context.ui.click(
        "open_samsung_browser_bookmarks",
        _selector(context, "samsung_browser.bookmarks"),
        expected=_selector(context, "samsung_browser.screen.bookmarks"),
    )


def _collect_list(
    context,
    *,
    surface: str,
    target_id: str,
    item_type: str,
    parser,
    opener,
) -> tuple[int, ActionRef]:
    context.begin_identification(target_id)
    attempt_id = context.item_attempt(
        f"samsung-browser:{surface}",
        target_id,
        item_type,
        source={"surface": surface},
    )
    records: list[dict[str, object]] = []
    source_action = None
    traversal_error = None
    try:
        source_action = opener()
        seen_hierarchies: set[str] = set()
        window = 0
        while True:
            hierarchy = context.device.hierarchy()
            signature = hashlib.sha256(hierarchy.encode()).hexdigest()
            if signature in seen_hierarchies:
                break
            seen_hierarchies.add(signature)
            window += 1
            linked = {"surface": surface, "window": window}
            action = context.linked_action(
                "observe_samsung_browser_ui",
                attempt_id,
                source_action=source_action,
                context=linked,
            )
            context.retain_observation(
                f"samsung-browser/{surface}/window-{window:04d}",
                context.device.screenshot(),
                hierarchy.encode(),
                attempt_id=attempt_id,
                action=action,
                context=linked,
            )
            records = _merge_windows(records, parser(hierarchy))
            source_action = context.ui.swipe(
                f"scroll_samsung_browser_{surface}",
                "up",
                context=linked,
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.3))

    except (Exception, KeyboardInterrupt) as error:
        traversal_error = error
    if target_id == "samsung_browser.history":
        records = dedupe_history(records)
    linked = {"surface": surface, "record_count": len(records)}
    action = context.linked_action(
        "retain_samsung_browser_records",
        attempt_id,
        source_action=source_action,
        context=linked,
    )
    payload = {
        "record_kind": f"samsung_browser_{surface}",
        "record_count": len(records),
        "records": records,
    }
    artifact = context.artifacts.write_bytes(
        f"samsung-browser/{surface}.json",
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        kind="structured_data",
        attempt_id=attempt_id,
        action=action,
        context=linked,
    )
    context.register_collection_records(target_id, artifact, records)
    context.finish_identification(target_id,
        completion_condition=None if traversal_error else "repeated_hierarchy",
        reason=(str(traversal_error) or type(traversal_error).__name__) if traversal_error else None,
    )
    context.finish_attempt(
        attempt_id,
        ProcedureStatus.INTERRUPTED if traversal_error else ProcedureStatus.COMPLETED,
        AcquisitionStatus.PARTIAL if traversal_error else context.result_status(attempt_id),
        reason=(str(traversal_error) or type(traversal_error).__name__) if traversal_error else None,
        action=action,
        details={"record_count": len(records)},
    )
    if traversal_error:
        raise traversal_error
    return len(records), action


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules({
        'samsung_browser.history': ('samsung_browser.parse_history', {'row_resource_id': 'history_relative', 'required_child_resource_id': 'history_title', 'identity_fields': ['title', 'displayed_address', 'displayed_time']}),
        'samsung_browser.downloads': ('samsung_browser.parse_downloads', {'row_resource_id': 'download_item_parent', 'required_child_resource_id': 'download_item_title'}),
        'samsung_browser.saved_pages': ('samsung_browser.parse_saved_pages', {'row_resource_id': 'saved_page_list_view_layout', 'required_child_resource_id': 'saved_page_list_view_title_text_view'}),
        'samsung_browser.bookmarks': ('samsung_browser.parse_bookmarks', {'row_resource_id': 'displayed_view', 'required_child_resource_id': 'bookmark_folder_title'}),
    })
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if (
        not isinstance(target, dict)
        or target.get("kind") != "browser"
        or target.get("ref") != "browser.samsung-browser.all"
    ):
        return Outcome(OutcomeStatus.FAILED, "samsung_browser_target_invalid")
    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED, "samsung_browser_app_start_not_verified"
        )

    specifications = (
        (
            "history",
            "samsung_browser.history",
            "history_collection",
            parse_history,
            lambda: _open_surface(
                context,
                "samsung_browser.menu.history",
                "samsung_browser.screen.history",
            ),
        ),
        (
            "downloads",
            "samsung_browser.downloads",
            "download_collection",
            parse_downloads,
            lambda: _open_surface(
                context,
                "samsung_browser.menu.downloads",
                "samsung_browser.screen.downloads",
            ),
        ),
        (
            "saved-pages",
            "samsung_browser.saved_pages",
            "saved_page_collection",
            parse_saved_pages,
            lambda: _open_surface(
                context,
                "samsung_browser.menu.saved-pages",
                "samsung_browser.screen.saved-pages",
            ),
        ),
        (
            "bookmarks",
            "samsung_browser.bookmarks",
            "bookmark_collection",
            parse_bookmarks,
            lambda: _open_bookmarks(context),
        ),
    )
    counts = {}
    for surface, target_id, item_type, parser, opener in specifications:
        counts[surface], action = _collect_list(
            context,
            surface=surface,
            target_id=target_id,
            item_type=item_type,
            parser=parser,
            opener=opener,
        )
    return Outcome(
        OutcomeStatus.COMPLETE,
        action=action,
        context={"target_ref": target["ref"]},
        details={"record_counts": counts},
    )


__all__ = [
    "parse_bookmarks",
    "parse_downloads",
    "dedupe_history",
    "parse_history",
    "parse_saved_pages",
    "collect",
]
