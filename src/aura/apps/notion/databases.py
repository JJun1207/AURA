"""Notion 0.6.4030 Board database parsing and collection."""

from __future__ import annotations
from ...collection import (
    seek_window,
    matching_candidates,
    overlap_candidates,
    iter_windows,
    visit_candidates,
    reconcile_overlap,
)


import xml.etree.ElementTree as ET
from collections import Counter
from itertools import groupby
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
    database_control_labels,
    property_text_cells,
    node_label as _label,
    NotionCollectorError,
    bounds,
    is_page_document,
    observe,
    onscreen,
    retain_pair,
    safe_component,
    viewport_bounds,
    write_json,
)


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


def parse_database(
    root: ET.Element,
    expected_title: str,
    profile,
) -> dict[str, object]:
    titles = [
        node
        for node in root.iter()
        if onscreen(node, root)
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
        if onscreen(node, root)
        and node.get("class") == "android.widget.Button"
    }
    if not database_control_labels(profile) <= controls:
        raise NotionCollectorError("Notion database controls are unavailable")
    views = [
        label
        for node in root.iter()
        if (
            onscreen(node, root)
            and node.get("class") == "android.widget.Button"
            and (label := _label(node)).endswith(" View")
        )
    ]
    if len(views) != 1:
        raise NotionCollectorError("Notion database view is ambiguous")
    descriptions = [
        node.get("text", "").strip()
        for node in root.iter()
        if onscreen(node, root)
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
    root: ET.Element, *, include_clipped: bool = False,
) -> tuple[dict[str, object], ...]:
    viewport_left, _, viewport_right, viewport_bottom = viewport_bounds(root)
    navigation_top = min(
        (
            bounds(node)[1]
            for node in root.iter()
            if node.get("resource-id")
            == "com.android.systemui:id/navigation_bar_frame"
        ),
        default=viewport_bottom,
    )
    parents = {child: parent for parent in root.iter() for child in parent}
    # In the observed WebView tree each column owns a New page button. Its XML
    # sibling index survives horizontal clipping; pixel positions do not.
    columns = {}
    for control in root.iter():
        if control.get("class") != "android.widget.Button" or control.get("text") != "New page":
            continue
        owner = parents.get(control)
        while owner is not None:
            if sum(child.get("class") == "android.widget.Button" and child.get("text") == "New page"
                   for child in owner.iter()) > 1:
                break  # This is the board, not the empty column owning this button.
            if any(child.get("content-desc", "").strip() and child.get("class") == "android.view.View"
                   and child.get("clickable") == "true" and child.get("focusable") == "true"
                   for child in owner.iter()):
                if owner.get("index") is not None:
                    for child in owner.iter():
                        columns[child] = owner.get("index")
                break
            owner = parents.get(owner)
    result = []
    for node in root.iter():
        description = node.get("content-desc", "")
        if (
            not onscreen(node, root)
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
        selectable = (left > viewport_left and right < viewport_right
                      and top >= 0 and bottom <= navigation_top)
        if not title or (not include_clipped and not selectable):
            continue
        result.append({
            "title": title,
            **({"column_slot": columns[node]} if node in columns else {}),
            "bounds": (left, top, right, bottom),
            **({"selectable": selectable} if include_clipped else {}),
        })
    result.sort(key=lambda item: (item["bounds"][0], item["bounds"][1]))
    return tuple(result)


def _property_rows(root: ET.Element) -> list[dict[str, object]]:
    add_property_tops = [
        bounds(node)[1]
        for node in root.iter()
        if onscreen(node, root)
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
    for node in property_text_cells(root):
        left, top, right, bottom = bounds(node)
        if bottom > add_property_tops[0]:
            continue
        groups.setdefault((top, bottom), []).append(
            (left, right, node.get("text", ""))
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
    documents = [node for node in root.iter() if is_page_document(node, root)]
    if not documents and sum(
        onscreen(node, root)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == _EMPTY_BODY
        for node in root.iter()
    ) == 1:
        return ""
    if len(documents) != 1:
        raise NotionCollectorError(
            "Notion database item body is ambiguous"
        )
    document = documents[0]
    lines = document.get("text", "").splitlines()
    visible = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if line.strip()
    ]
    expected = [title]
    for row in properties:
        expected.extend((str(row["name"]).strip(), str(row["value"]).strip()))
    expected.append("Add a property")
    labels = [value for _, value in visible]
    position = len(expected)
    # The nested GridView and its following control prove the property scope.
    # Its accessibility values are independent of the aggregate document text.
    grids = [node for node in document if node.get("class") == "android.widget.GridView"]
    boundaries = [node for node in document
                  if node.get("class") == "android.widget.Button"
                  and _label(node) == "Add a property"]
    structured = (
        len(grids) == len(boundaries) == 1
        and list(document).index(grids[0]) < list(document).index(boundaries[0])
        and bounds(grids[0])[3] <= bounds(boundaries[0])[3]
        and any(node.get("class") == "android.widget.EditText"
                and node.get("clickable") == "true"
                and node.get("text", "").strip() == title
                and bounds(node)[3] <= bounds(grids[0])[1]
                for node in document.iter() if onscreen(node, root))
        and [node.get("text", "").strip() for node in property_text_cells(grids[0])] == expected[1:-1]
    )
    prefix = labels[:position]
    if (labels.count("Add a property") != 1
            or len(prefix) != position
            or prefix[0] != title
            or prefix[-1] != "Add a property"
            or (prefix != expected and not (
                structured and prefix[1:-1:2] == expected[1:-1:2]
            ))):
        raise NotionCollectorError(
            "Notion database item body prefix is inconsistent"
        )
    start = visible[position][0] if position < len(visible) else len(lines)
    template_suffix = ("New Task", "Empty", "New template")
    end = (
        visible[-len(template_suffix)][0]
        if tuple(labels[-len(template_suffix):]) == template_suffix
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


def _item_properties(
    root: ET.Element,
    expected_title: str,
) -> list[dict[str, object]]:
    titles = [
        node
        for node in root.iter()
        if onscreen(node, root)
        and node.get("class") == "android.widget.EditText"
        and node.get("clickable") == "true"
        and node.get("text", "").strip() == expected_title
    ]
    if len(titles) != 1:
        raise NotionCollectorError(
            "Notion database item title was not verified"
        )
    return _property_rows(root)


def parse_database_item(
    root: ET.Element,
    expected_title: str,
) -> dict[str, object]:
    properties = _item_properties(root, expected_title)
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
) -> tuple:
    hierarchy = tuple(
        (
            node.get("class", ""),
            node.get("resource-id", ""),
            node.get("text", ""),
            node.get("content-desc", ""),
        )
        for node in root.iter()
        if node.get("package") == PACKAGE
    )
    # A clipped card becoming selectable is progress even if its label existed.
    visible_cards = tuple((card.get("column_slot"), card["title"], card["selectable"]) for card in parse_board_cards(root, include_clipped=True))
    return hierarchy, visible_cards


def _board_bounds(root: ET.Element, profile, interaction_area) -> tuple[int, int, int, int]:
    controls = database_control_labels(profile)
    control_bottoms = [
        bounds(node)[3]
        for node in root.iter()
        if onscreen(node, root)
        and node.get("class") == "android.widget.Button"
        and _label(node) in controls
    ]
    if not control_bottoms:
        raise NotionCollectorError("Notion Board bounds are unavailable")
    left, top, right, bottom = interaction_area
    top = max(top, *control_bottoms)
    if top >= bottom:
        raise NotionCollectorError("Notion Board bounds are unavailable")
    return left, top, right, bottom


def _swipe_observe(
    context,
    sequence,
    page_ref: str,
    direction: str,
    observation: Mapping[str, object],
    *, align: bool = False,
):
    action_name = _action_name(
        sequence, f"notion_database_swipe_{direction}"
    )
    if direction in {"left", "right"}:
        action = context.ui.swipe_bounds(
            action_name,
            _board_bounds(observation["root"], context.app_profile, context.device.interaction_bounds()),
            direction,
            **({"duration": context.app_profile.timings["board_alignment_duration"],
                "distance_ratio": context.app_profile.parameters["board_alignment_distance_ratio"]}
               if align else {}),
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


def _swipe_to_edge(context, sequence, page_ref: str, direction: str, observation: Mapping[str, object]):
    current = (None, observation)
    def advance():
        nonlocal current
        current = _swipe_observe(context, sequence, page_ref, direction, current[1])
    _, last = seek_window(lambda: current, advance, lambda value: _signature(value[1]['root']),
                          lambda value: None, adjacent=True)
    return last


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


def _overlap(previous: Sequence[str], current: Sequence[str]) -> int:
    return max(overlap_candidates(previous, current), default=0)


def _merge_board_cards(previous, current, *, same_column=False):
    if same_column:
        slots = {row['column_slot'] for row in (*previous, *current) if 'column_slot' in row}
        if len(slots) == 1:
            slot = next(iter(slots))
            previous = [{**row, 'column_slot': slot} for row in previous]
            current = [{**row, 'column_slot': slot} for row in current]
    overlap = _overlap([(row.get("column_slot"), row["title"]) for row in previous],
                       [(row.get("column_slot"), row["title"]) for row in current])
    return reconcile_overlap(previous, current, overlap,
        lambda old, new: old if old["selectable"] and not new["selectable"] else new)


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
    current = horizontal_top
    def advance_horizontal():
        nonlocal action, horizontal_top, horizontal_index
        action, horizontal_top = _swipe_to_edge(
            context,
            sequence,
            page_ref,
            "down",
            current,
        )
        action, after = _swipe_observe(
            context,
            sequence,
            page_ref,
            "left",
            horizontal_top,
        )
        horizontal_top = after
        horizontal_index += 1
    for _ in iter_windows(lambda: horizontal_top, advance_horizontal,
            lambda value: _signature(value["root"]), adjacent=True,
            boundary_repeats=context.app_profile.parameters["board_boundary_repeats"]):
        current = horizontal_top
        vertical_index = 0
        column_cards = {}
        def advance_vertical():
            nonlocal action, current, vertical_index
            action, current = _swipe_observe(context, sequence, page_ref, "up", current)
            vertical_index += 1
        for current in iter_windows(lambda: current, advance_vertical,
                                     lambda value: _signature(value["root"]), adjacent=True):
            cards = [
                {**card, "horizontal_index": horizontal_index,
                 "vertical_index": vertical_index, "visible_index": index}
                for index, card in enumerate(parse_board_cards(
                    current["root"], include_clipped=True))
            ]
            # Partition by the observed column geometry before merging vertical
            # overlap; neighboring clipped columns must not interleave rows.
            for column, rows in groupby(cards, key=lambda row: (row["bounds"][0], row["bounds"][2])):
                column_cards[column] = _merge_board_cards(column_cards.get(column, []), list(rows), same_column=True)
        result = _merge_board_cards(result,
            [row for column in sorted(column_cards) for row in column_cards[column]])

    return tuple(result), action, horizontal_top


def _find_card(
    context,
    sequence,
    page_ref: str,
    observation: Mapping[str, object],
    item: Mapping[str, object],
    *,
    inventory=(),
    entry_action=None,
):
    if entry_action is not None and sum(row["title"] == item["title"] for row in inventory) == 1:
        matches = list(matching_candidates(
            parse_board_cards(observation["root"], include_clipped=True), item,
            key=lambda row: row["title"]))
        if len(matches) == 1 and matches[0]["selectable"]:
            row = matches[0]
            return {"title": row["title"], "bounds": row["bounds"]}, entry_action, observation

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
        cards = parse_board_cards(current["root"], include_clipped=True)
        index = int(candidate["visible_index"])
        row = (cards[index] if index < len(cards)
               and cards[index]["title"] == candidate["title"]
               and cards[index].get("column_slot") == candidate.get("column_slot") else None)
        if row is None and sum(c["title"] == candidate["title"] for c in inventory) == 1:
            matches = [c for c in cards if c["title"] == candidate["title"]]
            row = matches[0] if len(matches) == 1 else None
        for _ in range(context.app_profile.parameters["board_alignment_attempts"]):
            if row is None or row["selectable"]:
                break
            left, top, right, bottom = row["bounds"]
            view_left, _, view_right, view_bottom = _board_bounds(
                current["root"], context.app_profile, context.device.interaction_bounds())
            direction = ("right" if left <= view_left else "left" if right >= view_right
                         else "up" if bottom > view_bottom else "down")
            action, current = _swipe_observe(
                context, sequence, page_ref, direction, current, align=True)
            matches = list(matching_candidates(
                parse_board_cards(current["root"], include_clipped=True), candidate,
                key=lambda value: (value.get("column_slot"), value["title"])))
            # A title alone cannot resolve multiple equal cards after movement.
            row = matches[0] if len(matches) == 1 else None
        if row is not None and row["selectable"]:
            return {"title": row["title"], "bounds": row["bounds"]}, action, current
        return None, action, current

    current = observation
    for _ in range(2):
        row, action, current = replay(item, current)
        if row is not None:
            return row, action, current

    raise DatabaseCollectionError(
        "notion_database_card_unresolved", item_ref=str(item["database_item_ref"]), action=action)


def _observe_parent(context, sequence, page_ref: str, action):
    observation = observe(
        context,
        _action_name(sequence, "observation_notion_database_parent"),
        action,
    )
    return action, observation


def _is_board(root: ET.Element, title: str, profile) -> bool:
    try:
        return parse_database(root, title, profile)["view_layout"] == "board"
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
        _item_properties(root, title)
        return True
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
            context.app_profile,
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
        rendered_document = None
        parser_error = None
        attachment_records = []
        try:
            row, last_action, current = _find_card(
                context,
                sequence,
                page_ref,
                current,
                item,
                inventory=inventory,
                entry_action=last_action,
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
                context.app_profile,
            )
            item_state_known = True
            if not item_opened:
                raise DatabaseCollectionError(
                    "notion_database_item_open_failed",
                    item_ref=item_ref,
                    action=last_action,
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
            item_root = item_observation["root"]
            documents = [node.get("text", "") for node in item_root.iter()
                         if is_page_document(node, item_root)]
            rendered_document = documents[0] if len(documents) == 1 else None
            item_parsed = {
                "properties": _item_properties(item_root, title),
                "rendered_body": "",
            }
            try:
                item_parsed["rendered_body"] = _rendered_body(
                    item_root, title, item_parsed["properties"]
                )
            except NotionCollectorError as error:
                parser_error = str(error)
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
                "rendered_document": rendered_document,
                "parser_error": parser_error,
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
                    "partial" if parser_error or attachment_partial else "complete"
                ),
                "reason_code": (
                    "notion_database_item_body_unverified" if parser_error
                    else "notion_attachment_materialize_partial"
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
                "rendered_document": rendered_document,
                "parser_error": parser_error or str(error),
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
                    item_opened = _item_context_visible(
                        ET.fromstring(context.device.hierarchy()), title)
                if item_opened and _item_context_visible(ET.fromstring(context.device.hierarchy()), title):
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
                    context.app_profile,
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
