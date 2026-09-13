"""Notion 0.6.4030 general-page parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from itertools import count
from pathlib import PurePosixPath

from .attachments import (
    AttachmentCollectionError,
    candidate_key,
    materialize_candidate,
    parse_page_candidates,
)
from .databases import DatabaseCollectionError, collect_open_database
from .support import (
    PACKAGE,
    NotionCollectorError,
    item_attempt,
    bounds,
    observe,
    onscreen,
    retain_pair,
    safe_component,
    selector,
    write_json,
)


_PAGE_ROW = re.compile(
    r"^home-tab\.(private|shared|teamspaces?)\.page-row\."
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)
_SECTION_HEADER = re.compile(
    r"^home-tab\.sections\.(private|shared|teamspaces?)-header$",
    re.IGNORECASE,
)
_DATABASE_CONTROLS = {
    "Filter and Sort",
    "Edit view layout, grouping and more...",
}


class PageCollectionError(NotionCollectorError):
    def __init__(
        self,
        reason_code: str,
        *,
        page_ref: str | None = None,
        page_id: str | None = None,
        action=None,
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.page_ref = page_ref
        self.page_id = page_id
        self.action = action


def _section(value: str) -> str:
    normalized = value.lower()
    return (
        "teamspaces"
        if normalized.startswith("teamspace")
        else normalized
    )


def parse_section_headers(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    result = []
    for node in root.iter():
        if not onscreen(node):
            continue
        match = _SECTION_HEADER.fullmatch(node.get("resource-id", ""))
        if match is None:
            continue
        result.append({
            "section": _section(match.group(1)),
            "bounds": bounds(node),
        })
    result.sort(key=lambda item: item["bounds"][1])
    return tuple(result)


def parse_home_rows(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    result = []
    for row in root.iter():
        if not onscreen(row):
            continue
        match = _PAGE_ROW.fullmatch(row.get("resource-id", ""))
        if match is None:
            continue
        section = _section(match.group(1))
        expanders = [
            node
            for node in row.iter()
            if onscreen(node)
            and node.get("resource-id")
            == f"home-tab.{match.group(1).lower()}.page-row.expand"
        ]
        if len(expanders) != 1:
            raise NotionCollectorError("Notion page row is ambiguous")
        _, expander_top, _, expander_bottom = bounds(expanders[0])
        titles = [
            node.get("text", "").strip()
            for node in row.iter()
            if onscreen(node)
            and node.get("class") == "android.widget.TextView"
            and node.get("text", "").strip()
            and bounds(node)[1] < expander_bottom
            and bounds(node)[3] > expander_top
        ]
        if len(titles) != 1:
            raise NotionCollectorError("Notion page row is ambiguous")
        page_id = match.group(2).lower()
        result.append({
            "page_id": page_id,
            "page_ref": f"page-{page_id}",
            "title": titles[0],
            "section": section,
            "indent_x": bounds(expanders[0])[0],
            "bounds": bounds(row),
            "expand_bounds": bounds(expanders[0]),
        })
    result.sort(key=lambda item: item["bounds"][1])
    return tuple(result)


def merge_rows(
    inventory: Mapping[str, Mapping[str, object]],
    stacks: Mapping[str, Sequence[tuple[int, str]]],
    rows: Sequence[Mapping[str, object]],
) -> tuple[
    dict[str, dict[str, object]],
    dict[str, list[tuple[int, str]]],
]:
    merged = {
        page_id: dict(record)
        for page_id, record in inventory.items()
    }
    current_stacks = {
        section: list(stack)
        for section, stack in stacks.items()
    }
    for row in rows:
        page_id = str(row["page_id"])
        section = str(row["section"])
        indent_x = int(row["indent_x"])
        stack = current_stacks.setdefault(section, [])
        while stack and stack[-1][0] >= indent_x:
            stack.pop()
        parent_page_id = stack[-1][1] if stack else None
        record = {
            **dict(row),
            "parent_page_id": parent_page_id,
        }
        existing = merged.get(page_id)
        if existing is not None:
            identity = ("title", "section", "parent_page_id")
            if any(existing.get(key) != record.get(key) for key in identity):
                raise NotionCollectorError(
                    "Notion page identity is inconsistent"
                )
            existing.update({
                "bounds": record["bounds"],
                "expand_bounds": record["expand_bounds"],
                "indent_x": record["indent_x"],
            })
        else:
            merged[page_id] = record
        stack.append((indent_x, page_id))
    return merged, current_stacks


def validate_inventory(
    inventory: Mapping[str, Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    records = {
        page_id: {
            **dict(record),
            "page_id": page_id,
            "page_ref": record.get("page_ref", f"page-{page_id}"),
        }
        for page_id, record in inventory.items()
    }
    depths: dict[str, int] = {}

    def depth(page_id: str, active: frozenset[str]) -> int:
        if page_id in depths:
            return depths[page_id]
        if page_id in active:
            raise NotionCollectorError(
                "Notion page inventory contains a cycle"
            )
        try:
            parent = records[page_id].get("parent_page_id")
        except KeyError:
            raise NotionCollectorError(
                "Notion page inventory is inconsistent"
            ) from None
        if parent is None:
            value = 0
        elif parent not in records:
            raise NotionCollectorError(
                "Notion page parent is unavailable"
            )
        else:
            value = depth(str(parent), active | {page_id}) + 1
        depths[page_id] = value
        return value

    result = []
    for page_id, record in records.items():
        parent = record.get("parent_page_id")
        record["parent_page_ref"] = (
            records[str(parent)]["page_ref"]
            if parent is not None and str(parent) in records
            else None
        )
        record["depth"] = depth(page_id, frozenset())
        result.append(record)
    return tuple(result)


def _is_page_document(node: ET.Element) -> bool:
    if (
        not onscreen(node)
        or node.get("class") != "android.widget.EditText"
        or node.get("clickable") == "true"
        or not node.get("text", "")
    ):
        return False
    left, top, right, bottom = bounds(node)
    return left == 0 and right == 1080 and bottom - top >= 1800


def _document_title_visible(
    root: ET.Element,
    expected_title: str,
) -> bool:
    for node in root.iter():
        if not _is_page_document(node) or bounds(node)[1] != 234:
            continue
        first_line = next(
            (
                line.strip()
                for line in node.get("text", "").splitlines()
                if line.strip()
            ),
            "",
        )
        if first_line == expected_title:
            return True
    return False


def parse_page(
    root: ET.Element,
    expected_title: str,
) -> dict[str, object]:
    if not any(
        onscreen(node)
        and node.get("class") == "android.widget.Button"
        and node.get("content-desc", "").strip() == "Back"
        for node in root.iter()
    ):
        raise NotionCollectorError("Notion page header is unavailable")
    titles = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
    ]
    if len(titles) != 1 and not _document_title_visible(
        root, expected_title
    ):
        raise NotionCollectorError("Notion page title was not verified")
    documents = [
        node.get("text", "")
        for node in root.iter()
        if _is_page_document(node)
    ]
    if len(documents) == 1:
        return {
            "classification": "general_page",
            "rendered_text": documents[0],
        }
    controls = {
        node.get("content-desc", "").strip()
        or node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.Button"
    }
    block_editors = sorted(
        (
            bounds(node)[1],
            bounds(node)[0],
            node.get("text", ""),
        )
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
    )
    if not documents and _DATABASE_CONTROLS <= controls:
        return {"classification": "database"}
    if len(titles) == 1:
        return {
            "classification": "general_page",
            "rendered_text": "\n".join(
                text for _, _, text in block_editors if text.strip()
            ),
        }
    raise NotionCollectorError("Notion page classification is unavailable")


def parse_internal_subpages(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    obstruction_tops = [
        bounds(node)[1]
        for node in root.iter()
        if node.get("resource-id") in {
            "com.android.systemui:id/navigation_bar_frame",
            "floating-toolbar.search-button",
        }
    ]
    content_bottom = min(obstruction_tops, default=2400)
    result = []
    for node in root.iter():
        title = node.get("content-desc", "").strip()
        if (
            not onscreen(node)
            or node.get("class") != "android.view.View"
            or node.get("clickable") != "true"
            or node.get("focusable") != "true"
            or node.get("resource-id", "")
            or not title
        ):
            continue
        node_bounds = bounds(node)
        if node_bounds[3] > content_bottom:
            continue
        labels = [
            child
            for child in node.iter()
            if child is not node
            and child.get("class") == "android.widget.TextView"
            and child.get("text", "").strip() == title
        ]
        icons = [
            child
            for child in node.iter()
            if child is not node
            and child.get("class") == "android.widget.Button"
            and child.get("clickable") == "true"
            and not child.get("text", "").strip()
            and not child.get("content-desc", "").strip()
        ]
        if len(labels) == 1 and len(icons) == 1:
            result.append({"title": title, "bounds": node_bounds})
    result.sort(key=lambda item: item["bounds"][1])
    return tuple(result)


def page_prefix(
    workspace_prefix: str,
    page_ref: str,
    inventory: Mapping[str, Mapping[str, object]],
) -> str:
    lineage = []
    current = page_ref
    active = set()
    while current is not None:
        if current in active:
            raise NotionCollectorError(
                "Notion page inventory contains a cycle"
            )
        active.add(current)
        try:
            record = inventory[current]
        except KeyError:
            raise NotionCollectorError(
                "Notion page output path is unavailable"
            ) from None
        component = (
            f'{current} ({safe_component(record["title"])})'
        )
        if PurePosixPath(component).name != component:
            raise NotionCollectorError("Notion page output path is invalid")
        lineage.append(component)
        parent = record.get("parent_page_ref")
        current = str(parent) if parent is not None else None
    lineage.reverse()
    result = PurePosixPath(workspace_prefix)
    for index, component in enumerate(lineage):
        if index:
            result /= "subpages"
        result /= component
    return str(result)


def _home_signature(root: ET.Element) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            node.get("resource-id", ""),
            node.get("text", ""),
            node.get("bounds", ""),
        )
        for node in root.iter()
        if node.get("package") == PACKAGE
        and (
            node.get("resource-id", "").startswith("home-tab.")
            or node.get("resource-id") == "vision_tab_Home"
        )
    )


def _action_name(sequence, stem: str) -> str:
    return f"{stem}_{next(sequence):06d}"


def _observe_home(context, sequence, workspace_ref: str, stem: str):
    if not context.ui.wait_for(selector(context, "notion.home")):
        raise PageCollectionError("notion_home_unavailable")
    action = context.journal.record_action(
        _action_name(sequence, stem),
        status="success",
        context={"workspace_ref": workspace_ref},
        details={"operation": "observe_home"},
    )
    return action, observe(
        context,
        _action_name(sequence, f"{stem}_observation"),
        action,
    )


def _swipe_observe(
    context,
    sequence,
    workspace_ref: str,
    direction: str,
):
    action = context.ui.swipe(
        _action_name(sequence, f"notion_page_home_swipe_{direction}"),
        direction,
        expect_change=False,
        context={"workspace_ref": workspace_ref},
    )
    return action, observe(
        context,
        _action_name(sequence, "observation_notion_page_home"),
        action,
    )


def _rewind_home(context, sequence, workspace_ref: str):
    action, observation = _observe_home(
        context,
        sequence,
        workspace_ref,
        "notion_page_home_scan_start",
    )
    signature = _home_signature(observation["root"])
    while True:
        action, current = _swipe_observe(
            context,
            sequence,
            workspace_ref,
            "down",
        )
        current_signature = _home_signature(current["root"])
        observation = current
        if current_signature == signature:
            return action, observation
        signature = current_signature


def _scan_home(context, sequence, workspace_ref: str):
    action, observation = _rewind_home(
        context, sequence, workspace_ref
    )
    inventory: dict[str, dict[str, object]] = {}
    stacks: dict[str, list[tuple[int, str]]] = {}
    headers: dict[str, tuple[int, int, int, int]] = {}
    previous = None
    while True:
        root = observation["root"]
        inventory, stacks = merge_rows(
            inventory,
            stacks,
            parse_home_rows(root),
        )
        headers.update({
            str(item["section"]): item["bounds"]
            for item in parse_section_headers(root)
        })
        signature = _home_signature(root)
        if signature == previous:
            break
        previous = signature
        action, observation = _swipe_observe(
            context,
            sequence,
            workspace_ref,
            "up",
        )
    return inventory, headers, action


def _find_home_item(
    context,
    sequence,
    workspace_ref: str,
    *,
    page_id: str | None = None,
    section: str | None = None,
):
    action, observation = _rewind_home(
        context, sequence, workspace_ref
    )
    previous = None
    while True:
        root = observation["root"]
        if page_id is not None:
            match = next(
                (
                    item
                    for item in parse_home_rows(root)
                    if item["page_id"] == page_id
                ),
                None,
            )
        else:
            match = next(
                (
                    item
                    for item in parse_section_headers(root)
                    if item["section"] == section
                ),
                None,
            )
        if match is not None:
            return match, action
        signature = _home_signature(root)
        if signature == previous:
            return None, action
        previous = signature
        action, observation = _swipe_observe(
            context,
            sequence,
            workspace_ref,
            "up",
        )


def _recent_page_bounds(
    root: ET.Element,
    expected_title: str,
) -> tuple[int, int, int, int] | None:
    matches = [
        item
        for item in _recent_cards(root)
        if item["title"] == expected_title
    ]
    return matches[0]["bounds"] if len(matches) == 1 else None


def _recent_header(root: ET.Element) -> ET.Element | None:
    headings = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("resource-id")
        == "home-tab.sections.recent-header"
    ]
    if len(headings) != 1:
        return None
    return headings[0]


def _recent_heading(root: ET.Element) -> str | None:
    heading = _recent_header(root)
    if heading is None:
        return None
    labels = [
        node.get("text", "").strip()
        for node in heading.iter()
        if node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
    ]
    return labels[0] if len(labels) == 1 else None


def _recent_cards(root: ET.Element) -> tuple[dict[str, object], ...]:
    prefix = "home-tab.recents.page-row."
    result = []
    for node in root.iter():
        resource_id = node.get("resource-id", "")
        if (
            not onscreen(node)
            or node.get("class") != "android.view.View"
            or node.get("clickable") != "true"
            or not resource_id.startswith(prefix)
        ):
            continue
        title = resource_id[len(prefix):].strip()
        labels = [
            child
            for child in node.iter()
            if child.get("class") == "android.widget.TextView"
            and child.get("text", "").strip() == title
        ]
        if title and len(labels) == 1:
            result.append({"title": title, "bounds": bounds(node)})
    result.sort(key=lambda item: item["bounds"][0])
    return tuple(result)


def _carousel_source(root: ET.Element) -> str | None:
    return {
        "Recents": "recents",
        "Offline pages": "offline_pages",
    }.get(_recent_heading(root))


def _carousel_cards(root: ET.Element) -> tuple[dict[str, object], ...]:
    return _recent_cards(root) if _carousel_source(root) is not None else ()


def _carousel_signature(
    root: ET.Element,
) -> tuple[tuple[str, tuple[int, int, int, int]], ...]:
    return tuple(
        (str(item["title"]), item["bounds"])
        for item in _carousel_cards(root)
    )


def _carousel_at_start(root: ET.Element) -> bool:
    header = _recent_header(root)
    cards = _carousel_cards(root)
    if header is None or not cards:
        return False
    return min(item["bounds"][0] for item in cards) > bounds(header)[0]


def _carousel_at_end(root: ET.Element) -> bool:
    header = _recent_header(root)
    cards = _carousel_cards(root)
    if header is None or not cards:
        return False
    return max(item["bounds"][2] for item in cards) < bounds(header)[2]


def _carousel_bounds(
    cards: Sequence[Mapping[str, object]],
) -> tuple[int, int, int, int]:
    rectangles = [item["bounds"] for item in cards]
    return (
        min(item[0] for item in rectangles),
        min(item[1] for item in rectangles),
        max(item[2] for item in rectangles),
        max(item[3] for item in rectangles),
    )


def _swipe_carousel_observe(
    context,
    sequence,
    workspace_ref: str,
    observation,
    direction: str,
):
    cards = _carousel_cards(observation["root"])
    if not cards:
        raise PageCollectionError("notion_page_carousel_unavailable")
    action = context.ui.swipe_bounds(
        _action_name(sequence, f"notion_page_carousel_swipe_{direction}"),
        _carousel_bounds(cards),
        direction,
        context={"workspace_ref": workspace_ref},
    )
    return action, observe(
        context,
        _action_name(sequence, "observation_notion_page_carousel"),
        action,
    )


def _rewind_visible_carousel(
    context,
    sequence,
    workspace_ref: str,
    action,
    observation,
):
    if not _carousel_cards(observation["root"]):
        return action, observation
    seen = set()
    while not _carousel_at_start(observation["root"]):
        signature = _carousel_signature(observation["root"])
        if signature in seen:
            return action, observation
        seen.add(signature)
        action, observation = _swipe_carousel_observe(
            context,
            sequence,
            workspace_ref,
            observation,
            "right",
        )
    return action, observation


def _rewind_carousel(context, sequence, workspace_ref: str):
    action, observation = _rewind_home(
        context, sequence, workspace_ref
    )
    return _rewind_visible_carousel(
        context,
        sequence,
        workspace_ref,
        action,
        observation,
    )


def _discover_carousel_cards(context, sequence, workspace_ref: str):
    action, observation = _rewind_home(
        context, sequence, workspace_ref
    )
    source = _carousel_source(observation["root"])
    if source == "recents":
        return (), None, action
    action, observation = _rewind_visible_carousel(
        context,
        sequence,
        workspace_ref,
        action,
        observation,
    )
    source = _carousel_source(observation["root"])
    if source is not None and not _carousel_cards(observation["root"]):
        header = _recent_header(observation["root"])
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_expand_page_carousel"),
            bounds(header),
            context={
                "workspace_ref": workspace_ref,
                "discovery_source": source,
            },
        )
        observation = observe(
            context,
            _action_name(sequence, "observation_notion_page_carousel"),
            action,
        )
        if _carousel_source(observation["root"]) != source:
            raise PageCollectionError("notion_page_carousel_unavailable")
        action, observation = _rewind_visible_carousel(
            context,
            sequence,
            workspace_ref,
            action,
            observation,
        )
    if not _carousel_cards(observation["root"]) or source is None:
        return (), None, action
    titles: list[str] = []
    seen = set()
    while True:
        signature = _carousel_signature(observation["root"])
        if signature in seen:
            break
        seen.add(signature)
        visible = [title for title, _ in signature]
        titles.extend(visible[_overlap(titles, visible):])
        if _carousel_at_end(observation["root"]):
            break
        action, current = _swipe_carousel_observe(
            context,
            sequence,
            workspace_ref,
            observation,
            "left",
        )
        if not context.ui.wait_for(
            selector(context, "notion.home"), timeout=0
        ):
            action = context.ui.back(
                _action_name(
                    sequence, "notion_restore_home_after_carousel"
                ),
                expected=selector(context, "notion.home"),
                context={"workspace_ref": workspace_ref},
            )
            restored = observe(
                context,
                _action_name(
                    sequence,
                    "observation_notion_carousel_home_restored",
                ),
                action,
            )
            if _carousel_source(restored["root"]) != source:
                raise PageCollectionError(
                    "notion_page_carousel_unavailable"
                )
            break
        observation = current
    return tuple(
        {"carousel_index": index, "title": title}
        for index, title in enumerate(titles, 1)
    ), source, action


def _find_carousel_card(
    context,
    sequence,
    workspace_ref: str,
    carousel_index: int,
    expected_title: str,
    expected_source: str,
):
    action, observation = _rewind_carousel(
        context, sequence, workspace_ref
    )
    titles: list[str] = []
    seen = set()
    target = carousel_index - 1
    while True:
        if _carousel_source(observation["root"]) != expected_source:
            return None, action
        signature = _carousel_signature(observation["root"])
        if signature in seen:
            return None, action
        seen.add(signature)
        cards = list(_carousel_cards(observation["root"]))
        visible = [str(item["title"]) for item in cards]
        overlap = _overlap(titles, visible)
        start = len(titles) - overlap
        if start <= target < start + len(cards):
            match = cards[target - start]
            if match["title"] != expected_title:
                raise PageCollectionError(
                    "notion_page_carousel_inventory_inconsistent"
                )
            return match, action
        titles.extend(visible[overlap:])
        if _carousel_at_end(observation["root"]):
            return None, action
        action, observation = _swipe_carousel_observe(
            context,
            sequence,
            workspace_ref,
            observation,
            "left",
        )


def _merge_inventory(target, source):
    for page_id, record in source.items():
        existing = target.get(page_id)
        if existing is None:
            target[page_id] = dict(record)
            continue
        for key in ("title", "section", "parent_page_id"):
            if existing.get(key) != record.get(key):
                raise PageCollectionError(
                    "notion_page_inventory_inconsistent",
                    page_id=page_id,
                )
        existing.update({
            key: record[key]
            for key in ("bounds", "expand_bounds", "indent_x")
        })


def _discover_top_level_pages(context, sequence, workspace_ref: str):
    inventory: dict[str, dict[str, object]] = {}
    current, headers, action = _scan_home(
        context, sequence, workspace_ref
    )
    _merge_inventory(inventory, current)

    populated = {item["section"] for item in inventory.values()}
    for section in tuple(headers):
        if section in populated:
            continue
        header, action = _find_home_item(
            context,
            sequence,
            workspace_ref,
            section=section,
        )
        if header is None:
            continue
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_open_page_section"),
            header["bounds"],
            context={
                "workspace_ref": workspace_ref,
                "section": section,
            },
        )
        observe(
            context,
            _action_name(sequence, "observation_notion_page_section"),
            action,
        )
        current, _, action = _scan_home(
            context, sequence, workspace_ref
        )
        _merge_inventory(inventory, current)
        populated = {item["section"] for item in inventory.values()}

    top_level = [
        item
        for item in validate_inventory(inventory)
        if item["depth"] == 0
    ]
    result = {}
    for index, item in enumerate(top_level, 1):
        page_ref = f"page-{index:06d}"
        result[page_ref] = {
            "page_ref": page_ref,
            "notion_page_id": item["page_id"],
            "title": item["title"],
            "section": item["section"],
            "parent_page_ref": None,
            "depth": 0,
            "top_index": index,
            "child_index": None,
            "discovery_source": "home",
        }
    return result, action


def _page_signature(
    root: ET.Element,
) -> tuple[tuple[str, str, str, str, str], ...]:
    return tuple(
        (
            node.get("class", ""),
            node.get("resource-id", ""),
            node.get("text", ""),
            node.get("content-desc", ""),
            node.get("bounds", ""),
        )
        for node in root.iter()
        if node.get("package") == PACKAGE
    )


def _observe_page(context, sequence, page_ref: str, stem: str):
    action = context.journal.record_action(
        _action_name(sequence, stem),
        status="success",
        context={"page_ref": page_ref},
        details={"operation": "observe_page"},
    )
    return action, observe(
        context,
        _action_name(sequence, f"{stem}_observation"),
        action,
    )


def _swipe_page_observe(
    context,
    sequence,
    page_ref: str,
    direction: str,
):
    action = context.ui.swipe(
        _action_name(sequence, f"notion_page_swipe_{direction}"),
        direction,
        expect_change=False,
        context={"page_ref": page_ref},
    )
    return action, observe(
        context,
        _action_name(sequence, "observation_notion_page_scroll"),
        action,
    )


def _page_title_nodes(root: ET.Element) -> tuple[ET.Element, ...]:
    return tuple(
        node
        for node in root.iter()
        if (
            onscreen(node)
            and node.get("class") == "android.widget.EditText"
            and node.get("clickable") == "true"
            and bounds(node)[0] == 54
            and bounds(node)[2] == 1026
        )
    )


def _page_title_visible(root: ET.Element, expected_title: str) -> bool:
    return _document_title_visible(root, expected_title) or any(
        onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
        for node in root.iter()
    )


def _page_loading_visible(root: ET.Element) -> bool:
    return any(
        onscreen(node) and node.get("content-desc", "").strip() == "Loading"
        for node in root.iter()
    )


def _page_load_error_visible(root: ET.Element) -> bool:
    texts = {
        node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node) and node.get("class") == "android.widget.TextView"
    }
    return {
        "Oops, there was an error loading this page.",
        "Refresh to load it again.",
    } <= texts


def _page_top_visible(root: ET.Element, expected_title: str) -> bool:
    return bool(_page_title_nodes(root)) or _document_title_visible(
        root, expected_title
    )


def _rewind_page(
    context,
    sequence,
    page_ref: str,
    expected_title: str,
):
    action, observation = _observe_page(
        context, sequence, page_ref, "notion_page_scan_start"
    )
    if _page_title_visible(observation["root"], expected_title):
        return action, observation
    signature = _page_signature(observation["root"])
    while True:
        action, current = _swipe_page_observe(
            context, sequence, page_ref, "down"
        )
        current_signature = _page_signature(current["root"])
        observation = current
        if _page_title_visible(current["root"], expected_title):
            return action, observation
        if current_signature == signature:
            raise PageCollectionError(
                "notion_page_top_unavailable",
                page_ref=page_ref,
                action=action,
            )
        signature = current_signature


def _overlap(previous: Sequence[str], current: Sequence[str]) -> int:
    for size in range(min(len(previous), len(current)), 0, -1):
        if list(previous[-size:]) == list(current[:size]):
            return size
    return 0


def _page_context_visible(
    root: ET.Element,
    expected_title: str,
) -> bool:
    return _page_title_visible(root, expected_title) or any(
        onscreen(node)
        and node.get("class") == "android.widget.Button"
        and (
            node.get("text", "").strip() == expected_title
            or node.get("text", "").strip().startswith(
                f"{expected_title} "
            )
        )
        for node in root.iter()
    )


def _scan_page_content(
    context,
    sequence,
    *,
    page: Mapping[str, object],
    page_prefix: str,
    linked: Mapping[str, object],
):
    page_ref = str(page["page_ref"])
    expected_title = str(page["title"])
    action, observation = _rewind_page(
        context, sequence, page_ref, expected_title
    )
    titles: list[str] = []
    attachment_keys: list[tuple[object, ...]] = []
    attachment_records: list[dict[str, object]] = []
    previous = None
    while True:
        visible = [
            str(item["title"])
            for item in parse_internal_subpages(observation["root"])
        ]
        titles.extend(visible[_overlap(titles, visible):])
        candidates = list(
            parse_page_candidates(observation["root"])
        )
        keys = [candidate_key(item) for item in candidates]
        overlap = _overlap(attachment_keys, keys)
        for index in range(overlap, len(candidates)):
            current = list(
                parse_page_candidates(observation["root"])
            )
            if (
                index >= len(current)
                or candidate_key(current[index]) != keys[index]
            ):
                raise PageCollectionError(
                    "notion_attachment_candidate_changed",
                    page_ref=page_ref,
                    action=action,
                )
            attachment_ref = (
                f"attachment-{len(attachment_records) + 1:06d}"
            )
            try:
                action, observation, record = materialize_candidate(
                    context,
                    sequence,
                    candidate=current[index],
                    attachment_ref=attachment_ref,
                    parent_prefix=page_prefix,
                    linked=linked,
                    observation=observation,
                    entry_action=action,
                    verify_parent=lambda root: _page_context_visible(
                        root,
                        expected_title,
                    ),
                )
            except AttachmentCollectionError as error:
                raise PageCollectionError(
                    error.reason_code,
                    page_ref=page_ref,
                    action=error.action or action,
                ) from error
            attachment_records.append(record)
        attachment_keys.extend(keys[overlap:])
        signature = _page_signature(observation["root"])
        if signature == previous:
            return (
                tuple(titles),
                tuple(attachment_records),
                action,
            )
        previous = signature
        action, observation = _swipe_page_observe(
            context, sequence, page_ref, "up"
        )


def _find_internal_subpage(
    context,
    sequence,
    page_ref: str,
    child_index: int,
    expected_title: str,
    parent_title: str,
):
    action, observation = _rewind_page(
        context, sequence, page_ref, parent_title
    )
    titles: list[str] = []
    previous = None
    while True:
        visible = list(parse_internal_subpages(observation["root"]))
        visible_titles = [str(item["title"]) for item in visible]
        overlap = _overlap(titles, visible_titles)
        start = len(titles) - overlap
        if start <= child_index < start + len(visible):
            match = visible[child_index - start]
            if match["title"] != expected_title:
                break
            return match, action
        titles.extend(visible_titles[overlap:])
        signature = _page_signature(observation["root"])
        if signature == previous:
            break
        previous = signature
        action, observation = _swipe_page_observe(
            context, sequence, page_ref, "up"
        )
    raise PageCollectionError(
        "notion_subpage_row_unavailable",
        page_ref=page_ref,
        action=action,
    )


def _write_inventory(
    context,
    workspace_prefix: str,
    target_ref: str,
    workspace_ref: str,
    records,
    status: str,
    action,
):
    document = {
        "schema_version": "1.0",
        "record_kind": "notion_pages",
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "status": status,
        "discovered_page_count": len(records),
        "page_count": sum(
            item.get("classification") == "general_page"
            and item.get("acquisition_status") == "complete"
            for item in records
        ),
        "database_count": sum(
            item.get("classification") == "database"
            and item.get("acquisition_status") == "complete"
            for item in records
        ),
        "database_item_count": sum(
            int(item.get("item_count", 0))
            for item in records
            if item.get("classification") == "database"
        ),
        "database_out_of_scope_count": sum(
            item.get("classification") == "database"
            and item.get("acquisition_status") == "out_of_scope"
            for item in records
        ),
        "attachment_count": sum(
            int(item.get("attachment_count", 0))
            for item in records
        ),
        "attachment_materialized_count": sum(
            int(item.get("attachment_materialized_count", 0))
            for item in records
        ),
        "attachment_out_of_scope_count": sum(
            int(item.get("attachment_out_of_scope_count", 0))
            for item in records
        ),
        "pages": records,
    }
    artifact = write_json(
        context,
        f"{workspace_prefix}/pages.json",
        document,
        action,
        {
            "target_ref": target_ref,
            "workspace_ref": workspace_ref,
        },
    )
    return document, {
        "artifact_id": artifact.artifact_id,
        "path": artifact.relative_path,
    }


_RECORD_KEYS = (
    "page_ref",
    "notion_page_id",
    "title",
    "section",
    "parent_page_ref",
    "depth",
    "top_index",
    "child_index",
    "discovery_source",
)


def _page_record(page: Mapping[str, object]) -> dict[str, object]:
    return {key: page.get(key) for key in _RECORD_KEYS}


def _database_record(record, database):
    return {
        **record,
        "classification": "database",
        "acquisition_status": database["acquisition_status"],
        "reason_code": database["reason_code"],
        "view_ref": database["view_ref"],
        "view_name": database["view_name"],
        "view_layout": database["view_layout"],
        "discovered_item_count": database["discovered_item_count"],
        "item_count": database["item_count"],
        "attachment_count": database.get("attachment_count", 0),
        "attachment_materialized_count": database.get(
            "attachment_materialized_count",
            0,
        ),
        "attachment_out_of_scope_count": database.get(
            "attachment_out_of_scope_count",
            0,
        ),
        "source": database["source"],
    }


def _parse_open_page(page, observation, action):
    try:
        return parse_page(observation["root"], str(page["title"]))
    except NotionCollectorError as error:
        reason = (
            "notion_page_title_unverified"
            if str(error) == "Notion page title was not verified"
            else "notion_page_classification_unavailable"
        )
        raise PageCollectionError(
            reason,
            page_ref=str(page["page_ref"]),
            page_id=page.get("notion_page_id"),
            action=action,
        ) from error


def _normalize_open_page(
    context,
    sequence,
    page: Mapping[str, object],
    action,
    observation,
):
    expected_title = str(page["title"])
    if _page_top_visible(observation["root"], expected_title):
        return action, observation
    if _page_load_error_visible(observation["root"]):
        raise PageCollectionError(
            "notion_page_entry_unavailable",
            page_ref=str(page["page_ref"]),
            page_id=page.get("notion_page_id"),
            action=action,
        )
    if _page_loading_visible(observation["root"]):
        def loading_finished() -> bool:
            root = ET.fromstring(context.device.hierarchy())
            return not _page_loading_visible(root)

        if context.ui.wait_until(loading_finished):
            action, observation = _observe_page(
                context,
                sequence,
                str(page["page_ref"]),
                "notion_page_loading_complete",
            )
            if context.ui.wait_for(
                selector(context, "notion.home"), timeout=0
            ):
                raise PageCollectionError(
                    "notion_page_entry_unavailable",
                    page_ref=str(page["page_ref"]),
                    page_id=page.get("notion_page_id"),
                    action=action,
                )
            if _page_top_visible(observation["root"], expected_title):
                return action, observation
            if _page_load_error_visible(observation["root"]):
                raise PageCollectionError(
                    "notion_page_entry_unavailable",
                    page_ref=str(page["page_ref"]),
                    page_id=page.get("notion_page_id"),
                    action=action,
                )
        else:
            raise PageCollectionError(
                "notion_page_entry_unavailable",
                page_ref=str(page["page_ref"]),
                page_id=page.get("notion_page_id"),
                action=action,
            )
    return _rewind_page(
        context,
        sequence,
        str(page["page_ref"]),
        expected_title,
    )


def _collect_open_page(
    context,
    sequence,
    workspace_ref: str,
    workspace_prefix: str,
    target_ref: str,
    page: Mapping[str, object],
    inventory: dict[str, dict[str, object]],
    records: list[dict[str, object]],
    entry_action,
    observation,
):
    try:
        entry_action, observation = _normalize_open_page(
            context, sequence, page, entry_action, observation
        )
        parsed = _parse_open_page(page, observation, entry_action)
    except PageCollectionError as error:
        if error.page_id is None:
            error.page_id = page.get("notion_page_id")
        records.append({
            **_page_record(page),
            "acquisition_status": "partial",
            "reason_code": error.reason_code,
        })
        raise

    page_ref = str(page["page_ref"])
    prefix = page_prefix(
        f"{workspace_prefix}/pages", page_ref, inventory
    )
    record = _page_record(page)
    if parsed["classification"] == "database":
        try:
            last_action, database = collect_open_database(
                context,
                sequence,
                workspace_ref=workspace_ref,
                workspace_prefix=workspace_prefix,
                target_ref=target_ref,
                page=page,
                page_prefix=prefix,
                entry_action=entry_action,
                observation=observation,
            )
        except DatabaseCollectionError as error:
            if error.database is not None:
                records.append(_database_record(record, error.database))
            raise PageCollectionError(
                error.reason_code,
                page_ref=page_ref,
                page_id=page.get("notion_page_id"),
                action=error.action,
            ) from error
        record = _database_record(record, database)
        records.append(record)
        return last_action

    linked = {
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "page_ref": page_ref,
        "notion_page_id": page.get("notion_page_id"),
    }
    source = retain_pair(
        context,
        f"{prefix}/page",
        observation,
        entry_action,
        linked,
    )
    child_titles, attachments, last_action = _scan_page_content(
        context,
        sequence,
        page=page,
        page_prefix=prefix,
        linked=linked,
    )
    children = []
    for index, title in enumerate(child_titles, 1):
        child_ref = f"{page_ref}-subpage-{index:06d}"
        child = {
            "page_ref": child_ref,
            "notion_page_id": None,
            "title": title,
            "section": page["section"],
            "parent_page_ref": page_ref,
            "depth": int(page["depth"]) + 1,
            "top_index": None,
            "child_index": index,
            "discovery_source": "page_content",
        }
        inventory[child_ref] = child
        children.append(child)

    attachment_partial = any(
        item["acquisition_status"] == "not_materialized"
        for item in attachments
    )
    acquisition_status = (
        "partial" if attachment_partial else "complete"
    )
    reason_code = (
        "notion_attachment_materialize_partial"
        if attachment_partial
        else None
    )
    page_document = {
        "schema_version": "1.0",
        "record_kind": "notion_general_page",
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        **record,
        "rendered_text": parsed["rendered_text"],
        "immediate_subpage_count": len(children),
        "attachment_count": len(attachments),
        "attachment_materialized_count": sum(
            item["acquisition_status"] == "materialized"
            for item in attachments
        ),
        "attachment_out_of_scope_count": sum(
            item["acquisition_status"] == "out_of_scope"
            for item in attachments
        ),
        "attachments": attachments,
        "acquisition_status": acquisition_status,
        "reason_code": reason_code,
        "source": source,
    }
    write_json(
        context,
        f"{prefix}/page.json",
        page_document,
        entry_action,
        {**linked, "observation_id": source["observation_id"]},
    )
    record.update({
        "classification": "general_page",
        "acquisition_status": acquisition_status,
        "reason_code": reason_code,
        "immediate_subpage_count": len(children),
        "attachment_count": page_document["attachment_count"],
        "attachment_materialized_count": page_document[
            "attachment_materialized_count"
        ],
        "attachment_out_of_scope_count": page_document[
            "attachment_out_of_scope_count"
        ],
        "source": source,
    })
    records.append(record)

    for child in children:
        child_ref = str(child["page_ref"])
        try:
            row, last_action = _find_internal_subpage(
                context,
                sequence,
                page_ref,
                int(child["child_index"]) - 1,
                str(child["title"]),
                str(page["title"]),
            )
        except PageCollectionError as error:
            error.page_ref = child_ref
            raise
        last_action = context.ui.click_bounds(
            _action_name(sequence, "notion_open_subpage"),
            row["bounds"],
            context={
                "workspace_ref": workspace_ref,
                "page_ref": child_ref,
                "parent_page_ref": page_ref,
            },
        )
        child_observation = observe(
            context,
            _action_name(sequence, "observation_notion_subpage"),
            last_action,
        )
        try:
            last_action = _collect_open_page(
                context,
                sequence,
                workspace_ref,
                workspace_prefix,
                target_ref,
                child,
                inventory,
                records,
                last_action,
                child_observation,
            )
        finally:
            try:
                last_action = context.ui.back(
                    _action_name(sequence, "notion_close_subpage"),
                    context={
                        "workspace_ref": workspace_ref,
                        "page_ref": child_ref,
                        "parent_page_ref": page_ref,
                    },
                )
                verify_action, parent_observation = _observe_page(
                    context,
                    sequence,
                    page_ref,
                    "notion_verify_parent_page",
                )
                last_action, parent_observation = _normalize_open_page(
                    context,
                    sequence,
                    page,
                    verify_action,
                    parent_observation,
                )
                _parse_open_page(page, parent_observation, last_action)
            except Exception as error:
                raise PageCollectionError(
                    "notion_subpage_close_failed",
                    page_ref=child_ref,
                    action=last_action,
                ) from error
    return last_action


def _collect_carousel_pages(
    context,
    sequence,
    workspace_ref: str,
    workspace_prefix: str,
    target_ref: str,
    inventory: dict[str, dict[str, object]],
    records: list[dict[str, object]],
    last_action,
):
    cards, source, last_action = _discover_carousel_cards(
        context, sequence, workspace_ref
    )
    if not cards or source is None:
        return last_action

    kind = "recent" if source == "recents" else "offline"
    targets = []
    for card in cards:
        title = str(card["title"])
        page_ref = f'{kind}-page-{int(card["carousel_index"]):06d}'
        page = {
            "page_ref": page_ref,
            "notion_page_id": None,
            "title": title,
            "section": source,
            "parent_page_ref": None,
            "depth": 0,
            "top_index": None,
            "child_index": None,
            "discovery_source": source,
        }
        inventory[page_ref] = page
        targets.append((card, page))

    for target_index, (card, page) in enumerate(targets):
        page_ref = str(page["page_ref"])
        item_attempt(context, {"target_ref": target_ref, "workspace_ref": workspace_ref, "page_ref": page_ref}, retention=False)
        page_id = page.get("notion_page_id")
        entry_action = last_action
        try:
            row, entry_action = _find_carousel_card(
                context,
                sequence,
                workspace_ref,
                int(card["carousel_index"]),
                str(page["title"]),
                source,
            )
            if row is None:
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": f"notion_{kind}_page_row_unavailable",
                })
                continue
            entry_action = context.ui.click_bounds(
                _action_name(sequence, f"notion_open_{kind}_page"),
                row["bounds"],
                context={
                    "workspace_ref": workspace_ref,
                    "page_ref": page_ref,
                    "notion_page_id": page_id,
                    "carousel_index": card["carousel_index"],
                    "discovery_source": source,
                },
            )
            observation = observe(
                context,
                _action_name(
                    sequence, f"observation_notion_{kind}_page"
                ),
                entry_action,
            )
            if context.ui.wait_for(
                selector(context, "notion.home"), timeout=0
            ):
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": "notion_page_entry_unavailable",
                })
                continue
            last_action = _collect_open_page(
                context,
                sequence,
                workspace_ref,
                workspace_prefix,
                target_ref,
                page,
                inventory,
                records,
                entry_action,
                observation,
            )
        except PageCollectionError as error:
            processed = {item["page_ref"] for item in records}
            if page_ref not in processed:
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": error.reason_code,
                })
                processed.add(page_ref)
            if error.reason_code == "notion_page_entry_unavailable":
                last_action = error.action or entry_action
                continue
            records.extend(
                {
                    **_page_record(pending_page),
                    "acquisition_status": "not_attempted",
                }
                for _, pending_page in targets[target_index + 1:]
                if pending_page["page_ref"] not in processed
            )
            error.page_ref = error.page_ref or page_ref
            error.page_id = error.page_id or page_id
            error.action = error.action or entry_action
            raise
        finally:
            if not context.ui.wait_for(
                selector(context, "notion.home"), timeout=0
            ):
                try:
                    last_action = context.ui.back(
                        _action_name(
                            sequence, f"notion_close_{kind}_page"
                        ),
                        expected=selector(context, "notion.home"),
                        context={
                            "workspace_ref": workspace_ref,
                            "page_ref": page_ref,
                            "notion_page_id": page_id,
                            "carousel_index": card["carousel_index"],
                            "discovery_source": source,
                        },
                    )
                except Exception as error:
                    raise PageCollectionError(
                        f"notion_{kind}_page_close_failed",
                        page_ref=page_ref,
                        page_id=page_id,
                        action=entry_action,
                    ) from error
    return last_action


def collect_workspace_pages(
    context,
    workspace: Mapping[str, object],
    workspace_prefix: str,
    target_ref: str,
) -> dict[str, object]:
    workspace_ref = str(workspace["workspace_ref"])
    sequence = count(1)
    records: list[dict[str, object]] = []
    try:
        inventory, last_action = _discover_top_level_pages(
            context, sequence, workspace_ref
        )
    except PageCollectionError:
        raise
    except Exception as error:
        raise PageCollectionError(
            "notion_page_inventory_unavailable"
        ) from error

    for page in tuple(inventory.values()):
        page_ref = str(page["page_ref"])
        item_attempt(context, {"target_ref": target_ref, "workspace_ref": workspace_ref, "page_ref": page_ref}, retention=False)
        page_id = str(page["notion_page_id"])
        entry_action = last_action
        try:
            row, entry_action = _find_home_item(
                context,
                sequence,
                workspace_ref,
                page_id=page_id,
            )
            if row is None:
                raise PageCollectionError(
                    "notion_page_row_unavailable",
                    page_ref=page_ref,
                    page_id=page_id,
                    action=entry_action,
                )
            entry_action = context.ui.click_bounds(
                _action_name(sequence, "notion_open_page"),
                row["bounds"],
                context={
                    "workspace_ref": workspace_ref,
                    "page_ref": page_ref,
                    "notion_page_id": page_id,
                },
            )
            observation = observe(
                context,
                _action_name(sequence, "observation_notion_page"),
                entry_action,
            )
            if context.ui.wait_for(
                selector(context, "notion.home"), timeout=0
            ):
                recent_bounds = _recent_page_bounds(
                    observation["root"], str(page["title"])
                )
                if recent_bounds is None:
                    raise PageCollectionError(
                        "notion_page_entry_unavailable",
                        page_ref=page_ref,
                        page_id=page_id,
                        action=entry_action,
                    )
                entry_action = context.ui.click_bounds(
                    _action_name(sequence, "notion_open_recent_page"),
                    recent_bounds,
                    context={
                        "workspace_ref": workspace_ref,
                        "page_ref": page_ref,
                        "notion_page_id": page_id,
                    },
                )
                observation = observe(
                    context,
                    _action_name(
                        sequence, "observation_notion_recent_page"
                    ),
                    entry_action,
                )
                if context.ui.wait_for(
                    selector(context, "notion.home"), timeout=0
                ):
                    raise PageCollectionError(
                        "notion_page_entry_unavailable",
                        page_ref=page_ref,
                        page_id=page_id,
                        action=entry_action,
                    )
            last_action = _collect_open_page(
                context,
                sequence,
                workspace_ref,
                workspace_prefix,
                target_ref,
                page,
                inventory,
                records,
                entry_action,
                observation,
            )
        except PageCollectionError as error:
            processed = {item["page_ref"] for item in records}
            if page_ref not in processed:
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": error.reason_code,
                })
                processed.add(page_ref)
            if error.reason_code == "notion_page_entry_unavailable":
                last_action = error.action or entry_action
                continue
            records.extend(
                {
                    **_page_record(pending),
                    "acquisition_status": "not_attempted",
                }
                for pending in inventory.values()
                if pending["page_ref"] not in processed
            )
            document, source = _write_inventory(
                context,
                workspace_prefix,
                target_ref,
                workspace_ref,
                records,
                "partial",
                error.action or entry_action,
            )
            error.action = error.action or entry_action
            error.source = source
            error.report = document
            raise
        finally:
            if not context.ui.wait_for(
                selector(context, "notion.home"), timeout=0
            ):
                try:
                    last_action = context.ui.back(
                        _action_name(sequence, "notion_close_page"),
                        expected=selector(context, "notion.home"),
                        context={
                            "workspace_ref": workspace_ref,
                            "page_ref": page_ref,
                            "notion_page_id": page_id,
                        },
                    )
                except Exception as error:
                    raise PageCollectionError(
                        "notion_page_close_failed",
                        page_ref=page_ref,
                        page_id=page_id,
                        action=entry_action,
                    ) from error

    try:
        last_action = _collect_carousel_pages(
            context,
            sequence,
            workspace_ref,
            workspace_prefix,
            target_ref,
            inventory,
            records,
            last_action,
        )
    except PageCollectionError as error:
        document, source = _write_inventory(
            context,
            workspace_prefix,
            target_ref,
            workspace_ref,
            records,
            "partial",
            error.action or last_action,
        )
        error.action = error.action or last_action
        error.source = source
        error.report = document
        raise

    partial_reason = next(
        (
            item.get("reason_code")
            for item in records
            if item.get("acquisition_status") == "partial"
            and item.get("reason_code")
        ),
        None,
    )
    document, source = _write_inventory(
        context,
        workspace_prefix,
        target_ref,
        workspace_ref,
        records,
        "partial" if partial_reason else "complete",
        last_action,
    )
    return {
        "status": "partial" if partial_reason else "complete",
        "reason_code": partial_reason,
        "source": source,
        "discovered_page_count": document["discovered_page_count"],
        "page_count": document["page_count"],
        "database_count": document["database_count"],
        "database_item_count": document["database_item_count"],
        "database_out_of_scope_count": (
            document["database_out_of_scope_count"]
        ),
        "attachment_count": document["attachment_count"],
        "attachment_materialized_count": (
            document["attachment_materialized_count"]
        ),
        "attachment_out_of_scope_count": (
            document["attachment_out_of_scope_count"]
        ),
        "_action": last_action,
    }
