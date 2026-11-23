"""Notion 0.6.4030 general-page parsing."""

from __future__ import annotations
from ...collection import (
    seek_window,
    overlap_candidates,
    visit_scope,
    display_text,
    walk_hierarchy,
    iter_windows,
    display_pattern,
    content_signature,
)


from ...parsing import read_rows, parsing_spec


import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from hashlib import sha256
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
    output_action,
    page_actions_bounds,
    page_buttons,
    database_control_labels,
    is_page_document,
    bounds,
    dismiss_page_keyboard,
    page_keyboard_visible,
    observe,
    onscreen,
    retain_pair,
    safe_component,
    selector,
    viewport_bounds,
    write_json,
)


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
    root: ET.Element, profile,
) -> tuple[dict[str, object], ...]:
    result = []
    for node in root.iter():
        if not onscreen(node, root):
            continue
        match = display_pattern(profile, 'section_header', flags=re.IGNORECASE).fullmatch(node.get("resource-id", ""))
        if match is None:
            continue
        result.append({
            "section": _section(match.group(1)),
            "bounds": bounds(node),
        })
    result.sort(key=lambda item: item["bounds"][1])
    return tuple(result)


def parse_home_rows(
    root: ET.Element, profile,
) -> tuple[dict[str, object], ...]:
    result = []
    for row in root.iter():
        if not onscreen(row, root):
            continue
        match = display_pattern(profile, 'page_row', flags=re.IGNORECASE).fullmatch(row.get("resource-id", ""))
        if match is None:
            continue
        section = _section(match.group(1))
        expanders = [
            node
            for node in row.iter()
            if onscreen(node, root)
            and node.get("resource-id")
            == display_text(profile, 'page_expander').replace("{section}", match.group(1).lower())
        ]
        if len(expanders) != 1:
            raise NotionCollectorError("Notion page row is ambiguous")
        _, expander_top, _, expander_bottom = bounds(expanders[0])
        titles = [
            values['title']
            for values, node in read_rows(row, parsing_spec(profile, 'page_titles'), ui_fields=profile.parameters.get('ui_fields', {}))
            if onscreen(node, root)
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


def _document_title_visible(
    root: ET.Element,
    expected_title: str,
) -> bool:
    for node in root.iter():
        if not is_page_document(node, root):
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
    profile,
) -> dict[str, object]:
    if _page_loading_visible(root) or _page_load_error_visible(root):
        raise NotionCollectorError("notion_page_entry_unavailable")
    if not page_buttons(root, "Back"):
        raise NotionCollectorError("Notion page header is unavailable")
    titles = [
        node
        for node in root.iter()
        if onscreen(node, root)
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
        if is_page_document(node, root)
    ]
    if len(documents) == 1:
        if not _document_title_visible(root, expected_title):
            raise NotionCollectorError("Notion page document title was not verified")
        return {
            "classification": "general_page",
            "rendered_text": documents[0],
        }
    controls = {
        node.get("content-desc", "").strip()
        or node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node, root)
        and node.get("class") == "android.widget.Button"
    }
    block_editors = sorted(
        (
            bounds(node)[1],
            bounds(node)[0],
            node.get("text", ""),
        )
        for node in root.iter()
        if onscreen(node, root)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
    )
    if not documents and database_control_labels(profile) <= controls:
        return {"classification": "database"}
    if len(titles) == 1 and not documents:
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
    content_bottom = min(obstruction_tops, default=viewport_bounds(root)[3])
    result = []
    for node in root.iter():
        title = node.get("content-desc", "").strip()
        if (
            not onscreen(node, root)
            or node.get("class") != "android.view.View"
            or node.get("clickable") != "true"
            or node.get("enabled") == "false"
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
            and (child.get("class") == "android.widget.Image"
                 or (child.get("class") == "android.widget.Button"
                     and child.get("clickable") == "true"))
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


def _home_signature(root: ET.Element, profile) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            node.get("resource-id", ""),
            node.get("text", ""),
        )
        for node in root.iter()
        if node.get("package") == PACKAGE
        and (
            display_pattern(profile, 'page_row', flags=re.IGNORECASE).fullmatch(node.get("resource-id", ""))
            or display_pattern(profile, 'section_header', flags=re.IGNORECASE).fullmatch(node.get("resource-id", ""))
            or node.get("resource-id", "").startswith("home-tab.")
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


def _rewind_home(context, sequence, workspace_ref: str, *, entry=None):
    if entry is not None and not context.ui.wait_for(selector(context, "notion.home"), timeout=0):
        raise PageCollectionError("notion_home_unavailable")
    current = entry if entry is not None else _observe_home(context, sequence, workspace_ref, 'notion_page_home_scan_start')
    def advance():
        nonlocal current
        current = _swipe_observe(context, sequence, workspace_ref, 'down')
    _, last = seek_window(lambda: current, advance,
        lambda value: _home_signature(value[1]['root'], profile=context.app_profile),
        lambda value: None, adjacent=True)
    return last


def _scan_home(context, sequence, workspace_ref: str, *, entry=None):
    action, observation = _rewind_home(
        context, sequence, workspace_ref, entry=entry
    )
    inventory: dict[str, dict[str, object]] = {}
    stacks: dict[str, list[tuple[int, str]]] = {}
    headers: dict[str, tuple[int, int, int, int]] = {}
    def advance():
        nonlocal action, observation
        action, observation = _swipe_observe(context, sequence, workspace_ref, "up")
    for observation in iter_windows(lambda: observation, advance,
            lambda value: _home_signature(value["root"], profile=context.app_profile), include_boundary=True, adjacent=True):
        root = observation["root"]
        inventory, stacks = merge_rows(
            inventory,
            stacks,
            parse_home_rows(root, profile=context.app_profile),
        )
        headers.update({
            str(item["section"]): item["bounds"]
            for item in parse_section_headers(root, profile=context.app_profile)
        })
    return inventory, headers, action


def _find_home_item(context, sequence, workspace_ref: str, *, page_id: str | None = None, section: str | None = None):
    current = _observe_home(context, sequence, workspace_ref, 'notion_page_home_find')
    def advance():
        nonlocal current
        current = _swipe_observe(context, sequence, workspace_ref, 'up')
    def match(value):
        root = value[1]['root']
        items = (parse_home_rows(root, profile=context.app_profile) if page_id is not None
                 else parse_section_headers(root, profile=context.app_profile))
        key, expected = ('page_id', page_id) if page_id is not None else ('section', section)
        return next((item for item in items if item[key] == expected), None)
    found = match(current)
    if found is not None:
        return found, current[0]
    current = _rewind_home(context, sequence, workspace_ref, entry=current)
    found, last = seek_window(lambda: current, advance,
        lambda value: _home_signature(value[1]['root'], profile=context.app_profile), match, adjacent=True)
    return found, last[0]


def _recent_header(root: ET.Element) -> ET.Element | None:
    headings = [
        node
        for node in root.iter()
        if onscreen(node, root)
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
            not onscreen(node, root)
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
) -> tuple[str, ...]:
    return tuple(
        str(item["title"])
        for item in _carousel_cards(root)
    )


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
    current = observe(
        context,
        _action_name(sequence, "observation_notion_page_carousel"),
        action,
    )
    if not context.ui.wait_for(selector(context, "notion.home"), timeout=0):
        action = context.ui.back(
            _action_name(sequence, "notion_restore_home_after_carousel"),
            expected=selector(context, "notion.home"),
            context={"workspace_ref": workspace_ref},
        )
        current = observe(context, _action_name(sequence, "observation_notion_carousel_home_restored"), action)
        if _carousel_source(current['root']) != _carousel_source(observation['root']):
            raise PageCollectionError('notion_page_carousel_unavailable')
    return action, current


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
    while True:
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
        visible = list(signature)
        titles.extend(visible[_overlap(titles, visible):])
        action, observation = _swipe_carousel_observe(
            context,
            sequence,
            workspace_ref,
            observation,
            "left",
        )
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
    target = carousel_index - 1
    def advance():
        nonlocal action, observation
        action, observation = _swipe_carousel_observe(context, sequence, workspace_ref, observation, 'left')
    for observation in iter_windows(lambda: observation, advance,
            lambda value: None if _carousel_source(value['root']) != expected_source else _carousel_signature(value['root'])):
        if _carousel_source(observation["root"]) != expected_source:
            return None, action
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
    return None, action


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


def _discover_top_level_pages(context, sequence, workspace_ref: str, *, entry=None):
    inventory: dict[str, dict[str, object]] = {}
    current, headers, action = _scan_home(
        context, sequence, workspace_ref, entry=entry
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
        observation = observe(
            context,
            _action_name(sequence, "observation_notion_page_section"),
            action,
        )
        current, _, action = _scan_home(
            context, sequence, workspace_ref, entry=(action, observation)
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


def _page_signature(observation, profile):
    """Compare accessible page content; screenshots are retained as evidence only."""
    root = observation["root"]
    if page_keyboard_visible(root, profile):
        raise PageCollectionError("notion_page_keyboard_visible")
    return content_signature(ET.tostring(root), PACKAGE)


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
    *, distance_ratio: float | None = None,
):
    action = context.ui.swipe(
        _action_name(sequence, f"notion_page_swipe_{direction}"),
        direction,
        distance_ratio=(distance_ratio if distance_ratio is not None else
                        context.app_profile.parameters["page_scroll_ratio"]
                        if direction == "up" else None),
        expect_change=False,
        context={"page_ref": page_ref},
    )
    return action, observe(
        context,
        _action_name(sequence, "observation_notion_page_scroll"),
        action,
    )


def _page_title_nodes(root: ET.Element, expected_title: str) -> tuple[ET.Element, ...]:
    return tuple(
        node
        for node in root.iter()
        if (
            onscreen(node, root)
            and node.get("class") == "android.widget.EditText"
            and node.get("clickable") == "true"
            and node.get("text", "").strip() == expected_title
        )
    )


def _page_title_visible(root: ET.Element, expected_title: str) -> bool:
    return _document_title_visible(root, expected_title) or any(
        onscreen(node, root)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
        for node in root.iter()
    )


def _page_loading_visible(root: ET.Element) -> bool:
    return any(
        onscreen(node, root) and node.get("content-desc", "").strip() == "Loading"
        for node in root.iter()
    )


def _page_load_error_visible(root: ET.Element) -> bool:
    texts = {
        node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node, root) and node.get("class") == "android.widget.TextView"
    }
    return {
        "Oops, there was an error loading this page.",
        "Refresh to load it again.",
    } <= texts


def _page_top_visible(root: ET.Element, expected_title: str) -> bool:
    # Aggregate accessibility text can contain the title while scrolled midway.
    return len(_page_title_nodes(root, expected_title)) == 1


def _retain_page_navigation(context, page_ref, title, action, observation, linked=None):
    retain_pair(context, f"notion/diagnostics/navigation-{action.action_id}-{len(context.artifacts.records)}",
                observation, action, {**dict(linked or action.context), "page_ref": page_ref})
    if not _page_context_visible(observation["root"], title):
        raise PageCollectionError("notion_page_context_unverified", page_ref=page_ref, action=action)


def _rewind_page(
    context,
    sequence,
    page_ref: str,
    expected_title: str,
    *, entry=None, linked=None,
):
    action, observation = entry if entry is not None else _observe_page(
        context, sequence, page_ref, "notion_page_scan_start"
    )
    try:
        action, observation = dismiss_page_keyboard(context, sequence, action, observation,
            linked=dict(action.context), verify_parent=lambda root: _page_context_visible(root, expected_title))
    except NotionCollectorError as error:
        raise PageCollectionError(str(error), page_ref=page_ref, action=action) from error
    if _page_top_visible(observation["root"], expected_title):
        return action, observation
    _retain_page_navigation(context, page_ref, expected_title, action, observation, linked)
    signature = _page_signature(observation, context.app_profile)
    for _ in range(context.app_profile.parameters["page_max_windows"]):
        action, current = _swipe_page_observe(
            context, sequence, page_ref, "down"
        )
        _retain_page_navigation(context, page_ref, expected_title, action, current, linked)
        current_signature = _page_signature(current, context.app_profile)
        observation = current
        if _page_top_visible(current["root"], expected_title):
            return action, observation
        if current_signature == signature:
            if _document_title_visible(current["root"], expected_title):
                return action, observation
            raise PageCollectionError(
                "notion_page_title_unverified"
                if any(is_page_document(node, current["root"]) for node in current["root"].iter())
                else "notion_page_top_unavailable",
                page_ref=page_ref,
                action=action,
            )
        signature = current_signature
    raise PageCollectionError("notion_page_rewind_limit", page_ref=page_ref, action=action)


def _overlap(previous: Sequence[str], current: Sequence[str]) -> int:
    return max(overlap_candidates(previous, current), default=0)


def _page_context_visible(
    root: ET.Element,
    expected_title: str,
) -> bool:
    return _page_title_visible(root, expected_title) or any(
        onscreen(node, root)
        and node.get("class") == "android.widget.Button"
        and (
            node.get("text", "").strip() == expected_title
            or node.get("text", "").strip().startswith(
                f"{expected_title} "
            )
        )
        for node in root.iter()
    )


def _page_object_content(root):
    previews = []
    for node in root.iter():
        if (onscreen(node, root) and node.get("class") == "android.view.View"
                and node.get("clickable") == "true" and node.get("content-desc")):
            labels = [child.get("text", "") for child in node.iter()
                      if onscreen(child, root) and child.get("class") == "android.widget.TextView"
                      and child.get("text")]
            if any(re.fullmatch(r"https?://\S+", text) for text in labels):
                previews.append({"description": node.get("content-desc"),
                                 "visible_text": labels, "bounds": bounds(node)})
    return {
        "subpages": list(parse_internal_subpages(root)),
        "attachments": list(parse_page_candidates(root)),
        "link_previews": previews,
    }


def _content_window(root, expected_title, source, *, unchanged, text_source=None):
    documents = [node for node in root.iter() if is_page_document(node, root)]
    editors = sorted(
        (node for node in root.iter() if onscreen(node, root)
         and node.get("class") == "android.widget.EditText"
         and node.get("clickable") == "true" and node.get("text", "").strip()),
        key=lambda node: (bounds(node)[1], bounds(node)[0]),
    )
    non_editor_content = _page_object_content(root)
    # Notion's aggregate accessibility text can change as blocks leave the
    # viewport. Prefer identified editors; preserve aggregates as raw evidence.
    if text_source is None:
        body_editors = [node for node in editors if node.get("text", "").strip() != expected_title]
        text_source = "accessibility_document" if documents and not body_editors else "editable_blocks"
    use_document = text_source == "accessibility_document"
    document_matches_editors = len(documents) == 1
    if use_document and document_matches_editors:
        cursor = 0
        for editor in editors:
            value = editor.get("text", "")
            start = documents[0].get("text", "").find(value, cursor)
            if start < 0:
                document_matches_editors = False
                break
            cursor = start + len(value)
    verified = _page_context_visible(root, expected_title) and not (
        _page_loading_visible(root) or _page_load_error_visible(root)
    ) and (
        (len(documents) == 1 and _document_title_visible(root, expected_title)
         and (not use_document or document_matches_editors))
        or (not use_document and not documents
            and (bool(editors) or any(non_editor_content.values())))
    )
    text = documents[0].get("text", "") if use_document and documents else "\n".join(
        node.get("text", "") for node in editors
    )
    blocks = (
        [{"resource_id": "", "text": line} for line in text.split("\n")]
        if use_document else [
            {"resource_id": node.get("resource-id", ""), "text": node.get("text", "")}
            for node in editors
        ]
    )
    back_controls = page_buttons(root, "Back")
    top_clipped = (len(documents) == len(back_controls) == 1
                   and not _page_top_visible(root, expected_title)
                   and bounds(documents[0])[1] < bounds(back_controls[0])[3])
    return {"rendered_text": text, "blocks": blocks, "source": source,
            "unchanged": unchanged, "text_verified": verified,
            "text_source": "accessibility_document" if use_document else "editable_blocks",
            "document_top_clipped": top_clipped,
            "accessibility_documents": [node.get("text", "") for node in documents],
            "non_editor_content": non_editor_content}


def _merge_content_windows(windows, expected_title):
    """Only join overlapping stable block identities, never deduplicate prose."""
    if any(window.get("text_source") == "accessibility_document" for window in windows):
        # A document snapshot is already one text value; do not splice its lines.
        text = windows[0]["rendered_text"]
        valid = True
        suffix_offset = 0
        for window in windows:
            if (window.get("text_source") != "accessibility_document"
                    or not window["text_verified"]):
                valid = False
                break
            current = window["rendered_text"]
            if not suffix_offset and current.startswith(text):
                text = current
                continue
            # Keep one whole observed snapshot, never join anonymous fragments.
            # Notion can drop offscreen leading blocks after reaching that full
            # snapshot. Accept only a clipped, forward-moving, exact suffix.
            header = re.match(r"[^\n]*\n*", text)[0]
            body, tail = text[len(header):], current[len(header):]
            offset = len(body) - len(tail)
            if (not window.get("document_top_clipped") or not tail
                    or re.match(r"[^\n]*\n*", current)[0] != header
                    or offset <= 0 or offset < suffix_offset
                    or not body.endswith(tail) or body[offset - 1] != "\n"):
                valid = False
                break
            suffix_offset = offset
        return text, None if valid else "notion_page_text_continuity_unverified"
    merged = list(windows[0]["blocks"])
    reason = None
    if not windows[0]["text_verified"]:
        reason = "notion_page_text_unavailable"
    for window in windows[1:]:
        if not window["text_verified"]:
            reason = reason or "notion_page_text_unavailable"
        if reason or window["unchanged"]:
            continue
        current = list(window["blocks"])
        if not current and any(window.get("non_editor_content", {}).values()):
            # These screens have content but no editable text to join. Keep the
            # separate links/media and their full screen/tree in this window.
            continue
        overlap = _overlap(merged, current)
        if (not overlap and merged and current
                and current[0] == merged[0] and current[0]["resource_id"]
                and current[0]["text"].strip() == expected_title):
            current = current[1:]
            overlap = _overlap(merged, current)
        previous_ids = Counter(block["resource_id"] for block in merged)
        current_ids = [block["resource_id"] for block in current]
        current_counts = Counter(current_ids)
        # ponytail: absent/reused accessibility IDs leave separate windows;
        # add another continuity rule only with independently verified evidence.
        if (not overlap or not all(current_ids[:overlap])
                or (overlap == 1 and current[0] == merged[0]
                    and current[0]["text"].strip() == expected_title)
                or any(previous_ids[ref] != 1 or current_counts[ref] != 1
                       for ref in current_ids[:overlap])
                or any(ref and ref in previous_ids for ref in current_ids[overlap:])):
            reason = "notion_page_text_continuity_unverified"
            continue
        merged.extend(current[overlap:])
    return "\n".join(block["text"] for block in merged), reason


def _empty_page_body_visible(root, title):
    if (_page_loading_visible(root) or _page_load_error_visible(root)
            or not _page_top_visible(root, title)):
        return False
    editors = [node for node in root.iter() if onscreen(node, root)
               and node.get("class") == "android.widget.EditText" and node.get("text", "").strip()]
    return (len(editors) == 1 and editors[0] in _page_title_nodes(root, title)
            and not any(_page_object_content(root).values()))


def _copy_page_contents(context, sequence, page, prefix, linked, action, observation):
    """Keep the app's copied text intact; XML/screens establish its UI source."""
    page_ref, title = str(page["page_ref"]), str(page["title"])
    copy_label = display_text(context.app_profile, "copy_page_contents")
    confirmation = display_text(context.app_profile, "copied_page_contents")
    empty_before = _empty_page_body_visible(observation["root"], title)
    empty_clipboard_basis = None

    def copy_control(root):
        matches = [node for node in root.iter() if onscreen(node, root)
                   and node.get("class") == "android.view.MenuItem"
                   and node.get("text") == copy_label
                   and node.get("enabled") == node.get("clickable") == "true"]
        if len(matches) != 1 or not _page_context_visible(root, title):
            raise NotionCollectorError("notion_page_copy_action_unavailable")
        return bounds(matches[0])

    def copied(root):
        if not _page_context_visible(root, title) or not any(
            onscreen(node, root) and node.get("text") == confirmation
            for node in root.iter()
        ):
            raise NotionCollectorError("notion_page_copy_confirmation_unavailable")

    try:
        if page_keyboard_visible(observation["root"], context.app_profile):
            raise NotionCollectorError("notion_page_copy_keyboard_visible")
        before = retain_pair(context, f"{prefix}/copy/page-before", observation, action, linked)
        action = context.ui.click_bounds(_action_name(sequence, "notion_open_page_actions"),
                                        page_actions_bounds(observation["root"]), context=dict(linked))
        menu_before = observe(context, _action_name(sequence, "notion_copy_menu_open"), action)
        retain_pair(context, f"{prefix}/copy/menu-before-refresh", menu_before, action, linked)
        # The sheet can expose the previous page or stale menu bounds in XML.
        # Refresh first, then require the Copy control before clicking it.
        action = context.ui.swipe(_action_name(sequence, "notion_refresh_copy_menu"), "up",
            distance_ratio=context.app_profile.parameters["copy_menu_refresh_ratio"], context=dict(linked))
        menu = observe(context, _action_name(sequence, "notion_copy_menu_ready"), action, copy_control)
        menu_source = retain_pair(context, f"{prefix}/copy/menu", menu, action, linked)
        action = context.ui.click(_action_name(sequence, "notion_copy_page_contents"),
            {"text": copy_label, "className": "android.view.MenuItem", "packageName": PACKAGE},
            context=dict(linked))
        observation = observe(context, _action_name(sequence, "notion_page_copied"), action, copied)
        source = retain_pair(context, f"{prefix}/copy/confirmed", observation, action, linked)
        try:
            text = context.device.read_clipboard_text()
        except Exception as error:
            raise NotionCollectorError("notion_page_clipboard_unavailable") from error
        if text is None and empty_before and _empty_page_body_visible(observation["root"], title):
            text = ""
            empty_clipboard_basis = "copy_confirmed_empty_body_xml_before_and_after"
        if not isinstance(text, str):
            raise NotionCollectorError("notion_page_clipboard_unavailable")
    except NotionCollectorError as error:
        raise PageCollectionError(str(error), page_ref=page_ref, action=action) from error

    payload = text.encode("utf-8")
    read_action = context.journal.record_action(_action_name(sequence, "notion_read_copied_text"),
        status="success", context=dict(linked), details={"characters": len(text),
            "bytes": len(payload), "sha256": sha256(payload).hexdigest(),
            "method": "native_copy_page_contents", "clipboard_restored": False,
            "empty_clipboard_basis": empty_clipboard_basis})
    artifact_context = {**linked, "observation_id": source["observation_id"]}
    attempt = item_attempt(context, artifact_context, prefix=prefix)
    retained = output_action(context, attempt, read_action, artifact_context)
    artifact = context.artifacts.write_bytes(f"{prefix}/copy/clipboard.txt", payload,
        kind="ui_record", attempt_id=attempt, action=retained,
        observation_id=source["observation_id"], context=artifact_context)
    return {"text": text, "artifact_id": artifact.artifact_id,
            "artifact_path": artifact.relative_path, "sha256": artifact.sha256,
            "format": "notion_clipboard_text", "page_title_included": False,
            "empty_clipboard_basis": empty_clipboard_basis,
            "clipboard_restored": False, "page_source": before,
            "menu_source": menu_source, "source": source}, action, observation


def _scan_page_content(
    context,
    sequence,
    *,
    page: Mapping[str, object],
    page_prefix: str,
    linked: Mapping[str, object],
    content_windows: list[dict[str, object]] | None = None,
    collect_text: bool = True,
    entry=None,
):
    page_ref = str(page["page_ref"])
    expected_title = str(page["title"])
    action, observation = (_rewind_page(context, sequence, page_ref, expected_title,
                            entry=entry, linked=linked) if collect_text or entry is None else entry)
    titles: list[str] = []
    attachment_keys: list[tuple[object, ...]] = []
    attachment_records: list[dict[str, object]] = []
    previous = None
    windows_seen = 0
    def window_record(observed, source, unchanged):
        if collect_text:
            return _content_window(observed["root"], expected_title, source,
                unchanged=unchanged,
                text_source=content_windows[0]["text_source"] if content_windows else None)
        return {"source": source, "unchanged": unchanged, "scope": "object_discovery",
                "non_editor_content": _page_object_content(observed["root"])}
    def advance():
        nonlocal action, observation, windows_seen
        windows_seen += 1
        if windows_seen >= context.app_profile.parameters['page_max_windows']:
            raise PageCollectionError('notion_page_boundary_unverified', page_ref=page_ref, action=action)
        action, observation = _swipe_page_observe(context, sequence, page_ref, "up", **(
            {"distance_ratio": context.app_profile.parameters["page_discovery_scroll_ratio"]}
            if not collect_text else {}))
    for _ in iter_windows(lambda: observation, advance, lambda _: _page_signature(observation, context.app_profile),
                          include_boundary=True, adjacent=True,
                          boundary_repeats=context.app_profile.parameters['page_boundary_repeats']):
        observed_signature = _page_signature(observation, context.app_profile)
        if content_windows is not None:
            source = retain_pair(
                context,
                (f"{page_prefix}/page" if not content_windows else
                 f"{page_prefix}/content/window-{len(content_windows) + 1:06d}"),
                observation, action, linked,
            )
            content_windows.append({
                "ordinal": len(content_windows) + 1,
                **window_record(observation, source, observed_signature == previous),
            })
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
            if content_windows is not None:
                restored_signature = _page_signature(observation, context.app_profile)
                source = retain_pair(
                    context, f"{page_prefix}/content/window-{len(content_windows) + 1:06d}",
                    observation, action, linked,
                )
                content_windows.append({
                    "ordinal": len(content_windows) + 1,
                    **window_record(observation, source, restored_signature == observed_signature),
                })
                observed_signature = restored_signature
        attachment_keys.extend(keys[overlap:])
        previous = observed_signature
    return tuple(titles), tuple(attachment_records), action


def _find_internal_subpage(
    context,
    sequence,
    page_ref: str,
    child_index: int,
    expected_title: str,
    parent_title: str,
    *,
    entry=None,
    child_titles=(),
    linked=None,
):
    if entry is not None:
        _retain_page_navigation(context, page_ref, parent_title, *entry, linked)
    # Only the complete scanned inventory can rule out an off-screen namesake.
    if (entry is not None and child_titles.count(expected_title) == 1
            and 0 <= child_index < len(child_titles)
            and child_titles[child_index] == expected_title):
        matches = [row for row in parse_internal_subpages(entry[1]["root"])
                   if row["title"] == expected_title]
        if len(matches) == 1:
            return matches[0], entry[0]
        action, observation = entry
        can_advance, previous_start, advances = False, -1, 0
        def match_window(value):
            nonlocal can_advance, previous_start
            rows = parse_internal_subpages(value[1]["root"])
            labels = tuple(row["title"] for row in rows)
            positions = [i for i in range(len(child_titles) - len(labels) + 1)
                         if labels and tuple(child_titles[i:i + len(labels)]) == labels]
            can_advance = False
            if len(positions) != 1 or positions[0] < previous_start:
                return None
            start = previous_start = positions[0]
            if start <= child_index < start + len(rows):
                return rows[child_index - start]
            can_advance = child_index >= start + len(rows)
            return None
        def advance():
            nonlocal action, observation, advances
            if not can_advance or advances >= context.app_profile.parameters["page_max_windows"]:
                return False
            advances += 1
            action, observation = _swipe_page_observe(context, sequence, page_ref, "up")
            _retain_page_navigation(context, page_ref, parent_title, action, observation, linked)
        row, _ = seek_window(lambda: (action, observation), advance,
            lambda value: _page_signature(value[1], context.app_profile), match_window, adjacent=True)
        if row is not None:
            return row, action
        if advances:
            raise PageCollectionError("notion_subpage_inventory_changed", page_ref=page_ref, action=action)
        entry = action, observation
    action, observation = _rewind_page(
        context, sequence, page_ref, parent_title, entry=entry, linked=linked
    )
    titles: list[str] = []
    previous = None
    for _ in range(context.app_profile.parameters["page_max_windows"]):
        _retain_page_navigation(context, page_ref, parent_title, action, observation, linked)
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
        signature = _page_signature(observation, context.app_profile)
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


def _parse_open_page(context, page, observation, action):
    try:
        return parse_page(observation["root"], str(page["title"]), context.app_profile)
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


def _parse_page_entry(root, expected_title, profile):
    try:
        return parse_page(root, expected_title, profile)
    except NotionCollectorError as error:
        # Loading/error/header checks in parse_page must pass before recovery.
        if (str(error) == "Notion page title was not verified"
                and _page_context_visible(root, expected_title)):
            return {"classification": "retained_position"}
        raise


def _observe_page_entry(context, observation_id, action, expected_title):
    # Page counters restart per workspace; action IDs are unique within the run.
    observation_id = f"{observation_id}-{action.action_id}"
    timeout = context.app_profile.timings["page_entry_timeout"]
    fallback_after = context.ui._monotonic() + context.app_profile.timings["default_timeout"]
    block_editor_reobserved = False
    def parse_entry(root):
        nonlocal block_editor_reobserved
        parsed = _parse_page_entry(root, expected_title, context.app_profile)
        if (parsed["classification"] == "general_page"
                and not any(is_page_document(node, root) for node in root.iter())):
            # ponytail: bounded reobservation preserves empty/block pages; slower
            # DB loads need a stronger XML readiness signal, not unlimited polling.
            block_editor_reobserved = True
            if context.ui._monotonic() < fallback_after:
                raise NotionCollectorError("notion_page_classification_pending")
        return parsed
    try:
        observation = observe(context, observation_id, action,
                              parser=parse_entry,
                              diagnostic_name=observation_id, timeout=timeout)
    except NotionCollectorError as error:
        raise PageCollectionError(
            ("notion_page_title_unverified" if str(error) == "Notion page title was not verified"
             else "notion_page_entry_unavailable"),
            page_ref=action.context.get("page_ref"),
            page_id=action.context.get("notion_page_id"),
            action=action,
        ) from error
    retain_pair(context, f"notion/diagnostics/{observation_id}",
                observation, action, {**dict(action.context),
                    "entry_classification": observation["parsed"]["classification"],
                    "block_editor_reobserved": block_editor_reobserved})
    return observation


def _verify_open_page(context, sequence, page, action, observation, *, preserve_position=False):
    """Recover a verified retained-position entry; parent returns keep position."""
    title = str(page["title"])
    try:
        action, observation = dismiss_page_keyboard(context, sequence, action, observation,
            linked=dict(action.context), verify_parent=lambda root: _page_context_visible(root, title))
        if preserve_position:
            if not _page_context_visible(observation["root"], title):
                raise NotionCollectorError("notion_page_context_unverified")
        else:
            if observation.get("parsed", {}).get("classification") == "retained_position":
                action, observation = _rewind_page(
                    context, sequence, str(page["page_ref"]), title,
                    entry=(action, observation), linked=dict(action.context))
                if not _page_top_visible(observation["root"], title):
                    raise NotionCollectorError("notion_page_top_unavailable")
            _parse_open_page(context, page, observation, action)
    except NotionCollectorError as error:
        retain_pair(context, f"notion/diagnostics/page-context-{action.action_id}",
                    observation, action, dict(action.context))
        if isinstance(error, PageCollectionError):
            raise
        raise PageCollectionError(str(error), page_ref=str(page["page_ref"]),
                                  page_id=page.get("notion_page_id"), action=action) from error
    return action, observation


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
    return walk_hierarchy((context, sequence, workspace_ref, workspace_prefix, target_ref,
                           page, inventory, records, entry_action, observation), _page_level)


def _page_level(task):
    (context, sequence, workspace_ref, workspace_prefix, target_ref,
     page, inventory, records, entry_action, observation) = task
    try:
        entry_action, observation = _verify_open_page(
            context, sequence, page, entry_action, observation
        )
        parsed = _parse_open_page(context, page, observation, entry_action)
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
        "parent_page_ref": page.get("parent_page_ref"),
    }
    copied_text = None
    if context.app_profile.parameters["page_text_source"] == "clipboard":
        copied_text, entry_action, observation = _copy_page_contents(
            context, sequence, page, prefix, linked, entry_action, observation)
    content_windows = []
    child_titles, attachments, last_action = _scan_page_content(
        context,
        sequence,
        page=page,
        page_prefix=prefix,
        linked=linked,
        content_windows=content_windows,
        collect_text=copied_text is None,
        entry=(entry_action, observation),
    )
    source = content_windows[0]["source"]
    rendered_text, text_reason = ((copied_text["text"], None) if copied_text is not None
        else _merge_content_windows(content_windows, str(page["title"])))
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
        "partial" if attachment_partial or text_reason else "complete"
    )
    reason_code = (
        "notion_attachment_materialize_partial"
        if attachment_partial
        else text_reason
    )
    page_document = {
        "schema_version": "1.0",
        "record_kind": "notion_general_page",
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        **record,
        "rendered_text": rendered_text,
        "rendered_text_scope": "native_clipboard_text" if copied_text else content_windows[0]["text_source"],
        "rendered_text_source": copied_text["source"] if copied_text else next((window["source"] for window in content_windows
                                      if window["text_source"] == "accessibility_document"
                                      and window["rendered_text"] == rendered_text), None),
        "content_windows": content_windows,
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
    if copied_text is not None:
        page_document["copied_text"] = {key: value for key, value in copied_text.items() if key != "text"}
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

    parent_entry = None
    for child in children:
        child_ref = str(child["page_ref"])
        def enter():
            nonlocal last_action, parent_entry
            try:
                entry, parent_entry = parent_entry, None
                row, last_action = _find_internal_subpage(
                    context,
                    sequence,
                    page_ref,
                    int(child["child_index"]) - 1,
                    str(child["title"]),
                    str(page["title"]),
                    entry=entry,
                    child_titles=child_titles,
                    linked=linked,
                )
            except PageCollectionError as error:
                error.page_ref = child_ref
                raise
            last_action = context.ui.click_bounds(
                _action_name(sequence, "notion_open_subpage"),
                row["bounds"],
                context={
                    "target_ref": target_ref,
                    "workspace_ref": workspace_ref,
                    "page_ref": child_ref,
                    "parent_page_ref": page_ref,
                },
            )
            child_observation = _observe_page_entry(
                context,
                _action_name(sequence, "observation_notion_subpage"),
                last_action,
                str(child["title"]),
            )
            return child_observation

        def restore():
            nonlocal last_action, parent_entry
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
                last_action, parent_observation = _verify_open_page(
                    context,
                    sequence,
                    page,
                    verify_action,
                    parent_observation,
                    preserve_position=True,
                )
                _retain_page_navigation(context, page_ref, str(page["title"]),
                                        last_action, parent_observation, linked)
                parent_entry = (last_action, parent_observation)
            except Exception as error:
                raise PageCollectionError(
                    "notion_subpage_close_failed",
                    page_ref=child_ref,
                    action=last_action,
                ) from error
        with visit_scope(enter, restore) as child_observation:
            last_action = yield (
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
    return last_action


def _carousel_page(card, source):
    kind = "recent" if source == "recents" else "offline"
    return {
        "page_ref": f'{kind}-page-{int(card["carousel_index"]):06d}',
        "notion_page_id": None, "title": str(card["title"]), "section": source,
        "parent_page_ref": None, "depth": 0, "top_index": None,
        "child_index": None, "discovery_source": source,
    }


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
    targets = [(card, _carousel_page(card, source)) for card in cards]
    carousel_inventory = {page["page_ref"]: page for _, page in targets}
    inventory.update(carousel_inventory)
    _retain_home_inventory(context, workspace_prefix, target_ref, workspace_ref,
                           carousel_inventory, last_action, name="home-carousel-inventory")

    for target_index, (card, page) in enumerate(targets):
        page_ref = str(page["page_ref"])
        item_attempt(context, {"target_ref": target_ref, "workspace_ref": workspace_ref, "page_ref": page_ref}, retention=False)
        page_id = page.get("notion_page_id")
        entry_action = last_action
        try:
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
                        "target_ref": target_ref,
                        "workspace_ref": workspace_ref,
                        "page_ref": page_ref,
                        "notion_page_id": page_id,
                        "carousel_index": card["carousel_index"],
                        "discovery_source": source,
                    },
                )
                observation = _observe_page_entry(
                    context,
                    _action_name(
                        sequence, f"observation_notion_{kind}_page"
                    ),
                    entry_action,
                    str(page["title"]),
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
            finally:
                root = ET.fromstring(context.device.hierarchy())
                if (_page_context_visible(root, str(page["title"]))
                        or _page_load_error_visible(root) or _page_loading_visible(root)):
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
        except PageCollectionError as error:
            processed = {item["page_ref"] for item in records}
            if page_ref not in processed:
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": error.reason_code,
                })
                processed.add(page_ref)
            if (error.reason_code == "notion_page_entry_unavailable"
                    and context.ui.wait_for(selector(context, "notion.home"), timeout=0)):
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
    return last_action


def _retain_home_inventory(context, workspace_prefix, target_ref, workspace_ref, inventory, action, *, name="home-inventory"):
    records = [{**_page_record(page), "content_acquisition_status": "not_attempted"}
               for page in inventory.values()]
    document = {
        "schema_version": "1.0", "record_kind": "notion_home_inventory",
        "workspace_ref": workspace_ref, "target_ref": target_ref,
        "status": "complete", "reason_code": None,
        "discovered_page_count": len(records), "records": records,
        **{key: 0 for key in ("page_count", "database_count", "database_item_count",
            "database_out_of_scope_count", "attachment_count", "attachment_materialized_count",
            "attachment_out_of_scope_count")},
    }
    artifact = write_json(context, f"{workspace_prefix}/{name}.json", document, action,
                          {"workspace_ref": workspace_ref, "target_ref": target_ref})
    context.register_collection_records("notion.inventory", artifact, records,
                                        item_type="page_listing", record_key="records")
    return {**document, "source": {"artifact_id": artifact.artifact_id, "path": artifact.relative_path},
            "_action": action}


def collect_workspace_inventory(context, workspace, workspace_prefix, target_ref, *, entry=None):
    """Retain the existing Home scan without opening a page or database."""
    workspace_ref = str(workspace["workspace_ref"])
    sequence = count(1)
    inventory, action = _discover_top_level_pages(context, sequence, workspace_ref, entry=entry)
    cards, source, action = _discover_carousel_cards(context, sequence, workspace_ref)
    for card in cards:
        page = _carousel_page(card, source)
        inventory[page["page_ref"]] = page
    return _retain_home_inventory(context, workspace_prefix, target_ref, workspace_ref, inventory, action)


def collect_workspace_pages(
    context,
    workspace: Mapping[str, object],
    workspace_prefix: str,
    target_ref: str,
    *, entry=None,
) -> dict[str, object]:
    workspace_ref = str(workspace["workspace_ref"])
    sequence = count(1)
    records: list[dict[str, object]] = []
    try:
        inventory, last_action = _discover_top_level_pages(
            context, sequence, workspace_ref, entry=entry
        )
    except PageCollectionError:
        raise
    except Exception as error:
        raise PageCollectionError(
            "notion_page_inventory_unavailable"
        ) from error

    _retain_home_inventory(context, workspace_prefix, target_ref, workspace_ref, inventory, last_action)

    for page in tuple(inventory.values()):
        page_ref = str(page["page_ref"])
        item_attempt(context, {"target_ref": target_ref, "workspace_ref": workspace_ref, "page_ref": page_ref}, retention=False)
        page_id = str(page["notion_page_id"])
        entry_action = last_action
        try:
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
                        "target_ref": target_ref,
                        "workspace_ref": workspace_ref,
                        "page_ref": page_ref,
                        "notion_page_id": page_id,
                    },
                )
                observation = _observe_page_entry(
                    context,
                    _action_name(sequence, "observation_notion_page"),
                    entry_action,
                    str(page["title"]),
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
            finally:
                root = ET.fromstring(context.device.hierarchy())
                if (_page_context_visible(root, str(page["title"]))
                        or _page_load_error_visible(root) or _page_loading_visible(root)):
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

        except PageCollectionError as error:
            processed = {item["page_ref"] for item in records}
            if page_ref not in processed:
                records.append({
                    **_page_record(page),
                    "acquisition_status": "partial",
                    "reason_code": error.reason_code,
                })
                processed.add(page_ref)
            if (error.reason_code == "notion_page_entry_unavailable"
                    and context.ui.wait_for(selector(context, "notion.home"), timeout=0)):
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
