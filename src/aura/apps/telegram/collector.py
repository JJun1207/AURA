"""Mutable Telegram Collector with T01 and T02 row recognition."""

from collections.abc import Mapping
from datetime import datetime, timedelta
import re
import unicodedata
import xml.etree.ElementTree as ET

from .adapter import normalize_retained_basename


PACKAGE = "org.telegram.messenger"
_BOUNDS = re.compile(r"^\[(\d+),(\d+)\]\[(\d+),(\d+)\]$")
_TIME_LABEL = (
    r"(?:(?:[1-9]|1[0-2]):[0-5][0-9] (?:AM|PM)|"
    r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9])"
)
_MESSAGE_STATUS = re.compile(
    r"\A(.+)\n(Received|Sent) at "
    rf"({_TIME_LABEL})"
    r"(?:\n|, (?P<status>Seen|Not seen)\n?)\Z",
    re.DOTALL,
)
_CHANNEL_STATUS = re.compile(
    r"\A(?P<body>.+)\nSent at "
    rf"(?P<time>{_TIME_LABEL}), Not seen\n"
    r"Viewed (?P<views>\d[\d,]*) times?\n\Z",
    re.DOTALL,
)
_MEDIA_SIZE = r"\d+(?:\.\d+)? (?:KB|MB|GB)"
_CHANNEL_VIDEO = re.compile(
    rf"\AVideo, (?P<duration>\d+) seconds?(?:, {_MEDIA_SIZE})?\Z"
)
_FILE_BODY = rf", [A-Z0-9]+ file.+, {_MEDIA_SIZE}"
_VIDEO_DURATION = r"(?:\d+ minutes? )?\d+ seconds?"
_MESSAGE_VIDEO = (
    rf"(?:Video, {_VIDEO_DURATION}(?:, {_MEDIA_SIZE})?|"
    rf"Video(?:: |\n)Downloaded {_MEDIA_SIZE} of {_MEDIA_SIZE}, "
    rf"{_VIDEO_DURATION})"
)
_CHANNEL_FILE = re.compile(rf"\A{_FILE_BODY}\Z", re.DOTALL)
_ATTACHMENT_ROW = re.compile(
    # ponytail: T03-M knows Photo; T03-A adds live-validated shapes.
    r"\A(?P<kind>Photo)\n"
    r"(?:(?:Received|Sent) at )?"
    rf"(?P<time>{_TIME_LABEL})"
    r"(?:, (?:Seen|Not seen))?\n?\Z"
)
_MESSAGE_ATTACHMENT_ROW = re.compile(
    r"\A(?:"
    r"(?P<photo>Photo)|"
    rf"(?P<video>{_MESSAGE_VIDEO})|"
    rf"(?P<file>{_FILE_BODY})"
    r")(?:\n(?!Received at |Sent at ).+)?\n(?:Received|Sent) at "
    rf"(?P<time>{_TIME_LABEL})"
    # Live video XML can append an empty reactions section to the timestamp.
    r"(?:, (?:Seen|Not seen))?(?:\n|Reactions: \n\n)\Z",
    re.DOTALL,
)
_DATE_CONTEXT = re.compile(
    r"\A(?:"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
    r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?) \d{1,2}(?:, \d{4})?|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Today|Yesterday"
    r")\Z"
)
_MEMBER_COUNT = re.compile(r"(?P<count>\d[\d,]*)\s+members?")
_CHROME = ("telegram", "chats", "contacts", "settings")
_DEFAULT_LIST_TITLES = {
    ("telegram",),
    ("waiting for network",),
    ("waiting for network...",),
    ("connecting...",),
}
_MEDIA_ROOTS = {
    "photo": ("/sdcard/Pictures/Telegram",),
    "video": (
        "/sdcard/Movies/Telegram",
        "/sdcard/Pictures/Telegram",
    ),
}


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split()).casefold()


def _own_labels(node):
    return tuple(
        dict.fromkeys(
            label
            for label in (_norm(node.get("text")), _norm(node.get("content-desc")))
            if label
        )
    )


def _rect(node):
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        return None
    rect = tuple(map(int, match.groups()))
    return rect if rect[0] < rect[2] and rect[1] < rect[3] else None


def _owned(node):
    return node.get("package") == PACKAGE


def _shown(node):
    return node.get("visible-to-user") == "true"


def _contains(outer, inner):
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def _intersects(first, second):
    return (
        first[0] < second[2]
        and first[2] > second[0]
        and first[1] < second[3]
        and first[3] > second[1]
    )


def _visible_row_rect(rect, viewport):
    if not _intersects(rect, viewport):
        return None
    visible = (
        max(rect[0], viewport[0]),
        max(rect[1], viewport[1]),
        min(rect[2], viewport[2]),
        min(rect[3], viewport[3]),
    )
    full_height = rect[3] - rect[1]
    minimum_height = max(44, min(96, int(full_height * 0.45)))
    return visible if visible[3] - visible[1] >= minimum_height else None


def _semantic_node_selector(node, selector_id):
    value = node.get("content-desc") or node.get("text")
    kind = (
        "content_description"
        if node.get("content-desc")
        else "text"
        if node.get("text")
        else None
    )
    class_name = node.get("class")
    if kind is None or not value or not class_name:
        return None
    return {
        "kind": kind,
        "owner_package": PACKAGE,
        "selector_id": selector_id,
        "value": value,
        "constraints": {"class_name": class_name},
    }


def _default_list(device, root):
    nodes = list(root.iter())
    parents = {child: parent for parent in nodes for child in parent}
    visible = [node for node in nodes if _owned(node) and _shown(node)]
    rects = [_rect(node) for node in visible]
    rects = [rect for rect in rects if rect is not None]
    if not rects:
        return None
    screen = max(rects, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    title_region = (
        screen[0],
        screen[1],
        screen[2],
        screen[1] + (screen[3] - screen[1]) * 0.15,
    )
    lists = [
        node
        for node in device.matching_elements(root, "telegram.list")
        if node.get("scrollable") == "true"
        and _rect(node) == screen
    ]
    navigation = {
        label: [
            node
            for node in visible
            if _own_labels(node) == (label,)
        ]
        for label in _CHROME[1:]
    }
    titles = [
        node
        for node in visible
        if _own_labels(node) in _DEFAULT_LIST_TITLES
        and _rect(node) is not None
        and _contains(title_region, _rect(node))
    ]
    searches = [
        node
        for node in visible
        if node.get("class") == "android.widget.EditText"
        and _own_labels(node) == ("search chats",)
        and _rect(node) is not None
        and _rect(node)[3] <= screen[1] + (screen[3] - screen[1]) * 0.2
    ]
    if (
        len(lists) != 1
        or any(len(matches) != 1 for matches in navigation.values())
        or len(titles) != 1
        and len(searches) != 1
    ):
        return None

    header = titles[0] if titles else searches[0]
    nav = [navigation[label][0] for label in _CHROME[1:]]
    title_bar = parents.get(header)
    nav_items = [parents.get(node) for node in nav]
    if title_bar is None or any(item is None for item in nav_items):
        return None
    title_bar_rect = _rect(title_bar)
    nav_rects = [_rect(item) for item in nav_items]
    key_rects = [_rect(header), *(_rect(node) for node in nav)]
    if (
        not _owned(title_bar)
        or not _shown(title_bar)
        or title_bar_rect is None
        or any(
            not _owned(item) or not _shown(item) or rect is None
            for item, rect in zip(nav_items, nav_rects, strict=True)
        )
        or any(rect is None or not _contains(screen, rect) for rect in key_rects)
        or title_bar_rect[2] - title_bar_rect[0] < (screen[2] - screen[0]) * 0.9
    ):
        return None
    viewport = (
        screen[0],
        int(max(title_bar_rect[3], title_region[3])),
        screen[2],
        min(rect[1] for rect in nav_rects),
    )
    if viewport[1] >= viewport[3]:
        return None
    sentinels = [
        node
        for node in device.matching_elements(
            root, "telegram.list.end.contacts"
        )
        if _owned(node)
        and _shown(node)
        and _rect(node) is not None
        and _intersects(_rect(node), viewport)
    ]
    if len(sentinels) > 1:
        return None
    return {
        "list": lists[0],
        "header": header,
        "bounds": screen,
        "viewport": viewport,
        "end_sentinel_top": (
            _rect(sentinels[0])[1] if sentinels else None
        ),
    }


def _establish_list_top(device, state, observation_id, root, page):
    selectors = (
        "telegram.list.top.title", "telegram.list.top.waiting",
        "telegram.list.top.waiting-ellipsis", "telegram.list.top.connecting",
    )
    try:
        matches = [node for selector in selectors
                   for node in device.matching_elements(root, selector)
                   if node is page["header"]]
        if len(matches) != 1:
            return observation_id, root, page, "chat_list_top_control_unavailable"
        action_id = _next_id(state, "actions", "action-chat-list-top")
        device.click_bounds(action_id, _rect(matches[0]), observation_id, anchor="center")
        observation_id, root, reason = _observe(device, state)
        page = _default_list(device, root) if root is not None else None
        return observation_id, root, page, reason or (None if page else "default_list_not_restored")
    except Exception:
        return observation_id, root, page, "chat_list_top_action_failed"


def _navigation_bounds(device, root, element_id):
    matches = [
        node
        for node in device.matching_elements(root, element_id)
        if _owned(node) and _shown(node)
    ]
    if len(matches) != 1:
        return None
    return _clickable_ancestor_bounds(root, matches[0])


def _account_profile_page(device, root):
    visible = [
        node for node in root.iter() if _owned(node) and _shown(node)
    ]
    rects = [_rect(node) for node in visible]
    rects = [rect for rect in rects if rect is not None]
    edits = list(
        device.matching_elements(root, "telegram.account.edit-info")
    )
    qr_codes = list(
        device.matching_elements(root, "telegram.account.qr-code")
    )
    profile_bounds = _navigation_bounds(
        device, root, "telegram.navigation.profile"
    )
    chats_bounds = _navigation_bounds(
        device, root, "telegram.navigation.chats"
    )
    if (
        not rects
        or len(edits) != 1
        or len(qr_codes) != 1
        or profile_bounds is None
        or chats_bounds is None
        or _rect(edits[0]) is None
        or _rect(qr_codes[0]) is None
    ):
        return None
    return {
        "root": root,
        "bounds": max(
            rects,
            key=lambda rect: (
                (rect[2] - rect[0]) * (rect[3] - rect[1])
            ),
        ),
        "edit_info": edits[0],
        "qr_code": qr_codes[0],
        "profile_bounds": profile_bounds,
        "chats_bounds": chats_bounds,
    }


def _parse_account_profile(page):
    edit_rect = _rect(page["edit_info"])
    qr_rect = _rect(page["qr_code"])
    if edit_rect is None or qr_rect is None:
        raise ValueError("account profile geometry is invalid")
    excluded = {
        "online",
        "public photo",
        "chats",
        "contacts",
        "settings",
        "profile",
    }
    names = [
        node
        for node in page["root"].iter()
        if _owned(node)
        and _shown(node)
        and node.get("class") == "android.widget.TextView"
        and _norm(node.get("text")) not in excluded
        and _norm(node.get("text"))
        and _rect(node) is not None
        and _rect(node)[1] >= qr_rect[3]
        and _rect(node)[3] <= edit_rect[1]
    ]
    if not names:
        raise ValueError("account display name is unavailable")
    name = max(
        names,
        key=lambda node: (
            _rect(node)[3] - _rect(node)[1],
            _rect(node)[2] - _rect(node)[0],
        ),
    ).get("text", "").strip()
    result = {"display_name": name}
    for key, prefix in (
        ("mobile", "mobile:"),
        ("username", "username:"),
    ):
        values = {
            raw.split(":", 1)[1].strip()
            for node in page["root"].iter()
            if _owned(node)
            and _shown(node)
            for raw in (
                node.get("content-desc", ""),
                node.get("text", ""),
            )
            if _norm(raw).startswith(prefix)
        }
        if len(values) > 1:
            raise ValueError(f"account {key} is ambiguous")
        if values:
            result[key] = values.pop()
    return result


def _acquire_account_profile(
    device,
    state,
    observation_id,
    root,
    outputs,
    target_ref,
):
    if hasattr(outputs, "begin_target"):
        outputs.begin_target("telegram.account")
    profile_bounds = _navigation_bounds(
        device, root, "telegram.navigation.profile"
    )
    if profile_bounds is None:
        return None, observation_id, root, _default_list(device, root), (
            "account_profile_entry_not_verified"
        )
    action_id = _next_id(state, "actions", "action-account-profile")
    try:
        device.click_bounds(
            action_id,
            profile_bounds,
            observation_id,
            anchor="center",
        )
    except Exception:
        return None, observation_id, root, _default_list(device, root), (
            "account_profile_entry_failed"
        )
    profile_observation_id, profile_root, reason = _observe(device, state)
    page = _account_profile_page(device, profile_root)
    record = None
    pending_reason = reason
    if pending_reason is None and page is None:
        pending_reason = "account_profile_not_verified"
    if pending_reason is None:
        try:
            profile = _parse_account_profile(page)
            evidence = _write_observation_pair(
                device,
                outputs,
                artifact_class="account",
                screen_artifact_id="account-screen",
                tree_artifact_id="account-tree",
                action_id=action_id,
                observation_id=profile_observation_id,
                comparison_ref="account-profile",
            )
            record = {
                "schema_version": "1.0",
                "record_kind": "account",
                "target_ref": target_ref,
                "source": {
                    "action_id": action_id,
                    "observation_id": profile_observation_id,
                    **evidence,
                },
                "profile": profile,
            }
            outputs.write_json(
                artifact_id="account",
                output_kind="ui_record",
                artifact_class="account",
                value=record,
                action_id=action_id,
                observation_id=profile_observation_id,
                comparison_ref="account-profile",
            )
            if hasattr(outputs, "context"):
                outputs.context.finish_identification("telegram.account", completion_condition="account_profile_verified")
        except Exception:
            pending_reason = "account_profile_output_failed"

    chats_bounds = _navigation_bounds(
        device, profile_root, "telegram.navigation.chats"
    )
    if chats_bounds is None:
        return record, profile_observation_id, profile_root, None, (
            pending_reason or "account_profile_return_not_verified"
        )
    return_action_id = _next_id(
        state, "actions", "action-account-profile-back"
    )
    try:
        device.click_bounds(
            return_action_id,
            chats_bounds,
            profile_observation_id,
            anchor="center",
        )
    except Exception:
        return record, profile_observation_id, profile_root, None, (
            pending_reason or "account_profile_return_failed"
        )
    observation_id, root, reason = _observe(device, state)
    page = _default_list(device, root)
    if reason is not None or page is None:
        return record, observation_id, root, page, (
            reason or "default_list_not_restored"
        )
    return record, observation_id, root, page, pending_reason


def _row_occurrences(page, page_number, selector_prefix="telegram.chat-row"):
    if page is None or type(page_number) is not int or page_number < 1:
        raise ValueError("list page is invalid")
    viewport = page["viewport"]
    result = {"eligible": [], "deferred": [], "excluded": []}

    for literal_slot, node in enumerate(list(page["list"])):
        if not _owned(node) or not _shown(node):
            continue
        if any(
            label == "archived chats" or label.startswith("archived chats.")
            for label in _own_labels(node)
        ):
            result["excluded"].append(
                {"literal_slot": literal_slot, "reason": "archive_surface"}
            )
            continue
        if node.get("focusable") != "true":
            result["excluded"].append(
                {"literal_slot": literal_slot, "reason": "non_focusable"}
            )
            continue
        rect = _rect(node)
        if rect is None:
            raise ValueError("candidate row geometry is invalid")
        if (
            page["end_sentinel_top"] is not None
            and rect[1] >= page["end_sentinel_top"]
        ):
            result["excluded"].append({
                "literal_slot": literal_slot,
                "reason": "contacts_section",
            })
            continue
        center_y = (rect[1] + rect[3]) / 2
        if not _intersects(rect, viewport):
            result["excluded"].append(
                {"literal_slot": literal_slot, "reason": "outside_viewport"}
            )
            continue
        selector_id = (
            f"{selector_prefix}.p{page_number:04d}.s{literal_slot:04d}"
        )
        visible_rect = _visible_row_rect(rect, viewport)
        item = {
            "page_number": page_number,
            "literal_slot": literal_slot,
            "rect": rect,
            "visible_rect": visible_rect,
            "labeled": bool(_own_labels(node)),
            "selector_id": selector_id,
        }
        if visible_rect is not None:
            result["eligible"].append(item)
        else:
            result["deferred"].append(
                {**item, "edge": "top" if center_y < viewport[1] else "bottom"}
            )

    return {key: tuple(value) for key, value in result.items()}


def _message_composers(device, root, screen):
    midpoint = screen[1] + (screen[3] - screen[1]) / 2
    lower = (screen[0], midpoint, screen[2], screen[3])
    matches = [
        node
        for node in device.matching_elements(root, "telegram.message.composer")
        if _rect(node) is not None
        and _contains(lower, _rect(node))
        and _rect(node)[2] - _rect(node)[0] >= (screen[2] - screen[0]) * 0.9
        and _rect(node)[3] - _rect(node)[1] <= (screen[3] - screen[1]) * 0.25
        and any(
            child is not node
            and _owned(child)
            and _shown(child)
            and child.get("focusable") == "true"
            and child.get("clickable") == "true"
            and _rect(child) is not None
            and _contains(_rect(node), _rect(child))
            for child in node.iter()
        )
    ]
    return [
        node
        for node in matches
        if not any(
            other is not node
            and _rect(other) != _rect(node)
            and _contains(_rect(other), _rect(node))
            for other in matches
        )
    ]


def _message_page(device, root):
    if root is None or _default_list(device, root) is not None:
        return None
    visible = [node for node in root.iter() if _owned(node) and _shown(node)]
    rects = [_rect(node) for node in visible]
    rects = [rect for rect in rects if rect is not None]
    if not rects:
        return None
    screen = max(rects, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    midpoint = screen[1] + (screen[3] - screen[1]) / 2
    upper = (screen[0], screen[1], screen[2], midpoint)
    lists = [
        node
        for node in device.matching_elements(root, "telegram.list")
        if _rect(node) == screen
    ]
    backs = [
        node
        for node in device.matching_elements(root, "telegram.navigation.back")
        if _rect(node) is not None and _contains(upper, _rect(node))
    ]
    composers = _message_composers(device, root, screen)
    if len(backs) != 1 or len(lists) != 1 or len(composers) != 1:
        return None
    back_rect = _rect(backs[0])
    texts = set(device.matching_elements(root, "telegram.text"))
    headers = [
        node
        for node in device.matching_elements(root, "telegram.message.info-header")
        if _rect(node) is not None
        and _contains(upper, _rect(node))
        and (_rect(node)[0] + _rect(node)[2]) / 2 >= back_rect[2]
        and _rect(node)[1] < back_rect[3]
        and _rect(node)[3] > back_rect[1]
        and any(
            child is not node
            and child in texts
            and bool(_norm(child.get("text")))
            and _rect(child) is not None
            and _contains(_rect(node), _rect(child))
            and _contains(upper, _rect(child))
            for child in node.iter()
        )
    ]
    headers = [
        node
        for node in headers
        if not any(
            other is not node
            and _rect(other) != _rect(node)
            and _contains(_rect(node), _rect(other))
            for other in headers
        )
    ]
    if len(headers) != 1:
        return None
    header_rect = _rect(headers[0])
    composer_rect = _rect(composers[0])
    viewport = (
        screen[0],
        max(back_rect[3], header_rect[3]),
        screen[2],
        composer_rect[1],
    )
    if viewport[1] >= viewport[3]:
        return None
    return {
        "back": backs[0],
        "info_header": headers[0],
        "list": lists[0],
        "bounds": screen,
        "viewport": viewport,
    }


def _message_display_name(device, page):
    if page is None:
        return None
    candidates = [
        node
        for node in device.matching_elements(
            page["info_header"], "telegram.text"
        )
        if bool(node.get("text", "").strip()) and _rect(node) is not None
    ]
    if not candidates:
        return None
    top = min(_rect(node)[1] for node in candidates)
    matches = [node for node in candidates if _rect(node)[1] == top]
    return matches[0].get("text", "").strip() if len(matches) == 1 else None


def _container_type(type_label):
    value = _norm(type_label)
    return value if value in {"direct", "group", "channel", "bot", "service"} else None


def _container_info_page(device, root):
    if root is None:
        return None
    visible = [node for node in root.iter() if _owned(node) and _shown(node)]
    rects = [_rect(node) for node in visible]
    rects = [rect for rect in rects if rect is not None]
    if not rects:
        return None
    screen = max(rects, key=lambda item: (item[2] - item[0]) * (item[3] - item[1]))
    midpoint = screen[1] + (screen[3] - screen[1]) / 2
    upper = (screen[0], screen[1], screen[2], midpoint)
    backs = [
        node
        for node in device.matching_elements(root, "telegram.navigation.back")
        if _rect(node) is not None and _contains(upper, _rect(node))
    ]
    if len(backs) != 1:
        return None
    back_rect = _rect(backs[0])
    lists = [
        node
        for node in device.matching_elements(root, "telegram.info.list")
        if _rect(node) is not None
        and _contains(screen, _rect(node))
        and _rect(node)[1] >= back_rect[3]
        and _rect(node)[2] - _rect(node)[0] >= (screen[2] - screen[0]) * 0.9
    ]
    scrollable_lists = [
        node for node in lists if node.get("scrollable") == "true"
    ]
    if scrollable_lists:
        lists = [
            node
            for node in scrollable_lists
            if not any(
                other is not node
                and _contains(_rect(other), _rect(node))
                for other in scrollable_lists
            )
        ]
    elif len(lists) != 1 or lists[0].get("scrollable") != "false":
        lists = []
    if len(lists) != 1:
        return None
    list_rect = _rect(lists[0])
    child_rects = [
        _rect(child) for child in lists[0] if _rect(child) is not None
    ]
    header_bottom = (
        min(rect[1] for rect in child_rects) if child_rects else list_rect[3]
    )
    header_region = (
        screen[0], back_rect[3], screen[2], header_bottom
    )
    header_labels = tuple(dict.fromkeys(
        node.get("text", "").strip()
        for node in device.matching_elements(root, "telegram.text")
        if node.get("text", "").strip()
        and _rect(node) is not None
        and _contains(header_region, _rect(node))
    ))
    list_labels = tuple(dict.fromkeys(
        value.strip()
        for child in lists[0]
        for value in (child.get("text", ""), child.get("content-desc", ""))
        if _norm(value)
    ))
    if not header_labels and not list_labels:
        return None
    cue_ids = []
    for element_id, region in (
        ("telegram.info.action.call", header_region),
        ("telegram.info.type.private-channel", header_region),
        ("telegram.info.type.public-channel", header_region),
        ("telegram.info.action.add-members", list_rect),
        ("telegram.info.action.channel-settings", list_rect),
        ("telegram.info.field.mobile", list_rect),
        ("telegram.info.label.subscribers", list_rect),
    ):
        matches = [
            node for node in device.matching_elements(root, element_id)
            if _rect(node) is not None and _contains(region, _rect(node))
        ]
        if len(matches) == 1:
            cue_ids.append(element_id)
    return {
        "back": backs[0],
        "list": lists[0],
        "labels": list_labels,
        "header_labels": header_labels,
        "list_labels": list_labels,
        "cue_ids": tuple(cue_ids),
        "bounds": screen,
        "viewport": list_rect,
    }


def _metadata_fingerprint(device, observed, container_type):
    member_labels = (
        sorted({
            _norm(label)
            for label in observed["member_labels"]
            if _norm(label)
        })
        if observed["members_complete"] else []
    )
    values = {
        "type_label": _norm(observed["type_label"]) or None,
        "username": _norm(observed["username"]) or None,
        "mobile": _norm(observed["mobile"]) or None,
        "member_labels": member_labels,
    }
    if values["username"] is None and values["mobile"] is None:
        values.update({
            "display_name": _norm(observed.get("display_name")) or None,
            "member_count": observed.get("member_count"),
            "subscriber_count": observed.get("subscriber_count"),
        })
    inputs = [
        key for key, value in values.items() if value not in (None, [], "")
    ]
    payload = {
        "method": "telegram-info-v1",
        "container_type": container_type,
        "fields": {key: values[key] for key in inputs},
    }
    return device.canonical_sha256(payload), inputs


def _logical_chatroom_id(device, target_ref, metadata):
    digest = device.canonical_sha256({
        "method": "telegram-chatroom-v1",
        "target_ref": target_ref,
        "container_type": metadata["container_type"],
        "metadata_fingerprint": metadata["fingerprint"],
    })
    return f"telegram-chat-{digest}"


def _chatroom_revisit_key(
    logical_chatroom_id,
    identity_status,
):
    # ponytail: exact same type/name/count stays ambiguous until Telegram
    # exposes a native identifier in this UI.
    if identity_status in {"identified", "ambiguous"}:
        return logical_chatroom_id
    return None


def _classify_container_type(page, observed):
    cues = set(page["cue_ids"])
    candidates = {}
    normalized_headers = {
        _norm(label) for label in observed["header_labels"]
    }
    if any("monthly users" in label for label in normalized_headers):
        candidates["bot"] = (
            "corroborated",
            ["header.monthly-users"],
        )
    if "service notifications" in normalized_headers:
        candidates["service"] = (
            "corroborated",
            ["header.service-notifications"],
        )
    explicit_type = _container_type(observed["type_label"])
    if explicit_type is not None:
        candidates[explicit_type] = ("explicit", ["list.type-label"])
    if "telegram.info.type.private-channel" in cues:
        candidates["channel"] = (
            "explicit", ["telegram.info.type.private-channel"]
        )
    elif "telegram.info.type.public-channel" in cues:
        candidates["channel"] = (
            "explicit", ["telegram.info.type.public-channel"]
        )
    elif {
        "telegram.info.label.subscribers",
        "telegram.info.action.channel-settings",
    } <= cues:
        candidates["channel"] = (
            "corroborated",
            [
                "telegram.info.action.channel-settings",
                "telegram.info.label.subscribers",
            ],
        )
    if {
        "telegram.info.action.call", "telegram.info.field.mobile"
    } <= cues and observed["mobile"] is not None:
        candidates["direct"] = (
            "corroborated",
            ["telegram.info.action.call", "telegram.info.field.mobile"],
        )
    if observed["member_count"] is not None and (
        "telegram.info.action.add-members" in cues
    ):
        candidates["group"] = (
            "corroborated",
            ["header.member-count", "telegram.info.action.add-members"],
        )
    if len(candidates) != 1:
        return None, "unresolved", []
    container_type, (status, basis) = candidates.popitem()
    return container_type, status, basis


def _parse_container_metadata(device, page, *, display_name=None):
    header_labels = list(page["header_labels"])
    labels = list(page["list_labels"])
    field_indexes = [
        index
        for index, label in enumerate(labels)
        for key, separator, _value in (label.partition(":"),)
        if separator and _norm(key) in {"bio", "mobile", "username"}
    ]
    prefix = labels[:2] if (
        len(labels) > 1 and _container_type(labels[1]) is not None
    ) else (
        labels[:min(field_indexes)]
        if field_indexes and min(field_indexes) >= 2 else []
    )
    display_name = prefix[0] if prefix else display_name
    type_label = prefix[1] if len(prefix) > 1 else None
    username = None
    mobile = None
    subscriber_count = None
    for label in labels:
        key, separator, value = label.partition(":")
        if separator and _norm(key) == "username":
            username = value.strip().removeprefix("@") or None
        elif separator and _norm(key) == "mobile":
            normalized = " ".join(value.split())
            mobile = (
                normalized
                if any(character.isdigit() for character in normalized)
                else None
            )
        elif separator and _norm(key) == "subscribers":
            normalized = value.strip().replace(",", "")
            subscriber_count = int(normalized) if normalized.isdigit() else None
    member_count = None
    for label in header_labels:
        match = _MEMBER_COUNT.fullmatch(_norm(label))
        if match is not None:
            member_count = int(match.group("count").replace(",", ""))
            break
    observed = {
        "display_name": display_name,
        "type_label": type_label,
        "username": username,
        "mobile": mobile,
        "member_labels": [],
        "members_complete": False,
        "header_labels": header_labels,
        "list_labels": labels,
        "member_count": member_count,
        "subscriber_count": subscriber_count,
    }
    container_type, type_status, type_basis = _classify_container_type(
        page, observed
    )
    fingerprint, inputs = _metadata_fingerprint(
        device, observed, container_type
    )
    stable = username is not None or mobile is not None
    if container_type in {"direct", "bot", "service"} and stable:
        identity_status = "identified"
    elif container_type in {"group", "channel"}:
        identity_status = "ambiguous"
    else:
        identity_status = "insufficient"
    return {
        "observed": observed,
        "container_type": container_type,
        "type_status": type_status,
        "type_basis": type_basis,
        "limitations": (
            ["container_type_unresolved"]
            if type_status == "unresolved" else []
        ),
        "fingerprint_method": "telegram-info-v1",
        "fingerprint_inputs": inputs,
        "fingerprint": fingerprint,
        "identity_status": identity_status,
    }


def _single_text_message(device, page):
    if page is None:
        return None, "message_list_not_verified"
    if page["list"].get("scrollable") != "false":
        return None, "history_boundary_not_established"
    candidates = []
    text_rows = set(
        device.matching_elements(page["list"], "telegram.message.text-row")
    )
    for literal_slot, node in enumerate(list(page["list"])):
        if not _owned(node) or not _shown(node):
            return None, "message_list_not_verified"
        rect = _rect(node)
        if rect is None:
            return None, "message_geometry_invalid"
        if node.get("focusable") == "true" and not _contains(
            page["viewport"], rect
        ):
            return None, "history_boundary_not_established"
        match = _MESSAGE_STATUS.fullmatch(node.get("text", ""))
        if (
            node in text_rows
            and match is not None
        ):
            candidates.append({
                "message_literal_slot": literal_slot,
                "rect": rect,
                "history_boundary": {
                    "latest": "established",
                    "earliest": "established",
                    "basis": "non_scrollable_complete_view",
                },
                "rendered": {
                    "direction": match.group(2).casefold(),
                    "text": match.group(1),
                    "time_label": match.group(3),
                },
            })
    if len(candidates) != 1:
        return None, "text_message_candidate_not_unique"
    return candidates[0], None


def _parse_message_row(raw, container_type, sender_context):
    if _DATE_CONTEXT.fullmatch(raw.strip()) is not None:
        return {
            "row_class": "date_context",
            "rendered": {},
            "time_label": None,
            "limitations": [],
        }, sender_context
    if container_type == "channel" and raw.strip() == "Channel created":
        return {
            "row_class": "system_context",
            "rendered": {},
            "time_label": None,
            "limitations": [],
        }, sender_context
    attachment = _ATTACHMENT_ROW.fullmatch(raw)
    if attachment is not None:
        return {
            "row_class": "attachment",
            "rendered": {"kind": attachment.group("kind").casefold()},
            "time_label": attachment.group("time"),
            "limitations": ["attachment_deferred_t03a"],
        }, sender_context
    message_attachment = (
        _MESSAGE_ATTACHMENT_ROW.fullmatch(raw)
        if container_type in {"direct", "group"} else None
    )
    if message_attachment is not None:
        return {
            "row_class": "attachment",
            "rendered": {
                "kind": (
                    "photo"
                    if message_attachment.group("photo")
                    else (
                        "video"
                        if message_attachment.group("video")
                        else "file"
                    )
                ),
            },
            "time_label": message_attachment.group("time"),
            "limitations": ["attachment_deferred_t03a"],
        }, sender_context
    channel = (
        _CHANNEL_STATUS.fullmatch(raw)
        if container_type == "channel" else None
    )
    if channel is not None:
        body = channel.group("body")
        time_label = channel.group("time")
        view_count = int(channel.group("views").replace(",", ""))
        if body.splitlines()[0] == "Photo":
            return {
                "row_class": "attachment",
                "rendered": {
                    "kind": "photo",
                    "view_count": view_count,
                },
                "time_label": time_label,
                "limitations": ["attachment_deferred_t03a"],
            }, sender_context
        video = _CHANNEL_VIDEO.fullmatch(body)
        if video is not None:
            return {
                "row_class": "attachment",
                "rendered": {
                    "duration_seconds": int(video.group("duration")),
                    "kind": "video",
                    "view_count": view_count,
                },
                "time_label": time_label,
                "limitations": ["attachment_deferred_t03a"],
            }, sender_context
        if _CHANNEL_FILE.fullmatch(body) is not None:
            return {
                "row_class": "attachment",
                "rendered": {
                    "kind": "file",
                    "view_count": view_count,
                },
                "time_label": time_label,
                "limitations": ["attachment_deferred_t03a"],
            }, sender_context
        return {
            "row_class": "message",
            "rendered": {
                "direction": "sent",
                "sender": None,
                "status": "not_seen",
                "text": body,
                "view_count": view_count,
            },
            "time_label": time_label,
            "limitations": [],
        }, sender_context
    match = (
        _MESSAGE_STATUS.fullmatch(raw)
        if container_type in {"direct", "bot", "service", "group"}
        else None
    )
    if match is None:
        return {
            "row_class": "raw",
            "rendered": {},
            "time_label": None,
            "limitations": ["message_shape_unclassified"],
        }, sender_context
    body = match.group(1)
    sender = None
    if container_type == "group":
        lines = body.split("\n")
        # ponytail: only the reviewed group shape; extend after a live fixture.
        if len(lines) > 1:
            sender_context, body = lines[0], "\n".join(lines[1:])
        sender = sender_context
    return {
        "row_class": "message",
        "rendered": {
            "direction": match.group(2).casefold(),
            "sender": sender,
            "status": (
                match.group("status").casefold().replace(" ", "_")
                if match.group("status") else None
            ),
            "text": body,
        },
        "time_label": match.group(3),
        "limitations": [],
    }, sender_context


def _message_window(
    page, container_type, *, action_id, observation_id, window_index, state
):
    if page is None:
        return None, [], "message_list_not_verified"
    candidates = []
    deferred = []
    for literal_slot, node in enumerate(list(page["list"])):
        if not _owned(node) or not _shown(node):
            return None, [], "message_list_not_verified"
        rect = _rect(node)
        if rect is None:
            return None, [], "message_geometry_invalid"
        if not _intersects(page["viewport"], rect):
            continue
        raw = node.get("text", "")
        source = {
            "action_id": action_id,
            "observation_id": observation_id,
            "window_index": window_index,
            "literal_slot": literal_slot,
            "bounds": list(rect),
            "viewport_visibility": (
                "full"
                if _contains(page["viewport"], rect)
                else "clipped"
            ),
        }
        if source["viewport_visibility"] == "clipped":
            if node.get("focusable") == "true":
                parsed, _ = _parse_message_row(
                    raw, container_type, None
                )
                action_bounds = _bottom_clipped_file_bounds(
                    page["viewport"],
                    rect,
                    parsed.get("rendered", {}).get("kind"),
                )
                if parsed["row_class"] not in {
                    "message", "date_context", "system_context",
                } and action_bounds is None:
                    deferred.append({
                        "edge": (
                            "both"
                            if (
                                rect[1] < page["viewport"][1]
                                and rect[3] > page["viewport"][3]
                            )
                            else (
                                "top"
                                if rect[1] < page["viewport"][1]
                                else "bottom"
                            )
                        ),
                        "reason": "row_clipped_by_viewport",
                        "raw": raw,
                        "source": source,
                    })
                    continue
                if action_bounds is not None:
                    source["action_bounds"] = list(action_bounds)
            else:
                continue
        if not raw and node.get("focusable") != "true":
            continue
        candidates.append((rect, literal_slot, raw, source, node))

    rows = []
    sender_context = None
    for rect, literal_slot, raw, source, node in sorted(
        candidates, key=lambda item: (item[0][1], item[0][3], item[1])
    ):
        if (
            container_type == "bot"
            and raw.strip()
            and node.get("class") == "android.view.View"
            and node.get("enabled") == "false"
            and node.get("focusable") == "false"
            and node.get("clickable") == "false"
        ):
            parsed = {
                "row_class": "bot_context",
                "rendered": {"kind": "bot_description"},
                "time_label": None,
                "limitations": [],
            }
        else:
            parsed, sender_context = _parse_message_row(
                raw, container_type, sender_context
            )
        rendered = parsed["rendered"]
        time_label = parsed["time_label"]
        rows.append({
            "row_class": parsed["row_class"],
            "raw": raw,
            "signature": (
                parsed["row_class"],
                raw,
                rendered.get("sender"),
                rendered.get("direction"),
                time_label,
                rendered.get("status"),
            ),
            "source": source,
            "rendered": rendered,
            "time_label": time_label,
            "date_marker_label": None,
            "temporal_precision": "minute" if time_label else None,
            "temporal_resolution": "unresolved",
            "resolved_datetime": None,
            "limitations": [
                *parsed["limitations"],
                *(
                    ["row_clipped_by_viewport"]
                    if source["viewport_visibility"] == "clipped"
                    else []
                ),
            ],
        })
    for row in reversed(rows):
        occurrence_ref = _next_id(
            state, "message_occurrences", "message-occurrence"
        )
        row["message_occurrence_ref"] = occurrence_ref
        row["sightings"] = [{
            "message_occurrence_ref": occurrence_ref,
            **row["source"],
        }]
    return rows, deferred, None


def _attachment_action_selector(page, row):
    if page is None or row.get("row_class") != "attachment":
        return None
    literal_slot = row.get("source", {}).get("literal_slot")
    children = list(page["list"])
    if (
        type(literal_slot) is not int
        or not 0 <= literal_slot < len(children)
    ):
        return None
    node = children[literal_slot]
    rect = tuple(row.get("source", {}).get("bounds", ()))
    if (
        len(rect) != 4
        or _rect(node) != rect
        or node.get("text", "") != row.get("raw")
    ):
        return None
    action_bounds = _bottom_clipped_file_bounds(
        page["viewport"],
        rect,
        row.get("rendered", {}).get("kind"),
    )
    if (
        not _contains(page["viewport"], rect)
        and tuple(row.get("source", {}).get("action_bounds", ()))
        != action_bounds
    ):
        return None
    return _semantic_node_selector(node, "telegram.attachment-row")


def _attachment_menu_bounds(row):
    source = row.get("source", {})
    rect = tuple(source.get("action_bounds", source.get("bounds", ())))
    if len(rect) != 4:
        return None
    left, top, right, bottom = rect
    width = right - left
    if width < 1:
        return None
    return right - max(1, width // 100), top, right, bottom


def _bottom_clipped_file_bounds(viewport, rect, kind):
    if (
        kind != "file"
        or rect[1] < viewport[1]
        or rect[3] <= viewport[3]
    ):
        return None
    return _visible_row_rect(rect, viewport)


def _attachment_menu_element(kind):
    return (
        "telegram.attachment.save-to-downloads"
        if kind == "file"
        else "telegram.attachment.save-to-gallery"
    )


def _attachment_loading_dialog(root):
    visible = [
        node for node in root.iter() if _owned(node) and _shown(node)
    ]
    loading = [
        node
        for node in visible
        if node.get("class") == "android.widget.TextView"
        and node.get("text") == "Loading..."
    ]
    percentages = [
        node
        for node in visible
        if node.get("class") == "android.widget.TextView"
        and re.fullmatch(r"\d{1,3}%", node.get("text", ""))
    ]
    progress_bars = [
        rect
        for node in visible
        if node.get("class") == "android.view.View"
        and (rect := _rect(node)) is not None
        and rect[2] - rect[0] >= 300
        and 1 <= rect[3] - rect[1] <= 24
    ]
    if len(loading) != 1 or len(percentages) != 1 or not progress_bars:
        return None
    return {"percent": int(percentages[0].get("text", "")[:-1])}


def _clickable_ancestor_bounds(root, node):
    parents = {child: parent for parent in root.iter() for child in parent}
    target = node
    while target is not None and target.get("clickable") != "true":
        target = parents.get(target)
    return (
        _rect(target)
        if target is not None and _owned(target) and _shown(target)
        else None
    )


def _snapshot_attachment_files(
    device, kind, action_id, observation_id, baseline=None
):
    if kind == "file":
        return device.snapshot_downloads(
            action_id, observation_id, baseline=baseline
        )
    return device.snapshot_external_files(
        action_id,
        observation_id,
        _MEDIA_ROOTS[kind],
        baseline=baseline,
    )


def _pull_attachment_file(
    device, kind, action_id, remote_path, observation_id
):
    if kind == "file":
        return device.pull_download(
            action_id, remote_path, observation_id
        )
    return device.pull_external_file(
        action_id,
        remote_path,
        observation_id,
        _MEDIA_ROOTS[kind],
    )


def _changed_downloads(before, after):
    previous = {path: (size, mtime) for path, size, mtime in before}
    result = []
    for path, size, mtime in after:
        old = previous.get(path)
        if old == (size, mtime):
            continue
        result.append({
            "device_path": path,
            "basename": path.rsplit("/", 1)[-1],
            "size": size,
            "mtime": mtime,
            "candidate_reason": (
                "new_path" if old is None else "modified_path"
            ),
        })
    return tuple(result)


def _history_window_signature(rows):
    return tuple(
        (
            row["signature"],
            row["source"]["literal_slot"],
            tuple(row["source"]["bounds"]),
        )
        for row in rows
    )


def _write_observation_pair(
    device,
    outputs,
    *,
    artifact_class,
    screen_artifact_id,
    tree_artifact_id,
    action_id,
    observation_id,
    comparison_ref,
):
    if hasattr(outputs, "write_observation_pair"):
        return outputs.write_observation_pair(
            artifact_class=artifact_class,
            screen_artifact_id=screen_artifact_id,
            tree_artifact_id=tree_artifact_id,
            action_id=action_id,
            observation_id=observation_id,
            comparison_ref=comparison_ref,
        )
    screen = outputs.write_bytes(
        artifact_id=screen_artifact_id,
        output_kind="screen_image",
        artifact_class=artifact_class,
        value=device.read_observation(observation_id, "screen_image"),
        action_id=action_id,
        observation_id=observation_id,
        comparison_ref=comparison_ref,
        suffix=".png",
    )
    tree = outputs.write_bytes(
        artifact_id=tree_artifact_id,
        output_kind="ui_hierarchy",
        artifact_class=artifact_class,
        value=device.read_observation(observation_id, "ui_tree"),
        action_id=action_id,
        observation_id=observation_id,
        comparison_ref=comparison_ref,
        suffix=".xml",
    )
    return {
        "screen_artifact_id": screen.artifact_id,
        "screen_path": screen.relative_path,
        "ui_tree_artifact_id": tree.artifact_id,
        "ui_tree_path": tree.relative_path,
    }


def _write_attachment_outputs(
    device,
    outputs,
    *,
    attachment_ref,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
    message_ref,
    row,
    outcome,
    limitation,
    before,
    after,
    candidates,
    original,
    decision_action_id,
    decision_observation_id,
    pull_action_id=None,
    menu_action_id=None,
    menu_observation_id=None,
    unavailable_action_id=None,
    unavailable_observation_id=None,
):
    ordinal = attachment_ref.removeprefix("attachment-")
    selected = candidates[0] if len(candidates) == 1 else None
    try:
        retained_basename = (
            normalize_retained_basename(selected["basename"])
            if selected is not None
            else None
        )
        if original is not None:
            basename = selected["basename"]
            suffix = (
                "." + basename.rsplit(".", 1)[-1].lower()
                if "." in basename
                else ""
            )
            if re.fullmatch(r"\.[a-z0-9]+", suffix or "") is None:
                suffix = ".bin"
            outputs.write_bytes(
                artifact_id=f"attachment-original-{ordinal}",
                output_kind="original_artifact",
                artifact_class="attachment",
                value=original,
                action_id=pull_action_id,
                observation_id=decision_observation_id,
                comparison_ref=attachment_ref,
                suffix=suffix,
                retained_basename=retained_basename,
            )
        decision_record = outputs.write_json(
            artifact_id=f"attachment-ui-{ordinal}",
            output_kind="ui_record",
            artifact_class="attachment",
            value={
                "schema_version": "1.0",
                "record_kind": "attachment",
                "target_ref": target_ref,
                "logical_chatroom_id": logical_chatroom_id,
                "container_ref": container_ref,
                "occurrence_ref": occurrence_ref,
                "message_ref": message_ref,
                "attachment_ref": attachment_ref,
                "source": row["source"],
                "raw": row["raw"],
                "rendered": row["rendered"],
                "time_label": row["time_label"],
                "route": "materialize",
                "outcome": outcome,
                "limitation": limitation,
                "selected_device_basename": (
                    selected["basename"] if selected else None
                ),
                "retained_basename": retained_basename,
            },
            action_id=decision_action_id,
            observation_id=decision_observation_id,
            comparison_ref=attachment_ref,
        )
        failure_evidence = None
        if menu_action_id is not None and menu_observation_id is not None:
            menu_evidence = _write_observation_pair(
                device,
                outputs,
                artifact_class="attachment",
                screen_artifact_id=(
                    f"attachment-menu-screen-{ordinal}"
                ),
                tree_artifact_id=f"attachment-menu-tree-{ordinal}",
                action_id=menu_action_id,
                observation_id=menu_observation_id,
                comparison_ref=attachment_ref,
            )
            if limitation == "attachment_materialize_save_action_unavailable":
                failure_evidence = menu_evidence
        if (
            unavailable_action_id is not None
            and unavailable_observation_id is not None
        ):
            failure_evidence = _write_observation_pair(
                device,
                outputs,
                artifact_class="attachment",
                screen_artifact_id=(
                    f"attachment-unavailable-screen-{ordinal}"
                ),
                tree_artifact_id=(
                    f"attachment-unavailable-tree-{ordinal}"
                ),
                action_id=unavailable_action_id,
                observation_id=unavailable_observation_id,
                comparison_ref=attachment_ref,
            )
        outputs.write_json(
            artifact_id=f"attachment-audit-{ordinal}",
            output_kind="audit_record",
            artifact_class="attachment",
            value={
                "schema_version": "1.0",
                "record_kind": "attachment_materialize_audit",
                "target_ref": target_ref,
                "logical_chatroom_id": logical_chatroom_id,
                "container_ref": container_ref,
                "occurrence_ref": occurrence_ref,
                "message_ref": message_ref,
                "attachment_ref": attachment_ref,
                "before_manifest_sha256": (
                    device.canonical_sha256(before)
                ),
                "before_count": len(before),
                "after_manifest_sha256": (
                    device.canonical_sha256(after) if after is not None else None
                ),
                "after_count": len(after) if after is not None else None,
                **({"after_inventory_status": "not_performed"} if after is None else {}),
                "candidate_count": len(candidates),
                "candidate_reason": (
                    selected["candidate_reason"] if selected else None
                ),
                "selected_device_path": (
                    selected["device_path"] if selected else None
                ),
                "materialized_file_retained": original is not None,
                "limitation": limitation,
            },
            action_id=decision_action_id,
            observation_id=decision_observation_id,
            comparison_ref=attachment_ref,
        )
        if hasattr(outputs, "finish_attachment"):
            outputs.finish_attachment(attachment_ref, limitation, evidence=failure_evidence or {
                "observation_id": decision_record.observation_id,
            })
    except Exception:
        return None, "attachment_output_failed"
    return {
        "attachment_ref": attachment_ref,
        "status": outcome,
        "limitation": limitation,
    }, None


def _write_preinventory_display_fallback(
    device,
    outputs,
    attachment_ref,
    row,
    root,
    observation_id,
    *,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
    message_ref,
):
    source_action_id = row["source"]["action_id"]
    source_observation_id = row["source"]["observation_id"]
    capture, reason = _write_attachment_outputs(
        device,
        outputs,
        attachment_ref=attachment_ref,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
        message_ref=message_ref,
        row=row,
        outcome="display_fallback",
        limitation="attachment_materialize_preinventory_failed",
        before=(),
        after=(),
        candidates=(),
        original=None,
        decision_action_id=source_action_id,
        decision_observation_id=source_observation_id,
    )
    return observation_id, root, capture, reason


def _observe_restored_attachment_window(
    device,
    state,
    transition_action_id,
    row,
    rows,
    container_type,
):
    observation_id, root, reason = _observe(device, state)
    if reason is not None:
        return observation_id, root, reason
    return observation_id, root, _restored_attachment_window_reason(
        device,
        root,
        transition_action_id,
        observation_id,
        row,
        rows,
        container_type,
    )


def _restored_attachment_window_reason(
    device,
    root,
    transition_action_id,
    observation_id,
    row,
    rows,
    container_type,
):
    page = _message_page(device, root)
    if page is None:
        return "attachment_message_page_not_restored"
    restored_rows, _, parse_reason = _message_window(
        page,
        container_type,
        action_id=transition_action_id,
        observation_id=observation_id,
        window_index=row["source"]["window_index"],
        state={"message_occurrences": 0},
    )
    if (
        parse_reason is not None
        or _history_window_signature(restored_rows)
        != _history_window_signature(rows)
    ):
        return "attachment_window_not_restored"
    return None


def _materialize_attachment(
    device,
    state,
    row,
    root,
    page,
    observation_id,
    rows,
    *,
    container_type,
    outputs,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
    message_ref,
):
    kind = row.get("rendered", {}).get("kind")
    if kind not in {"file", "photo", "video"}:
        return (
            observation_id,
            root,
            None,
            "attachment_kind_not_supported",
        )
    attachment_ref = _next_id(state, "attachments", "attachment")
    if hasattr(outputs, "begin_attachment"):
        outputs.begin_attachment(attachment_ref, row, container_ref=container_ref, occurrence_ref=occurrence_ref, message_ref=message_ref)
    if _attachment_action_selector(page, row) is None:
        return (
            observation_id,
            root,
            None,
            "attachment_action_not_verified",
        )

    source_observation_id = row["source"]["observation_id"]
    menu_element = _attachment_menu_element(kind)
    snapshot_prefix = (
        "action-snapshot-downloads"
        if kind == "file"
        else "action-snapshot-gallery"
    )
    save_prefix = (
        "action-save-to-downloads"
        if kind == "file"
        else "action-save-to-gallery"
    )
    pull_prefix = (
        "action-pull-download"
        if kind == "file"
        else "action-pull-external"
    )
    before_action_id = _next_id(state, "actions", snapshot_prefix)
    try:
        before = _snapshot_attachment_files(
            device, kind, before_action_id, source_observation_id
        )
    except Exception:
        return _write_preinventory_display_fallback(
            device,
            outputs,
            attachment_ref,
            row,
            root,
            observation_id,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            message_ref=message_ref,
        )

    open_action_id = _next_id(
        state, "actions", "action-attachment-open"
    )
    try:
        menu_bounds = _attachment_menu_bounds(row)
        if menu_bounds is None:
            raise ValueError
        device.click_bounds(
            open_action_id,
            menu_bounds,
            observation_id,
            anchor="center",
        )
    except Exception:
        return (
            observation_id,
            root,
            None,
            "attachment_semantic_action_unavailable",
        )

    opened_observation_id, opened_root, reason = _observe(device, state)
    if reason is not None:
        return opened_observation_id, opened_root, None, reason

    menu_observation_id = opened_observation_id
    menu_root = opened_root

    try:
        menu = device.matching_elements(menu_root, menu_element)
    except Exception:
        menu = ()
    save_bounds = (
        _clickable_ancestor_bounds(menu_root, menu[0])
        if len(menu) == 1 else None
    )
    if save_bounds is None:
        dismiss_action_id = _next_id(
            state, "actions", "action-attachment-menu-dismiss"
        )
        try:
            device.back(dismiss_action_id, menu_observation_id)
        except Exception:
            return (
                menu_observation_id,
                menu_root,
                None,
                "attachment_menu_dismiss_failed",
            )
        restored_observation_id, restored_root, reason = (
            _observe_restored_attachment_window(
                device,
                state,
                dismiss_action_id,
                row,
                rows,
                container_type,
            )
        )
        if reason is not None:
            return (
                restored_observation_id,
                restored_root,
                None,
                reason,
            )
        capture, output_reason = _write_attachment_outputs(
            device,
            outputs,
            attachment_ref=attachment_ref,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            message_ref=message_ref,
            row=row,
            outcome="display_fallback",
            limitation=(
                "attachment_materialize_save_action_unavailable"
            ),
            before=before,
            after=None,
            candidates=(),
            original=None,
            decision_action_id=open_action_id,
            decision_observation_id=menu_observation_id,
            menu_action_id=open_action_id,
            menu_observation_id=menu_observation_id,
        )
        return (
            restored_observation_id,
            restored_root,
            capture,
            output_reason,
        )

    save_action_id = _next_id(state, "actions", save_prefix)
    try:
        device.click_bounds(
            save_action_id,
            save_bounds,
            menu_observation_id,
            anchor="center",
        )
    except Exception:
        return (
            menu_observation_id,
            menu_root,
            None,
            "attachment_save_action_failed",
        )

    restored_observation_id, restored_root, reason = (
        _observe_restored_attachment_window(
            device,
            state,
            save_action_id,
            row,
            rows,
            container_type,
        )
    )
    loading = (
        _attachment_loading_dialog(restored_root)
        if reason == "attachment_message_page_not_restored"
        else None
    )
    after = None
    candidates = None
    limitation = None
    decision_action_id = None
    if loading is not None:
        loading_observation_id = restored_observation_id
        after_action_id = _next_id(state, "actions", snapshot_prefix)
        inventory_failed = False
        try:
            after = _snapshot_attachment_files(
                device,
                kind,
                after_action_id,
                loading_observation_id,
                baseline=before,
            )
        except Exception:
            after = before
            inventory_failed = True
        candidates = _changed_downloads(before, after)
        dismiss_action_id = _next_id(
            state, "actions", "action-attachment-loading-dismiss"
        )
        current_observation_id, current_root, observe_reason = _observe(
            device, state
        )
        if observe_reason is not None:
            return (
                current_observation_id,
                current_root,
                None,
                observe_reason,
            )
        reason = _restored_attachment_window_reason(
            device,
            current_root,
            dismiss_action_id,
            current_observation_id,
            row,
            rows,
            container_type,
        )
        if reason is None:
            restored_observation_id = current_observation_id
            restored_root = current_root
        elif _attachment_loading_dialog(current_root) is not None:
            try:
                device.back(dismiss_action_id, current_observation_id)
            except Exception:
                return (
                    current_observation_id,
                    current_root,
                    None,
                    "attachment_loading_dialog_dismiss_failed",
                )
            restored_observation_id, restored_root, reason = (
                _observe_restored_attachment_window(
                    device,
                    state,
                    dismiss_action_id,
                    row,
                    rows,
                    container_type,
                )
            )
        else:
            return (
                current_observation_id,
                current_root,
                None,
                reason,
            )
        if reason is not None:
            return restored_observation_id, restored_root, None, reason
        decision_action_id = (
            dismiss_action_id if inventory_failed else after_action_id
        )
        limitation = (
            "attachment_materialize_download_unavailable"
            if not candidates
            else "attachment_materialize_ambiguous_candidates"
            if len(candidates) > 1
            else None
        )
    if reason is not None:
        return (
            restored_observation_id,
            restored_root,
            None,
            reason,
        )

    if after is None:
        after_action_id = _next_id(state, "actions", snapshot_prefix)
        try:
            after = _snapshot_attachment_files(
                device,
                kind,
                after_action_id,
                restored_observation_id,
                baseline=before,
            )
        except Exception:
            after = before
            candidates = ()
            limitation = "attachment_materialize_inventory_failed"
            decision_action_id = save_action_id
        else:
            decision_action_id = after_action_id
            candidates = _changed_downloads(before, after)
            limitation = (
                "attachment_materialize_no_candidate"
                if not candidates
                else "attachment_materialize_ambiguous_candidates"
                if len(candidates) > 1
                else None
            )

    if limitation is not None:
        capture, output_reason = _write_attachment_outputs(
            device,
            outputs,
            attachment_ref=attachment_ref,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            message_ref=message_ref,
            row=row,
            outcome="display_fallback",
            limitation=limitation,
            before=before,
            after=after,
            candidates=candidates,
            original=None,
            decision_action_id=decision_action_id,
            decision_observation_id=restored_observation_id,
            menu_action_id=open_action_id,
            menu_observation_id=menu_observation_id,
            unavailable_action_id=(
                save_action_id if loading is not None else None
            ),
            unavailable_observation_id=(
                loading_observation_id if loading is not None else None
            ),
        )
        return (
            restored_observation_id,
            restored_root,
            capture,
            output_reason,
        )

    pull_action_id = _next_id(state, "actions", pull_prefix)
    try:
        original = _pull_attachment_file(
            device,
            kind,
            pull_action_id,
            candidates[0]["device_path"],
            restored_observation_id,
        )
    except Exception:
        capture, output_reason = _write_attachment_outputs(
            device,
            outputs,
            attachment_ref=attachment_ref,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            message_ref=message_ref,
            row=row,
            outcome="display_fallback",
            limitation="attachment_materialize_pull_failed",
            before=before,
            after=after,
            candidates=candidates,
            original=None,
            decision_action_id=after_action_id,
            decision_observation_id=restored_observation_id,
            menu_action_id=open_action_id,
            menu_observation_id=menu_observation_id,
        )
        return (
            restored_observation_id,
            restored_root,
            capture,
            output_reason,
        )

    def postpull_fallback(limitation, inventory, decision_action_id):
        capture, output_reason = _write_attachment_outputs(
            device,
            outputs,
            attachment_ref=attachment_ref,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            message_ref=message_ref,
            row=row,
            outcome="display_fallback",
            limitation=limitation,
            before=before,
            after=inventory,
            candidates=candidates,
            original=None,
            decision_action_id=decision_action_id,
            decision_observation_id=restored_observation_id,
            menu_action_id=open_action_id,
            menu_observation_id=menu_observation_id,
        )
        return (
            restored_observation_id,
            restored_root,
            capture,
            output_reason,
        )

    if len(original) != candidates[0]["size"]:
        return postpull_fallback(
            "attachment_materialize_size_mismatch",
            after,
            pull_action_id,
        )

    verify_action_id = _next_id(state, "actions", snapshot_prefix)
    try:
        final_inventory = _snapshot_attachment_files(
            device,
            kind,
            verify_action_id,
            restored_observation_id,
        )
    except Exception:
        return postpull_fallback(
            "attachment_materialize_inventory_failed",
            after,
            pull_action_id,
        )
    expected = (
        candidates[0]["device_path"],
        candidates[0]["size"],
        candidates[0]["mtime"],
    )
    if expected not in final_inventory:
        return postpull_fallback(
            "attachment_materialize_postpull_changed",
            final_inventory,
            verify_action_id,
        )

    capture, output_reason = _write_attachment_outputs(
        device,
        outputs,
        attachment_ref=attachment_ref,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
        message_ref=message_ref,
        row=row,
        outcome="materialized",
        limitation=None,
        before=before,
        after=final_inventory,
        candidates=candidates,
        original=original,
        decision_action_id=verify_action_id,
        decision_observation_id=restored_observation_id,
        pull_action_id=pull_action_id,
        menu_action_id=open_action_id,
        menu_observation_id=menu_observation_id,
    )
    return (
        restored_observation_id,
        restored_root,
        capture,
        output_reason,
    )


def _materialize_file_attachment(
    device,
    state,
    row,
    root,
    page,
    observation_id,
    rows,
    **kwargs,
):
    return _materialize_attachment(
        device,
        state,
        row,
        root,
        page,
        observation_id,
        rows,
        container_type="group",
        **kwargs,
    )


def _overlap_candidates(older, newer):
    # ponytail: UI windows are bounded; replace O(n²) only if measured.
    return [
        size
        for size in range(1, min(len(older), len(newer)) + 1)
        if [row["signature"] for row in older[-size:]]
        == [row["signature"] for row in newer[:size]]
    ]


def _merge_older_window(older, newer, clipped=()):
    candidates = _overlap_candidates(older, newer)
    if not candidates:
        anchors = [
            row
            for row in clipped
            if (
                row.get("edge") in {"bottom", "both"}
                and newer
                and row.get("raw") == newer[0].get("raw")
            )
        ]
        if len(anchors) == 1:
            return [*older, *newer], [], None
        return None, [], "message_window_continuity_not_established"
    if len(candidates) != 1:
        return [*older, *newer], ["message_revisit_ambiguity"], None
    size = candidates[0]
    for previous, current in zip(
        older[-size:], newer[:size], strict=True
    ):
        current["sightings"] = [
            *previous["sightings"], *current["sightings"]
        ]
    return [*older[:-size], *newer], [], None


def _enrich_temporal_context(rows, device_temporal_anchor=None):
    marker = None
    weekdays = {
        "monday", "tuesday", "wednesday", "thursday", "friday",
        "saturday", "sunday",
    }
    try:
        anchor = datetime.fromisoformat(device_temporal_anchor)
        if anchor.tzinfo is None:
            anchor = None
    except (TypeError, ValueError):
        anchor = None
    for row in rows:
        if row["row_class"] == "date_context":
            marker = row["raw"].strip()
            continue
        if row["row_class"] not in {
            "message", "attachment", "system_context",
        }:
            continue
        row["date_marker_label"] = marker
        if marker is None:
            continue
        normalized = _norm(marker)
        if normalized in weekdays:
            row["temporal_resolution"] = "unresolved"
            continue
        resolved_date = None
        if normalized in {"today", "yesterday"}:
            if anchor is None:
                row["temporal_resolution"] = "relative"
                continue
            resolved_date = (
                anchor - timedelta(days=normalized == "yesterday")
            ).date()
        elif re.search(r", \d{4}\Z", marker) is None:
            if anchor is None:
                row["temporal_resolution"] = "partial"
                continue
            for date_format in ("%Y %B %d", "%Y %b %d"):
                try:
                    resolved_date = datetime.strptime(
                        f"{anchor.year} {marker}", date_format
                    ).date()
                except ValueError:
                    continue
                break
        else:
            for date_format in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    resolved_date = datetime.strptime(
                        marker, date_format
                    ).date()
                except ValueError:
                    continue
                break
        if resolved_date is None or anchor is None or not row["time_label"]:
            continue
        try:
            parsed_time = datetime.strptime(
                row["time_label"],
                (
                    "%I:%M %p"
                    if row["time_label"].endswith((" AM", " PM"))
                    else "%H:%M"
                ),
            ).time()
        except ValueError:
            continue
        resolved = datetime.combine(
            resolved_date, parsed_time, tzinfo=anchor.tzinfo
        )
        row["temporal_resolution"] = "exact"
        row["resolved_datetime"] = resolved.isoformat(
            timespec="seconds"
        )
    return rows


def _next_id(state, key, prefix):
    state[key] += 1
    value = f"{prefix}-{state[key]:06d}"
    if key == "actions":
        state["last_action_id"] = value
    return value


def _observe(device, state):
    observation_id = _next_id(state, "observations", "observation")
    try:
        observed = device.observe(observation_id)
        if observed.get("observation_id") != observation_id:
            raise ValueError
        payload = device.read_observation(observation_id, "ui_tree")
        if not isinstance(payload, bytes):
            raise ValueError
        root = ET.fromstring(payload)
    except Exception:
        return None, None, "observation_failed"
    return observation_id, root, None


def _inspect_container_metadata(device, state, message_observation_id, root):
    message_page = _message_page(device, root)
    bounds = (
        None
        if message_page is None
        else _rect(message_page["info_header"])
    )
    display_name = _message_display_name(device, message_page)
    if bounds is None:
        return None, message_observation_id, root, "container_metadata_entry_failed"
    entry_action_id = _next_id(state, "actions", "action-container-info")
    try:
        device.click_bounds(
            entry_action_id,
            bounds,
            message_observation_id,
            anchor="center",
        )
    except Exception:
        return None, message_observation_id, root, "container_metadata_entry_failed"

    info_observation_id, info_root, reason = _observe(device, state)
    if reason is not None:
        return None, info_observation_id, info_root, reason
    page = _container_info_page(device, info_root)
    try:
        metadata = (
            _parse_container_metadata(
                device, page, display_name=display_name
            )
            if page is not None
            else None
        )
        pending_reason = (
            None if page is not None else "container_metadata_not_verified"
        )
    except Exception:
        metadata = None
        pending_reason = "container_metadata_parse_failed"
    return_action_id = _next_id(
        state, "actions", "action-container-info-back"
    )
    if metadata is not None:
        metadata["linkage"] = {
            "source_observation_id": message_observation_id,
            "entry_action_id": entry_action_id,
            "info_observation_id": info_observation_id,
            "return_action_id": return_action_id,
            "return_observation_id": None,
        }
    try:
        device.click(
            return_action_id,
            device.element_selector("telegram.navigation.back"),
            info_observation_id,
        )
    except Exception:
        return metadata, info_observation_id, info_root, (
            "container_metadata_return_failed"
        )
    return_observation_id, return_root, reason = _observe(device, state)
    if metadata is not None:
        metadata["linkage"]["return_observation_id"] = return_observation_id
    if reason is not None or _message_page(device, return_root) is None:
        return metadata, return_observation_id, return_root, (
            "container_metadata_return_failed"
        )
    return metadata, return_observation_id, return_root, pending_reason


def _write_single_text_message(
    device,
    outputs,
    *,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
    action_id,
    observation_id,
    root,
):
    linkage = (
        target_ref,
        logical_chatroom_id,
        container_ref,
        occurrence_ref,
        action_id,
        observation_id,
    )
    if not all(isinstance(value, str) and value for value in linkage):
        return None, "message_output_failed"
    item, reason = _single_text_message(device, _message_page(device, root))
    if reason is not None:
        return None, reason
    try:
        comparison_ref = "message-occurrence-000001"
        evidence = _write_observation_pair(
            device,
            outputs,
            artifact_class="message",
            screen_artifact_id="message-window-screen-000001",
            tree_artifact_id="message-window-tree-000001",
            action_id=action_id,
            observation_id=observation_id,
            comparison_ref=comparison_ref,
        )
        outputs.write_json(
            artifact_id="message-ui-000001",
            output_kind="ui_record",
            artifact_class="message",
            value={
                "schema_version": "1.0",
                "target_ref": target_ref,
                "logical_chatroom_id": logical_chatroom_id,
                "container_ref": container_ref,
                "occurrence_ref": occurrence_ref,
                "message_ref": comparison_ref,
                "source": {
                    "action_id": action_id,
                    "observation_id": observation_id,
                    "message_literal_slot": item["message_literal_slot"],
                    "bounds": list(item["rect"]),
                },
                "source_evidence": {
                    "message_window_ref": "message-window-000001",
                    **evidence,
                    "bounds": list(item["rect"]),
                },
                "history_boundary": item["history_boundary"],
                "rendered": item["rendered"],
            },
            action_id=action_id,
            observation_id=observation_id,
            comparison_ref=comparison_ref,
        )
    except Exception:
        return None, "message_output_failed"
    return {
        "message_ref": comparison_ref,
        "ui_artifact_id": "message-ui-000001",
        **evidence,
        "outcome": "acquired",
    }, None


def _write_chatroom_record(
    device,
    outputs,
    *,
    target_ref,
    logical_chatroom_id,
    display_name,
    container_ref,
    occurrence_ref,
    metadata,
    surface,
):
    linkage = metadata.get("linkage", {})
    try:
        evidence = _write_observation_pair(
            device,
            outputs,
            artifact_class="chatroom",
            screen_artifact_id="chatroom-screen",
            tree_artifact_id="chatroom-tree",
            action_id=linkage["entry_action_id"],
            observation_id=linkage["info_observation_id"],
            comparison_ref=logical_chatroom_id,
        )
        outputs.write_json(
            artifact_id="chatroom",
            output_kind="ui_record",
            artifact_class="chatroom",
            value={
                "schema_version": "1.0",
                "record_kind": "chatroom",
                "target_ref": target_ref,
                "logical_chatroom_id": logical_chatroom_id,
                "display_name": display_name,
                "container_ref": container_ref,
                "occurrence_ref": occurrence_ref,
                "surface": surface,
                "source": {
                    "action_id": linkage["entry_action_id"],
                    "observation_id": linkage["info_observation_id"],
                    **evidence,
                },
                "metadata": metadata,
            },
            action_id=linkage["entry_action_id"],
            observation_id=linkage["info_observation_id"],
            comparison_ref=logical_chatroom_id,
        )
    except Exception:
        return "chatroom_output_failed"
    return None


def _enter_occurrence(
    device,
    state,
    occurrence,
    source_observation_id,
    *,
    target_ref,
    return_checkpoint=_default_list,
    surface="default",
    outputs=None,
):
    click_id = _next_id(state, "actions", "action-click")
    try:
        device.click_bounds(
            click_id,
            occurrence["visible_rect"],
            source_observation_id,
            anchor="lower_third",
        )
    except Exception:
        return None, source_observation_id, None, "entry_action_failed"
    message_observation_id, root, reason = _observe(device, state)
    message_page = _message_page(device, root)
    if reason is not None or message_page is None:
        return None, message_observation_id, root, reason or "message_page_not_verified"
    display_name = _message_display_name(device, message_page)
    if display_name is None:
        return None, message_observation_id, root, "message_display_name_not_verified"

    occurrence_ref = _next_id(state, "occurrences", "occurrence")
    container_ref = _next_id(state, "containers", "container")
    metadata, current_observation_id, root, reason = (
        _inspect_container_metadata(
            device,
            state,
            message_observation_id,
            root,
        )
    )
    if reason is not None or metadata is None:
        return (
            None,
            current_observation_id,
            root,
            reason or "container_metadata_not_verified",
        )
    logical_chatroom_id = _logical_chatroom_id(
        device,
        target_ref,
        metadata,
    )
    capture = None
    if outputs is not None:
        try:
            chat_outputs = outputs.for_chatroom(
                logical_chatroom_id,
                display_name,
                container_ref=container_ref,
                identity_status=metadata["identity_status"],
            )
        except Exception:
            return None, current_observation_id, root, "chatroom_output_failed"
        reason = _write_chatroom_record(
            device,
            chat_outputs,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            display_name=display_name,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            metadata=metadata,
            surface=surface,
        )
        if reason is not None:
            return None, current_observation_id, root, reason
        capture, reason = _write_single_text_message(
            device,
            chat_outputs,
            target_ref=target_ref,
            logical_chatroom_id=logical_chatroom_id,
            container_ref=container_ref,
            occurrence_ref=occurrence_ref,
            action_id=click_id,
            observation_id=message_observation_id,
            root=root,
        )
        if reason is not None:
            return None, current_observation_id, root, reason
    back_id = _next_id(state, "actions", "action-back")
    try:
        device.click(
            back_id,
            device.element_selector("telegram.navigation.back"),
            current_observation_id,
        )
    except Exception:
        return None, current_observation_id, root, "return_action_failed"
    return_observation_id, return_root, reason = _observe(device, state)
    if reason is not None or return_checkpoint(device, return_root) is None:
        return (
            None,
            return_observation_id,
            return_root,
            reason or f"{surface}_list_not_restored",
        )
    record = {
        "occurrence_ref": occurrence_ref,
        "container_ref": container_ref,
        "page_number": occurrence["page_number"],
        "literal_slot": occurrence["literal_slot"],
        "selector_id": occurrence["selector_id"],
        "rect": occurrence["rect"],
        "visible_rect": occurrence["visible_rect"],
        "display_name": display_name,
        "logical_chatroom_id": logical_chatroom_id,
        "identity_method": "telegram-chatroom-v1",
        "identity_inputs": metadata["fingerprint_inputs"],
        "identity_status": metadata["identity_status"],
        "container_type": metadata["container_type"],
        "type_status": metadata["type_status"],
        "metadata_linkage": metadata["linkage"],
        "source_observation_id": source_observation_id,
        "entry_action_id": click_id,
        "message_observation_id": message_observation_id,
        "return_action_id": back_id,
        "return_observation_id": return_observation_id,
        "surface": surface,
        "outcome": "visited",
    }
    if capture is not None:
        record["message_capture"] = capture
    return record, return_observation_id, return_root, None


def _failed_report(target_ref, reason, state, containers, deferred, limitations):
    return {
        "status": "failed",
        "reason_code": reason,
        "target_ref": target_ref,
        "top_boundary": state["top_boundary"],
        "bottom_boundary": state["bottom_boundary"],
        "archive": state["archive"],
        "containers": tuple(containers),
        "deferred": tuple(deferred),
        "limitations": tuple(dict.fromkeys(limitations)),
        "scroll_probes": state["scrolls"],
    }


def _scroll(device, state, before_observation_id, direction):
    action_id = _next_id(state, "actions", "action-scroll")
    try:
        moved = device.scroll(
            action_id, before_observation_id, direction=direction
        )
    except Exception:
        return None, None, "list_scroll_failed"
    if type(moved) is not bool:
        return None, None, "list_scroll_result_invalid"
    state["scrolls"] += 1
    return action_id, moved, None


def _normalize_history_boundary(
    device, state, observation_id, root, direction
):
    if direction not in {"forward", "backward"}:
        return None, observation_id, root, "history_direction_invalid"
    page = _message_page(device, root)
    if page is None:
        return None, observation_id, root, "message_page_not_verified"
    boundary_reason = (
        "latest_boundary_not_established"
        if direction == "forward"
        else "earliest_boundary_not_established"
    )
    scrolls_before = state["scrolls"]
    go_to_bottom_action_ids = []
    go_to_bottom_succeeded = None
    retry_go_to_bottom = False

    def visible_go_to_bottom(current_root):
        return [
            node
            for node in device.matching_elements(
                current_root, "telegram.message.go-to-bottom"
            )
            if _owned(node)
            and _shown(node)
            and _rect(node) is not None
            and _contains(page["bounds"], _rect(node))
        ]

    if direction == "forward":
        matches = visible_go_to_bottom(root)
        if len(matches) == 1:
            action_id = _next_id(state, "actions", "action-go-to-bottom")
            go_to_bottom_action_ids.append(action_id)
            try:
                device.click(
                    action_id,
                    device.element_selector("telegram.message.go-to-bottom"),
                    observation_id,
                )
            except Exception:
                return None, observation_id, root, boundary_reason
            observation_id, root, reason = _observe(device, state)
            if reason is not None or _message_page(device, root) is None:
                return None, observation_id, root, (
                    reason or "message_page_not_verified"
                )
            remaining = visible_go_to_bottom(root)
            retry_go_to_bottom = len(remaining) == 1
            go_to_bottom_succeeded = not remaining
    stationary = []
    while len(stationary) < state["stagnation_rounds"]:
        action_id, moved, reason = _scroll(
            device, state, observation_id, direction
        )
        if reason is not None:
            return None, observation_id, root, reason
        observation_id, root, reason = _observe(device, state)
        if reason is not None or _message_page(device, root) is None:
            return None, observation_id, root, (
                reason or "message_page_not_verified"
            )
        if moved:
            stationary.clear()
            if retry_go_to_bottom:
                retry_go_to_bottom = False
                matches = visible_go_to_bottom(root)
                if len(matches) == 1:
                    action_id = _next_id(
                        state, "actions", "action-go-to-bottom"
                    )
                    go_to_bottom_action_ids.append(action_id)
                    try:
                        device.click(
                            action_id,
                            device.element_selector(
                                "telegram.message.go-to-bottom"
                            ),
                            observation_id,
                        )
                    except Exception:
                        pass
                    else:
                        observation_id, root, reason = _observe(device, state)
                        if (
                            reason is not None
                            or _message_page(device, root) is None
                        ):
                            return None, observation_id, root, (
                                reason or "message_page_not_verified"
                            )
                        go_to_bottom_succeeded = not visible_go_to_bottom(
                            root
                        )
        else:
            stationary.append((action_id, observation_id))
    return {
        "established": True,
        "direction": direction,
        "go_to_bottom_action_ids": go_to_bottom_action_ids,
        "go_to_bottom_succeeded": go_to_bottom_succeeded,
        "scroll_action_count": state["scrolls"] - scrolls_before,
        "stationary_action_ids": [item[0] for item in stationary],
        "stationary_observation_ids": [item[1] for item in stationary],
    }, observation_id, root, None


def _finalize_message_history(
    state,
    rows,
    deferred,
    limitations,
    *,
    latest,
    earliest,
    representative_action_id,
    representative_observation_id,
    reason_code=None,
):
    device_temporal_anchor = state.get("device_temporal_anchor")
    _enrich_temporal_context(rows, device_temporal_anchor)
    _register_acquired_rows(state, rows)
    for row in rows:
        limitations.extend(row["limitations"])
    significant = {
        "message_revisit_ambiguity",
        "message_shape_unclassified",
        "attachment_materialize_preinventory_failed",
        "attachment_materialize_inventory_failed",
        "attachment_materialize_no_candidate",
        "attachment_materialize_ambiguous_candidates",
        "attachment_materialize_pull_failed",
        "attachment_materialize_size_mismatch",
        "attachment_materialize_postpull_changed",
    }
    reason_code = reason_code or next(
        (item for item in limitations if item in significant), None
    )
    if reason_code is not None:
        limitations.append(reason_code)
    limitations = list(dict.fromkeys(limitations))
    messages = [row for row in rows if row["row_class"] == "message"]
    attachments = [
        row for row in rows if row["row_class"] == "attachment"
    ]
    return {
        "status": "partial" if reason_code else "complete",
        "reason_code": reason_code,
        "latest_boundary": latest,
        "earliest_boundary": earliest,
        "device_temporal_anchor": device_temporal_anchor,
        "rows": rows,
        "deferred": deferred,
        "limitations": limitations,
        "representative_action_id": representative_action_id,
        "representative_observation_id": representative_observation_id,
        "logical_message_count": len(messages),
        "message_occurrence_count": sum(
            len(row["sightings"]) for row in messages
        ),
        "attachment_count": len(attachments),
        "attachment_materialize_attempts": sum(
            row.get("attachment_outcome")
            in {"materialized", "display_fallback"}
            for row in attachments
        ),
        "attachment_materialize_actions": sum(
            row.get("attachment_outcome") == "materialized"
            for row in attachments
        ),
        "attachment_display_fallbacks": sum(
            row.get("attachment_outcome") == "display_fallback"
            for row in attachments
        ),
        "attachment_deferred_count": sum(
            "attachment_outcome" not in row for row in attachments
        ),
    }


def _dispatch_attachments(
    device,
    state,
    rows,
    root,
    page,
    observation_id,
    container_type,
    *,
    attachment_kinds=("file",),
    eligible_rows=None,
    outputs,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
):
    if (
        outputs is None
        or not isinstance(attachment_kinds, tuple)
        or len(set(attachment_kinds)) != len(attachment_kinds)
        or any(
            kind not in {"file", "photo", "video"}
            for kind in attachment_kinds
        )
    ):
        return observation_id, root, (
            None if outputs is None else "attachment_kind_not_supported"
        )
    selected = set(attachment_kinds)
    acquired = rows if eligible_rows is None else eligible_rows
    for row in reversed(acquired):
        kind = row.get("rendered", {}).get("kind")
        if (
            row.get("row_class") != "attachment"
            or kind not in selected
            or "attachment_outcome" in row
        ):
            continue
        if "message_ref" not in row:
            row["message_ref"] = _next_id(
                state, "messages", "message"
            )
        observation_id, root, outcome, reason = (
            _materialize_attachment(
                device,
                state,
                row,
                root,
                page,
                observation_id,
                rows,
                container_type=container_type,
                outputs=outputs,
                target_ref=target_ref,
                logical_chatroom_id=logical_chatroom_id,
                container_ref=container_ref,
                occurrence_ref=occurrence_ref,
                message_ref=row["message_ref"],
            )
        )
        if reason is not None:
            return observation_id, root, reason
        row["attachment_ref"] = outcome["attachment_ref"]
        row["attachment_outcome"] = outcome["status"]
        row["limitations"] = (
            [] if outcome["limitation"] is None
            else [outcome["limitation"]]
        )
    return observation_id, root, None


def _register_acquired_rows(state, rows):
    for row in reversed(rows):
        if (
            row["row_class"] in {"message", "attachment"}
            and "message_ref" not in row
        ):
            row["message_ref"] = _next_id(
                state, "messages", "message"
            )


def _acquire_message_history(
    device,
    state,
    observation_id,
    root,
    container_type,
    *,
    attachment_kinds=("file",),
    outputs=None,
    target_ref=None,
    logical_chatroom_id=None,
    container_ref=None,
    occurrence_ref=None,
):
    if hasattr(outputs, "begin_history"):
        outputs.begin_history()
    state["device_temporal_anchor"] = None
    read_device_time = getattr(device, "device_temporal_anchor", None)
    if callable(read_device_time):
        action_id = _next_id(
            state, "actions", "action-device-time"
        )
        try:
            state["device_temporal_anchor"] = read_device_time(
                action_id, observation_id
            )
        except Exception:
            pass
    latest, observation_id, root, reason = _normalize_history_boundary(
        device, state, observation_id, root, "forward"
    )
    if reason is not None:
        if latest is None:
            return None, observation_id, root, reason
        page = _message_page(device, root)
        rows, deferred, parse_reason = _message_window(
            page,
            container_type,
            action_id=latest["last_action_id"],
            observation_id=observation_id,
            window_index=0,
            state=state,
        )
        if parse_reason is not None:
            return None, observation_id, root, parse_reason
        _register_acquired_rows(state, rows)
        observation_id, root, dispatch_reason = (
            _dispatch_attachments(
                device,
                state,
                rows,
                root,
                page,
                observation_id,
                container_type,
                attachment_kinds=attachment_kinds,
                outputs=outputs,
                target_ref=target_ref,
                logical_chatroom_id=logical_chatroom_id,
                container_ref=container_ref,
                occurrence_ref=occurrence_ref,
            )
        )
        if dispatch_reason is not None:
            return None, observation_id, root, dispatch_reason
        return _finalize_message_history(
            state,
            rows,
            deferred,
            [],
            latest=latest,
            earliest=None,
            representative_action_id=latest["last_action_id"],
            representative_observation_id=observation_id,
            reason_code=reason,
        ), observation_id, root, None
    page = _message_page(device, root)
    rows, deferred, reason = _message_window(
        page,
        container_type,
        action_id=latest["stationary_action_ids"][-1],
        observation_id=observation_id,
        window_index=0,
        state=state,
    )
    if reason is not None:
        return None, observation_id, root, reason
    if not rows:
        return _finalize_message_history(
            state,
            [],
            deferred,
            [],
            latest=latest,
            earliest=None,
            representative_action_id=latest["stationary_action_ids"][-1],
            representative_observation_id=observation_id,
            reason_code="message_rows_not_verified",
        ), observation_id, root, None

    _register_acquired_rows(state, rows)
    observation_id, root, dispatch_reason = _dispatch_attachments(
        device,
        state,
        rows,
        root,
        page,
        observation_id,
        container_type,
        attachment_kinds=attachment_kinds,
        outputs=outputs,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
    )
    if dispatch_reason is not None:
        return None, observation_id, root, dispatch_reason
    page = _message_page(device, root)
    if page is None:
        return None, observation_id, root, "message_page_not_verified"

    history = rows
    limitations = []
    stationary = []
    pending_clipped_window = None
    window_index = 1
    while len(stationary) < state["stagnation_rounds"]:
        action_id, moved, reason = _scroll(
            device, state, observation_id, "backward"
        )
        if reason is not None:
            return None, observation_id, root, reason
        observation_id, root, reason = _observe(device, state)
        page = _message_page(device, root)
        if reason is not None or page is None:
            return None, observation_id, root, (
                reason or "message_page_not_verified"
            )
        if not moved:
            stationary.append((action_id, observation_id))
            continue
        stationary.clear()
        older, clipped, reason = _message_window(
            page,
            container_type,
            action_id=action_id,
            observation_id=observation_id,
            window_index=window_index,
            state=state,
        )
        if reason is not None:
            return None, observation_id, root, reason
        deferred.extend(clipped)
        previous_size = len(history)
        merged, new_limitations, reason = _merge_older_window(
            older, history, clipped
        )
        limitations.extend(new_limitations)
        if reason is not None:
            if not older and clipped:
                pending_clipped_window = (action_id, observation_id)
                window_index += 1
                continue
            failure_reason = (
                "message_rows_not_verified" if not older else reason
            )
            earliest = {
                "established": False,
                "direction": "backward",
                "reason_code": failure_reason,
                "last_action_id": action_id,
                "last_observation_id": observation_id,
            }
            return _finalize_message_history(
                state,
                history,
                deferred,
                limitations,
                latest=latest,
                earliest=earliest,
                representative_action_id=latest[
                    "stationary_action_ids"
                ][-1],
                representative_observation_id=latest[
                    "stationary_observation_ids"
                ][-1],
                reason_code=failure_reason,
            ), observation_id, root, None
        if "message_revisit_ambiguity" not in new_limitations:
            introduced_count = len(merged) - previous_size
            _register_acquired_rows(
                state, older[:introduced_count]
            )
            observation_id, root, dispatch_reason = (
                _dispatch_attachments(
                    device,
                    state,
                    older,
                    root,
                    page,
                    observation_id,
                    container_type,
                    attachment_kinds=attachment_kinds,
                    eligible_rows=older[:introduced_count],
                    outputs=outputs,
                    target_ref=target_ref,
                    logical_chatroom_id=logical_chatroom_id,
                    container_ref=container_ref,
                    occurrence_ref=occurrence_ref,
                )
            )
            if dispatch_reason is not None:
                return None, observation_id, root, dispatch_reason
            page = _message_page(device, root)
            if page is None:
                return (
                    None,
                    observation_id,
                    root,
                    "message_page_not_verified",
                )
        history = merged
        pending_clipped_window = None
        window_index += 1

    reason_code = None
    if pending_clipped_window is None:
        earliest = {
            "established": True,
            "direction": "backward",
            "stationary_action_ids": [item[0] for item in stationary],
            "stationary_observation_ids": [item[1] for item in stationary],
        }
    else:
        reason_code = "message_rows_not_verified"
        earliest = {
            "established": False,
            "direction": "backward",
            "reason_code": reason_code,
            "last_action_id": pending_clipped_window[0],
            "last_observation_id": pending_clipped_window[1],
        }
    return _finalize_message_history(
        state,
        history,
        deferred,
        limitations,
        latest=latest,
        earliest=earliest,
        representative_action_id=latest["stationary_action_ids"][-1],
        representative_observation_id=latest[
            "stationary_observation_ids"
        ][-1],
        reason_code=reason_code,
    ), observation_id, root, None


def _conversation_text(rows):
    lines = []
    for row in rows:
        row_class = row.get("row_class")
        if row_class not in {
            "message", "attachment", "system_context", "bot_context",
        }:
            continue
        if row_class == "bot_context":
            lines.append(
                f"[context] bot-description: {row['raw'].strip()}"
            )
            continue
        rendered = row.get("rendered", {})
        timestamp = (
            row.get("resolved_datetime")
            or " ".join(
                value
                for value in (
                    row.get("date_marker_label"),
                    row.get("time_label"),
                )
                if value
            )
            or "time unresolved"
        )
        if row_class == "system_context":
            actor, body = "system", row["raw"].strip()
        else:
            actor = (
                rendered.get("sender")
                or rendered.get("direction")
                or "message"
            )
            body = rendered.get("text")
        if not body:
            body = f"[{rendered.get('kind') or row['row_class']}]"
        if row.get("attachment_ref"):
            body += f" ({row['attachment_ref']})"
        lines.append(f"[{timestamp}] {actor}: {body}")
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _write_message_history(
    device,
    outputs,
    *,
    target_ref,
    logical_chatroom_id,
    container_ref,
    occurrence_ref,
    metadata,
    history,
):
    linkage = (
        target_ref,
        logical_chatroom_id,
        container_ref,
        occurrence_ref,
    )
    if not all(isinstance(value, str) and value for value in linkage):
        return None, "message_output_failed"
    action_id = history["representative_action_id"]
    observation_id = history["representative_observation_id"]
    container_ordinal = container_ref.removeprefix("container-")
    comparison_ref = f"message-history-{container_ordinal}"
    context_artifact_id = f"container-history-context-{container_ordinal}"
    messages = [
        row for row in history["rows"] if row["row_class"] == "message"
    ]
    context_rows = [
        row for row in history["rows"] if row["row_class"] != "message"
    ]
    try:
        outputs.write_json(
            artifact_id=context_artifact_id,
            output_kind="ui_record",
            artifact_class="message",
            value={
                "schema_version": "1.0",
                "record_kind": "container_history_context",
                "target_ref": target_ref,
                "logical_chatroom_id": logical_chatroom_id,
                "container_ref": container_ref,
                "occurrence_ref": occurrence_ref,
                "metadata": metadata,
                "latest_boundary": history["latest_boundary"],
                "earliest_boundary": history["earliest_boundary"],
                "device_temporal_anchor": history.get(
                    "device_temporal_anchor"
                ),
                "limitations": history["limitations"],
                "deferred": history["deferred"],
                "context_rows": context_rows,
                "ordered_message_refs": [
                    row["message_ref"] for row in messages
                ],
            },
            action_id=action_id,
            observation_id=observation_id,
            comparison_ref=comparison_ref,
        )
        message_artifact_ids = []
        windows = {}
        for row in messages:
            source = row["sightings"][0]
            source_observation_id = source["observation_id"]
            if source_observation_id in windows:
                continue
            window_ordinal = f"{len(windows) + 1:06d}"
            evidence = _write_observation_pair(
                device,
                outputs,
                artifact_class="message",
                screen_artifact_id=(
                    f"message-window-screen-{window_ordinal}"
                ),
                tree_artifact_id=f"message-window-tree-{window_ordinal}",
                action_id=source["action_id"],
                observation_id=source_observation_id,
                comparison_ref=comparison_ref,
            )
            windows[source_observation_id] = {
                "message_window_ref": (
                    f"message-window-{window_ordinal}"
                ),
                **evidence,
                "source_snapshot_id": f"telegram-snapshot:{source_observation_id}",
            }
        for row in messages:
            message_ordinal = row["message_ref"].removeprefix("message-")
            artifact_id = f"message-ui-{message_ordinal}"
            source = row["sightings"][0]
            outputs.write_json(
                artifact_id=artifact_id,
                output_kind="ui_record",
                artifact_class="message",
                value={
                    "schema_version": "1.0",
                    "record_kind": "message",
                    "target_ref": target_ref,
                    "logical_chatroom_id": logical_chatroom_id,
                    "container_ref": container_ref,
                    "occurrence_ref": occurrence_ref,
                    "message_ref": row["message_ref"],
                    "sightings": row["sightings"],
                    "raw": row["raw"],
                    "rendered": row["rendered"],
                    "time_label": row["time_label"],
                    "date_marker_label": row["date_marker_label"],
                    "temporal_precision": row["temporal_precision"],
                    "temporal_resolution": row["temporal_resolution"],
                    "resolved_datetime": row["resolved_datetime"],
                    "limitations": row["limitations"],
                    "source_evidence": {
                        **windows[source["observation_id"]],
                        "bounds": list(source["bounds"]),
                    },
                },
                action_id=source["action_id"],
                observation_id=source["observation_id"],
                comparison_ref=comparison_ref,
            )
            message_artifact_ids.append(artifact_id)
        outputs.write_bytes(
            artifact_id=f"conversation-{container_ordinal}",
            output_kind="derived_review",
            artifact_class="message",
            value=_conversation_text(history["rows"]),
            action_id=action_id,
            observation_id=observation_id,
            comparison_ref=comparison_ref,
            suffix=".txt",
        )
        boundary_artifact_ids = {}
        for name in ("latest", "earliest"):
            boundary = history[f"{name}_boundary"]
            if not boundary or boundary.get("established") is not True:
                continue
            boundary_action_id = boundary["stationary_action_ids"][-1]
            boundary_observation_id = boundary[
                "stationary_observation_ids"
            ][-1]
            boundary_screen_id = (
                f"message-history-{name}-screen-{container_ordinal}"
            )
            boundary_tree_id = (
                f"message-history-{name}-tree-{container_ordinal}"
            )
            evidence = _write_observation_pair(
                device,
                outputs,
                artifact_class="message",
                screen_artifact_id=boundary_screen_id,
                tree_artifact_id=boundary_tree_id,
                action_id=boundary_action_id,
                observation_id=boundary_observation_id,
                comparison_ref=comparison_ref,
            )
            boundary_artifact_ids[name] = evidence
    except Exception:
        return None, "message_output_failed"
    return {
        "comparison_ref": comparison_ref,
        "context_artifact_id": context_artifact_id,
        "message_artifact_ids": message_artifact_ids,
        "boundary_artifact_ids": boundary_artifact_ids,
    }, None


def _acquire_scrollable_occurrence(
    device,
    state,
    occurrence,
    source_observation_id,
    *,
    attachment_kinds=("file",),
    outputs,
    target_ref,
    return_checkpoint=_default_list,
    surface="default",
):
    click_id = _next_id(state, "actions", "action-click")
    try:
        device.click_bounds(
            click_id,
            occurrence["visible_rect"],
            source_observation_id,
            anchor="lower_third",
        )
    except Exception:
        return None, source_observation_id, None, "entry_action_failed"
    observation_id, root, reason = _observe(device, state)
    message_page = _message_page(device, root)
    if reason is not None or message_page is None:
        return None, observation_id, root, reason or "message_page_not_verified"
    display_name = _message_display_name(device, message_page)
    if display_name is None:
        return None, observation_id, root, "message_display_name_not_verified"

    occurrence_ref = _next_id(state, "occurrences", "occurrence")
    container_ref = _next_id(state, "containers", "container")
    metadata, observation_id, root, reason = _inspect_container_metadata(
        device, state, observation_id, root
    )
    if reason is not None or metadata is None:
        return None, observation_id, root, (
            reason or "container_metadata_not_verified"
        )
    logical_chatroom_id = _logical_chatroom_id(
        device, target_ref, metadata
    )
    scope_key = _chatroom_revisit_key(
        logical_chatroom_id,
        metadata["identity_status"],
    )
    seen_chatrooms = state.setdefault("seen_chatrooms", set())
    if scope_key is not None and scope_key in seen_chatrooms:
        back_id = _next_id(state, "actions", "action-back")
        try:
            device.click(
                back_id,
                device.element_selector("telegram.navigation.back"),
                observation_id,
            )
        except Exception:
            return None, observation_id, root, "return_action_failed"
        return_observation_id, return_root, reason = _observe(device, state)
        if reason is not None or return_checkpoint(device, return_root) is None:
            return None, return_observation_id, return_root, (
                reason or f"{surface}_list_not_restored"
            )
        return {
            "revisit": True,
            "logical_chatroom_id": logical_chatroom_id,
            "display_name": display_name,
        }, return_observation_id, return_root, None
    try:
        chat_outputs = outputs.for_chatroom(
            logical_chatroom_id,
            display_name,
            container_ref=container_ref,
            identity_status=metadata["identity_status"],
        )
    except Exception:
        return None, observation_id, root, "chatroom_output_failed"
    reason = _write_chatroom_record(
        device,
        chat_outputs,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        display_name=display_name,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
        metadata=metadata,
        surface=surface,
    )
    if reason is not None:
        return None, observation_id, root, reason
    if scope_key is not None:
        seen_chatrooms.add(scope_key)
    else:
        ambiguous = state.setdefault("seen_ambiguous_chatrooms", set())
        if logical_chatroom_id in ambiguous:
            state["container_revisit_ambiguity"] = True
        ambiguous.add(logical_chatroom_id)
    history, observation_id, root, reason = _acquire_message_history(
        device,
        state,
        observation_id,
        root,
        metadata["container_type"],
        attachment_kinds=attachment_kinds,
        outputs=chat_outputs,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
    )
    if reason is not None or history is None:
        return None, observation_id, root, reason or "message_history_failed"
    capture, reason = _write_message_history(
        device,
        chat_outputs,
        target_ref=target_ref,
        logical_chatroom_id=logical_chatroom_id,
        container_ref=container_ref,
        occurrence_ref=occurrence_ref,
        metadata=metadata,
        history=history,
    )
    if reason is not None:
        return None, observation_id, root, reason

    back_id = _next_id(state, "actions", "action-back")
    try:
        device.click(
            back_id,
            device.element_selector("telegram.navigation.back"),
            observation_id,
        )
    except Exception:
        return None, observation_id, root, "return_action_failed"
    return_observation_id, return_root, reason = _observe(device, state)
    if reason is not None or return_checkpoint(device, return_root) is None:
        return None, return_observation_id, return_root, (
            reason or f"{surface}_list_not_restored"
        )
    return {
        "occurrence_ref": occurrence_ref,
        "container_ref": container_ref,
        "page_number": occurrence["page_number"],
        "literal_slot": occurrence["literal_slot"],
        "selector_id": occurrence["selector_id"],
        "rect": occurrence["rect"],
        "visible_rect": occurrence["visible_rect"],
        "display_name": display_name,
        "logical_chatroom_id": logical_chatroom_id,
        "identity_method": "telegram-chatroom-v1",
        "identity_inputs": metadata["fingerprint_inputs"],
        "identity_status": metadata["identity_status"],
        "container_type": metadata["container_type"],
        "type_status": metadata["type_status"],
        "metadata_linkage": metadata["linkage"],
        "source_observation_id": source_observation_id,
        "entry_action_id": click_id,
        "history": {
            key: history[key]
            for key in (
                "status",
                "reason_code",
                "latest_boundary",
                "earliest_boundary",
                "limitations",
                "logical_message_count",
                "message_occurrence_count",
                "attachment_count",
                "attachment_materialize_attempts",
                "attachment_materialize_actions",
                "attachment_display_fallbacks",
                "attachment_deferred_count",
            )
        },
        "capture": capture,
        "return_action_id": back_id,
        "return_observation_id": return_observation_id,
        "surface": surface,
        "outcome": history["status"],
    }, return_observation_id, return_root, None


def _geometry(occurrences):
    items = (*occurrences["eligible"], *occurrences["deferred"])
    if not items:
        return ()
    top = min(item["rect"][1] for item in items)
    return tuple(
        (
            item["literal_slot"],
            (
                item["rect"][0],
                item["rect"][1] - top,
                item["rect"][2],
                item["rect"][3] - top,
            ),
        )
        for item in items
    )


def _safe_occurrences(page, page_number, selector_prefix="telegram.chat-row"):
    try:
        return _row_occurrences(page, page_number, selector_prefix), None
    except ValueError:
        return None, "candidate_geometry_invalid"


def _exact_display_name_occurrence(page, page_number, display_name):
    target = _norm(display_name)
    if page is None or not target:
        return None, "target_container_not_found"
    occurrences = _row_occurrences(page, page_number)
    children = list(page["list"])
    matches = []
    for item in occurrences["eligible"]:
        labels = set()
        for node in children[item["literal_slot"]].iter():
            for field in ("text", "content-desc"):
                value = node.get(field) or ""
                if _norm(value):
                    labels.add(_norm(value))
                if field == "content-desc":
                    labels.update(
                        _norm(part)
                        for part in value.split(". ")
                        if _norm(part)
                    )
        if target in labels:
            matches.append(item)
    if not matches:
        return None, "target_container_not_found"
    if len(matches) != 1:
        return None, "target_container_ambiguous"
    return matches[0], None


def _enumerate_containers(
    device, target_ref, outputs=None, attachment_kinds=("file",)
):
    state = {
        "stagnation_rounds": device.stagnation_rounds,
        "actions": 0,
        "observations": 0,
        "containers": 0,
        "occurrences": 0,
        "scrolls": 0,
        "message_occurrences": 0,
        "messages": 0,
        "attachments": 0,
        "top_boundary": {"established": False},
        "bottom_boundary": {"established": False},
        "archive": {
            "status": "out_of_scope",
            "reason_code": "archived_chatrooms_not_collected",
            "top_boundary": {"established": False},
            "bottom_boundary": {"established": False},
            "occurrence_count": 0,
        },
        "seen_chatrooms": set(),
        "seen_ambiguous_chatrooms": set(),
        "container_revisit_ambiguity": False,
    }
    containers = []
    deferred = []
    limitations = []

    observation_id, root, reason = _observe(device, state)
    page = _default_list(device, root)
    if reason is not None or page is None:
        return _failed_report(
            target_ref, reason or "default_list_not_verified",
            state, containers, deferred, limitations,
        )
    if outputs is not None:
        _, observation_id, root, page, account_reason = (
            _acquire_account_profile(
                device,
                state,
                observation_id,
                root,
                outputs,
                target_ref,
            )
        )
        if account_reason is not None:
            limitations.append(account_reason)
        if page is None:
            return _failed_report(
                target_ref,
                account_reason or "default_list_not_restored",
                state,
                containers,
                deferred,
                limitations,
            )

    observation_id, root, page, reason = _establish_list_top(
        device, state, observation_id, root, page
    )
    if reason is not None:
        return _failed_report(target_ref, reason, state, containers, deferred, limitations)
    state["top_boundary"] = {
        "established": True,
        "observation_id": observation_id,
    }

    if hasattr(outputs, "begin_target"):
        outputs.begin_target("telegram.conversations")
        outputs.begin_target("telegram.attachments")

    if any(item.get("outcome") == "partial" for item in containers):
        limitations.append("container_history_partial")
    if state["container_revisit_ambiguity"]:
        limitations.append("container_revisit_ambiguity")

    page_number = 1
    while True:
        occurrences, reason = _safe_occurrences(page, page_number)
        if reason is not None:
            return _failed_report(
                target_ref, reason, state, containers, deferred, limitations
            )
        baseline = _geometry(occurrences)
        deferred = [
            {
                "surface": "default",
                "page_number": item["page_number"],
                "literal_slot": item["literal_slot"],
                "source_observation_id": observation_id,
                "edge": item["edge"],
                "outcome": "partial_row_unresolved",
            }
            for item in occurrences["deferred"]
            if page_number == 1 or item["edge"] != "top"
        ]

        for expected in occurrences["eligible"]:
            current, reason = _safe_occurrences(page, page_number)
            if reason is not None:
                return _failed_report(
                    target_ref, reason, state, containers, deferred, limitations
                )
            if _geometry(current) != baseline:
                return _failed_report(
                    target_ref, "list_changed_after_return",
                    state, containers, deferred, limitations,
                )
            item = next(
                (
                    candidate
                    for candidate in current["eligible"]
                    if candidate["literal_slot"] == expected["literal_slot"]
                ),
                None,
            )
            if item is None:
                return _failed_report(
                    target_ref, "list_changed_after_return",
                    state, containers, deferred, limitations,
                )
            if outputs is None:
                record, observation_id, root, reason = _enter_occurrence(
                    device,
                    state,
                    item,
                    observation_id,
                    target_ref=target_ref,
                )
            else:
                record, observation_id, root, reason = (
                    _acquire_scrollable_occurrence(
                        device,
                        state,
                        item,
                        observation_id,
                        outputs=outputs,
                        target_ref=target_ref,
                        return_checkpoint=_default_list,
                        surface="default",
                        attachment_kinds=attachment_kinds,
                    )
                )
            if reason is not None:
                return _failed_report(
                    target_ref, reason, state, containers, deferred, limitations
                )
            if not record.get("revisit"):
                containers.append(record)
                if record.get("outcome") == "partial":
                    limitations.append("container_history_partial")
            if state["container_revisit_ambiguity"]:
                limitations.append("container_revisit_ambiguity")
            page = _default_list(device, root)
            current, reason = _safe_occurrences(page, page_number)
            if page is None or reason is not None or _geometry(current) != baseline:
                return _failed_report(
                    target_ref, "list_changed_after_return",
                    state, containers, deferred, limitations,
                )

        if page["end_sentinel_top"] is not None:
            last_action_id = state.get("last_action_id")
            state["bottom_boundary"] = {
                "established": True,
                "observation_id": observation_id,
                "reason_code": "contacts_section_reached",
                "stationary_action_ids": (
                    [last_action_id] if last_action_id else []
                ),
                "stationary_observation_ids": [observation_id],
            }
            if deferred:
                limitations.append("partial_rows_unresolved")
            limitations = list(dict.fromkeys(limitations))
            return {
                "status": "partial" if limitations else "complete",
                "reason_code": limitations[0] if limitations else None,
                "target_ref": target_ref,
                "top_boundary": state["top_boundary"],
                "bottom_boundary": state["bottom_boundary"],
                "archive": state["archive"],
                "containers": tuple(containers),
                "deferred": tuple(deferred),
                "limitations": tuple(limitations),
                "scroll_probes": state["scrolls"],
            }

        stationary = []
        moved = False
        while len(stationary) < state["stagnation_rounds"]:
            action_id, moved, reason = _scroll(
                device, state, observation_id, "forward"
            )
            if reason is not None:
                return _failed_report(
                    target_ref, reason, state, containers, deferred, limitations
                )
            observation_id, root, reason = _observe(device, state)
            page = _default_list(device, root)
            if reason is not None or page is None:
                return _failed_report(
                    target_ref, reason or "default_list_not_verified",
                    state, containers, deferred, limitations,
                )
            if moved:
                stationary = []
                page_number += 1
                break
            stationary.append((action_id, observation_id))
        if moved:
            continue
        state["bottom_boundary"] = {
            "established": True,
            "observation_id": stationary[-1][1],
            "stationary_action_ids": [item[0] for item in stationary],
            "stationary_observation_ids": [item[1] for item in stationary],
        }
        if deferred:
            limitations.append("partial_rows_unresolved")
        limitations = list(dict.fromkeys(limitations))
        return {
            "status": "partial" if limitations else "complete",
            "reason_code": limitations[0] if limitations else None,
            "target_ref": target_ref,
            "top_boundary": state["top_boundary"],
            "bottom_boundary": state["bottom_boundary"],
            "archive": state["archive"],
            "containers": tuple(containers),
            "deferred": tuple(deferred),
            "limitations": tuple(limitations),
            "scroll_probes": state["scrolls"],
        }


def _established_boundary(value, *, action_link=False):
    if (
        not isinstance(value, Mapping)
        or value.get("established") is not True
        or not isinstance(value.get("observation_id"), str)
    ):
        return False
    if not action_link:
        return True
    actions = value.get("stationary_action_ids")
    observations = value.get("stationary_observation_ids")
    return (
        isinstance(actions, (list, tuple))
        and isinstance(observations, (list, tuple))
        and len(actions) == len(observations) >= 1
        and all(isinstance(item, str) and item for item in actions)
        and all(isinstance(item, str) and item for item in observations)
        and observations[-1] == value["observation_id"]
    )


def _close_account_report(report):
    if not isinstance(report, Mapping):
        return None, "t04_closure_invalid"
    status = report.get("status")
    reason_code = report.get("reason_code")
    target_ref = report.get("target_ref")
    containers = report.get("containers")
    deferred = report.get("deferred")
    limitations = report.get("limitations")
    archive = report.get("archive")
    if (
        status not in {"complete", "partial"}
        or (status == "complete") != (reason_code is None)
        or not isinstance(target_ref, str)
        or re.fullmatch(r"target-[0-9a-f]{64}", target_ref) is None
        or not _established_boundary(report.get("top_boundary"))
        or not _established_boundary(
            report.get("bottom_boundary"), action_link=True
        )
        or not isinstance(containers, (list, tuple))
        or not isinstance(deferred, (list, tuple))
        or not isinstance(limitations, (list, tuple))
        or any(not isinstance(item, str) or not item for item in limitations)
        or len(set(limitations)) != len(limitations)
        or status == "partial"
        and (not limitations or reason_code != limitations[0])
        or status == "complete"
        and limitations
        or not isinstance(archive, Mapping)
        or archive.get("status") != "out_of_scope"
        or archive.get("reason_code")
        != "archived_chatrooms_not_collected"
        or archive.get("occurrence_count") != 0
    ):
        return None, "t04_closure_invalid"

    projected = []
    complete_count = 0
    partial_count = 0
    occurrence_refs = set()
    container_refs = set()
    for item in containers:
        history = item.get("history") if isinstance(item, Mapping) else None
        outcome = item.get("outcome") if isinstance(item, Mapping) else None
        occurrence_ref = (
            item.get("occurrence_ref") if isinstance(item, Mapping) else None
        )
        container_ref = (
            item.get("container_ref") if isinstance(item, Mapping) else None
        )
        if (
            outcome not in {"complete", "partial"}
            or not isinstance(occurrence_ref, str)
            or not occurrence_ref
            or occurrence_ref in occurrence_refs
            or not isinstance(container_ref, str)
            or not container_ref
            or container_ref in container_refs
            or item.get("surface") != "default"
            or not isinstance(history, Mapping)
            or history.get("status") != outcome
            or outcome == "complete"
            and history.get("reason_code") is not None
            or outcome == "partial"
            and (
                not isinstance(history.get("reason_code"), str)
                or not history.get("reason_code")
            )
        ):
            return None, "t04_closure_invalid"
        occurrence_refs.add(occurrence_ref)
        container_refs.add(container_ref)
        complete_count += outcome == "complete"
        partial_count += outcome == "partial"
        projected.append({
            "container_ref": container_ref,
            "occurrence_ref": occurrence_ref,
            "surface": item["surface"],
            "outcome": outcome,
            "history_status": history["status"],
            "history_reason_code": history.get("reason_code"),
        })

    if (
        any(
            not isinstance(item, Mapping)
            or item.get("outcome") != "partial_row_unresolved"
            for item in deferred
        )
        or deferred
        and "partial_rows_unresolved" not in limitations
    ):
        return None, "t04_closure_invalid"

    return {
        "schema_version": "1.0",
        "record_kind": "account_closure",
        "target_ref": target_ref,
        "status": status,
        "reason_code": reason_code,
        "top_boundary": dict(report["top_boundary"]),
        "bottom_boundary": dict(report["bottom_boundary"]),
        "archive": dict(archive),
        "occurrence_count": len(projected),
        "complete_count": complete_count,
        "partial_count": partial_count,
        "deferred_count": len(deferred),
        "limitations": list(limitations),
        "containers": projected,
    }, None


def _write_account_closure(outputs, report, closure):
    try:
        outputs.write_json(
            artifact_id="account-closure-000001",
            output_kind="audit_record",
            artifact_class="message",
            value=closure,
            action_id=report["bottom_boundary"]["stationary_action_ids"][-1],
            observation_id=report["bottom_boundary"][
                "stationary_observation_ids"
            ][-1],
            comparison_ref="account-closure-000001",
        )
    except Exception:
        return "account_closure_output_failed"
    return None


def collect(device, outputs, target_ref):
    report = _enumerate_containers(
        device,
        target_ref,
        outputs=outputs,
        attachment_kinds=("photo", "video", "file"),
    )
    report = {
        **report,
        "schema_version": "1.0",
        "record_kind": "telegram_message_history_acquisition",
        "occurrence_count": len(report.get("containers", ())),
    }
    bottom = report.get("bottom_boundary", {})
    actions = bottom.get("stationary_action_ids", ())
    observations = bottom.get("stationary_observation_ids", ())
    if actions and observations:
        outputs.write_json(
            artifact_id="chatroom-list",
            output_kind="ui_record",
            artifact_class="chatroom_list",
            value=report,
            action_id=actions[-1],
            observation_id=observations[-1],
            comparison_ref="chatroom-list",
        )
    if hasattr(outputs, "context"):
        complete = (
            report.get("top_boundary", {}).get("established")
            and report.get("bottom_boundary", {}).get("established")
            and not report.get("deferred")
            and all(item.get("history", {}).get("earliest_boundary", {}).get("established")
                    and item.get("history", {}).get("latest_boundary", {}).get("established")
                    for item in report.get("containers", ()))
        )
        for target_id in ("telegram.conversations", "telegram.attachments"):
            current = next(record for record in outputs.context.identifications if record.target_id == target_id)
            if current.started_at is not None:
                outputs.context.finish_identification(target_id, completion_condition="chat_list_and_histories_exhausted" if complete else None,
                                                       reason=None if complete else report.get("reason_code") or "history_boundary_not_established")
    return report
