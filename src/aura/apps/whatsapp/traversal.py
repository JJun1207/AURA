"""WhatsApp active chat-list traversal."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import sys
from operator import itemgetter
from ...collection import visit_candidates, iter_windows, visit_item, ordered_candidate, overlap_candidates
from ...parsing import node_values
from ...ui import hierarchy_signature
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
    retention_action,
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
        display_name = next(iter(node_values(names)), "")
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


def _name_signature(rows):
    return tuple(row["display_name"] for row in rows)


def _name_overlap(previous, current):
    return max(overlap_candidates(_name_signature(previous), _name_signature(current)), default=0)


def _settled_chat_rows(context):
    return _settled_root(
        context,
        lambda root: _active_chat_rows(
            root, context.app_profile
        ),
        "whatsapp_chat_list_unavailable",
    )


def _rewind_chat_list(context, linked, attempt_id):
    root, hierarchy, rows = _settled_chat_rows(context)
    signature = hierarchy_signature(_matching(root, context.app_profile, "whatsapp.chat-list")[0])
    seen = {signature}
    reason = "whatsapp_chat_list_top_limit"
    # ponytail: cap initial normalization at 100 scrolls; add a profile limit for larger lists.
    for step in range(100):
        before_signature = signature
        swipe = context.ui.swipe("whatsapp_toward_first_chat", "down", context=linked)
        root, hierarchy, rows = _settled_chat_rows(context)
        current = hierarchy_signature(_matching(root, context.app_profile, "whatsapp.chat-list")[0])
        if current == signature:
            reason = None
            break
        if current in seen:
            reason = "whatsapp_chat_list_top_cycle"
            break
        seen.add(current)
        signature = current
    action = context.journal.record_action(
        "verify_whatsapp_chat_list_top", status="failed" if reason else "success",
        attempt_id=attempt_id, context={**linked, "source_action_id": swipe.action_id},
        details={"reason": reason, "scroll_count": step + 1,
                 "before_signature": before_signature, "after_signature": current})
    retained = retention_action(context, attempt_id, action, linked)
    screen, _ = context.retain_observation(
        "whatsapp/observations/active-chat-list-top", context.device.screenshot(), hierarchy.encode("utf-8"),
        attempt_id=attempt_id, action=retained, context=dict(linked))
    if reason:
        raise WhatsAppCollectorError(reason, action=action)
    return rows, {"action_id": action.action_id, "observation_id": screen.observation_id,
                  "scroll_count": step + 1}


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
    summary_attempt_id = acquisition_item_attempt(context, linked, document={"record_kind": record_kind})
    target_ids = ("whatsapp.chat_export", "whatsapp.export_summary") if context.base_context.get("route") == "export" else ("whatsapp.conversations", "whatsapp.attachments")
    for target_id in target_ids:
        context.begin_identification(target_id)
    rows, top_boundary = _rewind_chat_list(context, linked, summary_attempt_id)
    previous_rows = []
    processed = []
    active_list_end_reached = False

    def read():
        return rows
    def advance():
        nonlocal rows, previous_rows, active_list_end_reached
        previous_rows = rows_after_return
        context.ui.swipe("whatsapp_toward_later_chats", "up", context=linked)
        _, _, rows = _settled_chat_rows(context)
        active_list_end_reached = _name_signature(rows) == _name_signature(previous_rows)
        return not active_list_end_reached
    # Only the adjacent boundary establishes completion; a revisited older window
    # remains an app-level identity question, not a silently completed traversal.
    for rows in iter_windows(read, advance, lambda _: None):
        overlap = _name_overlap(previous_rows, rows)
        rows_after_return = rows
        def reacquire(candidate):
            index, expected = candidate
            # Keep a fresh pre-click check: a settled return may still be an
            # intermediate animation. Only unchanged rows can reuse that settle.
            try:
                current = _active_chat_rows(ET.fromstring(context.device.hierarchy()), context.app_profile)
            except (ET.ParseError, TypeError, WhatsAppCollectorError):
                current = None
            if current is None or _name_signature(current) != _name_signature(rows_after_return):
                _, _, current = _settled_chat_rows(context)
            # A visited leading prefix may disappear, but all remaining occurrences must match.
            try:
                return ordered_candidate(rows, current, index, key=itemgetter("display_name"),
                                         allow_removed_prefix=True)
            except ValueError:
                raise WhatsAppCollectorError("whatsapp_chat_list_changed_after_return") from None
        for row in visit_candidates(enumerate(rows[overlap:], overlap), reacquire):
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
            def enter():
                context.ui.click_bounds(
                    "open_whatsapp_active_chat",
                    row["bounds"],
                    context=chat_context,
                )
            def read():
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
                    source_action = getattr(error, "action", None)
                    if source_action is not None:
                        item_context["source_action_id"] = source_action.action_id
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
                            "detail": str(error),
                        },
                    )
                    print(f"AURA: Chat acquisition failed ({row['display_name']}): {error}. Returning to the chat list to continue.",
                          file=sys.stderr, flush=True)
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
                return outcome
            _, (_, _, rows_after_return) = visit_item(
                enter, read, lambda: _restore_chat_list(context, chat_context))

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
            "active_list_start_reached": True,
            "top_boundary": top_boundary,
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
            "active_list_start_reached": True,
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
