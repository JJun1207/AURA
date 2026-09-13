"""WhatsApp Export and single-chat Materialize collectors."""

from __future__ import annotations

from collections.abc import Mapping

from ...models import AcquisitionStatus, Outcome, OutcomeStatus, Route
from ...runtime import RunContext
from .collector import (
    WhatsAppCollectorError,
    collect_export,
    export_bluetooth_condition,
)
from .materialize import collect_materialize
from .traversal import (
    collect_active_chat_exports,
    collect_active_chats,
)


def collect(context: RunContext) -> Outcome:
    context.app_profile.require_identification_rules({
        'whatsapp.conversations': ('whatsapp.parse_message_window', {'identity_basis': 'chat_and_ordered_message_occurrence', 'merge': 'adjacent_window_overlap'}),
        'whatsapp.attachments': ('whatsapp.visible_media_occurrences', {'kinds': ['photo', 'video'], 'identity_basis': 'chat_and_attachment_ref'}),
        'whatsapp.chat_export': ('whatsapp.chat_export_scope', {'identity_basis': 'declared_chat_or_active_list_ordinal', 'delivery': 'bluetooth', 'extension': '.zip'}),
    })
    target = context.base_context["condition"].get("target")
    route = context.base_context["route"]
    if (
        not isinstance(target, Mapping)
        or not isinstance(target.get("ref"), str)
        or not target["ref"]
        or (
            target.get("kind") == "chat"
            and (
                not isinstance(target.get("display_name"), str)
                or not target["display_name"]
            )
        )
        or target.get("kind") not in {"chat", "chat_list"}
    ):
        return Outcome(OutcomeStatus.FAILED, "whatsapp_target_invalid")
    linked = {"target_ref": target["ref"]}
    if target.get("display_name"):
        linked["display_name"] = target["display_name"]
    if route == Route.EXPORT.value:
        try:
            export_bluetooth_condition(context.base_context["condition"])
        except WhatsAppCollectorError as error:
            return Outcome(
                OutcomeStatus.FAILED,
                error.reason_code,
                context=linked,
            )
    if not context.start_app():
        return Outcome(
            OutcomeStatus.FAILED,
            "whatsapp_app_start_not_verified",
            context=linked,
        )
    try:
        if route == Route.MATERIALIZE.value:
            if target["kind"] == "chat_list":
                outcome = collect_active_chats(context, target)
            else:
                outcome = collect_materialize(context, target)
        elif target["kind"] == "chat_list":
            outcome = collect_active_chat_exports(context, target)
        else:
            report = collect_export(context, target)
            outcome = Outcome(
                OutcomeStatus.COMPLETE,
                action=report["action"],
                context=linked,
                details={
                    "artifact_id": report["artifact_id"],
                    "receiver_label": report["receiver_label"],
                    "received_filename": report["received_filename"],
                },
            )
    except WhatsAppCollectorError as error:
        context.interrupt_open_attempts(error.reason_code)
        return Outcome(
            OutcomeStatus.PARTIAL,
            error.reason_code,
            action=error.action,
            context=linked,
        )
    context.complete_open_attempts(AcquisitionStatus.ACQUIRED)
    return outcome


__all__ = ["collect"]
