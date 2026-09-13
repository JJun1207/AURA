"""Notion 0.6.4030 account and workspace context parsing."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from pathlib import PurePosixPath

from ...models import ActionRef, Route
from .exports import ExportCollectionError, collect_workspace_exports
from .pages import PageCollectionError, collect_workspace_pages
from .support import (
    PACKAGE,
    NotionCollectorError,
    bounds as _bounds,
    click as _click,
    observe as _observe,
    onscreen as _onscreen,
    retain_pair as _retain_pair,
    safe_component,
    selector as _selector,
    write_json as _write_json,
)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INVITE_LINK = re.compile(r"^https://app\.notion\.com/invite/[^\s]+$")
_PAGE_ROW_ID = re.compile(
    r"^home-tab\.(?:private|shared|teamspaces?)\.page-row\."
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


def _parse_workspace_menu(root: ET.Element) -> dict[str, bool]:
    found = [
        node.get("resource-id", "")
        for node in root.iter()
        if _onscreen(node)
        and node.get("clickable") == "true"
        and node.get("enabled") == "true"
        and node.get("resource-id") in {
            "home-tab.more.switch-workspace",
            "home-tab.more.members",
        }
    ]
    if found.count("home-tab.more.switch-workspace") != 1:
        raise NotionCollectorError("workspace menu is unavailable")
    if found.count("home-tab.more.members") > 1:
        raise NotionCollectorError("Members control is ambiguous")
    return {
        "members_available": "home-tab.more.members" in found,
    }


def _workspace_page_ids(root: ET.Element) -> frozenset[str]:
    return frozenset(
        match.group(1).lower()
        for node in root.iter()
        if _onscreen(node)
        and (match := _PAGE_ROW_ID.fullmatch(
            node.get("resource-id", "")
        )) is not None
    )


def _parse_workspace_home(
    root: ET.Element,
    *,
    previous_page_ids: frozenset[str] | None = None,
    expected_page_ids: frozenset[str] | None = None,
) -> frozenset[str]:
    homes = [
        node
        for node in root.iter()
        if _onscreen(node)
        and node.get("resource-id") == "vision_tab_Home"
        and node.get("selected") == "true"
    ]
    if len(homes) != 1:
        raise NotionCollectorError("Notion Home is unavailable")
    page_ids = _workspace_page_ids(root)
    if previous_page_ids and page_ids == previous_page_ids:
        raise NotionCollectorError(
            "workspace Home content did not change"
        )
    if expected_page_ids is not None and page_ids != expected_page_ids:
        raise NotionCollectorError(
            "workspace Home content is inconsistent"
        )
    return page_ids


def _selected_marker(row: ET.Element) -> bool:
    left, _, right, _ = _bounds(row)
    threshold = left + (right - left) * 0.75
    return any(
        _onscreen(child)
        and child.get("class") == "android.view.View"
        and child.get("clickable") != "true"
        and not child.get("text", "").strip()
        and not child.get("resource-id", "").strip()
        and not child.get("content-desc", "").strip()
        and _bounds(child)[0] >= threshold
        for child in row
    )


def _parse_switcher(root: ET.Element) -> dict[str, object]:
    headings = [
        node
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == "Accounts"
    ]
    if len(headings) != 1:
        raise NotionCollectorError("account switcher is unavailable")

    emails = [
        (_bounds(node)[1], node.get("text", "").strip())
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and _EMAIL.fullmatch(node.get("text", "").strip())
    ]
    emails.sort()
    if not emails or len({email for _, email in emails}) != len(emails):
        raise NotionCollectorError("account inventory is ambiguous")

    rows = []
    for node in root.iter():
        if (
            not _onscreen(node)
            or node.get("class") != "android.view.View"
            or node.get("clickable") != "true"
            or node.get("enabled") != "true"
        ):
            continue
        texts = [
            child.get("text", "").strip()
            for child in node
            if _onscreen(child)
            and child.get("class") == "android.widget.TextView"
            and child.get("text", "").strip()
        ]
        if (
            len(texts) not in {1, 2}
            or node.get("resource-id", "").strip()
            or _bounds(node)[2] - _bounds(node)[0] <= 800
            or any(
                text in {"New workspace", "Add an account", "Log out all"}
                for text in texts
            )
        ):
            continue
        top = _bounds(node)[1]
        preceding = [email for y, email in emails if y < top]
        if not preceding:
            raise NotionCollectorError("workspace account is unavailable")
        rows.append({
            "account_email": preceding[-1],
            "display_name": texts[0],
            "plan_label": texts[1] if len(texts) == 2 else None,
            "initial_selected": _selected_marker(node),
            "bounds": _bounds(node),
        })
    rows.sort(key=lambda item: (item["bounds"][1], item["bounds"][0]))
    if not rows:
        raise NotionCollectorError("workspace inventory is unavailable")

    account_refs = {
        email: f"account-{index:06d}"
        for index, (_, email) in enumerate(emails, 1)
    }
    name_counts: Counter[str] = Counter()
    workspaces = []
    for index, row in enumerate(rows, 1):
        name = str(row["display_name"])
        name_counts[name] += 1
        workspaces.append({
            "workspace_ref": f"workspace-{index:06d}",
            "account_ref": account_refs[str(row["account_email"])],
            **row,
            "same_name_occurrence": name_counts[name],
        })
    selected = [
        item["workspace_ref"]
        for item in workspaces
        if item["initial_selected"]
    ]
    if len(selected) != 1:
        raise NotionCollectorError(
            "initial workspace selection is ambiguous"
        )
    accounts = [
        {
            "account_ref": account_refs[email],
            "email": email,
            "workspace_refs": [
                item["workspace_ref"]
                for item in workspaces
                if item["account_email"] == email
            ],
        }
        for _, email in emails
    ]
    return {
        "accounts": accounts,
        "workspaces": workspaces,
        "initial_workspace_ref": selected[0],
    }


def _parse_members(root: ET.Element) -> dict[str, object]:
    headings = [
        node
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == "Members"
    ]
    if len(headings) != 1:
        raise NotionCollectorError("Members view is unavailable")
    links = [
        node.get("text", "").strip()
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.EditText"
        and _INVITE_LINK.fullmatch(node.get("text", "").strip())
    ]
    online_requirements = [
        node
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip()
        == "Please go online to manage settings."
    ]
    member_emails = [
        node
        for node in root.iter()
        if _onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and _EMAIL.fullmatch(node.get("text", "").strip())
    ]
    if len(online_requirements) == 1 and not links and not member_emails:
        return {
            "acquisition_status": "unavailable",
            "reason_code": "notion_members_online_required",
            "invite_link": None,
            "members": [],
        }
    if len(links) != 1:
        raise NotionCollectorError("workspace invite link is ambiguous")

    parents = {child: parent for parent in root.iter() for child in parent}
    members = []
    for email_node in root.iter():
        email = email_node.get("text", "").strip()
        if (
            not _onscreen(email_node)
            or email_node.get("class") != "android.widget.TextView"
            or _EMAIL.fullmatch(email) is None
        ):
            continue
        row = parents.get(email_node)
        if row is None:
            raise NotionCollectorError("member row is unavailable")
        roles = {
            node.get("text", "").strip()
            for node in row.iter()
            if _onscreen(node)
            and node.get("class") == "android.widget.Button"
            and node.get("text", "").strip()
        }
        if len(roles) != 1:
            raise NotionCollectorError("member role is ambiguous")
        role = roles.pop()
        names = [
            node.get("text", "").strip()
            for node in row.iter()
            if _onscreen(node)
            and node.get("class") == "android.widget.TextView"
            and node.get("text", "").strip() not in {email, role}
        ]
        if len(names) != 1:
            raise NotionCollectorError("member name is ambiguous")
        avatars = {
            node.get("content-desc", "").strip()
            for node in row.iter()
            if _onscreen(node)
            and node.get("class") in {
                "android.widget.Image",
                "android.widget.ImageView",
            }
            and node.get("content-desc", "").strip()
        }
        if len(avatars) > 1:
            raise NotionCollectorError("member avatar is ambiguous")
        members.append({
            "display_name": names[0],
            "email": email,
            "role": role,
            "is_current_user": names[0].endswith("(You)"),
            "avatar_description": next(iter(avatars), None),
            "_top": _bounds(row)[1],
        })
    if not members or len({item["email"] for item in members}) != len(members):
        raise NotionCollectorError("member inventory is ambiguous")
    members.sort(key=lambda item: item.pop("_top"))
    return {
        "acquisition_status": "complete",
        "reason_code": None,
        "invite_link": links[0],
        "members": members,
    }


def _open_switcher(
    context,
    sequence: int,
    *,
    label: str = "",
):
    action_label = f"{label}_" if label else ""
    observation_label = f"{label}-" if label else ""
    menu_action = _click(
        context,
        f"notion_{action_label}open_workspace_menu_{sequence:06d}",
        "notion.workspace-menu.open",
    )
    _observe(
        context,
        f"observation-{observation_label}workspace-menu-{sequence:06d}",
        menu_action,
        _parse_workspace_menu,
        f"{observation_label}workspace-menu-{sequence:06d}",
    )
    switch_action = _click(
        context,
        f"notion_{action_label}open_account_switcher_{sequence:06d}",
        "notion.workspace-menu.switch",
    )
    observation = _observe(
        context,
        f"observation-{observation_label}account-switcher-{sequence:06d}",
        switch_action,
        _parse_switcher,
        f"{observation_label}workspace-switcher-{sequence:06d}",
    )
    return switch_action, observation, observation["parsed"]


def _open_members(
    context,
    sequence: int,
    workspace: Mapping[str, object],
    expected_page_ids: frozenset[str],
):
    workspace_ref = str(workspace["workspace_ref"])
    menu_action = _click(
        context,
        f"notion_open_workspace_menu_members_{sequence:06d}",
        "notion.workspace-menu.open",
        workspace_ref=workspace_ref,
    )
    menu_observation = _observe(
        context,
        f"observation-workspace-menu-members-{sequence:06d}",
        menu_action,
        _parse_workspace_menu,
        f"workspace-menu-members-{sequence:06d}",
    )
    if not menu_observation["parsed"]["members_available"]:
        return (
            menu_action,
            menu_observation,
            {
                "acquisition_status": "unavailable",
                "reason_code": "notion_members_control_unavailable",
                "invite_link": None,
                "members": [],
            },
            False,
        )
    members_action = _click(
        context,
        f"notion_open_members_{sequence:06d}",
        "notion.workspace-menu.members",
        workspace_ref=workspace_ref,
    )
    try:
        observation = _observe(
            context,
            f"observation-members-{sequence:06d}",
            members_action,
            _parse_members,
            f"members-{sequence:06d}",
        )
    except NotionCollectorError:
        if not context.ui.wait_for(
            _selector(context, "notion.home"), timeout=0
        ):
            raise
        # The failed observation closed the first procedure. Recovery is a
        # real new attempt on the same workspace, before its next UI action.
        from .support import item_attempt
        item_attempt(context, {"workspace_ref": workspace_ref}, retention=False)
        actual_page_ids = _parse_workspace_home(
            ET.fromstring(context.device.hierarchy())
        )
        recovered = actual_page_ids != expected_page_ids
        if recovered:
            _, _, inventory = _open_switcher(
                context,
                sequence,
                label="recover_members",
            )
            target = next(
                item
                for item in inventory["workspaces"]
                if _workspace_key(item) == _workspace_key(workspace)
            )
            recovery_action = context.ui.click_bounds(
                f"notion_recover_members_select_workspace_{sequence:06d}",
                target["bounds"],
                context={"workspace_ref": workspace_ref},
            )
            _observe(
                context,
                f"observation-members-workspace-recovery-{sequence:06d}",
                recovery_action,
                lambda root: _parse_workspace_home(
                    root,
                    expected_page_ids=expected_page_ids,
                ),
                f"members-workspace-recovery-{sequence:06d}",
            )
        retry_menu_action = _click(
            context,
            f"notion_retry_workspace_menu_members_{sequence:06d}",
            "notion.workspace-menu.open",
            workspace_ref=workspace_ref,
        )
        _observe(
            context,
            f"observation-workspace-menu-members-retry-{sequence:06d}",
            retry_menu_action,
            _parse_workspace_menu,
            f"workspace-menu-members-retry-{sequence:06d}",
        )
        members_action = _click(
            context,
            f"notion_retry_members_{sequence:06d}",
            "notion.workspace-menu.members",
            workspace_ref=workspace_ref,
        )
        observation = _observe(
            context,
            f"observation-members-{sequence:06d}",
            members_action,
            _parse_members,
            f"members-{sequence:06d}",
        )
    else:
        recovered = False
    return members_action, observation, observation["parsed"], recovered


def _workspace_key(workspace: Mapping[str, object]) -> tuple[object, ...]:
    return (
        workspace["account_email"],
        workspace["display_name"],
        workspace["plan_label"],
        workspace["same_name_occurrence"],
    )


def _current_workspace(
    inventory: Mapping[str, object],
    expected: Mapping[str, object],
) -> dict[str, object]:
    matches = [
        item
        for item in inventory["workspaces"]
        if _workspace_key(item) == _workspace_key(expected)
    ]
    if len(matches) != 1 or not matches[0]["initial_selected"]:
        raise NotionCollectorError("workspace selection was not verified")
    return matches[0]


def _workspace_directory(workspace: Mapping[str, object]) -> str:
    label = safe_component(workspace["display_name"])
    value = f'{workspace["workspace_ref"]} ({label})'
    if PurePosixPath(value).name != value:
        raise NotionCollectorError("workspace output label is invalid")
    return f"notion/workspaces/{value}"


def _select_workspace(
    context,
    workspace: Mapping[str, object],
    sequence: int,
    previous_page_ids: frozenset[str] | None,
) -> tuple[ActionRef, frozenset[str]]:
    action = context.ui.click_bounds(
        f"notion_select_workspace_{sequence:06d}",
        workspace["bounds"],
        context={"workspace_ref": workspace["workspace_ref"]},
    )
    observation = _observe(
        context,
        f"observation-workspace-selection-{sequence:06d}",
        action,
        lambda root: _parse_workspace_home(
            root,
            previous_page_ids=previous_page_ids,
        ),
        f"workspace-selection-{sequence:06d}",
    )
    return action, observation["parsed"]


def _close_to_home(
    context,
    action_name: str,
    *,
    expected_page_ids: frozenset[str] | None = None,
    **linked,
) -> ActionRef:
    action = context.ui.back(
        action_name,
        expected=_selector(context, "notion.home"),
        context=dict(linked),
    )
    _observe(
        context,
        f"observation-{action_name}",
        action,
        (
            None
            if expected_page_ids is None
            else lambda root: _parse_workspace_home(
                root,
                expected_page_ids=expected_page_ids,
            )
        ),
        f"{action_name}-home",
    )
    return action


def _restore_initial(
    context,
    initial_workspace: Mapping[str, object],
    current_ref: str,
    sequence: int,
) -> tuple[bool, ActionRef | None]:
    home = _selector(context, "notion.home")
    if not context.ui.wait_for(home, timeout=0):
        try:
            _close_to_home(
                context,
                f"notion_recover_home_{sequence:06d}",
                workspace_ref=current_ref,
            )
        except Exception:
            return False, None
    initial_ref = str(initial_workspace["workspace_ref"])
    try:
        previous_page_ids = _parse_workspace_home(
            ET.fromstring(context.device.hierarchy())
        )
        sequence += 1
        _, _, inventory = _open_switcher(context, sequence)
        target = next(
            item
            for item in inventory["workspaces"]
            if _workspace_key(item) == _workspace_key(initial_workspace)
        )
        restore_action, restored_page_ids = _select_workspace(
            context,
            target,
            sequence,
            (
                None
                if current_ref == initial_ref
                else previous_page_ids
            ),
        )
        sequence += 1
        _, _, restored_inventory = _open_switcher(context, sequence)
        _current_workspace(restored_inventory, initial_workspace)
        _close_to_home(
            context,
            f"notion_close_restored_switcher_{sequence:06d}",
            expected_page_ids=restored_page_ids,
            workspace_ref=initial_ref,
        )
        return True, restore_action
    except Exception:
        if not context.ui.wait_for(home, timeout=0):
            try:
                _close_to_home(
                    context,
                    f"notion_recover_restore_home_{sequence:06d}",
                    workspace_ref=initial_ref,
                )
            except Exception:
                pass
        return False, None


def collect(
    context,
    target_ref: str,
    route: Route,
) -> dict[str, object]:
    route = Route(route)
    context.begin_identification("notion.content")
    from .support import item_attempt
    item_attempt(context, {"target_ref": target_ref})
    if not context.ui.wait_for(_selector(context, "notion.home")):
        return {
            "status": "failed",
            "reason_code": "notion_home_unavailable",
            "target_ref": target_ref,
        }
    try:
        current_page_ids = _parse_workspace_home(
            ET.fromstring(context.device.hierarchy())
        )
        initial_action, initial_observation, initial = _open_switcher(
            context, 1
        )
    except NotionCollectorError as error:
        return {
            "status": "failed",
            "reason_code": "notion_workspace_inventory_unavailable",
            "collector_error": str(error),
            "target_ref": target_ref,
        }
    initial_source = _retain_pair(
        context,
        "notion/account/workspace-list",
        initial_observation,
        initial_action,
        {"target_ref": target_ref},
    )
    account_record = {
        "schema_version": "1.0",
        "record_kind": "notion_accounts",
        "target_ref": target_ref,
        "account_count": len(initial["accounts"]),
        "accounts": initial["accounts"],
        "source": initial_source,
    }
    account_artifact = _write_json(
        context,
        "notion/account/account.json",
        account_record,
        initial_action,
        {
            "target_ref": target_ref,
            "observation_id": initial_source["observation_id"],
        },
    )
    context.register_collection_records(
        "notion.content", account_artifact, initial["accounts"],
        item_type="account", record_key="accounts",
    )
    initial_ref = str(initial["initial_workspace_ref"])
    initial_workspace = next(
        item
        for item in initial["workspaces"]
        if item["workspace_ref"] == initial_ref
    )
    current_inventory = initial
    current_ref = initial_ref
    workspace_records = []
    selection_count = 0
    transition_count = 0
    total_members = 0
    members_unavailable_workspace_refs = []
    members_partial_reason = None
    total_pages = 0
    total_database_count = 0
    total_database_items = 0
    total_databases = 0
    total_attachments = 0
    total_materialized_attachments = 0
    total_out_of_scope_attachments = 0
    total_discovered_exports = 0
    total_exports = 0
    total_general_page_exports = 0
    total_database_exports = 0
    total_include_subpages = 0
    payload_partial_reason = None
    sequence = 1
    last_action = initial_action
    failed_workspace_ref = None
    failed_page_ref = None
    failed_page_id = None
    failure_reason = "notion_workspace_context_partial"

    def payload_counts():
        if route is Route.EXPORT:
            return {
                "discovered_top_level_count": total_discovered_exports,
                "export_count": total_exports,
                "general_page_export_count": (
                    total_general_page_exports
                ),
                "database_export_count": total_database_exports,
                "include_subpages_count": total_include_subpages,
            }
        return {
            "page_count": total_pages,
            "database_count": total_database_count,
            "database_item_count": total_database_items,
            "database_out_of_scope_count": total_databases,
            "attachment_count": total_attachments,
            "attachment_materialized_count": (
                total_materialized_attachments
            ),
            "attachment_out_of_scope_count": (
                total_out_of_scope_attachments
            ),
        }

    def members_unavailable_details():
        if not members_unavailable_workspace_refs:
            return {}
        return {
            "members_unavailable_count": len(
                members_unavailable_workspace_refs
            ),
            "members_unavailable_workspace_refs": list(
                members_unavailable_workspace_refs
            ),
        }

    def write_workspace_summary(
        restored: bool,
        *,
        status: str = "complete",
        reason_code: str | None = None,
    ) -> None:
        summary = {
            "schema_version": "1.0",
            "record_kind": "notion_workspaces",
            "target_ref": target_ref,
            "workspace_count": len(workspace_records),
            "initial_workspace_ref": initial_ref,
            "initial_workspace_restored": restored,
            "workspace_selection_count": selection_count,
            "cross_workspace_transition_count": transition_count,
            "member_count": total_members,
            **members_unavailable_details(),
            **payload_counts(),
            "workspaces": workspace_records,
            "source": initial_source,
        }
        if status == "partial":
            summary.update({
                "status": status,
                "reason_code": reason_code,
                "discovered_workspace_count": len(
                    initial["workspaces"]
                ),
                "discovered_workspace_refs": [
                    str(item["workspace_ref"])
                    for item in initial["workspaces"]
                ],
                "failed_workspace_ref": failed_workspace_ref,
                "failed_page_ref": failed_page_ref,
                "failed_page_id": failed_page_id,
            })
        _write_json(
            context,
            "notion/account/workspaces.json",
            summary,
            initial_action,
            {
                "target_ref": target_ref,
                "observation_id": initial_source["observation_id"],
            },
        )

    try:
        for target in initial["workspaces"]:
            item_attempt(context, {"target_ref": target_ref, "workspace_ref": target["workspace_ref"]})
            failed_workspace_ref = str(target["workspace_ref"])
            failure_reason = "notion_workspace_selection_failed"
            current_target = next(
                item
                for item in current_inventory["workspaces"]
                if _workspace_key(item) == _workspace_key(target)
            )
            selection_action, current_page_ids = _select_workspace(
                context,
                current_target,
                sequence,
                (
                    None
                    if current_ref == target["workspace_ref"]
                    else current_page_ids
                ),
            )
            selection_count += 1
            if current_ref != target["workspace_ref"]:
                transition_count += 1
            current_ref = str(target["workspace_ref"])
            sequence += 1

            failure_reason = "notion_workspace_selection_unverified"
            verify_action, verify_observation, verified_inventory = (
                _open_switcher(context, sequence)
            )
            verified = _current_workspace(verified_inventory, target)
            selection_source = _retain_pair(
                context,
                (
                    f"{_workspace_directory(target)}/"
                    "selection-verification"
                ),
                verify_observation,
                verify_action,
                {
                    "target_ref": target_ref,
                    "workspace_ref": target["workspace_ref"],
                    "selection_action_id": selection_action.event_id,
                },
            )
            selection_source["selection_action_id"] = (
                selection_action.event_id
            )
            _close_to_home(
                context,
                f"notion_close_account_switcher_{sequence:06d}",
                expected_page_ids=current_page_ids,
                workspace_ref=target["workspace_ref"],
            )

            failure_reason = "notion_members_context_ambiguous"
            (
                members_action,
                members_observation,
                members,
                members_context_recovered,
            ) = _open_members(
                context,
                sequence,
                target,
                current_page_ids,
            )
            if members_context_recovered:
                selection_count += 1
                transition_count += 1
            members_source = _retain_pair(
                context,
                f"{_workspace_directory(target)}/members",
                members_observation,
                members_action,
                {
                    "target_ref": target_ref,
                    "workspace_ref": target["workspace_ref"],
                },
            )
            members_record = {
                "schema_version": "1.0",
                "record_kind": "notion_workspace_members",
                "target_ref": target_ref,
                "workspace_ref": target["workspace_ref"],
                "member_count": len(members["members"]),
                **members,
                "source": members_source,
            }
            _write_json(
                context,
                f"{_workspace_directory(target)}/members.json",
                members_record,
                members_action,
                {
                    "target_ref": target_ref,
                    "workspace_ref": target["workspace_ref"],
                    "observation_id": members_source["observation_id"],
                },
            )
            total_members += len(members["members"])
            if members["acquisition_status"] == "unavailable":
                members_unavailable_workspace_refs.append(
                    str(target["workspace_ref"])
                )
                members_partial_reason = (
                    members_partial_reason or members["reason_code"]
                )
            _close_to_home(
                context,
                f"notion_close_members_{sequence:06d}",
                expected_page_ids=current_page_ids,
                workspace_ref=target["workspace_ref"],
            )

            if route is Route.EXPORT:
                failure_reason = "notion_export_collection_failed"
                context.begin_identification("notion.export")
                payload_report = collect_workspace_exports(
                    context,
                    target,
                    _workspace_directory(target),
                    target_ref,
                )
                total_discovered_exports += int(
                    payload_report["discovered_top_level_count"]
                )
                total_exports += int(payload_report["export_count"])
                total_general_page_exports += int(
                    payload_report["general_page_export_count"]
                )
                total_database_exports += int(
                    payload_report["database_export_count"]
                )
                total_include_subpages += int(
                    payload_report["include_subpages_count"]
                )
            else:
                failure_reason = "notion_page_collection_failed"
                context.begin_identification("notion.attachments")
                payload_report = collect_workspace_pages(
                    context,
                    target,
                    _workspace_directory(target),
                    target_ref,
                )
                total_pages += int(payload_report["page_count"])
                total_database_count += int(
                    payload_report["database_count"]
                )
                total_database_items += int(
                    payload_report["database_item_count"]
                )
                total_databases += int(
                    payload_report["database_out_of_scope_count"]
                )
                total_attachments += int(
                    payload_report["attachment_count"]
                )
                total_materialized_attachments += int(
                    payload_report["attachment_materialized_count"]
                )
                total_out_of_scope_attachments += int(
                    payload_report["attachment_out_of_scope_count"]
                )
                if payload_report["status"] == "partial":
                    payload_partial_reason = (
                        payload_partial_reason
                        or payload_report["reason_code"]
                    )
            last_action = payload_report["_action"]

            failure_reason = "notion_workspace_output_failed"
            workspace_record = {
                "schema_version": "1.0",
                "record_kind": "notion_workspace",
                "target_ref": target_ref,
                "workspace_ref": target["workspace_ref"],
                "account_ref": target["account_ref"],
                "account_email": target["account_email"],
                "display_name": target["display_name"],
                "plan_label": target["plan_label"],
                "same_name_occurrence": target["same_name_occurrence"],
                "initial_selected": target["initial_selected"],
                "selection_verified": bool(verified["initial_selected"]),
                "selection_verification_source": selection_source,
                "members_source": members_source,
                "member_count": len(members["members"]),
                "members_acquisition_status": members[
                    "acquisition_status"
                ],
                "members_reason_code": members["reason_code"],
            }
            if route is Route.EXPORT:
                workspace_record.update({
                    "exports_source": payload_report["source"],
                    "discovered_top_level_count": payload_report[
                        "discovered_top_level_count"
                    ],
                    "export_count": payload_report["export_count"],
                    "general_page_export_count": payload_report[
                        "general_page_export_count"
                    ],
                    "database_export_count": payload_report[
                        "database_export_count"
                    ],
                    "include_subpages_count": payload_report[
                        "include_subpages_count"
                    ],
                })
            else:
                workspace_record.update({
                    "pages_source": payload_report["source"],
                    "pages_acquisition_status": payload_report["status"],
                    "pages_reason_code": payload_report["reason_code"],
                    "discovered_page_count": payload_report[
                        "discovered_page_count"
                    ],
                    "page_count": payload_report["page_count"],
                    "database_count": payload_report["database_count"],
                    "database_item_count": payload_report[
                        "database_item_count"
                    ],
                    "database_out_of_scope_count": payload_report[
                        "database_out_of_scope_count"
                    ],
                    "attachment_count": payload_report[
                        "attachment_count"
                    ],
                    "attachment_materialized_count": payload_report[
                        "attachment_materialized_count"
                    ],
                    "attachment_out_of_scope_count": payload_report[
                        "attachment_out_of_scope_count"
                    ],
                })
            workspace_records.append(workspace_record)
            if target is not initial["workspaces"][-1]:
                current_page_ids = _parse_workspace_home(
                    ET.fromstring(context.device.hierarchy())
                )
                sequence += 1
                _, _, current_inventory = _open_switcher(
                    context, sequence
                )
                current_ref = str(
                    current_inventory["initial_workspace_ref"]
                )

        failure_reason = "notion_initial_workspace_restore_failed"
        restored, restore_action = _restore_initial(
            context, initial_workspace, current_ref, sequence
        )
        if not restored:
            raise NotionCollectorError(
                "initial workspace restoration was not verified"
            )
        if restore_action is not None:
            last_action = restore_action
    except Exception as error:
        if isinstance(error, PageCollectionError):
            failure_reason = error.reason_code
            failed_page_ref = error.page_ref
            failed_page_id = error.page_id
            partial_report = getattr(error, "report", {})
            total_pages += int(partial_report.get("page_count", 0))
            total_database_count += int(
                partial_report.get("database_count", 0)
            )
            total_database_items += int(
                partial_report.get("database_item_count", 0)
            )
            total_databases += int(
                partial_report.get("database_out_of_scope_count", 0)
            )
            total_attachments += int(
                partial_report.get("attachment_count", 0)
            )
            total_materialized_attachments += int(
                partial_report.get(
                    "attachment_materialized_count",
                    0,
                )
            )
            total_out_of_scope_attachments += int(
                partial_report.get(
                    "attachment_out_of_scope_count",
                    0,
                )
            )
            if error.action is not None:
                last_action = error.action
        elif isinstance(error, ExportCollectionError):
            failure_reason = error.reason_code
            partial_report = error.report
            total_discovered_exports += int(
                partial_report.get("discovered_top_level_count", 0)
            )
            total_exports += int(
                partial_report.get("export_count", 0)
            )
            total_general_page_exports += int(
                partial_report.get("general_page_export_count", 0)
            )
            total_database_exports += int(
                partial_report.get("database_export_count", 0)
            )
            total_include_subpages += int(
                partial_report.get("include_subpages_count", 0)
            )
            failed_export = next(
                (
                    item
                    for item in partial_report.get("exports", ())
                    if item.get("acquisition_status") == "partial"
                ),
                {},
            )
            failed_page_ref = failed_export.get("page_ref")
            failed_page_id = failed_export.get("notion_page_id")
            if error.action is not None:
                last_action = error.action
        restored, restore_action = _restore_initial(
            context, initial_workspace, current_ref, sequence
        )
        if restore_action is not None:
            last_action = restore_action
        write_workspace_summary(
            restored,
            status="partial",
            reason_code=failure_reason,
        )
        return {
            "status": "partial",
            "reason_code": failure_reason,
            "target_ref": target_ref,
            "account_count": len(initial["accounts"]),
            "workspace_count": len(initial["workspaces"]),
            "workspace_selection_count": selection_count,
            "cross_workspace_transition_count": transition_count,
            "member_count": total_members,
            **members_unavailable_details(),
            **payload_counts(),
            "initial_workspace_restored": restored,
            "failed_workspace_ref": failed_workspace_ref,
            "failed_page_ref": failed_page_ref,
            "failed_page_id": failed_page_id,
            "_action": last_action,
        }

    write_workspace_summary(restored)
    context.finish_identification("notion.content", completion_condition="all_workspaces_visited" if not payload_partial_reason else None,
                                  reason=payload_partial_reason)
    payload_target = "notion.export" if route is Route.EXPORT else "notion.attachments"
    current = next(record for record in context.identifications if record.target_id == payload_target)
    if current.started_at is not None:
        context.finish_identification(payload_target, completion_condition="all_workspaces_visited" if not payload_partial_reason else None, reason=payload_partial_reason)
    return {
        "status": (
            "partial" if payload_partial_reason or members_partial_reason
            else "complete"
        ),
        "reason_code": (
            payload_partial_reason or members_partial_reason
        ),
        "target_ref": target_ref,
        "account_count": len(initial["accounts"]),
        "workspace_count": len(workspace_records),
        "workspace_selection_count": selection_count,
        "cross_workspace_transition_count": transition_count,
        "member_count": total_members,
        **members_unavailable_details(),
        **payload_counts(),
        "initial_workspace_restored": restored,
        "_action": last_action,
    }
