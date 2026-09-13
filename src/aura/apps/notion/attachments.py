"""Notion 0.6.4030 attachment parsing and materialization."""

from __future__ import annotations

import hashlib
import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .support import (
    NotionCollectorError,
    bounds,
    observe,
    item_attempt,
    onscreen,
    output_action,
    retain_pair,
    safe_component,
    write_json,
)


_FILE_BUTTON = re.compile(
    r"^(?P<name>.+?) (?P<size>\d+(?:\.\d+)? (?:B|KB|MB|GB))$"
)
_EXTERNAL_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_ATTACHMENT_ROOTS = (
    PurePosixPath("/sdcard/Download/Notion"),
    PurePosixPath("/sdcard/Pictures/Notion"),
    PurePosixPath("/sdcard/Movies/Notion"),
    PurePosixPath("/sdcard/Music/Notion"),
)


class AttachmentCollectionError(NotionCollectorError):
    def __init__(
        self,
        reason_code: str,
        *,
        action=None,
        attachments=(),
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.action = action
        self.attachments = tuple(attachments)


def candidate_key(
    candidate: Mapping[str, object],
) -> tuple[object, ...]:
    return (
        candidate.get("kind"),
        candidate.get("displayed_name"),
        candidate.get("displayed_size"),
        candidate.get("location"),
        candidate.get("open_mode"),
    )


def _parents(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {
        child: parent
        for parent in root.iter()
        for child in parent
    }


def _labels(node: ET.Element) -> set[str]:
    return {
        child.get("content-desc", "").strip()
        for child in node.iter()
        if onscreen(child) and child.get("content-desc", "").strip()
    }


def _container(
    node: ET.Element,
    parents: Mapping[ET.Element, ET.Element],
    predicate,
) -> ET.Element | None:
    current = parents.get(node)
    while current is not None:
        if predicate(current):
            return current
        current = parents.get(current)
    return None


def _candidate(
    kind: str,
    name: str | None,
    size: str | None,
    location: str,
    open_mode: str,
    box: tuple[int, int, int, int],
) -> dict[str, object]:
    return {
        "kind": kind,
        "displayed_name": name,
        "displayed_size": size,
        "location": location,
        "open_mode": open_mode,
        "bounds": box,
    }


def parse_page_candidates(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    result = []
    parents = _parents(root)
    for node in root.iter():
        if not onscreen(node):
            continue
        class_name = node.get("class", "")
        text = node.get("text", "").strip()
        if (
            class_name == "android.widget.EditText"
            and node.get("clickable") == "true"
            and _EXTERNAL_URL.fullmatch(text)
        ):
            result.append(_candidate(
                "external_url",
                text,
                None,
                "page_body",
                "none",
                bounds(node),
            ))
            continue
        if (
            class_name == "android.widget.Button"
            and node.get("clickable") == "true"
            and (match := _FILE_BUTTON.fullmatch(text))
        ):
            result.append(_candidate(
                "file",
                match.group("name"),
                match.group("size"),
                "page_body",
                "file_preview",
                bounds(node),
            ))
            continue
        if class_name == "android.widget.Image":
            left, top, right, bottom = bounds(node)
            if right - left >= 300 and bottom - top >= 150:
                result.append(_candidate(
                    "image",
                    None,
                    None,
                    "page_body",
                    "image_gallery",
                    (left, top, right, bottom),
                ))
            continue
        if (
            class_name == "android.widget.Button"
            and node.get("clickable") == "true"
            and node.get("content-desc") == "Open block actions menu"
            and _container(
                node,
                parents,
                lambda parent: {
                    "play",
                    "enter full screen",
                } <= _labels(parent),
            )
            is not None
        ):
            result.append(_candidate(
                "video",
                None,
                None,
                "page_body",
                "video_actions",
                bounds(node),
            ))
            continue
        if (
            class_name == "android.widget.Button"
            and node.get("clickable") == "true"
            and not text
            and not node.get("content-desc", "").strip()
        ):
            audio = _container(
                node,
                parents,
                lambda parent: (
                    {"play", "mute"} <= _labels(parent)
                    and "enter full screen" not in _labels(parent)
                    and any(
                        child.get("class") == "android.widget.SeekBar"
                        and onscreen(child)
                        for child in parent.iter()
                    )
                ),
            )
            if audio is not None:
                result.append(_candidate(
                    "audio",
                    None,
                    None,
                    "page_body",
                    "audio_menu",
                    bounds(node),
                ))
    blockers = [
        bounds(node)
        for node in root.iter()
        if onscreen(node)
        and node.get("resource-id", "").startswith("floating-toolbar.")
    ]
    result = [
        item
        for item in result
        if not any(
            item["bounds"][0] < right
            and item["bounds"][2] > left
            and item["bounds"][1] < bottom
            and item["bounds"][3] > top
            for left, top, right, bottom in blockers
        )
    ]
    result.sort(key=lambda item: (
        item["bounds"][1],
        item["bounds"][0],
    ))
    return tuple(result)


def parse_database_property_candidates(
    root: ET.Element,
) -> tuple[dict[str, object], ...]:
    labels = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.view.View"
        and node.get("text", "").strip() == "Files & media"
    ]
    if not labels:
        return ()
    if len(labels) != 1:
        raise NotionCollectorError(
            "Notion Files & media property is ambiguous"
        )
    _, top, _, bottom = bounds(labels[0])
    result = []
    for node in root.iter():
        if (
            not onscreen(node)
            or node.get("class") != "android.widget.Button"
            or node.get("clickable") != "true"
            or not node.get("text", "").strip()
            or node.get("text", "").strip() == "Empty"
        ):
            continue
        left, button_top, right, button_bottom = bounds(node)
        if button_top < bottom and button_bottom > top:
            result.append(_candidate(
                "file",
                node.get("text", "").strip(),
                None,
                "database_property",
                "file_preview",
                (left, button_top, right, button_bottom),
            ))
    result.sort(key=lambda item: (
        item["bounds"][1],
        item["bounds"][0],
    ))
    return tuple(result)


def _bottom_actions(
    root: ET.Element,
) -> tuple[ET.Element, ...]:
    return tuple(
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.view.View"
        and node.get("clickable") == "true"
        and bounds(node)[1] >= 2000
    )


def parse_materialize_control(
    root: ET.Element,
    *,
    kind: str,
    expected_filename: str | None,
) -> tuple[int, int, int, int]:
    if kind == "image":
        pagers = [
            node
            for node in root.iter()
            if onscreen(node)
            and node.get("resource-id") == "notion.id:id/imagesPager"
        ]
        downloads = [
            node
            for node in root.iter()
            if onscreen(node)
            and node.get("resource-id") == "notion.id:id/download"
            and node.get("clickable") == "true"
        ]
        if len(pagers) == len(downloads) == 1:
            return bounds(downloads[0])
    elif kind == "audio":
        downloads = [
            node
            for node in root.iter()
            if onscreen(node)
            and node.get("class") == "android.view.MenuItem"
            and node.get("content-desc") == "download media"
            and node.get("clickable") == "true"
        ]
        if len(downloads) == 1:
            return bounds(downloads[0])
    elif kind in {"file", "video"}:
        if kind == "file":
            names = [
                node
                for node in root.iter()
                if onscreen(node)
                and node.get("class") == "android.widget.TextView"
                and node.get("text", "").strip() == expected_filename
            ]
            verified = (
                isinstance(expected_filename, str)
                and bool(expected_filename)
                and len(names) == 1
            )
        else:
            verified = (
                any(
                    onscreen(node)
                    and node.get("class") == "android.widget.Button"
                    and node.get("content-desc") == "play"
                    for node in root.iter()
                )
                and any(
                    onscreen(node)
                    and node.get("class") == "android.widget.SeekBar"
                    for node in root.iter()
                )
            )
        actions = _bottom_actions(root)
        if verified and len(actions) == 2:
            return bounds(max(actions, key=lambda node: bounds(node)[0]))
    else:
        raise NotionCollectorError("Notion attachment kind is invalid")
    raise NotionCollectorError(
        "Notion attachment materialization control is unavailable"
    )


def select_changed_file(
    before: tuple[tuple[str, int, int], ...],
    after: tuple[tuple[str, int, int], ...],
) -> tuple[str, int, int]:
    previous = {
        path: (size, mtime)
        for path, size, mtime in before
    }
    changed = [
        row
        for row in after
        if previous.get(row[0]) != row[1:]
    ]
    if len(changed) != 1:
        raise AttachmentCollectionError(
            "notion_attachment_inventory_ambiguous"
        )
    return changed[0]


def _profile(context):
    try:
        roots = tuple(
            PurePosixPath(value)
            for value in context.app_profile.parameters[
                "attachment_roots"
            ]
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
        raise AttachmentCollectionError(
            "notion_attachment_profile_invalid"
        ) from None
    if roots != _ATTACHMENT_ROOTS or probes < 2 or interval < 0:
        raise AttachmentCollectionError(
            "notion_attachment_profile_invalid"
        )
    return roots, probes, interval


def _inventory_command(
    roots: tuple[PurePosixPath, ...],
) -> str:
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


def _scan_files(
    context,
    roots: tuple[PurePosixPath, ...],
) -> tuple[tuple[str, int, int], ...]:
    output = context.device.shell(_inventory_command(roots))
    fields = output.split("\0")
    if fields[-1:] != [""]:
        raise AttachmentCollectionError(
            "notion_attachment_inventory_invalid"
        )
    fields.pop()
    if len(fields) % 3:
        raise AttachmentCollectionError(
            "notion_attachment_inventory_invalid"
        )
    records = []
    for index in range(0, len(fields), 3):
        remote, size_raw, mtime_raw = fields[index:index + 3]
        path = PurePosixPath(remote)
        try:
            size, mtime = int(size_raw), int(mtime_raw)
        except ValueError:
            raise AttachmentCollectionError(
                "notion_attachment_inventory_invalid"
            ) from None
        if (
            not path.is_absolute()
            or ".." in path.parts
            or not any(path.is_relative_to(root) for root in roots)
            or size < 0
            or mtime < 0
        ):
            raise AttachmentCollectionError(
                "notion_attachment_inventory_invalid"
            )
        records.append((path.as_posix(), size, mtime))
    if len({row[0] for row in records}) != len(records):
        raise AttachmentCollectionError(
            "notion_attachment_inventory_invalid"
        )
    return tuple(sorted(records))


def _await_stable_change(context, roots, baseline):
    _, probes, interval = _profile(context)
    previous = None
    for probe in range(probes):
        if probe:
            time.sleep(interval)
        current = _scan_files(context, roots)
        try:
            changed = select_changed_file(baseline, current)
        except AttachmentCollectionError:
            changed = None
        if changed is not None and changed == previous:
            return changed
        previous = changed
    raise AttachmentCollectionError(
        "notion_attachment_inventory_did_not_settle"
    )


def _action_name(sequence, stem: str) -> str:
    return f"{stem}_{next(sequence):06d}"


def _video_original_bounds(root: ET.Element):
    headings = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == "Actions"
    ]
    types = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.widget.TextView"
        and node.get("text", "").strip() == "Video"
    ]
    originals = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.view.View"
        and node.get("text", "").strip() == "View original"
    ]
    if not (
        len(headings) == len(types) == len(originals) == 1
    ):
        raise AttachmentCollectionError(
            "notion_video_original_action_unavailable"
        )
    return bounds(originals[0])


def _attachment_prefix(
    parent_prefix: str,
    attachment_ref: str,
    label: object,
) -> str:
    if (
        not attachment_ref
        or "/" in attachment_ref
        or PurePosixPath(attachment_ref).name != attachment_ref
    ):
        raise AttachmentCollectionError(
            "notion_attachment_ref_invalid"
        )
    return (
        f"{parent_prefix}/attachments/{attachment_ref} "
        f"({safe_component(label)})"
    )


def _write_attachment(
    context,
    *,
    parent_prefix,
    attachment_ref,
    linked,
    record,
    source,
    action,
    payload=None,
):
    label = (
        record.get("retained_basename")
        or record.get("displayed_name")
        or record["kind"]
    )
    prefix = _attachment_prefix(
        parent_prefix,
        attachment_ref,
        label,
    )
    artifact_context = {
        **dict(linked),
        "attachment_ref": attachment_ref,
        "observation_id": source["observation_id"],
    }
    if payload is not None:
        attempt_id = item_attempt(
            context,
            artifact_context,
            record_kind="notion_attachment",
            prefix=prefix,
        )
        retained = output_action(
            context,
            attempt_id,
            action,
            artifact_context,
        )
        context.artifacts.write_bytes(
            f"{prefix}/{record['retained_basename']}",
            payload,
            kind="app_materialized_file",
            attempt_id=attempt_id,
            action=retained,
            context=artifact_context,
        )
    write_json(
        context,
        f"{prefix}/attachment.json",
        record,
        action,
        artifact_context,
    )


def _return_to_parent(
    context,
    sequence,
    *,
    kind,
    linked,
    attachment_ref,
    last_action,
    verify_parent,
):
    if kind in {"file", "image", "video"}:
        return_action = context.ui.back(
            _action_name(sequence, "notion_close_attachment"),
            context={**dict(linked), "attachment_ref": attachment_ref},
        )
    else:
        return_action = last_action
    returned = observe(
        context,
        _action_name(
            sequence,
            "observation_notion_attachment_parent",
        ),
        return_action,
    )
    if not verify_parent(returned["root"]):
        raise AttachmentCollectionError(
            "notion_attachment_parent_restore_failed",
            action=return_action,
        )
    return return_action, returned


def materialize_candidate(
    context,
    sequence,
    *,
    candidate: Mapping[str, object],
    attachment_ref: str,
    parent_prefix: str,
    linked: Mapping[str, object],
    observation: Mapping[str, object],
    entry_action,
    verify_parent,
):
    kind = str(candidate["kind"])
    item_attempt(context, {**dict(linked), "attachment_ref": attachment_ref}, retention=False)
    base_record = {
        "schema_version": "1.0",
        "record_kind": "notion_attachment",
        **dict(linked),
        "attachment_ref": attachment_ref,
        "kind": kind,
        "location": candidate["location"],
        "displayed_name": candidate.get("displayed_name"),
        "displayed_size": candidate.get("displayed_size"),
    }
    if kind == "external_url":
        source = retain_pair(
            context,
            (
                f"{_attachment_prefix(
                    parent_prefix,
                    attachment_ref,
                    candidate.get('displayed_name') or kind,
                )}/"
                "materialize-menu-screen"
            ),
            observation,
            entry_action,
            {**dict(linked), "attachment_ref": attachment_ref},
        )
        record = {
            **base_record,
            "device_source_path": None,
            "retained_basename": None,
            "size": None,
            "sha256": None,
            "acquisition_status": "out_of_scope",
            "reason_code": "notion_external_object_out_of_scope",
            "source": source,
        }
        _write_attachment(
            context,
            parent_prefix=parent_prefix,
            attachment_ref=attachment_ref,
            linked=linked,
            record=record,
            source=source,
            action=entry_action,
        )
        return entry_action, dict(observation), record

    roots, _, _ = _profile(context)
    baseline = _scan_files(context, roots)
    open_action = context.ui.click_bounds(
        _action_name(sequence, "notion_open_attachment"),
        candidate["bounds"],
        context={**dict(linked), "attachment_ref": attachment_ref},
    )
    control_observation = observe(
        context,
        _action_name(sequence, "observation_notion_attachment"),
        open_action,
    )
    control_action = open_action
    expected_filename = (
        str(candidate["displayed_name"])
        if candidate.get("displayed_name") is not None
        else None
    )
    try:
        control_bounds = (
            _video_original_bounds(control_observation["root"])
            if kind == "video"
            else parse_materialize_control(
                control_observation["root"],
                kind=kind,
                expected_filename=expected_filename,
            )
        )
    except NotionCollectorError:
        if not verify_parent(control_observation["root"]):
            raise
        source_prefix = _attachment_prefix(
            parent_prefix,
            attachment_ref,
            candidate.get("displayed_name") or kind,
        )
        source = retain_pair(
            context,
            f"{source_prefix}/materialize-menu-screen",
            control_observation,
            control_action,
            {**dict(linked), "attachment_ref": attachment_ref},
        )
        record = {
            **base_record,
            "device_source_path": None,
            "retained_basename": None,
            "size": None,
            "sha256": None,
            "acquisition_status": "not_materialized",
            "reason_code": "notion_attachment_control_unavailable",
            "source": source,
        }
        _write_attachment(
            context,
            parent_prefix=parent_prefix,
            attachment_ref=attachment_ref,
            linked=linked,
            record=record,
            source=source,
            action=control_action,
        )
        return control_action, control_observation, record
    if kind == "video":
        original_action = context.ui.click_bounds(
            _action_name(sequence, "notion_view_attachment_original"),
            control_bounds,
            context={
                **dict(linked),
                "attachment_ref": attachment_ref,
            },
        )
        control_observation = observe(
            context,
            _action_name(
                sequence,
                "observation_notion_attachment_preview",
            ),
            original_action,
        )
        control_action = original_action
        control_bounds = parse_materialize_control(
            control_observation["root"],
            kind=kind,
            expected_filename=expected_filename,
        )
    download_action = context.ui.click_bounds(
        _action_name(sequence, "notion_download_attachment"),
        control_bounds,
        context={**dict(linked), "attachment_ref": attachment_ref},
    )
    try:
        remote_path, size, _ = _await_stable_change(
            context,
            roots,
            baseline,
        )
        with TemporaryDirectory() as directory:
            local = Path(directory) / "attachment"
            context.device.pull(remote_path, local)
            payload = local.read_bytes()
        if len(payload) != size:
            raise AttachmentCollectionError(
                "notion_attachment_size_changed",
                action=download_action,
            )
    except Exception as error:
        reason = (
            error.reason_code
            if isinstance(error, AttachmentCollectionError)
            else "notion_attachment_pull_failed"
        )
        return_action, returned = _return_to_parent(
            context,
            sequence,
            kind=kind,
            linked=linked,
            attachment_ref=attachment_ref,
            last_action=download_action,
            verify_parent=verify_parent,
        )
        source_prefix = _attachment_prefix(
            parent_prefix,
            attachment_ref,
            candidate.get("displayed_name") or kind,
        )
        source = retain_pair(
            context,
            f"{source_prefix}/materialize-menu-screen",
            control_observation,
            control_action,
            {**dict(linked), "attachment_ref": attachment_ref},
        )
        record = {
            **base_record,
            "device_source_path": None,
            "retained_basename": None,
            "size": None,
            "sha256": None,
            "acquisition_status": "not_materialized",
            "reason_code": reason,
            "source": source,
        }
        _write_attachment(
            context,
            parent_prefix=parent_prefix,
            attachment_ref=attachment_ref,
            linked=linked,
            record=record,
            source=source,
            action=return_action,
        )
        return return_action, returned, record
    pull_action = context.journal.record_action(
        _action_name(sequence, "notion_pull_attachment"),
        status="success",
        context={**dict(linked), "attachment_ref": attachment_ref},
        details={"device_source_path": remote_path, "size": size},
    )
    _, returned = _return_to_parent(
        context,
        sequence,
        kind=kind,
        linked=linked,
        attachment_ref=attachment_ref,
        last_action=download_action,
        verify_parent=verify_parent,
    )
    basename = PurePosixPath(remote_path).name
    source_prefix = _attachment_prefix(
        parent_prefix,
        attachment_ref,
        basename,
    )
    source = retain_pair(
        context,
        f"{source_prefix}/materialize-menu-screen",
        control_observation,
        control_action,
        {**dict(linked), "attachment_ref": attachment_ref},
    )
    record = {
        **base_record,
        "device_source_path": remote_path,
        "retained_basename": basename,
        "size": size,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "acquisition_status": "materialized",
        "reason_code": None,
        "source": source,
    }
    _write_attachment(
        context,
        parent_prefix=parent_prefix,
        attachment_ref=attachment_ref,
        linked=linked,
        record=record,
        source=source,
        action=pull_action,
        payload=payload,
    )
    return pull_action, returned, record
