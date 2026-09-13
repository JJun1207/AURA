"""Notesnook Export collector."""

from __future__ import annotations

from ...models import Outcome, OutcomeStatus, Route
from ...runtime import RunContext
from . import collector
from .adapter import NotesnookDeviceAdapter, NotesnookOutputs


def collect(context: RunContext) -> Outcome:
    context.app_profile.require_identification_rules({
        'notesnook.notes': ('notesnook.note_revision_and_trash_cards', {'identity_fields': ['note_ref', 'version_ref', 'trash_ref'], 'workspace_gate': 'authenticated_for_materialize'}),
        'notesnook.attachments': ('notesnook.attachment_rows', {'identity_fields': ['kind_label', 'filename', 'displayed_size'], 'parent_scope': 'note_ref'}),
        'notesnook.export': ('notesnook.whole_container_selection', {'minimum_initial_selected': 1, 'selection_condition': 'select_all_increases_count', 'extension': '.zip'}),
    })
    condition = context.base_context["condition"]
    target = condition.get("target")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "container"
        or not isinstance(target.get("ref"), str)
        or not target["ref"]
    ):
        return Outcome(OutcomeStatus.FAILED, "notesnook_target_invalid")
    acquisition_environment = condition.get("acquisition_environment")
    if acquisition_environment not in {"device_only", "controlled_online"}:
        return Outcome(
            OutcomeStatus.PARTIAL,
            "notesnook_acquisition_environment_invalid",
            context={"target_ref": target["ref"]},
        )

    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED,
            "notesnook_app_start_not_verified",
            context={"target_ref": target["ref"]},
        )
    device = NotesnookDeviceAdapter(context)
    outputs = NotesnookOutputs(context, device, target["ref"])
    try:
        account = collector.collect_account_context(
            device,
            outputs,
            target["ref"],
            acquisition_environment,
        )
    except Exception as error:
        outputs.preserve_failure(str(error))
        raise
    route = Route(context.base_context["route"])
    if not account["sync_gate_passed"]:
        report = {
            "status": "partial",
            "reason_code": "notesnook_sync_condition_mismatch",
            **account,
        }
    elif route is Route.EXPORT:
        try:
            report = collector.collect(device, outputs, target["ref"])
        except Exception as error:
            outputs.preserve_failure(str(error))
            raise
    elif account["workspace_state"] == "local_only":
        report = {
            "status": "partial",
            "reason_code": "notesnook_authentication_required",
            "target_ref": target["ref"],
            **account,
        }
    else:
        report = collector.materialize(device, outputs, target["ref"])
    report = {**account, **report}
    reason = report.get("reason_code")
    try:
        status = OutcomeStatus(report.get("status"))
    except ValueError:
        status = OutcomeStatus.FAILED
        reason = reason or "notesnook_report_invalid"
    details = {
        key: report[key]
        for key in (
            "workspace_state",
            "acquisition_environment",
            "sync_label",
            "sync_gate_passed",
            "selected_document_count",
            "export_path",
            "export_size",
            "export_sha256",
            "note_count",
            "history_version_count",
            "attachment_count",
            "trash_count",
            "current_note_partial_count",
            "failure",
        )
        if key in report
    }
    if report.get("status") == "partial" and not report.get("failure") and reason != "notesnook_current_note_partial":
        outputs.preserve_failure(str(reason))
    outputs.finalize()
    return Outcome(
        status,
        reason,
        action=device.last_action,
        context={"target_ref": target["ref"]},
        details=details,
    )


__all__ = ["collect"]
