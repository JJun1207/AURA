"""Notion 0.6.4030 native Export collection."""

from __future__ import annotations
from ...artifacts import pull_bytes
from ...collection import one_candidate, display_text, poll_samples, stability_probe


import hashlib
from collections.abc import Mapping
from io import BytesIO
from itertools import count
from pathlib import PurePosixPath
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile


from ...models import AcquisitionStatus, ProcedureStatus
from ...ui import UiTimeout, hierarchy_signature
from .attachments import (
    AttachmentCollectionError, select_changed_file, _scan_files, _bottom_actions,
)
from .pages import (
    _action_name,
    _discover_top_level_pages,
    _find_home_item,
    _parse_page_entry,
    _verify_open_page,
    parse_page,
)
from .support import (
    NotionCollectorError,
    bounds,
    observe,
    item_attempt,
    onscreen,
    output_action,
    page_actions_bounds,
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
        payload=None,
    ):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.action = action
        self.report = report or {}
        self.completed_record = None
        self.payload = payload


def _unique(root: ET.Element, predicate, reason: str) -> ET.Element:
    matches = [
        node
        for node in root.iter()
        if onscreen(node, root) and predicate(node)
    ]
    try:
        return one_candidate(matches)
    except ValueError:
        raise ExportCollectionError(reason) from None


def parse_actions_export(
    root: ET.Element,
 profile) -> tuple[int, int, int, int]:
    node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("text") == display_text(profile, 'export')
            and item.get("clickable") == "true"
            and item.get("enabled") == "true"
        ),
        "notion_export_action_unavailable",
    )
    return bounds(node)


def parse_export_dialog(root: ET.Element, profile) -> dict[str, object]:
    format_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.widget.Button"
            and item.get("clickable") == "true"
            and item.get("text", "").startswith(display_text(profile, 'export_format_prefix'))
        ),
        "notion_export_format_unavailable",
    )
    include_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("clickable") == "true"
            and item.get("text") == display_text(profile, 'include_subpages')
        ),
        "notion_export_subpages_control_unavailable",
    )
    export_node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.widget.Button"
            and item.get("clickable") == "true"
            and item.get("text") == display_text(profile, 'export')
            and bounds(item)[1] > bounds(include_node)[3]
        ),
        "notion_export_submit_unavailable",
    )
    return {
        "format": format_node.get("text", "").removeprefix(
            display_text(profile, 'export_format_prefix')
        ),
        "format_bounds": bounds(format_node),
        "include_subpages_bounds": bounds(include_node),
        "export_bounds": bounds(export_node),
    }


def parse_export_format_option(
    root: ET.Element,
 profile) -> tuple[int, int, int, int]:
    node = _unique(
        root,
        lambda item: (
            item.get("class") == "android.view.MenuItem"
            and item.get("clickable") == "true"
            and item.get("enabled") == "true"
            and item.get("text") == display_text(profile, 'markdown_csv')
        ),
        "notion_export_markdown_csv_unavailable",
    )
    return bounds(node)


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
    actions = _bottom_actions(root)
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


def _observe_parse(context, observation_id, action, parser, reason: str, *, timeout=None):
    try:
        observation = observe(
            context,
            observation_id,
            action,
            parser,
            timeout=timeout,
        )
    except NotionCollectorError:
        raise ExportCollectionError(reason, action=action) from None
    return observation, observation["parsed"]


def _observe_open_page(context, sequence, page, action):
    title = str(page["title"])
    observation, parsed = _observe_parse(
        context,
        _action_name(sequence, "observation_notion_export_page"),
        action,
        lambda root: _parse_page_entry(root, title, context.app_profile),
        "notion_export_page_unavailable",
        timeout=context.app_profile.timings["export_page_entry_timeout"],
    )
    if parsed["classification"] == "retained_position":
        try:
            action, observation = _verify_open_page(context, sequence, page, action, observation)
            parsed = parse_page(observation["root"], title, context.app_profile)
        except NotionCollectorError as error:
            raise ExportCollectionError(str(error), action=getattr(error, "action", None) or action) from error
    return observation, parsed


def _actions_bounds(root: ET.Element):
    try:
        return page_actions_bounds(root)
    except NotionCollectorError:
        raise ExportCollectionError(
            "notion_export_actions_unavailable"
        ) from None


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
            export_bounds = parse_actions_export(observation["root"], profile=context.app_profile)
            break
        except ExportCollectionError:
            signature = hierarchy_signature(observation["root"])
            if signature in seen:
                raise ExportCollectionError(
                    "notion_export_action_unavailable",
                    action=action,
                ) from None
            seen.add(signature)
            action = context.ui.swipe(
                _action_name(sequence, "notion_scroll_page_actions"),
                "up",
                distance_ratio=context.app_profile.parameters["menu_scroll_ratio"],
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
        lambda root: parse_export_dialog(root, profile=context.app_profile),
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
    dialog = parse_export_dialog(observation["root"], profile=context.app_profile)
    if dialog["format"] != display_text(context.app_profile, 'markdown_csv'):
        action = context.ui.click_bounds(
            _action_name(sequence, "notion_open_export_formats"),
            dialog["format_bounds"],
            context={"page_ref": page_ref},
        )
        format_bounds = _wait_for_parse(
            context,
            lambda root: parse_export_format_option(root, profile=context.app_profile),
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
            lambda root: parse_export_dialog(root, profile=context.app_profile),
            "notion_export_dialog_unavailable",
        )
        if dialog["format"] != display_text(context.app_profile, 'markdown_csv'):
            raise ExportCollectionError(
                "notion_export_format_unverified",
                action=action,
            )

    # ponytail: a newly opened dialog defaults Off; revalidate this procedure if
    # a future app version remembers the option. XML does not expose its state.
    action = context.ui.click_bounds(
        _action_name(sequence, "notion_enable_export_subpages"),
        dialog["include_subpages_bounds"],
        context={"page_ref": page_ref, "include_subpages_basis": "default_off_single_click"},
    )
    observation, dialog = _observe_parse(
        context,
        _action_name(sequence, "observation_notion_export_subpages"),
        action,
        lambda root: parse_export_dialog(root, profile=context.app_profile),
        "notion_export_dialog_unavailable",
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
        payload = pull_bytes(context.device, path.as_posix(), filename='export.zip')
    except Exception:
        raise ExportCollectionError(
            "notion_export_pull_failed"
        ) from None
    if len(payload) != expected_size:
        raise ExportCollectionError("notion_export_size_changed", payload=payload)
    try:
        with ZipFile(BytesIO(payload)) as archive:
            corrupt = archive.testzip()
    except (BadZipFile, OSError):
        raise ExportCollectionError(
            "notion_export_zip_invalid", payload=payload,
        ) from None
    if corrupt is not None:
        raise ExportCollectionError("notion_export_zip_corrupt", payload=payload)
    return payload


def _await_export(context, baseline, filename, action):
    root = _export_root(context)
    stable, state = stability_probe(
        lambda: _scan_files(context, (root,)),
        key=lambda current: select_changed_file(baseline, current),
        retry_errors=(AttachmentCollectionError,),
    )
    last_error = ExportCollectionError("notion_attachment_inventory_did_not_settle", action=action)
    for ready in poll_samples(stable,
            timeout=context.app_profile.timings["export_download_timeout"],
            interval=context.app_profile.timings["inventory_probe_interval"]):
        if not ready:
            continue
        remote_path, size, modified = state["key"]
        if PurePosixPath(remote_path).name != filename:
            raise ExportCollectionError("notion_export_filename_mismatch", action=action)
        try:
            payload = _pull_export(context, remote_path, size)
        except ExportCollectionError as error:
            if error.reason_code not in {
                "notion_export_size_changed", "notion_export_zip_invalid", "notion_export_zip_corrupt",
            }:
                error.action = action
                raise
            error.action = action
            last_error = error
            context.journal.record_action("notion_wait_export_zip", status="waiting",
                context=dict(action.context), details={
                    "device_source_path": remote_path, "candidate_size": size,
                    "candidate_mtime": modified, "received_size": len(error.payload),
                    "received_sha256": hashlib.sha256(error.payload).hexdigest(),
                    "validation_reason": error.reason_code,
                })
            continue
        return remote_path, payload
    raise last_error


def _return_to_page(
    context,
    sequence,
    page: Mapping[str, object],
    action,
):
    seen = set()
    # Preview may return directly to the page, or through the still-open
    # Export dialog and Actions sheet. Each known sheet is dismissed once.
    for _ in range(3):
        action = context.ui.back(_action_name(sequence, "notion_close_export_surface"),
                                 context=dict(action.context))
        observation = observe(context, _action_name(sequence, "observation_notion_export_return"), action)
        retain_pair(context, f"notion/diagnostics/export-return-{action.action_id}",
                    observation, action, dict(action.context))
        try:
            parse_page(observation["root"], str(page["title"]), context.app_profile)
            return action
        except NotionCollectorError:
            pass
        surface = None
        for name, parser in (("dialog", parse_export_dialog), ("actions", parse_actions_export)):
            try:
                parser(observation["root"], context.app_profile)
                surface = name
                break
            except NotionCollectorError:
                pass
        if surface is None or surface in seen:
            break
        seen.add(surface)
    raise ExportCollectionError("notion_export_page_return_failed", action=action)


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
    try:
        action, dialog_observation, dialog = _configure_export_dialog(
            context, sequence, page_ref, action, dialog_observation,
        )
    except ExportCollectionError as error:
        failed_action = error.action or action
        try:
            failure = observe(context, _action_name(sequence, "notion_export_configuration_failure"), failed_action)
            retain_pair(context, f"{_page_prefix(workspace_prefix, page)}/configuration-failure", failure,
                        failed_action, {"target_ref": target_ref, "workspace_ref": workspace_ref,
                                        "page_ref": page_ref, "page_kind": parsed["classification"]})
        except Exception as capture_error:
            context.journal.record_action("notion_export_failure_evidence", status="failed",
                context={"page_ref": page_ref}, details={"error": str(capture_error)})
        error.action = failed_action
        raise
    root = _export_root(context)
    baseline = _scan_files(context, (root,))
    linked = {
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "page_ref": page_ref,
        "notion_page_id": page["notion_page_id"],
        "page_kind": parsed["classification"],
    }
    prefix = _page_prefix(workspace_prefix, page)
    configuration_source = retain_pair(
        context, f"{prefix}/export-settings", dialog_observation, action, linked,
    )
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
        timeout=context.app_profile.timings['export_preview_timeout'],
    )
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
        remote_path, payload = _await_export(context, baseline, preview["filename"], download_action)
    except ExportCollectionError as error:
        if error.payload is not None:
            diagnostic_context = {
                **linked, "observation_id": source["observation_id"],
                "device_source_path": (root / preview["filename"]).as_posix(),
                "validation_reason": error.reason_code, "completion_wait_exhausted": True,
            }
            attempt_id = item_attempt(context, diagnostic_context,
                record_kind="notion_page_export", prefix=prefix)
            retained = output_action(context, attempt_id, download_action, diagnostic_context)
            context.artifacts.write_bytes(f"{prefix}/incomplete-export.zip", error.payload,
                kind="diagnostic", attempt_id=attempt_id, action=retained,
                context=diagnostic_context)
        raise
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
    pull_action = context.journal.record_action(
        _action_name(sequence, "notion_pull_export"),
        status="success",
        context=linked,
        details={"device_source_path": remote_path, "size": len(payload)},
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
        "include_subpages_basis": "default_off_single_click",
        "configuration_source": configuration_source,
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
    attempt_id = item_attempt(
        context,
        {"target_ref": target_ref, "workspace_ref": workspace_ref,
         "page_ref": page_ref, "page_kind": record["classification"]},
        record_kind="notion_page_export",
    )
    context.finish_attempt(attempt_id, ProcedureStatus.COMPLETED, AcquisitionStatus.ACQUIRED)
    return record, last_action


def collect_workspace_exports(
    context,
    workspace: Mapping[str, object],
    workspace_prefix: str,
    target_ref: str,
    *, entry=None,
) -> dict[str, object]:
    workspace_ref = str(workspace["workspace_ref"])
    sequence = count(1)
    records = []
    inventory, last_action = _discover_top_level_pages(
        context,
        sequence,
        workspace_ref,
        entry=entry,
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
