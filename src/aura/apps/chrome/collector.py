"""Chrome UI record parsers."""

from __future__ import annotations

import hashlib
import json
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable

from ...models import (
    AcquisitionStatus,
    ActionRef,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
)
from ...ui import UiTimeout


PREFIX = "com.android.chrome:id/"
_UI_KEYS = {
    "resource_id": "resourceId",
    "text": "text",
    "content_description": "description",
    "class_name": "className",
}


def merge_windows(
    collected: list[dict[str, object]],
    visible: list[dict[str, object]],
) -> list[dict[str, object]]:
    overlap = min(len(collected), len(visible))
    while overlap and collected[-overlap:] != visible[:overlap]:
        overlap -= 1
    return [*collected, *visible[overlap:]]


def merge_download_windows(
    collected: list[dict[str, object]], visible: list[dict[str, object]]
) -> list[dict[str, object]]:
    # ponytail: identical adjacent occurrences use ordered overlap; retain UI
    # windows for ambiguity review if no distinct neighbouring row is visible.
    overlap = min(len(collected), len(visible))
    while overlap and not all(
        old["title"] == new["title"]
        and all(not old[key] or not new[key] or old[key] == new[key]
                for key in ("caption", "date_group"))
        for old, new in zip(collected[-overlap:], visible[:overlap])
    ):
        overlap -= 1
    result = [dict(row) for row in collected]
    for index, row in enumerate(visible[:overlap], len(result) - overlap):
        result[index].update({key: value for key, value in row.items() if value})
    date_group = result[-1]["date_group"] if overlap else ""
    for row in visible[overlap:]:
        date_group = row["date_group"] or date_group
        result.append({**row, "date_group": date_group})
    return result


def _id(node: ET.Element) -> str:
    return node.get("resource-id", "").rsplit("/", 1)[-1]


def _text(node: ET.Element) -> str:
    return node.get("text", "").strip()


def _descendant(node: ET.Element, resource_id: str) -> ET.Element | None:
    return next((child for child in node.iter() if _id(child) == resource_id), None)


def _value(node: ET.Element, resource_id: str) -> str:
    child = _descendant(node, resource_id)
    return "" if child is None else _text(child)


def _root(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _bookmark_title(xml: str) -> str:
    root = _root(xml)
    action_bar = next(
        (
            node
            for node in root.iter()
            if _id(node) == "action_bar"
        ),
        None,
    )
    if action_bar is None:
        return ""
    return next(
        (
            _text(node)
            for node in action_bar.iter()
            if node.get("class") == "android.widget.TextView" and _text(node)
        ),
        "",
    )


def wait_for_bookmark_title(
    context, current: str, *, expected: str | None = None
) -> None:
    def transitioned() -> bool:
        try:
            title = _bookmark_title(context.device.hierarchy())
        except ET.ParseError:
            return False
        return title == expected if expected is not None else bool(title and title != current)

    if not context.ui.wait_until(transitioned):
        raise RuntimeError("Chrome bookmark transition did not complete")


def _bookmark_root_visible(xml: str) -> bool:
    root = _root(xml)
    recycler = next(
        (node for node in root.iter() if _id(node) == "selectable_list_recycler_view"),
        None,
    )
    return recycler is not None and any(
        child.get("clickable") != "true" and _value(child, "title")
        for child in recycler
    )


def _return_to_bookmark_root(context, action: ActionRef) -> ActionRef:
    while True:
        hierarchy = context.device.hierarchy()
        if _bookmark_root_visible(hierarchy):
            return action
        current = _bookmark_title(hierarchy)
        if not current:
            raise RuntimeError("Chrome bookmark location is unavailable")
        action = context.ui.click(
            "return_to_chrome_bookmark_root",
            _selector(context, "chrome.bookmarks.go-back"),
            context={"surface": "bookmarks"},
        )
        wait_for_bookmark_title(context, current)


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
        raise RuntimeError(f"invalid Chrome selector: {name}") from None


def parse_history(xml: str) -> list[dict[str, str]]:
    records = []
    date_group = ""
    for node in _root(xml).iter():
        if (
            node.get("class") == "android.widget.TextView"
            and not node.get("resource-id")
            and _text(node)
        ):
            date_group = _text(node)
        if node.get("clickable") != "true":
            continue
        title = _value(node, "title")
        address = _value(node, "description")
        if title and address:
            records.append(
                {
                    "date_group": date_group,
                    "title": title,
                    "displayed_address": address,
                }
            )
    return records


def parse_downloads(xml: str) -> list[dict[str, str]]:
    records = []
    date_group = ""
    for node in _root(xml).iter():
        if _id(node) == "date" and _text(node):
            date_group = _text(node)
        if node.get("clickable") != "true":
            continue
        title = _value(node, "title")
        caption = _value(node, "caption")
        thumbnail = _descendant(node, "thumbnail")
        if not title and thumbnail is not None:
            title = thumbnail.get("content-desc", "").strip()
        if title:
            records.append(
                {"date_group": date_group, "title": title, "caption": caption}
            )
    return records


def parse_bookmarks(
    xml: str, *, folder_path: Iterable[str]
) -> list[dict[str, object]]:
    path = list(folder_path)
    root = _root(xml)
    sections: dict[int, str] = {}
    recycler = next(
        (node for node in root.iter() if _id(node) == "selectable_list_recycler_view"),
        None,
    )
    if not path and recycler is not None:
        section = ""
        for child in recycler:
            if child.get("clickable") == "true" and _value(child, "title"):
                sections[id(child)] = section
            else:
                section = _value(child, "title") or section
    records = []
    for node in root.iter():
        if node.get("clickable") != "true":
            continue
        title = _value(node, "title")
        if not title:
            continue
        child_count_node = _descendant(node, "child_count_text")
        bookmark = child_count_node is None
        section = sections.get(id(node))
        record: dict[str, object] = {
            "kind": "bookmark" if bookmark else "folder",
            "folder_path": [*path, *([section] if section else [])],
            "title": title,
        }
        child_count = _value(node, "child_count_text")
        if not bookmark and child_count:
            record["child_count"] = child_count
        records.append(record)
    return records


def parse_recent_tabs(xml: str) -> list[dict[str, str]]:
    records = []
    group = ""
    for node in _root(xml).iter():
        if _id(node) == "recent_tabs_group_view":
            group = _value(node, "device_label")
        elif _id(node) == "recent_tabs_list_item_layout":
            title = _value(node, "title_row")
            if title:
                records.append(
                    {
                        "group": group,
                        "title": title,
                        "domain": _value(node, "domain_row"),
                    }
                )
    return records


def parse_account(xml: str) -> dict[str, str]:
    root = _root(xml)
    row = next(
        (
            node
            for node in root.iter()
            if _id(node) == "account_management_account_row"
        ),
        None,
    )
    if row is None:
        return {}
    display_name = _value(row, "title")
    email = _value(row, "summary")
    return {
        key: value
        for key, value in {"display_name": display_name, "email": email}.items()
        if value
    }


def _parse_account_list(xml: str) -> list[dict[str, str]]:
    record = parse_account(xml)
    return [record] if record else []


def _bookmark_rows(
    xml: str, folder_path: tuple[str, ...]
) -> list[tuple[dict[str, object], dict[str, object] | None]]:
    root = _root(xml)
    records = parse_bookmarks(xml, folder_path=folder_path)
    rows = [
        node
        for node in root.iter()
        if node.get("clickable") == "true" and _value(node, "title")
    ]
    result = []
    for record, row in zip(records, rows, strict=True):
        description = row.get("content-desc", "").strip()
        if record["kind"] == "folder" and not description:
            raise RuntimeError("Chrome bookmark description is unavailable")
        result.append(
            (
                record,
                None
                if record["kind"] == "bookmark"
                else {
                    "description": description,
                    "packageName": "com.android.chrome",
                },
            )
        )
    return result


def _observe(
    context,
    attempt_id: str,
    prefix: str,
    source_action: ActionRef,
    linked: dict[str, object],
) -> tuple[str, ActionRef]:
    hierarchy = context.device.hierarchy()
    action = context.linked_action(
        "observe_chrome_ui",
        attempt_id,
        source_action=source_action,
        context=linked,
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


def _open_surface(context, menu_item: str, marker: str) -> ActionRef:
    close = _selector(context, "chrome.close")
    home = _selector(context, "chrome.home")
    menu = _selector(context, "chrome.menu")
    if context.device.exists(close):
        context.ui.click("close_chrome_surface", close, expected=menu)
    if context.device.exists(home):
        context.ui.click("open_chrome_home", home, expected=menu)
    menu_list = _selector(context, "chrome.menu.list")
    try:
        context.ui.click("open_chrome_menu", menu, expected=menu_list)
    except UiTimeout:
        time.sleep(context.app_profile.timings.get("transition_settle", 0.2))
        context.ui.click(
            "open_chrome_menu_retry", menu, expected=menu_list
        )
    return context.ui.click(
        f"open_{menu_item.replace('.', '_')}",
        _selector(context, menu_item),
        expected=_selector(context, marker),
    )


def _retain_records(
    context,
    attempt_id: str,
    surface: str,
    records: list[dict[str, object]],
    source_action: ActionRef,
) -> ActionRef:
    linked = {"surface": surface, "record_count": len(records)}
    action = context.linked_action(
        "retain_chrome_records",
        attempt_id,
        source_action=source_action,
        context=linked,
    )
    payload = {
        "record_kind": f"chrome_{surface}",
        "record_count": len(records),
        "records": records,
    }
    context.artifacts.write_bytes(
        f"chrome/{surface}.json",
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(),
        kind="structured_data",
        attempt_id=attempt_id,
        action=action,
        context=linked,
    )
    return action


def _collect_list(
    context,
    *,
    surface: str,
    target_id: str,
    item_type: str,
    menu_item: str,
    marker: str,
    parser,
    scroll: bool = True,
) -> tuple[int, ActionRef]:
    context.begin_identification(target_id)
    attempt_id = context.item_attempt(
        f"chrome:{surface}",
        target_id,
        item_type,
        source={"surface": surface},
    )
    records: list[dict[str, object]] = []
    source_action = None
    traversal_error = None
    try:
        source_action = _open_surface(context, menu_item, marker)
        seen_hierarchies: set[str] = set()
        window = 0
        while True:
            hierarchy = context.device.hierarchy()
            signature = hashlib.sha256(hierarchy.encode()).hexdigest()
            if signature in seen_hierarchies:
                break
            seen_hierarchies.add(signature)
            window += 1
            hierarchy, source_action = _observe(
                context,
                attempt_id,
                f"chrome/{surface}/window-{window:04d}",
                source_action,
                {"surface": surface, "window": window},
            )
            merge = merge_download_windows if surface == "downloads" else merge_windows
            records = merge(records, parser(hierarchy))
            if not scroll:
                break
            source_action = context.ui.swipe(
                f"scroll_chrome_{surface}",
                "up",
                context={"surface": surface, "window": window},
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.2))
    except (Exception, KeyboardInterrupt) as error:
        traversal_error = error
    action = _retain_records(
        context, attempt_id, surface, records, source_action
    )
    artifact = context.artifacts.records[-1]
    context.register_collection_records(target_id, artifact, records)
    context.finish_identification(
        target_id,
        completion_condition=None if traversal_error else ("repeated_hierarchy" if scroll else "single_screen_parsed"),
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


def _collect_bookmarks(context) -> tuple[int, ActionRef]:
    surface = "bookmarks"
    context.begin_identification("chrome.bookmarks")
    attempt_id = context.item_attempt(
        "chrome:bookmarks",
        "chrome.bookmarks",
        "bookmark_collection",
        source={"surface": surface},
    )
    source_action = _open_surface(
        context, "chrome.menu.bookmarks", "chrome.screen.bookmarks"
    )
    source_action = _return_to_bookmark_root(context, source_action)
    records: list[dict[str, object]] = []
    seen_records: set[tuple[str, tuple[str, ...], str]] = set()
    visited_folders: set[tuple[str, ...]] = {()}
    window = 0

    def walk(path: tuple[str, ...], action: ActionRef) -> ActionRef:
        nonlocal window
        seen_hierarchies: set[str] = set()
        while True:
            hierarchy = context.device.hierarchy()
            signature = hashlib.sha256(hierarchy.encode()).hexdigest()
            if signature in seen_hierarchies:
                return action
            seen_hierarchies.add(signature)
            parent_title = _bookmark_title(hierarchy)
            window += 1
            hierarchy, action = _observe(
                context,
                attempt_id,
                f"chrome/bookmarks/window-{window:04d}",
                action,
                {"surface": surface, "folder_path": list(path), "window": window},
            )
            for record, selector in _bookmark_rows(hierarchy, path):
                record_path = tuple(map(str, record["folder_path"]))
                key = (str(record["kind"]), record_path, str(record["title"]))
                if key not in seen_records:
                    seen_records.add(key)
                    records.append(record)
                if record["kind"] != "folder":
                    continue
                child_path = (*record_path, str(record["title"]))
                if child_path in visited_folders:
                    continue
                if selector is None:
                    raise RuntimeError("Chrome bookmark selector is unavailable")
                visited_folders.add(child_path)
                action = context.ui.click(
                    "open_chrome_bookmark_folder",
                    selector,
                    context={"folder_path": list(child_path)},
                )
                wait_for_bookmark_title(context, parent_title)
                action = walk(child_path, action)
                child_title = _bookmark_title(context.device.hierarchy())
                action = context.ui.click(
                    "return_from_chrome_bookmark_folder",
                    _selector(context, "chrome.bookmarks.go-back"),
                    expected=_selector(context, "chrome.screen.bookmarks"),
                    context={"folder_path": list(path)},
                )
                wait_for_bookmark_title(
                    context, child_title, expected=parent_title
                )
            action = context.ui.swipe(
                "scroll_chrome_bookmarks",
                "up",
                context={"folder_path": list(path), "window": window},
            )
            time.sleep(context.app_profile.timings.get("transition_settle", 0.2))

    traversal_error = None
    try:
        source_action = walk((), source_action)
    except (Exception, KeyboardInterrupt) as error:
        traversal_error = error
    action = _retain_records(
        context, attempt_id, surface, records, source_action
    )
    context.register_collection_records("chrome.bookmarks", context.artifacts.records[-1], records)
    context.finish_identification("chrome.bookmarks", completion_condition=None if traversal_error else "recursive_bookmarks_exhausted",
                                  reason=(str(traversal_error) or type(traversal_error).__name__) if traversal_error else None)
    context.finish_attempt(
        attempt_id,
        ProcedureStatus.INTERRUPTED if traversal_error else ProcedureStatus.COMPLETED,
        AcquisitionStatus.PARTIAL if traversal_error else AcquisitionStatus.ACQUIRED,
        reason=(str(traversal_error) or type(traversal_error).__name__) if traversal_error else None,
        action=action,
        details={"record_count": len(records)},
    )
    if traversal_error:
        raise traversal_error
    return len(records), action


def collect(context) -> Outcome:
    context.app_profile.require_identification_rules({
        'chrome.account': ('chrome.parse_account', {'row_resource_id': 'account_management_account_row', 'fields': ['android:id/title', 'android:id/summary']}),
        'chrome.history': ('chrome.parse_history', {'row_clickable': True, 'required_child_resource_ids': ['title', 'description'], 'date_marker': 'unlabelled_text'}),
        'chrome.downloads': ('chrome.parse_downloads', {'row_clickable': True, 'title_sources': ['title.text', 'thumbnail.content-desc'], 'optional_child_resource_ids': ['caption'], 'date_resource_id': 'date', 'window_merge': 'ordered_overlap_with_missing_field_enrichment'}),
        'chrome.bookmarks': ('chrome.parse_bookmarks', {'identity_fields': ['kind', 'folder_path', 'title'], 'traverse_folders': True}),
        'chrome.recent_tabs': ('chrome.parse_recent_tabs', {'row_resource_id': 'recent_tabs_list_item_layout', 'required_child_resource_id': 'title_row', 'group_resource_id': 'recent_tabs_group_view'}),
    })
    condition = context.base_context.get("condition", {})
    target = condition.get("target") if isinstance(condition, dict) else None
    if (
        not isinstance(target, dict)
        or target.get("kind") != "browser"
        or target.get("ref") != "browser.chrome.all"
    ):
        return Outcome(OutcomeStatus.FAILED, "chrome_target_invalid")
    if not context.start_app():
        return Outcome(OutcomeStatus.FAILED, "chrome_app_start_not_verified")

    counts: dict[str, int] = {}
    counts["history"], action = _collect_list(
        context,
        surface="history",
        target_id="chrome.history",
        item_type="history_collection",
        menu_item="chrome.menu.history",
        marker="chrome.screen.history",
        parser=parse_history,
    )
    counts["downloads"], action = _collect_list(
        context,
        surface="downloads",
        target_id="chrome.downloads",
        item_type="download_collection",
        menu_item="chrome.menu.downloads",
        marker="chrome.screen.downloads",
        parser=parse_downloads,
    )
    counts["bookmarks"], action = _collect_bookmarks(context)
    counts["recent_tabs"], action = _collect_list(
        context,
        surface="recent-tabs",
        target_id="chrome.recent_tabs",
        item_type="recent_tab_collection",
        menu_item="chrome.menu.recent-tabs",
        marker="chrome.screen.recent-tabs",
        parser=parse_recent_tabs,
    )
    counts["account"], action = _collect_list(
        context,
        surface="account",
        target_id="chrome.account",
        item_type="account_context",
        menu_item="chrome.menu.settings",
        marker="chrome.screen.settings",
        parser=_parse_account_list,
        scroll=False,
    )
    return Outcome(
        OutcomeStatus.COMPLETE,
        action=action,
        context={"target_ref": target["ref"]},
        details={"record_counts": counts},
    )


__all__ = [
    "merge_windows",
    "parse_bookmarks",
    "parse_account",
    "parse_downloads",
    "parse_history",
    "parse_recent_tabs",
    "wait_for_bookmark_title",
    "collect",
]
