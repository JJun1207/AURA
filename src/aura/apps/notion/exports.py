"""Notion 0.6.4030 native Export collection."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from io import BytesIO
from itertools import count
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from PIL import Image, UnidentifiedImageError

from ...ui import UiTimeout
from .attachments import _await_stable_change, _scan_files
from .pages import (
    _action_name,
    _discover_top_level_pages,
    _find_home_item,
    parse_page,
)
from .support import (
    NotionCollectorError,
    bounds,
    observe,
    item_attempt,
    onscreen,
    output_action,
    retain_pair,
    safe_component,
    selector,
    write_json,
)


class ExportCollectionError(NotionCollectorError):
    def __init__(
        self,
        reason_code: str,
        *,
        action=None,
        report=None,
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.action = action
        self.report = report or {}
        self.completed_record = None


def _unique(root: ET.Element, predicate, reason: str) -> ET.Element:
    matches = [
        node
        for node in root.iter()
        if onscreen(node) and predicate(node)
    ]
    if len(matches) != 1:
        raise ExportCollectionError(reason)
    return matches[0]


def parse_actions_export(
    root: ET.Element,
) -> tuple[int, int, int, int]:
    node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("text") == "Export"
            and item.get("clickable") == "true"
            and item.get("enabled") == "true"
        ),
        "notion_export_action_unavailable",
    )
    return bounds(node)


def parse_export_dialog(root: ET.Element) -> dict[str, object]:
    format_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.widget.Button"
            and item.get("clickable") == "true"
            and item.get("text", "").startswith("Export format ")
        ),
        "notion_export_format_unavailable",
    )
    include_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("clickable") == "true"
            and item.get("text") == "Include subpages"
        ),
        "notion_export_subpages_control_unavailable",
    )
    export_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.widget.Button"
            and item.get("clickable") == "true"
            and item.get("text") == "Export"
            and bounds(item)[1] > bounds(include_node)[3]
        ),
        "notion_export_submit_unavailable",
    )
    return {
        "format": format_node.get("text", "").removeprefix(
            "Export format "
        ),
        "format_bounds": bounds(format_node),
        "include_subpages_bounds": bounds(include_node),
        "export_bounds": bounds(export_node),
    }


def parse_export_format_option(
    root: ET.Element,
) -> tuple[int, int, int, int]:
    node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("clickable") == "true"
            and item.get("enabled") == "true"
            and item.get("text") == "Markdown & CSV"
        ),
        "notion_export_markdown_csv_unavailable",
    )
    return bounds(node)


def include_subpages_enabled(
    screen: bytes,
    row_bounds: tuple[int, int, int, int],
) -> bool:
    left, top, right, bottom = row_bounds
    try:
        with Image.open(BytesIO(screen)) as source:
            image = source.convert("RGB")
    except (OSError, UnidentifiedImageError):
        raise ExportCollectionError(
            "notion_export_subpages_screen_invalid"
        ) from None
    if (
        left < 0
        or top < 0
        or right <= left
        or bottom <= top
        or right > image.width
        or bottom > image.height
    ):
        raise ExportCollectionError(
            "notion_export_subpages_bounds_invalid"
        )
    switch = image.crop((
        left + (right - left) * 3 // 4,
        top,
        right,
        bottom,
    ))
    blue = sum(
        pixel_blue - red > 80
        and pixel_blue - green > 30
        and pixel_blue > 150
        for red, green, pixel_blue in switch.getdata()
    )
    return blue >= 100


def parse_export_preview(root: ET.Element) -> dict[str, object]:
    filename = _unique(
        root,
        lambda item: (
            item.get("class") == "android.widget.TextView"
            and item.get("text", "").startswith("ExportBlock-")
            and item.get("text", "").endswith(".zip")
        ),
        "notion_export_preview_filename_unavailable",
    )
    actions = [
        node
        for node in root.iter()
        if onscreen(node)
        and node.get("class") == "android.view.View"
        and node.get("clickable") == "true"
        and node.get("enabled") == "true"
        and bounds(node)[1] >= 2000
    ]
    if len(actions) != 2:
        raise ExportCollectionError(
            "notion_export_download_action_unavailable"
        )
    return {
        "filename": filename.get("text"),
        "download_bounds": bounds(
            max(actions, key=lambda item: bounds(item)[0])
        ),
    }


def _export_root(context) -> PurePosixPath:
    try:
        root = PurePosixPath(
            context.app_profile.parameters["export_root"]
        )
    except (KeyError, TypeError):
        raise ExportCollectionError(
            "notion_export_profile_invalid"
        ) from None
    if root != PurePosixPath("/sdcard/Download/Notion"):
        raise ExportCollectionError("notion_export_profile_invalid")
    return root


def _wait_for_parse(context, parser, reason: str):
    parsed = None

    def ready() -> bool:
        nonlocal parsed
        try:
            parsed = parser(
                ET.fromstring(context.device.hierarchy())
            )
        except (
            ET.ParseError,
            NotionCollectorError,
            TypeError,
        ):
            return False
        return True

    if not context.ui.wait_until(ready):
        raise ExportCollectionError(reason)
    return parsed


def _observe_parse(context, observation_id, action, parser, reason: str):
    try:
        observation = observe(
            context,
            observation_id,
            action,
            parser,
        )
    except NotionCollectorError:
        raise ExportCollectionError(reason, action=action) from None
    return observation, observation["parsed"]


def _observe_open_page(context, sequence, page, action):
    title = str(page["title"])
    return _observe_parse(
        context,
        _action_name(sequence, "observation_notion_export_page"),
        action,
        lambda root: parse_page(root, title),
        "notion_export_page_unavailable",
    )


def _actions_bounds(root: ET.Element):
    matches = {
        bounds(item)
        for item in root.iter()
        if onscreen(item)
        and (
            item.get("class") == "android.widget.Button"
            and item.get("clickable") == "true"
            and item.get("content-desc") == "Actions"
        )
    }
    if len(matches) != 1:
        raise ExportCollectionError(
            "notion_export_actions_unavailable"
        )
    return matches.pop()


def _open_export_dialog(
    context,
    sequence,
    page_ref: str,
    page_observation,
):
    action = context.ui.click_bounds(
        _action_name(sequence, "notion_open_page_actions"),
        _actions_bounds(page_observation["root"]),
        context={"page_ref": page_ref},
    )
    observation = observe(
        context,
        _action_name(sequence, "observation_notion_page_actions"),
        action,
    )
    seen = set()
    while True:
        try:
            export_bounds = parse_actions_export(observation["root"])
            break
        except ExportCollectionError:
            signature = observation["tree"]
            if signature in seen:
                raise ExportCollectionError(
                    "notion_export_action_unavailable",
                    action=action,
                ) from None
            seen.add(signature)
            action = context.ui.swipe(
                _action_name(sequence, "notion_scroll_page_actions"),
                "up",
                context={"page_ref": page_ref},
            )
            observation = observe(
                context,
                _action_name(
                    sequence,
                    "observation_notion_page_actions",
                ),
                action,
            )

    action = context.ui.click_bounds(
        _action_name(sequence, "notion_open_export_dialog"),
        export_bounds,
        context={"page_ref": page_ref},
    )
    observation, _ = _observe_parse(
        context,
        _action_name(sequence, "observation_notion_export_dialog"),
        action,
        parse_export_dialog,
        "notion_export_dialog_unavailable",
    )
    return action, observation


def _configure_export_dialog(
    context,
    sequence,
    page_ref: str,
    action,
    observation,
):
    dialog = parse_export_dialog(observation["root"])
    if dialog["format"] != "Markdown & CSV":
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_open_export_formats"),
            dialog["format_bounds"],
            context={"page_ref": page_ref},
        )
        format_bounds = _wait_for_parse(
            context,
            parse_export_format_option,
            "notion_export_markdown_csv_unavailable",
        )
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_select_markdown_csv"),
            format_bounds,
            context={"page_ref": page_ref},
        )
        observation, dialog = _observe_parse(
            context,
            _action_name(
                sequence,
                "observation_notion_export_dialog",
            ),
            action,
            parse_export_dialog,
            "notion_export_dialog_unavailable",
        )
        if dialog["format"] != "Markdown & CSV":
            raise ExportCollectionError(
                "notion_export_format_unverified",
                action=action,
            )

    if not include_subpages_enabled(
        observation["screen"],
        dialog["include_subpages_bounds"],
    ):
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_enable_export_subpages"),
            dialog["include_subpages_bounds"],
            context={"page_ref": page_ref},
        )
        observation = observe(
            context,
            _action_name(
                sequence,
                "observation_notion_export_subpages",
            ),
            action,
        )
        dialog = parse_export_dialog(observation["root"])
        if not include_subpages_enabled(
            observation["screen"],
            dialog["include_subpages_bounds"],
        ):
            raise ExportCollectionError(
                "notion_export_subpages_unverified",
                action=action,
            )
    return action, observation, dialog


def _pull_export(context, remote_path: str, expected_size: int) -> bytes:
    path = PurePosixPath(remote_path)
    root = _export_root(context)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path.parent != root
        or path.suffix.casefold() != ".zip"
    ):
        raise ExportCollectionError("notion_export_path_invalid")
    try:
        with TemporaryDirectory() as directory:
            local = Path(directory) / "export.zip"
            context.device.pull(path.as_posix(), local)
            payload = local.read_bytes()
    except Exception:
        raise ExportCollectionError(
            "notion_export_pull_failed"
        ) from None
    if len(payload) != expected_size:
        raise ExportCollectionError("notion_export_size_changed")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            corrupt = archive.testzip()
    except (BadZipFile, OSError):
        raise ExportCollectionError(
            "notion_export_zip_invalid"
        ) from None
    if corrupt is not None:
        raise ExportCollectionError("notion_export_zip_corrupt")
    return payload


def _return_to_page(
    context,
    sequence,
    page: Mapping[str, object],
    action,
):
    title = str(page["title"])
    seen = set()
    while True:
        action = context.ui.back(
            _action_name(sequence, "notion_close_export_surface"),
            context={"page_ref": page["page_ref"]},
        )
        observation = observe(
            context,
            _action_name(
                sequence,
                "observation_notion_export_return",
            ),
            action,
        )
        try:
            parse_page(observation["root"], title)
            return action
        except NotionCollectorError:
            pass
        if context.ui.wait_for(
            selector(context, "notion.home"),
            timeout=0,
        ):
            raise ExportCollectionError(
                "notion_export_page_return_failed",
                action=action,
            )
        signature = observation["tree"]
        if signature in seen:
            raise ExportCollectionError(
                "notion_export_page_return_failed",
                action=action,
            )
        seen.add(signature)


def _page_prefix(
    workspace_prefix: str,
    page: Mapping[str, object],
) -> str:
    component = (
        f'{page["page_ref"]} ({safe_component(page["title"])})'
    )
    if PurePosixPath(component).name != component:
        raise ExportCollectionError("notion_export_output_path_invalid")
    return f"{workspace_prefix}/exports/{component}"


def _export_page(
    context,
    sequence,
    workspace_ref: str,
    workspace_prefix: str,
    target_ref: str,
    page: Mapping[str, object],
    entry_action,
):
    page_ref = str(page["page_ref"])
    observation, parsed = _observe_open_page(
        context,
        sequence,
        page,
        entry_action,
    )
    item_attempt(context, {"target_ref": target_ref, "workspace_ref": workspace_ref, "page_ref": page_ref, "page_kind": parsed["classification"]},
                 record_kind="notion_page_export", prefix="export", retention=False)
    action, dialog_observation = _open_export_dialog(
        context,
        sequence,
        page_ref,
        observation,
    )
    action, dialog_observation, dialog = _configure_export_dialog(
        context,
        sequence,
        page_ref,
        action,
        dialog_observation,
    )
    root = _export_root(context)
    baseline = _scan_files(context, (root,))
    linked = {
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "page_ref": page_ref,
        "notion_page_id": page["notion_page_id"],
        "page_kind": parsed["classification"],
    }
    action = context.ui.click_bounds(
        _action_name(sequence, "notion_submit_export"),
        dialog["export_bounds"],
        context=linked,
    )
    preview_observation, preview = _observe_parse(
        context,
        _action_name(sequence, "observation_notion_export_preview"),
        action,
        parse_export_preview,
        "notion_export_preview_unavailable",
    )
    prefix = _page_prefix(workspace_prefix, page)
    source = retain_pair(
        context,
        f"{prefix}/export-screen",
        preview_observation,
        action,
        linked,
    )
    download_action = context.ui.click_bounds(
        _action_name(sequence, "notion_download_export"),
        preview["download_bounds"],
        context=linked,
    )
    try:
        remote_path, size, _ = _await_stable_change(
            context,
            (root,),
            baseline,
        )
    except NotionCollectorError as error:
        raise ExportCollectionError(
            getattr(
                error,
                "reason_code",
                "notion_export_inventory_unavailable",
            ),
            action=download_action,
        ) from error
    basename = PurePosixPath(remote_path).name
    if basename != preview["filename"]:
        raise ExportCollectionError(
            "notion_export_filename_mismatch",
            action=download_action,
        )
    payload = _pull_export(context, remote_path, size)
    pull_action = context.journal.record_action(
        _action_name(sequence, "notion_pull_export"),
        status="success",
        context=linked,
        details={"device_source_path": remote_path, "size": size},
    )
    artifact_context = {
        **linked,
        "observation_id": source["observation_id"],
    }
    attempt_id = item_attempt(
        context,
        artifact_context,
        record_kind="notion_page_export",
        prefix=prefix,
    )
    retained = output_action(
        context,
        attempt_id,
        pull_action,
        artifact_context,
    )
    context.artifacts.write_bytes(
        f"{prefix}/{basename}",
        payload,
        kind="app_export",
        attempt_id=attempt_id,
        action=retained,
        context=artifact_context,
    )
    record = {
        "schema_version": "1.0",
        "record_kind": "notion_page_export",
        **linked,
        "title": page["title"],
        "section": page["section"],
        "classification": parsed["classification"],
        "export_format": "markdown_csv",
        "include_subpages": True,
        "device_source_path": remote_path,
        "retained_basename": basename,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "acquisition_status": "complete",
        "reason_code": None,
        "source": source,
    }
    write_json(
        context,
        f"{prefix}/export.json",
        record,
        pull_action,
        {
            **linked,
            "observation_id": source["observation_id"],
        },
    )
    try:
        return record, _return_to_page(
            context,
            sequence,
            page,
            pull_action,
        )
    except (NotionCollectorError, UiTimeout) as failure:
        error = (
            failure
            if isinstance(failure, ExportCollectionError)
            else ExportCollectionError(
                "notion_export_page_return_failed",
                action=pull_action,
            )
        )
        error.completed_record = record
        raise error from failure


def _summary_document(
    target_ref: str,
    workspace_ref: str,
    records,
    *,
    discovered: int,
    status: str,
):
    complete = [
        item
        for item in records
        if item.get("acquisition_status") == "complete"
    ]
    return {
        "schema_version": "1.0",
        "record_kind": "notion_exports",
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "status": status,
        "discovered_top_level_count": discovered,
        "export_count": len(complete),
        "general_page_export_count": sum(
            item.get("classification") == "general_page"
            for item in complete
        ),
        "database_export_count": sum(
            item.get("classification") == "database"
            for item in complete
        ),
        "include_subpages_count": sum(
            item.get("include_subpages") is True
            for item in complete
        ),
        "exports": records,
    }


def _write_summary(
    context,
    workspace_prefix: str,
    document,
    action,
):
    artifact = write_json(
        context,
        f"{workspace_prefix}/exports/exports.json",
        document,
        action,
        {
            "target_ref": document["target_ref"],
            "workspace_ref": document["workspace_ref"],
        },
    )
    return {
        "artifact_id": artifact.artifact_id,
        "path": artifact.relative_path,
    }


def _recover_home(context, sequence, page_ref: str, action):
    home = selector(context, "notion.home")
    seen = set()
    while not context.ui.wait_for(home, timeout=0):
        action = context.ui.back(
            _action_name(sequence, "notion_recover_export_home"),
            context={"page_ref": page_ref},
        )
        observation = observe(
            context,
            _action_name(
                sequence,
                "observation_notion_export_recovery",
            ),
            action,
        )
        if observation["tree"] in seen:
            break
        seen.add(observation["tree"])
    return action


def _collect_top_level_export(
    context,
    sequence,
    workspace_ref: str,
    workspace_prefix: str,
    target_ref: str,
    page,
    last_action,
):
    page_ref = str(page["page_ref"])
    row, last_action = _find_home_item(
        context,
        sequence,
        workspace_ref,
        page_id=str(page["notion_page_id"]),
    )
    if row is None:
        raise ExportCollectionError(
            "notion_export_page_row_unavailable",
            action=last_action,
        )
    entry_action = context.ui.click_bounds(
        _action_name(sequence, "notion_open_export_page"),
        row["bounds"],
        context={
            "target_ref": target_ref,
            "workspace_ref": workspace_ref,
            "page_ref": page_ref,
            "notion_page_id": page["notion_page_id"],
        },
    )
    try:
        record, last_action = _export_page(
            context,
            sequence,
            workspace_ref,
            workspace_prefix,
            target_ref,
            page,
            entry_action,
        )
    except ExportCollectionError as error:
        error.action = error.action or entry_action
        raise
    try:
        last_action = context.ui.back(
            _action_name(sequence, "notion_close_export_page"),
            expected=selector(context, "notion.home"),
            context={
                "workspace_ref": workspace_ref,
                "page_ref": page_ref,
            },
        )
    except (NotionCollectorError, UiTimeout) as failure:
        error = (
            failure
            if isinstance(failure, ExportCollectionError)
            else ExportCollectionError(
                "notion_export_home_return_failed",
                action=last_action,
            )
        )
        error.completed_record = record
        raise error from failure
    return record, last_action


def collect_workspace_exports(
    context,
    workspace: Mapping[str, object],
    workspace_prefix: str,
    target_ref: str,
) -> dict[str, object]:
    workspace_ref = str(workspace["workspace_ref"])
    sequence = count(1)
    records = []
    inventory, last_action = _discover_top_level_pages(
        context,
        sequence,
        workspace_ref,
    )
    pages = tuple(inventory.values())
    for page_index, page in enumerate(pages):
        page_ref = str(page["page_ref"])
        try:
            record, last_action = _collect_top_level_export(
                context,
                sequence,
                workspace_ref,
                workspace_prefix,
                target_ref,
                page,
                last_action,
            )
            records.append(record)
        except (NotionCollectorError, UiTimeout) as failure:
            error = (
                failure
                if isinstance(failure, ExportCollectionError)
                else ExportCollectionError(
                    getattr(
                        failure,
                        "reason_code",
                        "notion_export_page_operation_failed",
                    ),
                    action=getattr(failure, "action", last_action),
                )
            )
            if error.completed_record is not None:
                records.append({
                    **error.completed_record,
                    "post_acquisition_status": "partial",
                    "post_acquisition_reason_code": error.reason_code,
                })
            else:
                classification = (
                    error.action.context.get("page_kind")
                    if error.action is not None
                    else None
                )
                records.append({
                    "page_ref": page_ref,
                    "notion_page_id": page["notion_page_id"],
                    "title": page["title"],
                    "section": page["section"],
                    **(
                        {"classification": classification}
                        if classification is not None
                        else {}
                    ),
                    "acquisition_status": "partial",
                    "reason_code": error.reason_code,
                })
            records.extend(
                {
                    "page_ref": pending["page_ref"],
                    "notion_page_id": pending["notion_page_id"],
                    "title": pending["title"],
                    "section": pending["section"],
                    "acquisition_status": "not_attempted",
                    "reason_code": None,
                }
                for pending in pages[page_index + 1:]
            )
            try:
                last_action = _recover_home(
                    context,
                    sequence,
                    page_ref,
                    error.action or last_action,
                )
            except (NotionCollectorError, UiTimeout):
                last_action = error.action or last_action
            document = _summary_document(
                target_ref,
                workspace_ref,
                records,
                discovered=len(pages),
                status="partial",
            )
            source = _write_summary(
                context,
                workspace_prefix,
                document,
                last_action,
            )
            error.action = last_action
            error.report = document
            error.source = source
            if error is failure:
                raise
            raise error from failure

    document = _summary_document(
        target_ref,
        workspace_ref,
        records,
        discovered=len(pages),
        status="complete",
    )
    source = _write_summary(
        context,
        workspace_prefix,
        document,
        last_action,
    )
    return {
        "status": "complete",
        "source": source,
        "discovered_top_level_count": document[
            "discovered_top_level_count"
        ],
        "export_count": document["export_count"],
        "general_page_export_count": document[
            "general_page_export_count"
        ],
        "database_export_count": document[
            "database_export_count"
        ],
        "include_subpages_count": document[
            "include_subpages_count"
        ],
        "_action": last_action,
    }
