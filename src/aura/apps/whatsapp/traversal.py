"""WhatsApp active chat-list traversal."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping

from ...models import (
    AcquisitionStatus,
    Outcome,
    OutcomeStatus,
    ProcedureStatus,
)
from .collector import (
    WhatsAppCollectorError,
    _bounds,
    _matching,
    _settled_root,
    acquisition_item_attempt,
    collect_export,
)
from .materialize import _write_json, collect_materialize


def _active_chat_rows(root, profile):
    lists = _matching(root, profile, "whatsapp.chat-list")
    if len(lists) != 1:
        raise WhatsAppCollectorError(
            "whatsapp_chat_list_unavailable"
            if not lists
            else "whatsapp_chat_list_ambiguous"
        )
    rows = []
    for row in _matching(
        lists[0], profile, "whatsapp.chat-row"
    ):
        names = _matching(row, profile, "whatsapp.chat-name")
        if len(names) != 1:
            raise WhatsAppCollectorError(
                "whatsapp_chat_row_invalid"
            )
        display_name = names[0].get("text", "").strip()
        if not display_name:
            raise WhatsAppCollectorError(
                "whatsapp_chat_row_invalid"
            )
        rows.append({
            "display_name": display_name,
            "bounds": _bounds(row),
        })
    if not rows:
        raise WhatsAppCollectorError("whatsapp_chat_list_empty")
    return rows


def parse_active_chat_rows(hierarchy: str, profile):
    try:
        root = ET.fromstring(hierarchy)
    except (ET.ParseError, TypeError):
        raise WhatsAppCollectorError(
            "whatsapp_chat_list_invalid"
        ) from None
    return _active_chat_rows(root, profile)


def _page_signature(rows):
    return tuple(
        (row["display_name"], row["bounds"]) for row in rows
    )


def _name_signature(rows):
    return tuple(row["display_name"] for row in rows)


def _name_overlap(previous, current):
    previous_names = _name_signature(previous)
    current_names = _name_signature(current)
    for size in range(
        min(len(previous_names), len(current_names)),
        0,
        -1,
    ):
        if previous_names[-size:] == current_names[:size]:
            return size
    return 0


def _settled_chat_rows(context):
    return _settled_root(
        context,
        lambda root: _active_chat_rows(
            root, context.app_profile
        ),
        "whatsapp_chat_list_unavailable",
    )


def _restore_chat_list(context, linked):
    last_error = None
    # ponytail: two backs cover the chat and one open media view; add
    # state-specific recovery only if another failure state is observed.
    for attempt in range(2):
        context.ui.back(
            "return_whatsapp_chat_list",
            context={**linked, "back_attempt": attempt + 1},
        )
        try:
            return _settled_chat_rows(context)
        except WhatsAppCollectorError as error:
            last_error = error
    raise last_error


def _collect_materialize_open(context, chat_target, chat_context):
    return collect_materialize(
        context,
        chat_target,
        already_open=True,
        identity_basis="active_list_ordinal",
    )


def _collect_export_open(context, chat_target, chat_context):
    report = collect_export(
        context,
        chat_target,
        already_open=True,
    )
    return Outcome(
        OutcomeStatus.COMPLETE,
        action=report["action"],
        context=chat_context,
        details={
            "artifact_id": report["artifact_id"],
            "received_filename": report["received_filename"],
        },
    )


def _collect_active_chats(
    context,
    target: Mapping[str, str],
    collect_open,
    *,
    summary_name: str,
    record_kind: str,
    incomplete_reason: str,
) -> Outcome:
    linked = {"target_ref": target["ref"]}
    context.item_attempt("whatsapp:list-summary", "whatsapp.conversations", "conversation_collection", context=linked)
    target_ids = ("whatsapp.chat_export",) if context.base_context.get("route") == "export" else ("whatsapp.conversations", "whatsapp.attachments")
    for target_id in target_ids:
        context.begin_identification(target_id)
    _, _, rows = _settled_chat_rows(context)
    previous_rows = []
    processed = []
    active_list_end_reached = False

    while True:
        overlap = _name_overlap(previous_rows, rows)
        rows_after_return = rows
        for row in rows[overlap:]:
            ordinal = len(processed) + 1
            chat_target = {
                "kind": "chat",
                "ref": (
                    f"{target['ref']}.chat-{ordinal:06d}"
                ),
                "display_name": row["display_name"],
            }
            chat_context = {
                **linked,
                "chat_ref": chat_target["ref"],
                "display_name": row["display_name"],
                "chat_ordinal": ordinal,
            }
            context.ui.click_bounds(
                "open_whatsapp_active_chat",
                row["bounds"],
                context=chat_context,
            )
            try:
                outcome = collect_open(
                    context,
                    chat_target,
                    chat_context,
                )
            except Exception as error:
                reason = getattr(
                    error,
                    "reason_code",
                    "whatsapp_chat_collection_exception",
                )
                item_context = {
                    **chat_context,
                    "target_ref": chat_target["ref"],
                }
                attempt_id = acquisition_item_attempt(
                    context, item_context
                )
                action = context.journal.record_action(
                    "collect_whatsapp_active_chat",
                    status="failed",
                    attempt_id=attempt_id,
                    context=item_context,
                    details={
                        "reason": reason,
                        "error_type": type(error).__name__,
                    },
                )
                outcome = Outcome(
                    OutcomeStatus.PARTIAL,
                    reason,
                    action=action,
                    context=chat_context,
                )
                attempt = next(
                    item
                    for item in context.attempts
                    if item.attempt_id == attempt_id
                )
                if attempt.procedure_status is None:
                    context.finish_attempt(
                        attempt_id,
                        ProcedureStatus.INTERRUPTED,
                        AcquisitionStatus.NOT_ACQUIRED,
                        reason=reason,
                        action=action,
                    )
            processed.append({
                "chat_ref": chat_target["ref"],
                "display_name": row["display_name"],
                "status": outcome.status.value,
                "reason": outcome.reason,
            })
            _, _, rows_after_return = _restore_chat_list(
                context, chat_context
            )

        previous_rows = rows_after_return
        before_scroll_signature = _page_signature(
            rows_after_return
        )
        context.ui.swipe(
            "whatsapp_toward_later_chats",
            "up",
            context=linked,
        )
        _, _, next_rows = _settled_chat_rows(context)
        if (
            _page_signature(next_rows)
            == before_scroll_signature
        ):
            active_list_end_reached = True
            break
        rows = next_rows

    completed_count = sum(
        item["status"] == OutcomeStatus.COMPLETE.value
        for item in processed
    )
    for target_id in target_ids:
        context.finish_identification(target_id, completion_condition="active_chat_list_exhausted" if active_list_end_reached and completed_count == len(processed) else None,
                                      reason=None if active_list_end_reached and completed_count == len(processed) else incomplete_reason)
    output_action = context.journal.record_action(
        f"write_{record_kind}",
        status="success",
        context=linked,
        details={
            "processed_count": len(processed),
            "completed_count": completed_count,
        },
    )
    _write_json(
        context,
        "whatsapp",
        summary_name,
        {
            "schema_version": "1.0",
            "record_kind": record_kind,
            "target_ref": target["ref"],
            "identity_basis": "active_list_ordinal",
            "active_list_end_reached": (
                active_list_end_reached
            ),
            "processed_count": len(processed),
            "completed_count": completed_count,
            "chats": processed,
        },
        output_action,
        linked,
    )
    status = (
        OutcomeStatus.COMPLETE
        if active_list_end_reached
        and completed_count == len(processed)
        else OutcomeStatus.PARTIAL
    )
    return Outcome(
        status,
        (
            None
            if status is OutcomeStatus.COMPLETE
            else incomplete_reason
        ),
        action=output_action,
        context=linked,
        details={
            "processed_count": len(processed),
            "completed_count": completed_count,
            "active_list_end_reached": (
                active_list_end_reached
            ),
        },
    )


def collect_active_chats(
    context,
    target: Mapping[str, str],
) -> Outcome:
    return _collect_active_chats(
        context,
        target,
        _collect_materialize_open,
        summary_name="active-chat-traversal.json",
        record_kind="whatsapp_active_chat_traversal",
        incomplete_reason="whatsapp_active_chat_traversal_incomplete",
    )


def collect_active_chat_exports(
    context,
    target: Mapping[str, str],
) -> Outcome:
    return _collect_active_chats(
        context,
        target,
        _collect_export_open,
        summary_name="active-chat-export-traversal.json",
        record_kind="whatsapp_active_chat_export_traversal",
        incomplete_reason=(
            "whatsapp_active_chat_export_traversal_incomplete"
        ),
    )


__all__ = [
    "collect_active_chat_exports",
    "collect_active_chats",
    "parse_active_chat_rows",
]
