"""Notion account and workspace context collector."""

from __future__ import annotations

from ...models import AcquisitionStatus, Outcome, OutcomeStatus, Route
from ...runtime import RunContext
from . import collector


def collect(context: RunContext) -> Outcome:
    context.app_profile.require_identification_rules({
        'notion.content': ('notion.workspace_page_and_database_identity', {'identity_fields': ['workspace_ref', 'page_ref', 'database_item_ref'], 'members_scope': 'selected_workspace'}),
        'notion.attachments': ('notion.parse_page_candidates', {'identity_basis': 'parent_page_and_attachment_ref', 'external_urls': 'out_of_scope'}),
        'notion.export': ('notion.top_level_export_scope', {'classification': ['general', 'database'], 'format': 'Markdown & CSV', 'include_subpages': True}),
    })
    target = context.base_context["condition"].get("target")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "account"
        or not isinstance(target.get("ref"), str)
        or not target["ref"]
    ):
        return Outcome(OutcomeStatus.FAILED, "notion_target_invalid")
    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED,
            "notion_app_start_not_verified",
            context={"target_ref": target["ref"]},
        )

    route = Route(context.base_context["route"])
    report = collector.collect(context, target["ref"], route)
    action = report.pop("_action", None)
    reason = report.get("reason_code")
    try:
        status = OutcomeStatus(report.get("status"))
    except ValueError:
        status = OutcomeStatus.FAILED
        reason = reason or "notion_report_invalid"
    details = {
        key: report[key]
        for key in (
            "account_count",
            "workspace_count",
            "workspace_selection_count",
            "cross_workspace_transition_count",
            "member_count",
            "members_unavailable_count",
            "members_unavailable_workspace_refs",
            "page_count",
            "database_count",
            "database_item_count",
            "database_out_of_scope_count",
            "attachment_count",
            "attachment_materialized_count",
            "attachment_out_of_scope_count",
            "discovered_top_level_count",
            "export_count",
            "general_page_export_count",
            "database_export_count",
            "include_subpages_count",
            "initial_workspace_restored",
            "failed_workspace_ref",
            "failed_page_ref",
            "failed_page_id",
            "collector_error",
        )
        if key in report
    }
    if status is OutcomeStatus.COMPLETE:
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
    elif status is OutcomeStatus.PARTIAL:
        context.interrupt_open_attempts(
            str(reason or "notion_collection_partial"),
            AcquisitionStatus.PARTIAL,
        )
    else:
        context.interrupt_open_attempts(
            str(reason or "notion_collection_failed")
        )
    return Outcome(
        status,
        reason,
        action=action,
        context={"target_ref": target["ref"]},
        details=details,
    )


__all__ = ["collect"]
