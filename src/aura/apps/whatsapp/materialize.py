"""WhatsApp single-chat UI Materialize collector."""

from __future__ import annotations

import json
import shlex
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

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
    _safe_component,
    _settled_root,
    acquisition_item_attempt,
    chat_row_bounds,
    retention_action,
)


class WhatsAppMaterializeError(WhatsAppCollectorError):
    pass


_EXTENSIONS = {
    "photo": {".jpg", ".jpeg", ".png", ".webp"},
    "video": {".mp4", ".mkv", ".mov", ".3gp"},
}
_ROOTS = {
    "photo": (PurePosixPath("/sdcard/Pictures/WhatsApp"),),
    "video": (PurePosixPath("/sdcard/Movies/WhatsApp"),),
}


def _one_text(
    root: ET.Element,
    profile,
    element_id: str,
) -> str | None:
    matches = _matching(root, profile, element_id)
    if not matches:
        return None
    return matches[0].get("text", "").strip() or None


def _contains(outer, inner) -> bool:
    return (
        outer[0] <= inner[0]
        and outer[1] <= inner[1]
        and outer[2] >= inner[2]
        and outer[3] >= inner[3]
    )


def parse_message_window(hierarchy: str, profile) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(hierarchy)
    except (ET.ParseError, TypeError):
        raise WhatsAppMaterializeError(
            "whatsapp_message_window_invalid"
        ) from None
    lists = _matching(root, profile, "whatsapp.message-list")
    if len(lists) != 1:
        raise WhatsAppMaterializeError(
            "whatsapp_message_list_unavailable"
            if not lists
            else "whatsapp_message_list_ambiguous"
        )
    viewport = _bounds(lists[0])
    messages = []
    for row in lists[0]:
        row_bounds = _bounds(row)
        geometrically_full = (
            _contains(viewport, row_bounds)
            and viewport[1] < row_bounds[1]
            and row_bounds[3] < viewport[3]
        )
        marker = _one_text(row, profile, "whatsapp.date-divider")
        displayed_time = _one_text(
            row, profile, "whatsapp.message-time"
        )
        viewport_visibility = (
            "full"
            if geometrically_full and displayed_time is not None
            else "clipped"
        )
        text = _one_text(row, profile, "whatsapp.message-text")
        status = _matching(row, profile, "whatsapp.message-status")
        documents = _matching(
            row, profile, "whatsapp.document-content"
        )
        videos = _matching(row, profile, "whatsapp.video")
        photos = _matching(row, profile, "whatsapp.photo")
        media = _matching(row, profile, "whatsapp.media-container")

        kind = None
        attachment = None
        action_bounds = None
        if documents and _one_text(
            row, profile, "whatsapp.document-title"
        ):
            if len(documents) != 1:
                raise WhatsAppMaterializeError(
                    "whatsapp_document_action_ambiguous"
                )
            kind = "document"
            action_bounds = _bounds(documents[0])
            attachment = {
                "displayed_name": _one_text(
                    row, profile, "whatsapp.document-title"
                ),
                "displayed_size": _one_text(
                    row, profile, "whatsapp.document-size"
                ),
                "displayed_type": _one_text(
                    row, profile, "whatsapp.document-type"
                ),
            }
        elif videos:
            if len(videos) != 1:
                raise WhatsAppMaterializeError(
                    "whatsapp_video_action_ambiguous"
                )
            kind = "video"
            action_bounds = _bounds(videos[0])
            attachment = {
                "description": videos[0].get(
                    "content-desc", ""
                ).strip()
                or None
            }
        elif photos:
            if len(photos) != 1 or len(media) != 1:
                raise WhatsAppMaterializeError(
                    "whatsapp_photo_action_ambiguous"
                )
            kind = "photo"
            action_bounds = _bounds(media[0])
            attachment = {
                "description": photos[0].get(
                    "content-desc", ""
                ).strip()
                or None
            }
        elif text is not None:
            kind = "text"

        if kind is None:
            continue
        if action_bounds is not None and not (
            _contains(viewport, action_bounds)
            and viewport[1] < action_bounds[1]
            and action_bounds[3] < viewport[3]
        ):
            action_bounds = None
        messages.append({
            "kind": kind,
            "body": text,
            "displayed_time": (
                displayed_time.replace("\u202f", " ")
                if displayed_time is not None
                else None
            ),
            "date_marker": marker,
            "direction": "outgoing" if status else "incoming",
            "attachment": attachment,
            "action_bounds": action_bounds,
            "viewport_visibility": viewport_visibility,
        })
    return messages


def _message_key(message: dict[str, Any]) -> str:
    return json.dumps(
        {
            key: value
            for key, value in message.items()
            if key not in {
                "action_bounds",
                "attachment_ref",
                "date_marker",
                "materialization",
                "source",
                "viewport_visibility",
                "window_ref",
            }
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _same_message(
    older: dict[str, Any],
    newer: dict[str, Any],
) -> bool:
    if _message_key(older) == _message_key(newer):
        return True
    if (
        older.get("kind") != newer.get("kind")
        or older.get("body") != newer.get("body")
        or older.get("attachment") != newer.get("attachment")
    ):
        return False
    older_time = older.get("displayed_time")
    newer_time = newer.get("displayed_time")
    if older_time and newer_time and older_time != newer_time:
        return False
    if older.get("direction") == newer.get("direction"):
        return True
    return "clipped" in {
        older.get("viewport_visibility"),
        newer.get("viewport_visibility"),
    }


def _overlap_size(
    older: list[dict[str, Any]],
    collected: list[dict[str, Any]],
) -> int:
    for size in range(min(len(older), len(collected)), 0, -1):
        if all(
            _same_message(left, right)
            for left, right in zip(
                older[-size:], collected[:size], strict=True
            )
        ):
            return size
    return 0


def merge_older_messages(
    older: list[dict[str, Any]],
    collected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    overlap = _overlap_size(older, collected)
    resolved = [dict(row) for row in collected]
    for index in range(overlap):
        older_row = older[len(older) - overlap + index]
        if (
            older_row.get("viewport_visibility") == "full"
            and resolved[index].get("viewport_visibility") == "clipped"
        ):
            newer_marker = resolved[index].get("date_marker")
            resolved[index] = dict(older_row)
            if newer_marker and not resolved[index].get("date_marker"):
                resolved[index]["date_marker"] = newer_marker
            continue
        marker = older_row.get("date_marker")
        if marker and not resolved[index].get("date_marker"):
            resolved[index]["date_marker"] = marker
        if (
            older_row.get("materialization")
            and not resolved[index].get("materialization")
        ):
            resolved[index]["attachment_ref"] = older_row.get(
                "attachment_ref"
            )
            resolved[index]["materialization"] = older_row[
                "materialization"
            ]
    return [*older[: len(older) - overlap], *resolved]


def select_materialized_file(
    baseline: tuple[tuple[str, int, int], ...],
    current: tuple[tuple[str, int, int], ...],
    *,
    kind: str,
) -> tuple[str, int, int, str]:
    if kind not in {"photo", "video"}:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_kind_invalid"
        )
    before = {path: (size, mtime) for path, size, mtime in baseline}
    candidates = []
    for path, size, mtime in current:
        name = PurePosixPath(path).name
        allowed = (
            not name.startswith(".")
            and PurePosixPath(name).suffix.casefold()
            in _EXTENSIONS[kind]
        )
        if allowed:
            candidates.append((path, size, mtime))
    changed = [
        row
        for row in candidates
        if before.get(row[0]) != row[1:]
    ]
    if len(changed) == 1:
        return (*changed[0], "changed_media_file")
    if len(changed) > 1 or len(candidates) > 1:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_file_ambiguous",
            "materialized file is ambiguous",
        )
    if not candidates:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_file_unavailable"
        )
    return (
        *candidates[0],
        "unique_existing_media_candidate",
    )


def _chat_state(root: ET.Element, profile, display_name: str) -> bool:
    headers = [
        node
        for node in _matching(root, profile, "whatsapp.chat-screen")
        if node.get("text", "").strip() == display_name
    ]
    lists = _matching(root, profile, "whatsapp.message-list")
    if len(headers) != 1 or len(lists) != 1:
        raise WhatsAppMaterializeError(
            "whatsapp_chat_screen_unavailable"
        )
    return True


def _profile_roots(context, kind: str):
    try:
        roots = tuple(
            PurePosixPath(value)
            for value in context.app_profile.parameters[
                "materialize_roots"
            ][kind]
        )
        probes = int(
            context.app_profile.parameters["inventory_probes"]
        )
        interval = float(
            context.app_profile.timings[
                "inventory_probe_interval"
            ]
        )
    except (KeyError, TypeError, ValueError):
        raise WhatsAppMaterializeError(
            "whatsapp_materialize_profile_invalid"
        ) from None
    if (
        roots != _ROOTS[kind]
        or probes < 2
        or interval < 0
    ):
        raise WhatsAppMaterializeError(
            "whatsapp_materialize_profile_invalid"
        )
    return roots, probes, interval


def _inventory_command(roots: tuple[PurePosixPath, ...]) -> str:
    values = " ".join(
        shlex.quote(root.as_posix()) for root in roots
    )
    emit = (
        'for file do printf "%s\\000%s\\000%s\\000" "$file" '
        '"$(stat -c %s "$file")" "$(stat -c %Y "$file")"; done'
    )
    return (
        f"for root in {values}; do "
        '[ ! -d "$root" ] || find "$root" -mindepth 1 -type f '
        f"-exec sh -c {shlex.quote(emit)} sh {{}} +; done"
    )


def _scan_files(context, roots):
    fields = context.device.shell(
        _inventory_command(roots)
    ).split("\0")
    if fields[-1:] != [""]:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_inventory_invalid"
        )
    fields.pop()
    if len(fields) % 3:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_inventory_invalid"
        )
    records = []
    for index in range(0, len(fields), 3):
        remote, size_raw, mtime_raw = fields[index:index + 3]
        path = PurePosixPath(remote)
        try:
            size, mtime = int(size_raw), int(mtime_raw)
        except ValueError:
            raise WhatsAppMaterializeError(
                "whatsapp_attachment_inventory_invalid"
            ) from None
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not any(path.is_relative_to(root) for root in roots)
            or size < 0
            or mtime < 0
        ):
            raise WhatsAppMaterializeError(
                "whatsapp_attachment_inventory_invalid"
            )
        records.append((path.as_posix(), size, mtime))
    if len({row[0] for row in records}) != len(records):
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_inventory_invalid"
        )
    return tuple(sorted(records))


def _await_materialized_file(
    context,
    roots,
    baseline,
    *,
    kind,
):
    _, probes, interval = _profile_roots(context, kind)
    previous = None
    last_error = None
    current = baseline
    for probe in range(probes):
        if probe:
            time.sleep(interval)
        current = _scan_files(context, roots)
        try:
            selected = select_materialized_file(
                baseline,
                current,
                kind=kind,
            )
        except WhatsAppMaterializeError as error:
            last_error = error
            continue
        if (
            selected[-1].startswith(
                ("unique_existing", "already_available")
            )
            or selected == previous
        ):
            return selected, current
        previous = selected
    if last_error is not None:
        raise last_error
    raise WhatsAppMaterializeError(
        "whatsapp_attachment_file_did_not_settle"
    )


def _media_view_state(root: ET.Element, profile):
    viewers = _matching(root, profile, "whatsapp.media-viewer")
    if len(viewers) != 1:
        raise WhatsAppMaterializeError(
            "whatsapp_media_viewer_unavailable"
        )
    saves = _matching(root, profile, "whatsapp.media-save")
    controls = _matching(root, profile, "whatsapp.media-controls")
    if len(saves) > 1 or len(controls) > 1:
        raise WhatsAppMaterializeError(
            "whatsapp_media_controls_ambiguous"
        )
    return (
        _bounds(saves[0]) if saves else None,
        _bounds(controls[0]) if controls else None,
    )


def _media_save_state(root: ET.Element, profile):
    state = _media_view_state(root, profile)
    if state[0] is None:
        raise WhatsAppMaterializeError(
            "whatsapp_media_save_unavailable"
        )
    return state


def _return_to_chat(context, target, linked):
    try:
        root = ET.fromstring(context.device.hierarchy())
        _chat_state(
            root, context.app_profile, target["display_name"]
        )
        return context.journal.record_action(
            "verify_whatsapp_attachment_return",
            status="success",
            context=linked,
        )
    except (ET.ParseError, WhatsAppMaterializeError):
        action = context.ui.back(
            "close_whatsapp_attachment",
            context=linked,
        )
        _settled_root(
            context,
            lambda value: _chat_state(
                value,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_attachment_return_failed",
        )
        return action


def _retained_basename(remote_path: str) -> str:
    name = PurePosixPath(remote_path).name
    if (
        not name
        or name in {".", ".."}
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_filename_invalid"
        )
    return name


def _materialize_attachment(
    context,
    target,
    prefix,
    row,
    attachment_ref,
    linked,
):
    kind = row["kind"]
    if kind not in {"photo", "video"}:
        raise WhatsAppMaterializeError(
            "whatsapp_attachment_kind_invalid"
        )
    attachment_context = {
        **dict(linked),
        "attachment_ref": attachment_ref,
        "attachment_kind": kind,
    }
    acquisition_item_attempt(context, attachment_context, document={"kind": kind})
    roots, _, _ = _profile_roots(context, kind)
    baseline = _scan_files(context, roots)

    open_action = context.ui.click_bounds(
        "open_whatsapp_attachment",
        row["action_bounds"],
        context=attachment_context,
    )
    viewer_root, viewer_hierarchy, controls = _settled_root(
        context,
        lambda root: _media_view_state(
            root, context.app_profile
        ),
        "whatsapp_media_viewer_unavailable",
    )
    save_bounds, control_bounds = controls
    menu_action = open_action
    if save_bounds is None:
        if control_bounds is None:
            raise WhatsAppMaterializeError(
                "whatsapp_media_save_unavailable",
                action=open_action,
            )
        menu_action = context.ui.click_bounds(
            "reveal_whatsapp_media_controls",
            control_bounds,
            context=attachment_context,
        )
        viewer_root, viewer_hierarchy, controls = _settled_root(
            context,
            lambda root: _media_save_state(
                root, context.app_profile
            ),
            "whatsapp_media_save_unavailable",
        )
        save_bounds = controls[0]
    source = _retain_observation(
        context,
        prefix,
        (
            f"attachments/{attachment_ref}/"
            "materialize-menu-screen"
        ),
        viewer_hierarchy,
        menu_action,
        attachment_context,
    )
    save_action = context.ui.click_bounds(
        "save_whatsapp_media",
        save_bounds,
        context=attachment_context,
    )
    selected, inventory = _await_materialized_file(
        context,
        roots,
        baseline,
        kind=kind,
    )
    remote_path, expected_size, mtime, selection_basis = selected
    basename = _retained_basename(remote_path)
    with tempfile.TemporaryDirectory(dir=context.run_dir) as directory:
        local = Path(directory) / basename
        context.device.pull(remote_path, local)
        if not local.is_file() or local.stat().st_size != expected_size:
            raise WhatsAppMaterializeError(
                "whatsapp_attachment_pull_size_mismatch",
                action=save_action,
            )
        pull_action = context.journal.record_action(
            "pull_whatsapp_attachment",
            status="success",
            context=attachment_context,
            details={
                "remote_path": remote_path,
                "size": expected_size,
            },
        )
        attempt_id = acquisition_item_attempt(
            context,
            attachment_context,
            document={
                "record_kind": "whatsapp_attachment",
                "kind": kind,
            },
        )
        retained = retention_action(
            context, attempt_id, pull_action, attachment_context
        )
        original = context.artifacts.retain_file(
            local,
            f"{prefix}/attachments/{attachment_ref}/{basename}",
            kind="app_materialized_file",
            attempt_id=attempt_id,
            action=retained,
            observation_id=source["observation_id"],
            context={
                **attachment_context,
                "observation_id": source["observation_id"],
            },
        )
    record = {
        "schema_version": "1.0",
        "record_kind": "whatsapp_attachment",
        "attachment_ref": attachment_ref,
        "kind": kind,
        "displayed": row["attachment"],
        "acquisition_status": "materialized",
        "device_source_path": remote_path,
        "retained_basename": basename,
        "size": expected_size,
        "sha256": original.sha256,
        "selection_basis": selection_basis,
        "source": source,
    }
    _write_json(
        context,
        prefix,
        f"attachments/{attachment_ref}/record.json",
        record,
        pull_action,
        attachment_context,
    )
    _write_json(
        context,
        prefix,
        f"attachments/{attachment_ref}/audit.json",
        {
            "schema_version": "1.0",
            "baseline_file_count": len(baseline),
            "final_file_count": len(inventory),
            "selected_path": remote_path,
            "selected_size": expected_size,
            "selected_mtime": mtime,
            "selection_basis": selection_basis,
        },
        pull_action,
        attachment_context,
    )
    try:
        _return_to_chat(context, target, attachment_context)
    except Exception as error:
        failure = WhatsAppMaterializeError(
            getattr(error, "reason_code", "whatsapp_attachment_return_failed"),
            str(error),
            action=getattr(error, "action", None),
        )
        failure.materialization = record
        raise failure from error
    return record


def _materialize_visible_attachments(
    context,
    target,
    prefix,
    rows,
    linked,
    cache,
):
    for row in reversed(rows):
        if row["kind"] not in {"photo", "video"}:
            continue
        if row.get("action_bounds") is None:
            continue
        if row.get("materialization"):
            continue
        attachment_ref = f"attachment-{len(cache) + 1:06d}"
        row["attachment_ref"] = attachment_ref
        try:
            result = _materialize_attachment(
                context,
                target,
                prefix,
                row,
                attachment_ref,
                linked,
            )
        except WhatsAppCollectorError as error:
            failure_context = {
                **dict(linked),
                "attachment_ref": attachment_ref,
                "attachment_kind": row["kind"],
            }
            attempt_id = acquisition_item_attempt(context, failure_context)
            source_action = error.action or next(
                (action for action in reversed(context.journal.actions)
                 if all(action.context.get(key) == value
                        for key, value in failure_context.items())),
                None,
            )
            if source_action is not None:
                failure_context["source_action_id"] = source_action.action_id
            failure_action = context.journal.record_action(
                "record_whatsapp_attachment_failure",
                status="failed",
                attempt_id=attempt_id,
                context=failure_context,
                details={"reason": error.reason_code},
            )
            error.action = failure_action
            source = {}
            try:
                source = _retain_observation(
                    context, prefix,
                    f"attachments/{attachment_ref}/failure-screen",
                    context.device.hierarchy(), failure_action, failure_context,
                )
            except Exception as capture_error:
                context.journal.record_action(
                    "retain_whatsapp_failure_observation",
                    status="failed",
                    attempt_id=attempt_id,
                    context=failure_context,
                    details={
                        "reason": error.reason_code,
                        "error_type": type(capture_error).__name__,
                        "error": str(capture_error),
                    },
                )
            if getattr(error, "materialization", None) is not None:
                recovery = _write_json(
                    context, prefix,
                    f"attachments/{attachment_ref}/recovery-failure.json",
                    {
                        "schema_version": "1.0",
                        "record_kind": "whatsapp_attachment_recovery_failure",
                        "attachment_ref": attachment_ref,
                        "acquisition_status": "materialized",
                        "reason_code": error.reason_code,
                        "error": str(error),
                        "source": source,
                    },
                    failure_action, {**failure_context, **source},
                )
                context.finish_attempt(
                    attempt_id, ProcedureStatus.INTERRUPTED,
                    AcquisitionStatus.ACQUIRED, reason=error.reason_code,
                    action=failure_action, observation_id=recovery.observation_id,
                    details={"recovery_failure_artifact_id": recovery.artifact_id},
                )
                raise error
            result = {
                "schema_version": "1.0",
                "record_kind": "whatsapp_attachment",
                "attachment_ref": attachment_ref,
                "kind": row["kind"],
                "displayed": row["attachment"],
                "acquisition_status": "not_materialized",
                "reason_code": error.reason_code,
                "source": source,
            }
            _write_json(
                context,
                prefix,
                f"attachments/{attachment_ref}/record.json",
                result,
                failure_action,
                {
                    **failure_context,
                    **source,
                },
            )
            try:
                _return_to_chat(context, target, failure_context)
            except Exception:
                raise error
        cache[attachment_ref] = result
        row["materialization"] = result


def _enter_chat(context, target: Mapping[str, str]):
    linked = {
        "target_ref": target["ref"],
        "display_name": target["display_name"],
    }

    def entry(root):
        try:
            _chat_state(
                root, context.app_profile, target["display_name"]
            )
            return "chat", None
        except WhatsAppMaterializeError:
            return (
                "list",
                chat_row_bounds(
                    root,
                    context.app_profile,
                    target["display_name"],
                ),
            )

    root, hierarchy, state = _settled_root(
        context, entry, "whatsapp_chat_target_unavailable"
    )
    if state[0] == "chat":
        action = context.journal.record_action(
            "reuse_open_whatsapp_chat",
            status="success",
            context=linked,
        )
        return action, root, hierarchy
    action = context.ui.click_bounds(
        "open_whatsapp_chat",
        state[1],
        context=linked,
    )
    root, hierarchy, _ = _settled_root(
        context,
        lambda value: _chat_state(
            value, context.app_profile, target["display_name"]
        ),
        "whatsapp_chat_screen_unavailable",
    )
    return action, root, hierarchy


def _window_signature(hierarchy: str) -> str:
    return sha256(hierarchy.encode("utf-8")).hexdigest()


def _write_bytes(
    context,
    prefix: str,
    relative: str,
    value: bytes,
    kind: str,
    action,
    linked: Mapping[str, Any],
    document: Mapping[str, object] | None = None,
):
    attempt_id = (
        context.item_attempt("whatsapp:list-summary", "whatsapp.conversations", "conversation_collection", context=dict(linked), retention=True)
        if document and document.get("record_kind") in {"whatsapp_active_chat_traversal", "whatsapp_active_chat_export_traversal"}
        else acquisition_item_attempt(context, linked, document=document)
    )
    retained = retention_action(context, attempt_id, action, linked)
    return context.artifacts.write_bytes(
        f"{prefix}/{relative}",
        value,
        kind=kind,
        attempt_id=attempt_id,
        action=retained,
        observation_id=next(
            (observation.observation_id for observation in context.observations
             if observation.observation_id == linked.get("observation_id")
             and observation.attempt_id == attempt_id),
            None,
        ),
        context=dict(linked),
    )


def _write_json(
    context,
    prefix: str,
    relative: str,
    value: object,
    action,
    linked: Mapping[str, Any],
):
    artifact = _write_bytes(
        context,
        prefix,
        relative,
        (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
        "structured_json",
        action,
        linked,
        value if isinstance(value, Mapping) else None,
    )
    if isinstance(value, Mapping) and value.get("acquisition_status") in {
        "partial",
        "not_materialized",
        "unavailable",
    }:
        attempt_id = acquisition_item_attempt(
            context, linked, document=value
        )
        attempt = next(
            item for item in context.attempts if item.attempt_id == attempt_id
        )
        if attempt.procedure_status is None:
            status = value["acquisition_status"]
            context.finish_attempt(
                attempt_id,
                ProcedureStatus.COMPLETED,
                (
                    AcquisitionStatus.PARTIAL
                    if status == "partial"
                    else AcquisitionStatus.NOT_ACQUIRED
                ),
                reason=str(value.get("reason_code") or status),
                action=next(action for action in context.journal.actions
                            if action.action_id == artifact.action_id),
                observation_id=artifact.observation_id,
            )
    return artifact


def _retain_observation(
    context,
    prefix: str,
    stem: str,
    hierarchy: str,
    action,
    linked: Mapping[str, Any],
):
    attempt_id = acquisition_item_attempt(context, linked)
    retained = retention_action(context, attempt_id, action, linked)
    screen, tree = context.retain_observation(
        f"{prefix}/{stem}",
        context.device.screenshot(),
        hierarchy.encode("utf-8"),
        attempt_id=attempt_id,
        action=retained,
        context=dict(linked),
    )
    observation_id = context.observations[-1].observation_id
    return {
        "observation_id": observation_id,
        "screen_artifact_id": screen.artifact_id,
        "ui_tree_artifact_id": tree.artifact_id,
    }


def _move_to_latest(context, target, root, hierarchy):
    linked = {
        "target_ref": target["ref"],
        "display_name": target["display_name"],
    }
    shortcuts = _matching(
        root, context.app_profile, "whatsapp.scroll-bottom"
    )
    if len(shortcuts) == 1:
        before = _window_signature(hierarchy)
        shortcut_action = context.ui.click_bounds(
            "whatsapp_go_to_most_recent_message",
            _bounds(shortcuts[0]),
            context=linked,
        )
        next_root, next_hierarchy, _ = _settled_root(
            context,
            lambda value: _chat_state(
                value,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_chat_screen_unavailable",
        )
        moved = _window_signature(next_hierarchy) != before
        context.journal.record_action(
            "verify_whatsapp_latest_shortcut",
            status="success",
            context=linked,
            details={
                "moved": moved,
                "shortcut_action_id": shortcut_action.event_id,
            },
        )
        if moved:
            root, hierarchy = next_root, next_hierarchy
    seen = set()
    while True:
        signature = _window_signature(hierarchy)
        if signature in seen:
            raise WhatsAppMaterializeError(
                "whatsapp_latest_boundary_cycle"
            )
        seen.add(signature)
        action = context.ui.swipe(
            "whatsapp_toward_latest_messages",
            "up",
            context=linked,
        )
        next_root, next_hierarchy, _ = _settled_root(
            context,
            lambda value: _chat_state(
                value,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_chat_screen_unavailable",
        )
        if _window_signature(next_hierarchy) == signature:
            return action, root, hierarchy
        root, hierarchy = next_root, next_hierarchy


def _date_value(value: str | None, today: date) -> date | None:
    if not value:
        return None
    normalized = value.strip()
    lowered = normalized.casefold()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    for pattern in ("%d %B %Y", "%d %b %Y", "%d %B", "%d %b"):
        try:
            parsed = datetime.strptime(normalized, pattern)
        except ValueError:
            continue
        return parsed.date().replace(
            year=parsed.year if "%Y" in pattern else today.year
        )
    return None


def _timestamp(
    date_value: date | None,
    displayed_time: str | None,
) -> str | None:
    if date_value is None or displayed_time is None:
        return None
    normalized = displayed_time.replace("\u202f", " ").strip()
    for pattern in ("%I:%M %p", "%H:%M"):
        try:
            parsed = datetime.strptime(normalized.upper(), pattern)
        except ValueError:
            continue
        return datetime.combine(
            date_value, parsed.time()
        ).isoformat(timespec="seconds")
    return None


def _finalize_messages(messages: list[dict[str, Any]]):
    current_date = None
    today = date.today()
    result = []
    for index, message in enumerate(messages, 1):
        marker_date = _date_value(message.get("date_marker"), today)
        if marker_date is not None:
            current_date = marker_date
        record = {
            key: value
            for key, value in message.items()
            if key != "action_bounds"
        }
        record.update({
            "schema_version": "1.0",
            "record_kind": "whatsapp_message",
            "message_ref": f"message-{index:06d}",
            "timestamp_local": _timestamp(
                current_date, message.get("displayed_time")
            ),
        })
        result.append(record)
    return result


def _conversation(messages: list[dict[str, Any]]) -> str:
    lines = []
    for message in messages:
        content = message.get("body") or f"<{message['kind']}>"
        when = (
            message.get("timestamp_local")
            or " ".join(
                filter(
                    None,
                    (
                        message.get("date_marker"),
                        message.get("displayed_time"),
                    ),
                )
            )
            or "unknown-time"
        )
        lines.append(
            f"[{when}] {message['direction']}: {content}"
        )
    return "\n".join(lines) + ("\n" if lines else "")


def collect_materialize(
    context,
    target: Mapping[str, str],
    *,
    already_open: bool = False,
    identity_basis: str = "declared_target_ref",
) -> Outcome:
    preceding_attempt_ids = {attempt.attempt_id for attempt in context.attempts}
    linked = {
        "target_ref": target["ref"],
        "display_name": target["display_name"],
    }
    logical_id = (
        "whatsapp-chat-"
        + sha256(target["ref"].encode("utf-8")).hexdigest()[:16]
    )
    prefix = (
        f"whatsapp/chatrooms/{logical_id} "
        f"({_safe_component(target['display_name'])})"
    )
    linked["logical_chatroom_id"] = logical_id
    context.begin_identification("whatsapp.conversations")
    context.begin_identification("whatsapp.attachments")
    acquisition_item_attempt(context, linked)
    if already_open:
        root, hierarchy, _ = _settled_root(
            context,
            lambda value: _chat_state(
                value,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_chat_screen_unavailable",
        )
    else:
        _, root, hierarchy = _enter_chat(context, target)
    latest_action, root, hierarchy = _move_to_latest(
        context, target, root, hierarchy
    )
    _retain_observation(
        context,
        prefix,
        "observations/latest-boundary",
        hierarchy,
        latest_action,
        linked,
    )

    collected: list[dict[str, Any]] = []
    attachment_cache: dict[str, dict[str, Any]] = {}
    seen = set()
    window_count = 0
    while True:
        signature = _window_signature(hierarchy)
        if signature in seen:
            raise WhatsAppMaterializeError(
                "whatsapp_history_cycle"
            )
        seen.add(signature)
        window_count += 1
        action = context.journal.record_action(
            "observe_whatsapp_message_window",
            status="success",
            context={**linked, "window_index": window_count},
        )
        source = _retain_observation(
            context,
            prefix,
            f"message-windows/message-window-{window_count:06d}",
            hierarchy,
            action,
            {**linked, "window_index": window_count},
        )
        rows = parse_message_window(
            hierarchy, context.app_profile
        )
        for row in rows:
            row["window_ref"] = f"message-window-{window_count:06d}"
            row["source"] = source
        overlap = _overlap_size(rows, collected)
        for index in range(overlap):
            previous = collected[index]
            current = rows[len(rows) - overlap + index]
            if previous.get("materialization"):
                current["attachment_ref"] = previous.get(
                    "attachment_ref"
                )
                current["materialization"] = previous[
                    "materialization"
                ]
        _materialize_visible_attachments(
            context,
            target,
            prefix,
            rows,
            linked,
            attachment_cache,
        )
        collected = merge_older_messages(rows, collected)

        swipe_action = context.ui.swipe(
            "whatsapp_toward_older_messages",
            "down",
            context=linked,
        )
        next_root, next_hierarchy, _ = _settled_root(
            context,
            lambda value: _chat_state(
                value,
                context.app_profile,
                target["display_name"],
            ),
            "whatsapp_chat_screen_unavailable",
        )
        if _window_signature(next_hierarchy) == signature:
            earliest_action = swipe_action
            break
        root, hierarchy = next_root, next_hierarchy

    _retain_observation(
        context,
        prefix,
        "observations/earliest-boundary",
        hierarchy,
        earliest_action,
        linked,
    )
    messages = _finalize_messages(collected)
    output_action = context.journal.record_action(
        "write_whatsapp_materialize_outputs",
        status="success",
        context=linked,
        details={
            "message_count": len(messages),
            "window_count": window_count,
        },
    )
    _write_json(
        context,
        prefix,
        "chatroom.json",
        {
            "schema_version": "1.0",
            "record_kind": "whatsapp_chatroom",
            "logical_chatroom_id": logical_id,
            "display_name": target["display_name"],
            "target_ref": target["ref"],
            "identity_basis": identity_basis,
        },
        output_action,
        linked,
    )
    for message in messages:
        _write_json(
            context,
            prefix,
            f"messages/{message['message_ref']}.json",
            message,
            output_action,
            {
                **linked,
                "message_ref": message["message_ref"],
                "observation_id": message["source"][
                    "observation_id"
                ],
            },
        )
    attachment_count = sum(
        message["kind"] in {"photo", "video"}
        for message in messages
    )
    materialized_attachment_count = sum(
        message.get("materialization", {}).get(
            "acquisition_status"
        )
        == "materialized"
        for message in messages
    )
    _write_json(
        context,
        prefix,
        "history.json",
        {
            "schema_version": "1.0",
            "record_kind": "whatsapp_history",
            "logical_chatroom_id": logical_id,
            "message_count": len(messages),
            "attachment_count": attachment_count,
            "materialized_attachment_count": (
                materialized_attachment_count
            ),
            "window_count": window_count,
            "latest_boundary_reached": True,
            "earliest_boundary_reached": True,
            "message_refs": [
                message["message_ref"] for message in messages
            ],
        },
        output_action,
        linked,
    )
    _write_bytes(
        context,
        prefix,
        "conversation.txt",
        _conversation(messages).encode("utf-8"),
        "conversation_text",
        output_action,
        linked,
    )
    status = (
        OutcomeStatus.COMPLETE
        if materialized_attachment_count == attachment_count
        else OutcomeStatus.PARTIAL
    )
    context.complete_open_attempts(AcquisitionStatus.ACQUIRED, attempt_ids={attempt.attempt_id for attempt in context.attempts} - preceding_attempt_ids)
    if context.base_context["condition"].get("target", {}).get("kind") == "chat":
        for target_id in ("whatsapp.conversations", "whatsapp.attachments"):
            context.finish_identification(target_id, completion_condition="chat_history_exhausted")
    return Outcome(
        status,
        (
            None
            if status is OutcomeStatus.COMPLETE
            else "whatsapp_attachment_materialize_incomplete"
        ),
        context=linked,
        action=output_action,
        details={
            "message_count": len(messages),
            "attachment_count": attachment_count,
            "materialized_attachment_count": (
                materialized_attachment_count
            ),
            "window_count": window_count,
        },
    )


__all__ = [
    "WhatsAppMaterializeError",
    "collect_materialize",
    "merge_older_messages",
    "parse_message_window",
    "select_materialized_file",
]
