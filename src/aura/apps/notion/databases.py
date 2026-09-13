"""Notion 0.6.4030 Board database parsing and collection."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from .attachments import (
    AttachmentCollectionError,
    materialize_candidate,
    parse_database_property_candidates,
    parse_page_candidates,
)
from .support import (
    PACKAGE,
    NotionCollectorError,
    bounds,
    observe,
    onscreen,
    retain_pair,
    safe_component,
    write_json,
)


_DATABASE_CONTROLS = {
    "Filter and Sort",
    "Edit view layout, grouping and more...",
}
_EMPTY_BODY = "Tap here to continue…"


class DatabaseCollectionError(NotionCollectorError):
    def __init__(
        self,
        reason_code: str,
        *,
        item_ref: str | None = None,
        action=None,
        database: Mapping[str, object] | None = None,
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.item_ref = item_ref
        self.action = action
        self.database = database


def _label(node: ET.Element) -> str:
    return (
        node.get("content-desc", "").strip()
        or node.get("text", "").strip()
    )


def parse_database(
    root: ET.Element,
    expected_title: str,
) -> dict[str, object]:
    titles = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
    ]
    if len(titles) != 1:
        raise NotionCollectorError(
            "Notion database title was not verified"
        )
    controls = {
        _label(node)
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.Button"
    }
    if not _DATABASE_CONTROLS <= controls:
        raise NotionCollectorError("Notion database controls are unavailable")
    views = [
        label
        for node in root.iter()
        if (
            onscreen(node)
            and node.get("class") == "android.widget.Button"
            and (label := _label(node)).endswith(" View")
        )
    ]
    if len(views) != 1:
        raise NotionCollectorError("Notion database view is ambiguous")
    descriptions = [
        node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node not in titles
        and node.get("text", "").strip()
    ]
    if len(descriptions) > 1:
        raise NotionCollectorError(
            "Notion database description is ambiguous"
        )
    return {
        "title": expected_title,
        "description": descriptions[0] if descriptions else "",
        "view_ref": "view-000001",
        "view_name": views[0],
        "view_layout": "board" if views[0] == "Board View" else "unsupported",
    }


def parse_board_cards(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    navigation_top = min(
        (
            bounds(node)[1]
            for node in root.iter()
            if node.get("resource-id")
            == "com.android.systemui:id/navigation_bar_frame"
        ),
        default=2400,
    )
    result = []
    for node in root.iter():
        description = node.get("content-desc", "")
        if (
            not onscreen(node)
            or node.get("class") != "android.view.View"
            or node.get("clickable") != "true"
            or node.get("focusable") != "true"
            or node.get("resource-id", "")
            or not description.strip()
        ):
            continue
        left, top, right, bottom = bounds(node)
        title = (
            description[3:].strip()
            if description.startswith(".. ")
            else description.strip()
        )
        if (
            not title
            or left <= 0
            or right >= 1080
            or top < 0
            or bottom > navigation_top
        ):
            continue
        result.append({
            "title": title,
            "bounds": (left, top, right, bottom),
        })
    result.sort(key=lambda item: (item["bounds"][0], item["bounds"][1]))
    return tuple(result)


def _property_rows(root: ET.Element) -> list[dict[str, object]]:
    add_property_tops = [
        bounds(node)[1]
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.Button"
        and _label(node) == "Add a property"
    ]
    if len(add_property_tops) != 1:
        raise NotionCollectorError(
            "Notion database item property boundary is unavailable"
        )
    groups: dict[
        tuple[int, int],
        list[tuple[int, int, str]],
    ] = {}
    for node in root.iter():
        if (
            not onscreen(node)
            or node.get("class") != "android.view.View"
            or node.get("clickable") == "true"
            or not node.get("text", "").strip()
        ):
            continue
        left, top, right, bottom = bounds(node)
        if bottom > add_property_tops[0]:
            continue
        groups.setdefault((top, bottom), []).append(
            (left, right, node.get("text", "").strip())
        )
    rows = []
    for (top, _), cells in groups.items():
        cells.sort()
        if (
            len(cells) == 2
            and cells[0][1] <= cells[1][0]
        ):
            rows.append((top, cells[0][2], cells[1][2]))
    rows.sort()
    return [
        {"name": name, "value": value, "ordinal": index}
        for index, (_, name, value) in enumerate(rows, 1)
    ]


def _rendered_body(
    root: ET.Element,
    title: str,
    properties: list[dict[str, object]],
) -> str:
    documents = []
    for node in root.iter():
        if (
            not onscreen(node)
            or node.get("class") != "android.widget.EditText"
            or node.get("clickable") == "true"
            or not node.get("text", "")
        ):
            continue
        left, top, right, bottom = bounds(node)
        if left == 0 and right == 1080 and bottom - top >= 1800:
            documents.append(node.get("text", ""))
    if not documents and sum(
        onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == _EMPTY_BODY
        for node in root.iter()
    ) == 1:
        return ""
    if len(documents) != 1:
        raise NotionCollectorError(
            "Notion database item body is ambiguous"
        )
    lines = documents[0].splitlines()
    visible = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if line.strip()
    ]
    expected = [title]
    for row in properties:
        expected.extend((str(row["name"]), str(row["value"])))
    expected.append("Add a property")
    position = 0
    for value in expected:
        match = next(
            (
                index
                for index in range(position, len(visible))
                if visible[index][1] == value
            ),
            None,
        )
        if match is None:
            raise NotionCollectorError(
                "Notion database item body prefix is inconsistent"
            )
        position = match + 1
    start = visible[position][0] if position < len(visible) else len(lines)
    template_suffix = ("New Task", "Empty", "New template")
    labels = tuple(value for _, value in visible)
    end = (
        visible[-len(template_suffix)][0]
        if labels[-len(template_suffix):] == template_suffix
        else len(lines)
    )
    body_lines = lines[start:end]
    while body_lines and not body_lines[0].strip():
        body_lines.pop(0)
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()
    if (
        len(body_lines) == 1
        and body_lines[0].strip() == _EMPTY_BODY
    ):
        return ""
    return "\n".join(body_lines)


def parse_database_item(
    root: ET.Element,
    expected_title: str,
) -> dict[str, object]:
    titles = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
    ]
    if len(titles) != 1:
        raise NotionCollectorError(
            "Notion database item title was not verified"
        )
    properties = _property_rows(root)
    return {
        "title": expected_title,
        "properties": properties,
        "rendered_body": _rendered_body(
            root,
            expected_title,
            properties,
        ),
    }


def _action_name(sequence, stem: str) -> str:
    return f"{stem}_{next(sequence):06d}"


def _signature(
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


def _board_bounds(root: ET.Element) -> tuple[int, int, int, int]:
    control_bottoms = [
        bounds(node)[3]
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.Button"
        and _label(node) in _DATABASE_CONTROLS
    ]
    navigation = [
        bounds(node)
        for node in root.iter()
        if node.get("resource-id")
        == "com.android.systemui:id/navigation_bar_frame"
    ]
    if not control_bottoms or len(navigation) != 1:
        raise NotionCollectorError("Notion Board bounds are unavailable")
    left, bottom, right, _ = navigation[0]
    top = max(control_bottoms)
    if top >= bottom:
        raise NotionCollectorError("Notion Board bounds are unavailable")
    return left, top, right, bottom


def _swipe_observe(
    context,
    sequence,
    page_ref: str,
    direction: str,
    observation: Mapping[str, object],
):
    action_name = _action_name(
        sequence, f"notion_database_swipe_{direction}"
    )
    if direction in {"left", "right"}:
        action = context.ui.swipe_bounds(
            action_name,
            _board_bounds(observation["root"]),
            direction,
            context={"page_ref": page_ref},
        )
    else:
        action = context.ui.swipe(
            action_name,
            direction,
            expect_change=False,
            context={"page_ref": page_ref},
        )
    return action, observe(
        context,
        _action_name(sequence, "observation_notion_database_scroll"),
        action,
    )


def _swipe_to_edge(
    context,
    sequence,
    page_ref: str,
    direction: str,
    observation: Mapping[str, object],
):
    current = observation
    while True:
        before = _signature(current["root"])
        action, after = _swipe_observe(
            context,
            sequence,
            page_ref,
            direction,
            current,
        )
        current = after
        if _signature(after["root"]) == before:
            return action, current


def _normalize_board(
    context,
    sequence,
    page_ref: str,
    observation: Mapping[str, object],
):
    action, current = _swipe_to_edge(
        context,
        sequence,
        page_ref,
        "down",
        observation,
    )
    return _swipe_to_edge(
        context,
        sequence,
        page_ref,
        "right",
        current,
    )


def _overlap(
    previous: Sequence[str],
    current: Sequence[str],
) -> int:
    for size in range(min(len(previous), len(current)), 0, -1):
        if list(previous[-size:]) == list(current[:size]):
            return size
    return 0


def _scan_board(
    context,
    sequence,
    page_ref: str,
    observation: Mapping[str, object],
):
    action, horizontal_top = _normalize_board(
        context,
        sequence,
        page_ref,
        observation,
    )
    result = []
    horizontal_index = 0
    while True:
        current = horizontal_top
        vertical_index = 0
        column_titles: list[str] = []
        while True:
            cards = parse_board_cards(current["root"])
            visible_titles = [str(card["title"]) for card in cards]
            overlap = _overlap(column_titles, visible_titles)
            start = len(column_titles) - overlap
            for index, card in enumerate(cards):
                position = start + index
                if position < len(column_titles):
                    continue
                result.append({
                    **card,
                    "horizontal_index": horizontal_index,
                    "vertical_index": vertical_index,
                    "visible_index": index,
                })
            column_titles.extend(visible_titles[overlap:])
            before = _signature(current["root"])
            action, after = _swipe_observe(
                context,
                sequence,
                page_ref,
                "up",
                current,
            )
            current = after
            if _signature(after["root"]) == before:
                break
            vertical_index += 1

        action, horizontal_top = _swipe_to_edge(
            context,
            sequence,
            page_ref,
            "down",
            current,
        )
        before = _signature(horizontal_top["root"])
        action, after = _swipe_observe(
            context,
            sequence,
            page_ref,
            "left",
            horizontal_top,
        )
        if _signature(after["root"]) == before:
            return tuple(result), action, horizontal_top
        horizontal_top = after
        horizontal_index += 1


def _find_card(
    context,
    sequence,
    page_ref: str,
    observation: Mapping[str, object],
    item: Mapping[str, object],
):
    def replay(candidate, start):
        action, current = _normalize_board(
            context,
            sequence,
            page_ref,
            start,
        )
        for _ in range(int(candidate["horizontal_index"])):
            action, current = _swipe_observe(
                context,
                sequence,
                page_ref,
                "left",
                current,
            )
        for _ in range(int(candidate["vertical_index"])):
            action, current = _swipe_observe(
                context,
                sequence,
                page_ref,
                "up",
                current,
            )
        cards = parse_board_cards(current["root"])
        index = int(candidate["visible_index"])
        row = (
            cards[index]
            if index < len(cards)
            and cards[index]["title"] == candidate["title"]
            else None
        )
        return row, action, current

    row, action, current = replay(item, observation)
    if row is not None:
        return row, action, current

    inventory, action, current = _scan_board(
        context,
        sequence,
        page_ref,
        current,
    )
    matches = [
        candidate
        for candidate in inventory
        if candidate["title"] == item["title"]
    ]
    occurrence = int(item["same_title_occurrence"])
    if occurrence <= 0 or occurrence > len(matches):
        raise DatabaseCollectionError(
            "notion_database_item_unavailable",
            item_ref=str(item["database_item_ref"]),
            action=action,
        )
    row, action, current = replay(matches[occurrence - 1], current)
    if row is None:
        raise DatabaseCollectionError(
            "notion_database_item_unavailable",
            item_ref=str(item["database_item_ref"]),
            action=action,
        )
    return row, action, current


def _observe_parent(context, sequence, page_ref: str, action):
    observation = observe(
        context,
        _action_name(sequence, "observation_notion_database_parent"),
        action,
    )
    return action, observation


def _is_board(root: ET.Element, title: str) -> bool:
    try:
        return parse_database(root, title)["view_layout"] == "board"
    except NotionCollectorError:
        return False


def _item_prefix(
    page_prefix: str,
    item_ref: str,
    title: str,
) -> str:
    component = f"{item_ref} ({safe_component(title)})"
    if PurePosixPath(component).name != component:
        raise NotionCollectorError(
            "Notion database item output path is invalid"
        )
    return f"{page_prefix}/items/{component}"


def _item_attachment_candidates(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    candidates = [
        *parse_database_property_candidates(root),
        *(
            {
                **candidate,
                "location": "database_item_body",
            }
            for candidate in parse_page_candidates(root)
        ),
    ]
    candidates.sort(key=lambda item: (
        item["bounds"][1],
        item["bounds"][0],
    ))
    return tuple(candidates)


def _item_context_visible(
    root: ET.Element,
    title: str,
) -> bool:
    try:
        return parse_database_item(root, title)["title"] == title
    except NotionCollectorError:
        return False


def _attachment_counts(items):
    return {
        "attachment_count": sum(
            int(item.get("attachment_count", 0))
            for item in items
        ),
        "attachment_materialized_count": sum(
            int(item.get("attachment_materialized_count", 0))
            for item in items
        ),
        "attachment_out_of_scope_count": sum(
            int(item.get("attachment_out_of_scope_count", 0))
            for item in items
        ),
    }


def collect_open_database(
    context,
    sequence,
    *,
    workspace_ref: str,
    workspace_prefix: str,
    target_ref: str,
    page: Mapping[str, object],
    page_prefix: str,
    entry_action,
    observation: Mapping[str, object],
):
    if not page_prefix.startswith(f"{workspace_prefix}/pages/"):
        raise DatabaseCollectionError(
            "notion_database_output_path_invalid",
            action=entry_action,
        )
    page_ref = str(page["page_ref"])
    try:
        parsed = parse_database(
            observation["root"],
            str(page["title"]),
        )
    except NotionCollectorError as error:
        raise DatabaseCollectionError(
            "notion_database_context_unavailable",
            action=entry_action,
        ) from error
    linked = {
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "page_ref": page_ref,
        "notion_page_id": page.get("notion_page_id"),
        "view_ref": parsed["view_ref"],
    }
    source = retain_pair(
        context,
        f"{page_prefix}/database",
        observation,
        entry_action,
        linked,
    )

    if parsed["view_layout"] != "board":
        document = {
            "schema_version": "1.0",
            "record_kind": "notion_database",
            **linked,
            **parsed,
            "discovered_item_count": 0,
            "item_count": 0,
            **_attachment_counts(()),
            "acquisition_status": "out_of_scope",
            "reason_code": "notion_database_view_unsupported",
            "items": [],
            "source": source,
        }
        write_json(
            context,
            f"{page_prefix}/database.json",
            document,
            entry_action,
            {**linked, "observation_id": source["observation_id"]},
        )
        return entry_action, document

    inventory, last_action, current = _scan_board(
        context,
        sequence,
        page_ref,
        observation,
    )
    title_counts: Counter[str] = Counter()
    items = []
    for index, candidate in enumerate(inventory, 1):
        title = str(candidate["title"])
        title_counts[title] += 1
        item_ref = f"database-item-{index:06d}"
        item = {
            **candidate,
            "database_item_ref": item_ref,
            "same_title_occurrence": title_counts[title],
        }
        click_dispatched = False
        item_state_known = False
        item_opened = False
        item_parsed = None
        item_source = None
        attachment_records = []
        try:
            row, last_action, current = _find_card(
                context,
                sequence,
                page_ref,
                current,
                item,
            )
            last_action = context.ui.click_bounds(
                _action_name(sequence, "notion_open_database_item"),
                row["bounds"],
                context={
                    **linked,
                    "database_item_ref": item_ref,
                },
            )
            click_dispatched = True
            item_observation = observe(
                context,
                _action_name(sequence, "observation_notion_database_item"),
                last_action,
            )
            item_opened = not _is_board(
                item_observation["root"],
                str(page["title"]),
            )
            item_state_known = True
            if not item_opened:
                raise DatabaseCollectionError(
                    "notion_database_item_open_failed",
                    item_ref=item_ref,
                    action=last_action,
                )
            item_parsed = parse_database_item(
                item_observation["root"],
                title,
            )
            item_source = retain_pair(
                context,
                f"{_item_prefix(page_prefix, item_ref, title)}/item",
                item_observation,
                last_action,
                {
                    **linked,
                    "database_item_ref": item_ref,
                },
            )
            item_prefix = _item_prefix(
                page_prefix,
                item_ref,
                title,
            )
            for attachment_index, attachment in enumerate(
                _item_attachment_candidates(
                    item_observation["root"]
                ),
                1,
            ):
                attachment_ref = (
                    f"attachment-{attachment_index:06d}"
                )
                last_action, item_observation, attachment_record = (
                    materialize_candidate(
                        context,
                        sequence,
                        candidate=attachment,
                        attachment_ref=attachment_ref,
                        parent_prefix=item_prefix,
                        linked={
                            **linked,
                            "database_item_ref": item_ref,
                        },
                        observation=item_observation,
                        entry_action=last_action,
                        verify_parent=lambda root: (
                            _item_context_visible(root, title)
                        ),
                    )
                )
                attachment_records.append(attachment_record)
            attachment_partial = any(
                record["acquisition_status"] == "not_materialized"
                for record in attachment_records
            )
            item_record = {
                "schema_version": "1.0",
                "record_kind": "notion_database_item",
                **linked,
                "database_item_ref": item_ref,
                "title": title,
                "same_title_occurrence": title_counts[title],
                "properties": item_parsed["properties"],
                "rendered_body": item_parsed["rendered_body"],
                "attachment_count": len(attachment_records),
                "attachment_materialized_count": sum(
                    record["acquisition_status"] == "materialized"
                    for record in attachment_records
                ),
                "attachment_out_of_scope_count": sum(
                    record["acquisition_status"] == "out_of_scope"
                    for record in attachment_records
                ),
                "attachments": attachment_records,
                "acquisition_status": (
                    "partial" if attachment_partial else "complete"
                ),
                "reason_code": (
                    "notion_attachment_materialize_partial"
                    if attachment_partial
                    else None
                ),
                "source": item_source,
            }
            write_json(
                context,
                f"{_item_prefix(page_prefix, item_ref, title)}/item.json",
                item_record,
                last_action,
                {
                    **linked,
                    "database_item_ref": item_ref,
                    "observation_id": item_source["observation_id"],
                },
            )
        except Exception as error:
            materialized_count = sum(
                record["acquisition_status"] == "materialized"
                for record in attachment_records
            )
            out_of_scope_count = sum(
                record["acquisition_status"] == "out_of_scope"
                for record in attachment_records
            )
            item_record = {
                "schema_version": "1.0",
                "record_kind": "notion_database_item",
                **linked,
                "database_item_ref": item_ref,
                "title": title,
                "same_title_occurrence": title_counts[title],
                "properties": (
                    item_parsed["properties"] if item_parsed else []
                ),
                "rendered_body": (
                    item_parsed["rendered_body"] if item_parsed else ""
                ),
                "attachment_count": len(attachment_records),
                "attachment_materialized_count": materialized_count,
                "attachment_out_of_scope_count": out_of_scope_count,
                "attachments": attachment_records,
                "acquisition_status": "partial",
                "reason_code": (
                    error.reason_code
                    if isinstance(
                        error,
                        (
                            AttachmentCollectionError,
                            DatabaseCollectionError,
                        ),
                    )
                    else "notion_database_item_collection_failed"
                ),
                "source": item_source,
            }
        finally:
            try:
                if click_dispatched and not item_state_known:
                    item_opened = not _is_board(
                        ET.fromstring(context.device.hierarchy()),
                        str(page["title"]),
                    )
                if item_opened:
                    last_action = context.ui.back(
                        _action_name(sequence, "notion_close_database_item"),
                        context={
                            **linked,
                            "database_item_ref": item_ref,
                        },
                    )
                last_action, current = _observe_parent(
                    context,
                    sequence,
                    page_ref,
                    last_action,
                )
                restored = parse_database(
                    current["root"],
                    str(page["title"]),
                )
                if restored["view_layout"] != "board":
                    raise NotionCollectorError(
                        "Notion Board View was not restored"
                    )
            except Exception as error:
                items.append(item_record)
                complete_count = sum(
                    item["acquisition_status"] == "complete"
                    for item in items
                )
                document = {
                    "schema_version": "1.0",
                    "record_kind": "notion_database",
                    **linked,
                    **parsed,
                    "discovered_item_count": len(inventory),
                    "item_count": complete_count,
                    **_attachment_counts(items),
                    "acquisition_status": "partial",
                    "reason_code": "notion_database_restore_failed",
                    "items": items,
                    "source": source,
                }
                write_json(
                    context,
                    f"{page_prefix}/database.json",
                    document,
                    last_action,
                    {
                        **linked,
                        "observation_id": source["observation_id"],
                    },
                )
                raise DatabaseCollectionError(
                    "notion_database_restore_failed",
                    item_ref=item_ref,
                    action=last_action,
                    database=document,
                ) from error
        items.append(item_record)

    complete_count = sum(
        item["acquisition_status"] == "complete" for item in items
    )
    status = "complete" if complete_count == len(items) else "partial"
    document = {
        "schema_version": "1.0",
        "record_kind": "notion_database",
        **linked,
        **parsed,
        "discovered_item_count": len(items),
        "item_count": complete_count,
        **_attachment_counts(items),
        "acquisition_status": status,
        "reason_code": (
            None if status == "complete"
            else "notion_database_item_collection_partial"
        ),
        "items": items,
        "source": source,
    }
    write_json(
        context,
        f"{page_prefix}/database.json",
        document,
        last_action,
        {**linked, "observation_id": source["observation_id"]},
    )
    return last_action, document
