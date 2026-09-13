"""Telegram Materialize collector."""

from __future__ import annotations

from ...models import AcquisitionStatus, Outcome, OutcomeStatus
from ...runtime import RunContext
from . import collector
from .adapter import TelegramDeviceAdapter, TelegramOutputs


def collect(context: RunContext) -> Outcome:
    context.app_profile.require_identification_rules({
        'telegram.account': ('telegram.parse_account_profile', {'identity_basis': 'visible_account_profile', 'entry_selector': 'telegram.navigation.profile'}),
        'telegram.conversations': ('telegram.logical_chatroom_and_message_occurrences', {'chat_identity': 'canonical_account_and_container_metadata', 'message_identity': 'ordered_occurrence_and_window_overlap', 'stagnation_parameter': 'stagnation_rounds'}),
        'telegram.attachments': ('telegram.rendered_attachment_occurrences', {'kinds': ['photo', 'video', 'file'], 'identity_basis': 'chatroom_message_occurrence', 'inventory_parameter': 'inventory_probes'}),
    })
    target = context.base_context["condition"].get("target")
    if (
        not isinstance(target, dict)
        or target.get("kind") != "account"
        or not isinstance(target.get("ref"), str)
        or not target["ref"]
    ):
        return Outcome(OutcomeStatus.FAILED, "telegram_target_invalid")

    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED,
            "telegram_app_start_not_verified",
            context={"target_ref": target["ref"]},
        )
    device = TelegramDeviceAdapter(context)
    outputs = TelegramOutputs(context, device, target["ref"])
    report = collector.collect(device, outputs, target["ref"])
    reason = report.get("reason_code")
    try:
        status = OutcomeStatus(report.get("status"))
    except ValueError:
        status = OutcomeStatus.FAILED
        reason = reason or "telegram_report_invalid"
    details = {
        key: report[key]
        for key in ("scroll_probes", "occurrence_count")
        if key in report
    }
    if status is OutcomeStatus.COMPLETE:
        context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
    elif status is OutcomeStatus.PARTIAL:
        context.interrupt_open_attempts(
            str(reason or "telegram_collection_partial"),
            AcquisitionStatus.PARTIAL,
        )
    else:
        context.interrupt_open_attempts(
            str(reason or "telegram_collection_failed")
        )
    return Outcome(
        status,
        reason,
        action=device.last_action,
        context={"target_ref": target["ref"]},
        details=details,
    )


__all__ = ["collect"]
