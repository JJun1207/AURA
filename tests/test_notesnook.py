import json
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

from aura.apps.notesnook import adapter
from aura.apps.notesnook import collector
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.models import Route
from aura.profiles import ProfileStore
from aura.runtime import RunContext


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ProfileStore(ROOT / "profiles").load_app(
    "notesnook", "3.4.5"
)


def output_context(tmp_path, journal, route):
    return RunContext(
        run_dir=tmp_path,
        base_context={
            "route": route.value,
            "condition": {"acquisition_environment": "device_only"},
        },
        device=SimpleNamespace(),
        journal=journal,
        artifacts=ArtifactStore(tmp_path, journal),
        ui=SimpleNamespace(),
        app_profile=PROFILE,
        system_ui_profile=ProfileStore(ROOT / "profiles").load_system_ui(
            "samsung"
        ),
    )


def notes_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="search-header" '
        'content-desc="Search in Notes" clickable="true" focusable="true" '
        'visible-to-user="true" bounds="[0,40][1080,224]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.ScrollView" resource-id="list.id" '
        'scrollable="true" visible-to-user="true" bounds="[0,224][1080,2256]">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="note-item-0" '
        'clickable="true" visible-to-user="true" bounds="[0,610][1080,918]">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="listitem.menu" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[925,711][1030,816]"/></node></node></hierarchy>'
    )


def selection_tree(count=1, *, tied_select_all=False):
    anonymous = (
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" clickable="true" focusable="true" '
        'visible-to-user="true" bounds="[700,2100][780,2180]"/>'
    )
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="search-header" '
        f'content-desc="{count} selected" clickable="true" focusable="true" '
        'visible-to-user="true" bounds="[0,40][1080,200]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" visible-to-user="true" '
        'bounds="[0,2050][1080,2256]">'
        f"{anonymous}{anonymous if tied_select_all else ''}"
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="select-export" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[900,2100][1000,2180]"/></node></hierarchy>'
    )


def export_options_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" '
        'content-desc=".., Markdown + Frontmatter" clickable="true" '
        'focusable="true" visible-to-user="true" '
        'bounds="[40,500][1040,650]"/></hierarchy>'
    )


def documents_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.google.android.documentsui" '
        'class="android.widget.FrameLayout" visible-to-user="true">'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.TextView" text="Download" clickable="true" '
        'visible-to-user="true" bounds="[0,200][500,300]"/>'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.TextView" text="AURA" clickable="true" '
        'visible-to-user="true" bounds="[0,300][500,400]"/>'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.Button" resource-id="android:id/button1" '
        'clickable="true" visible-to-user="true" '
        'bounds="[700,2100][1050,2200]"/></node></hierarchy>'
    )


def download_directory_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.google.android.documentsui" '
        'class="android.widget.TextView" '
        'resource-id="com.google.android.documentsui:id/header_title" '
        'text="Files in Download" visible-to-user="true"/>'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.LinearLayout" '
        'resource-id="com.google.android.documentsui:id/item_root" '
        'clickable="true" visible-to-user="true" bounds="[72,927][528,1071]">'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.TextView" resource-id="android:id/title" '
        'text="AURA" visible-to-user="true" '
        'bounds="[213,970][320,1027]"/></node></hierarchy>'
    )


def current_aura_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.google.android.documentsui" '
        'class="android.widget.FrameLayout" visible-to-user="true">'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.TextView" '
        'resource-id="com.google.android.documentsui:id/header_title" '
        'text="Files in AURA" visible-to-user="true" '
        'bounds="[72,368][864,548]"/>'
        '<node package="com.google.android.documentsui" '
        'class="android.widget.Button" resource-id="android:id/button1" '
        'text="USE THIS FOLDER" clickable="true" visible-to-user="true" '
        'bounds="[42,2112][1038,2256]"/></node></hierarchy>'
    )


def export_success_tree(count=6, filename="Notesnook.zip"):
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" visible-to-user="true">'
        '<node package="com.streetwriters.notesnook" '
        f'class="android.widget.TextView" text="{count} notes exported" '
        'visible-to-user="true" bounds="[200,1400][800,1500]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" '
        f'text="Notes exported as {filename} successfully" '
        'visible-to-user="true" bounds="[50,1500][1030,1650]"/>'
        '</node></hierarchy>'
    )


def account_tree(email="analyst@example.test", sync_label="Synced 3m ago"):
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        f'class="android.widget.TextView" text="{email}" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        f'class="android.widget.TextView" text="{sync_label}" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="icon, Sync now" '
        'clickable="true" visible-to-user="true">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Sync now" '
        'visible-to-user="true"/></node>'
        '</hierarchy>'
    )


def side_menu_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="sidemenu-settings-icon" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[750,110][870,230]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="Notes" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[48,334][870,435]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="Trash" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[48,730][870,831]"/></hierarchy>'
    )


def two_note_tree():
    root = notes_tree()
    note_list = next(
        node for node in root.iter() if node.get("resource-id") == "list.id"
    )
    first = next(
        node for node in root.iter() if node.get("resource-id") == "note-item-0"
    )
    first.set("content-desc", "18-07-2026, Alpha Overview, marker-1")
    note_list.append(ET.fromstring(
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="note-item-1" '
        'content-desc="18-07-2026, Duplicate Note, marker-2" '
        'clickable="true" visible-to-user="true" bounds="[0,918][1080,1226]">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="listitem.menu" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[925,1019][1030,1124]"/></node>'
    ))
    return root


def active_note_tree():
    root = notes_tree()
    card = next(
        node for node in root.iter() if node.get("resource-id") == "note-item-0"
    )
    card.set("content-desc", "18-07-2026, Alpha Overview, marker-1")
    return root


def history_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Note history " '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" '
        'content-desc="18-07-2026 07:45 PM -  07:48 PM, 1w" '
        'clickable="true" visible-to-user="true" '
        'bounds="[50,1510][1031,1645]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" '
        'content-desc="18-07-2026 12:44 PM -  12:45 PM, 1w" '
        'clickable="true" visible-to-user="true" '
        'bounds="[50,1675][1031,1810]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" '
        'content-desc="18-07-2026 12:44 PM -  12:45 PM, 1w" '
        'clickable="true" visible-to-user="true" '
        'bounds="[50,1840][1031,1975]"/></hierarchy>'
    )


def history_version_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Alpha Overview " '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="AURA_DATASET_ID=N01" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" '
        'text="Plain English content for export verification." '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="Restore" '
        'clickable="true" visible-to-user="true">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Restore" '
        'visible-to-user="true"/></node>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="Delete permanently" '
        'clickable="true" visible-to-user="true">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Delete permanently" '
        'visible-to-user="true"/></node></hierarchy>'
    )


def history_diff_version_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Alpha Overview" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="AURA_" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.View" text="DATASET" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.View" '
        'text="Plain English content for export verification." '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Restore" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Delete permanently" '
        'visible-to-user="true"/></hierarchy>'
    )


def current_note_tree(*, body=True):
    body_node = (
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.EditText" '
        'text="AURA_DATASET_ID=N06&#10;Document-only local workspace boundary." '
        'visible-to-user="true"/>'
        if body
        else ""
    )
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="6 words" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.EditText" resource-id="editor-title" '
        'text="Boundary Note" visible-to-user="true"/>'
        f"{body_node}</hierarchy>"
    )


def note_menu_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Alpha Overview" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Created at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="18-07-2026 12:44 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Last edited at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="18-07-2026 07:47 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="icon-history" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[445,1474][628,1611]"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="icon-attachments" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[840,1474][1023,1611]"/></hierarchy>'
    )


def attachment_page_tree(*rows):
    root = ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="All files" '
        'visible-to-user="true"/></hierarchy>'
    )
    for index, (kind, filename, displayed_size) in enumerate(rows):
        ET.SubElement(
            root,
            "node",
            {
                "package": "com.streetwriters.notesnook",
                "class": "android.view.ViewGroup",
                "content-desc": (
                    f"{kind}, {filename}, File size: {displayed_size}"
                ),
                "clickable": "true",
                "visible-to-user": "true",
                "bounds": f"[42,{410 + index * 160}][1038,{566 + index * 160}]",
            },
        )
    return root


def attachment_list_tree():
    return attachment_page_tree(
        ("MD", "AURA-Readiness-Disposable.md", "175 B"),
    )


def attachment_detail_tree():
    return ET.fromstring(
        '<hierarchy>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" '
        'text="AURA-Readiness-Disposable.md" visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="text/markdown" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="175 B" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="1 note" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="2b4bd6ff462a098e" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Created at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="29-07-2026 12:44 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Last modified at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="29-07-2026 12:45 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="Download" '
        'clickable="true" visible-to-user="true" '
        'bounds="[64,1800][1016,1940]">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Download" '
        'visible-to-user="true"/></node></hierarchy>'
    )


def media_attachment_detail_tree(filename, mime_type, displayed_size):
    root = attachment_detail_tree()
    values = [
        node
        for node in root.iter()
        if node.get("class") == "android.widget.TextView"
    ]
    values[0].set("text", filename)
    values[1].set("text", mime_type)
    values[2].set("text", displayed_size)
    return root


def trash_list_tree():
    root = notes_tree()
    header = next(
        node for node in root.iter() if node.get("resource-id") == "search-header"
    )
    header.set("content-desc", "Search in Trash")
    card = next(
        node for node in root.iter() if node.get("resource-id") == "note-item-0"
    )
    card.set(
        "content-desc",
        "12:49 PM, AURA_TRASH_TEST, Deleted on 2026-07-29, Note",
    )
    return root


def trash_menu_tree():
    return ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="AURA_TRASH_TEST" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Created at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="29-07-2026 12:48 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Deleted at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="29-07-2026 12:49 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="Last edited at" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.widget.TextView" text="29-07-2026 12:48 PM" '
        'visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="Restore" '
        'clickable="true" visible-to-user="true"/>'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" content-desc="Delete" '
        'clickable="true" visible-to-user="true"/></hierarchy>'
    )


def test_account_record_reads_unique_email_and_sync_label():
    assert collector._account_record(account_tree()) == {
        "account_email": "analyst@example.test",
        "sync_label": "Synced 3m ago",
    }


@pytest.mark.parametrize(
    ("sync_label", "acquisition_environment", "expected"),
    [
        ("Sync failed 6m (Offline) ..", "device_only", True),
        ("Synced just now", "controlled_online", True),
        ("Syncing   ..", "device_only", False),
        ("Synced just now", "device_only", False),
        (
            "Sync failed 6m (Offline) ..",
            "controlled_online",
            False,
        ),
    ],
)
def test_sync_state_requires_matching_terminal_label(
    sync_label,
    acquisition_environment,
    expected,
):
    assert collector._sync_state(
        account_tree(sync_label=sync_label),
        acquisition_environment,
    ) == {
        "sync_label": sync_label,
        "sync_gate_passed": expected,
    }


def test_sync_state_rejects_unknown_acquisition_environment():
    with pytest.raises(
        collector.NotesnookCollectorError,
        match="acquisition environment",
    ):
        collector._sync_state(account_tree(), "online")


@pytest.mark.parametrize(
    "root",
    [
        ET.fromstring(
            '<hierarchy><node package="com.streetwriters.notesnook" '
            'class="android.widget.TextView" text="one@example.test" '
            'visible-to-user="true"/>'
            '<node package="com.streetwriters.notesnook" '
            'class="android.widget.TextView" text="two@example.test" '
            'visible-to-user="true"/>'
            '<node package="com.streetwriters.notesnook" '
            'class="android.widget.TextView" text="Synced 3m ago" '
            'visible-to-user="true"/></hierarchy>'
        ),
        account_tree(sync_label="Sync now"),
        ET.fromstring(
            '<hierarchy><node package="com.streetwriters.notesnook" '
            'class="android.view.ViewGroup" content-desc="icon, Sync now" '
            'clickable="true" visible-to-user="true"/></hierarchy>'
        ),
    ],
)
def test_account_record_rejects_ambiguous_or_missing_values(root):
    with pytest.raises(
        collector.NotesnookCollectorError,
        match="account",
    ):
        collector._account_record(root)


def test_note_cards_keep_distinct_current_ui_keys():
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    cards = collector._note_cards(device, two_note_tree())

    assert [card["source_key"] for card in cards] == [
        (
            "note-item-0",
            "18-07-2026, Alpha Overview, marker-1",
        ),
        (
            "note-item-1",
            "18-07-2026, Duplicate Note, marker-2",
        ),
    ]
    assert [card["menu_bounds"] for card in cards] == [
        (925, 711, 1030, 816),
        (925, 1019, 1030, 1124),
    ]


def test_history_rows_preserve_duplicate_labels_with_distinct_ordinals():
    rows = collector._history_rows(history_tree())

    assert [row["version_ref"] for row in rows] == [
        "version-000001",
        "version-000002",
        "version-000003",
    ]
    assert [row["label"] for row in rows][1:] == [
        "18-07-2026 12:44 PM -  12:45 PM, 1w",
        "18-07-2026 12:44 PM -  12:45 PM, 1w",
    ]


def test_history_version_reads_content_without_action_labels():
    record = collector._history_version(history_version_tree())

    assert record == {
        "title": "Alpha Overview",
        "visible_content": [
            "AURA_DATASET_ID=N01",
            "Plain English content for export verification.",
        ],
        "restore_control_observed": True,
        "delete_control_observed": True,
    }


def test_history_version_keeps_styled_diff_text_exposed_as_views():
    record = collector._history_version(history_diff_version_tree())

    assert record["visible_content"] == [
        "AURA_",
        "DATASET",
        "Plain English content for export verification.",
    ]


def test_note_menu_record_reads_title_and_timestamps():
    assert collector._note_menu_record(note_menu_tree()) == {
        "title": "Alpha Overview",
        "created_at": "18-07-2026 12:44 PM",
        "last_edited_at": "18-07-2026 07:47 PM",
    }


def test_attachment_rows_preserve_displayed_metadata():
    assert collector._attachment_rows(attachment_list_tree()) == ({
        "attachment_ref": "attachment-000001",
        "kind_label": "MD",
        "filename": "AURA-Readiness-Disposable.md",
        "displayed_size": "175 B",
        "bounds": (42, 410, 1038, 566),
    },)


def test_attachment_rows_preserve_mixed_media_metadata():
    rows = collector._attachment_rows(attachment_page_tree(
        ("JPG", "evidence.jpg", "12 KB"),
        ("MP3", "interview.mp3", "30 KB"),
        ("MP4", "clip.mp4", "90 KB"),
        ("PDF", "report.pdf", "20 KB"),
    ))

    assert [
        (row["kind_label"], row["filename"], row["displayed_size"])
        for row in rows
    ] == [
        ("JPG", "evidence.jpg", "12 KB"),
        ("MP3", "interview.mp3", "30 KB"),
        ("MP4", "clip.mp4", "90 KB"),
        ("PDF", "report.pdf", "20 KB"),
    ]


def test_attachment_detail_reads_metadata_and_download_boundary():
    assert collector._attachment_detail(attachment_detail_tree()) == {
        "filename": "AURA-Readiness-Disposable.md",
        "mime_type": "text/markdown",
        "displayed_size": "175 B",
        "note_count_label": "1 note",
        "hash_fragment": "2b4bd6ff462a098e",
        "created_at": "29-07-2026 12:44 PM",
        "last_modified_at": "29-07-2026 12:45 PM",
        "download_bounds": (64, 1800, 1016, 1940),
    }


@pytest.mark.parametrize(
    "filename",
    ["../unsafe.md", "folder/unsafe.md"],
)
def test_attachment_rows_reject_path_like_filename(filename):
    root = attachment_list_tree()
    row = next(node for node in root.iter() if node.get("clickable") == "true")
    row.set("content-desc", f"MD, {filename}, File size: 175 B")

    with pytest.raises(
        collector.NotesnookCollectorError,
        match="attachment",
    ):
        collector._attachment_rows(root)


def test_changed_files_accepts_changed_existing_tuple_only():
    before = (("/sdcard/Download/AURA/a.md", 175, 1),)
    changed = (("/sdcard/Download/AURA/a.md", 175, 2),)

    assert collector._changed_files(before, changed) == changed
    with pytest.raises(collector.NotesnookCollectorError):
        collector._changed_files(before, before)
    with pytest.raises(collector.NotesnookCollectorError):
        collector._changed_files(
            before,
            (
                ("/sdcard/Download/AURA/a.md", 175, 2),
                ("/sdcard/Download/AURA/b.md", 20, 2),
            ),
        )


def test_trash_cards_preserve_complete_displayed_description():
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert collector._trash_cards(device, trash_list_tree()) == ({
        "trash_ref": "trash-000001",
        "source_key": (
            "note-item-0",
            "12:49 PM, AURA_TRASH_TEST, Deleted on 2026-07-29, Note",
        ),
        "content_description": (
            "12:49 PM, AURA_TRASH_TEST, Deleted on 2026-07-29, Note"
        ),
        "menu_bounds": (925, 711, 1030, 816),
    },)


def test_trash_metadata_reads_timestamps_without_action_boundaries():
    assert collector._trash_metadata(trash_menu_tree()) == {
        "title": "AURA_TRASH_TEST",
        "created_at": "29-07-2026 12:48 PM",
        "deleted_at": "29-07-2026 12:49 PM",
        "last_edited_at": "29-07-2026 12:48 PM",
        "restore_control_observed": True,
        "delete_control_observed": True,
    }


def test_workspace_parser_classifies_local_only_and_topmost_note():
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert collector._workspace_state(device, notes_tree()) == "authenticated"
    logged_out = notes_tree()
    ET.SubElement(
        logged_out,
        "node",
        {
            "package": "com.streetwriters.notesnook",
            "class": "android.widget.TextView",
            "text": "You are not logged in",
            "visible-to-user": "true",
        },
    )
    assert collector._workspace_state(device, logged_out) == "local_only"
    assert collector._topmost_note_bounds(
        device, notes_tree()
    ) == (0, 610, 1080, 918)


def test_topmost_note_parser_rejects_tied_cards():
    root = notes_tree()
    note_list = next(
        node for node in root.iter() if node.get("resource-id") == "list.id"
    )
    note_list.append(ET.fromstring(
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="note-item-1" '
        'clickable="true" visible-to-user="true" bounds="[0,610][1080,918]">'
        '<node package="com.streetwriters.notesnook" '
        'class="android.view.ViewGroup" resource-id="listitem.menu" '
        'clickable="true" focusable="true" visible-to-user="true" '
        'bounds="[925,711][1030,816]"/></node>'
    ))
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    with pytest.raises(
        collector.NotesnookCollectorError,
        match="note card",
    ):
        collector._topmost_note_bounds(device, root)


def test_note_cards_skip_menu_obscured_by_add_button():
    root = active_note_tree()
    ET.SubElement(
        root,
        "node",
        {
            "package": "com.streetwriters.notesnook",
            "class": "android.view.ViewGroup",
            "resource-id": "buttons.add",
            "clickable": "true",
            "visible-to-user": "true",
            "bounds": "[841,700][1008,867]",
        },
    )
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert collector._note_cards(device, root) == ()


def test_note_cards_skip_partially_visible_card():
    root = active_note_tree()
    card = next(
        node
        for node in root.iter()
        if node.get("resource-id") == "note-item-0"
    )
    menu = next(
        node
        for node in card.iter()
        if node.get("resource-id") == "listitem.menu"
    )
    card.set("bounds", "[0,2100][1080,2300]")
    menu.set("bounds", "[925,2190][1030,2295]")
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert collector._note_cards(device, root) == ()

    card.remove(menu)
    assert collector._note_cards(device, root) == ()


def test_selection_parser_reads_count_and_unique_select_all_boundary():
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert collector._selection_state(
        device, selection_tree()
    ) == ((700, 2100, 780, 2180), 1)
    with pytest.raises(
        collector.NotesnookCollectorError,
        match="select-all",
    ):
        collector._selection_state(
            device, selection_tree(tied_select_all=True)
        )


def test_adapter_matches_notesnook_and_documentsui_profile_elements():
    device = adapter.NotesnookDeviceAdapter(
        SimpleNamespace(app_profile=PROFILE)
    )

    assert len(device.matching_elements(
        notes_tree(), "notesnook.notes-list"
    )) == 1
    assert len(device.matching_elements(
        notes_tree(), "notesnook.note-menu"
    )) == 1
    assert len(device.matching_elements(
        documents_tree(), "documentsui.download-root"
    )) == 1
    assert len(device.matching_elements(
        documents_tree(), "documentsui.aura-directory"
    )) == 1


@pytest.mark.parametrize("storage_root", [False, True])
def test_select_aura_directory_accepts_picker_already_in_download(storage_root):
    class Device:
        def __init__(self):
            self.matcher = adapter.NotesnookDeviceAdapter(
                SimpleNamespace(app_profile=PROFILE)
            )
            self.observations = {
                "observation-documents": ET.tostring(
                    ET.fromstring(
                        '<hierarchy><node package="com.google.android.documentsui" '
                        'clickable="true" visible-to-user="true" bounds="[72,1767][528,1911]">'
                        '<node package="com.google.android.documentsui" class="android.widget.TextView" '
                        'text="Download" clickable="false" visible-to-user="true" '
                        'bounds="[213,1810][401,1867]"/></node></hierarchy>'
                    ) if storage_root else download_directory_tree()
                ),
            }
            self.trees = iter((
                *((download_directory_tree(),) if storage_root else ()),
                current_aura_tree(), documents_tree(),
            ))
            self.calls = []

        def read_observation(self, observation_id, kind):
            return self.observations[observation_id]

        def matching_elements(self, root, element_id):
            return self.matcher.matching_elements(root, element_id)

        def element_selector(self, element_id):
            return self.matcher.element_selector(element_id)

        def click(self, action_id, selector, observation_id):
            self.calls.append(("click", action_id))

        def click_bounds(self, action_id, bounds, observation_id):
            self.calls.append(("click_bounds", action_id, bounds))

        def observe(self, observation_id):
            self.observations[observation_id] = ET.tostring(
                next(self.trees)
            )
            return {"observation_id": observation_id}

    device = Device()

    assert collector._select_aura_directory(
        device,
        "observation-documents",
    )
    assert device.calls[int(storage_root)] == (
        "click_bounds",
        "action-open-aura-directory",
        (72, 927, 528, 1071),
    )
    if storage_root:
        assert device.calls[0] == (
            "click_bounds", "action-open-download", (72, 1767, 528, 1911),
        )


def test_adapter_observe_waits_for_two_equal_hierarchies():
    samples = iter(("<launcher/>", "<hierarchy/>", "<hierarchy/>"))

    class Journal:
        @staticmethod
        def record_action(action_id, **kwargs):
            return SimpleNamespace(event_id="event-1", action=action_id)

        @staticmethod
        def append(*args, **kwargs):
            pass

    context = SimpleNamespace(
        app_profile=PROFILE,
        device=SimpleNamespace(
            hierarchy=lambda: next(samples),
            screenshot=lambda: b"png",
        ),
        journal=Journal(),
        ui=SimpleNamespace(wait_until=lambda predicate: any(
            predicate() for _ in range(3)
        )),
    )

    device = adapter.NotesnookDeviceAdapter(context)
    observation = device.observe("observation-1")

    assert observation == {"observation_id": "observation-1"}
    assert device.read_observation(
        "observation-1", "ui_tree"
    ) == b"<hierarchy/>"


def test_adapter_swipe_journals_current_observation():
    calls = []

    def swipe(action_id, direction, **kwargs):
        calls.append((action_id, direction, kwargs))
        return SimpleNamespace(event_id="event-1", action=action_id)

    context = SimpleNamespace(
        app_profile=SimpleNamespace(
            parameters={
                "export_root": "/sdcard/Download/AURA",
                "inventory_probes": 20,
            },
            timings={"transition_settle": 0},
        ),
        ui=SimpleNamespace(swipe=swipe),
    )
    device = adapter.NotesnookDeviceAdapter(context)

    device.swipe("action-scroll-notes", "up", "observation-notes")

    assert calls == [(
        "action-scroll-notes",
        "up",
        {"context": {"before_observation_id": "observation-notes"}},
    )]
    assert device.last_action.action == "action-scroll-notes"


def test_inventory_parser_accepts_only_exact_export_root():
    root = PurePosixPath("/sdcard/Download/AURA")

    assert adapter._parse_inventory(
        "/sdcard/Download/AURA/Notes.zip\t123\t456\n",
        root,
    ) == (("/sdcard/Download/AURA/Notes.zip", 123, 456),)

    with pytest.raises(
        adapter.NotesnookAdapterError,
        match="inventory",
    ):
        adapter._parse_inventory(
            "/sdcard/Download/AURA2/Notes.zip\t123\t456\n",
            root,
        )


def test_inventory_prepares_configured_output_directory_without_deleting_files(tmp_path):
    import subprocess
    from dataclasses import replace
    output = tmp_path / "Download" / "AURA"
    profile = replace(PROFILE, parameters={**PROFILE.parameters, "export_root": str(output)})
    # Exercise the real shell command against an isolated filesystem, not a command-string mock.
    journal = EventJournal(tmp_path / "events.jsonl", {})
    device = adapter.NotesnookDeviceAdapter(SimpleNamespace(
        app_profile=profile, journal=journal,
        device=SimpleNamespace(shell=lambda command: subprocess.check_output(command, shell=True, text=True)),
    ))
    device.prepare_output_directory()
    assert output.is_dir()
    marker = output / "existing.txt"
    marker.write_text("preserve me")
    device.prepare_output_directory()
    assert marker.read_text() == "preserve me"


def test_output_writer_retains_one_linked_export_set(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "action-pull-export",
        status="success",
        context={"before_observation_id": "observation-returned"},
    )
    context = output_context(tmp_path, journal, Route.EXPORT)
    artifacts = context.artifacts
    device = SimpleNamespace(
        actions={"action-pull-export": action},
        read_observation=lambda observation_id, kind: {
            "screen_image": b"png",
            "ui_tree": b"<hierarchy/>",
        }[kind],
    )
    outputs = adapter.NotesnookOutputs(
        context,
        device,
        "container.notesnook.local-workspace.001",
    )
    record = {
        "target_ref": "container.notesnook.local-workspace.001",
        "workspace_state": "local_only",
        "selected_document_count": 6,
    }

    outputs.write_export(
        payload=b"zip",
        record=record,
        audit={**record, "device_path": "/sdcard/Download/AURA/Notes.zip"},
        action_id="action-pull-export",
        observation_id="observation-returned",
    )
    outputs.finalize()

    assert context.observations[0].source_snapshot_id == "notesnook-snapshot:observation-returned"
    assert [item.relative_path for item in artifacts.records] == [
        "artifacts/notesnook/export/export-screen.png",
        "artifacts/notesnook/export/export-screen.xml",
        "artifacts/notesnook/export/export.zip",
        "artifacts/notesnook/export/export.json",
        "artifacts/notesnook/export/audit.json",
    ]
    export_record = json.loads(
        (tmp_path / "artifacts/notesnook/export/export.json").read_text(
            encoding="utf-8"
        )
    )
    assert export_record == record
    assert all(
        item.context["source_action_id"] == action.action_id
        for item in artifacts.records
    )
    assert [item.item_type for item in context.items] == ["note_export"]
    assert context.attempts[0].acquisition_status.value == "acquired"
    assert next(row for row in artifacts.records if row.kind == "app_export").observation_id == "observation-000001"


def test_account_output_retains_record_and_source_observation(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "action-open-account",
        status="success",
        context={"before_observation_id": "observation-account-side-menu"},
    )
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    artifacts = context.artifacts
    device = SimpleNamespace(
        actions={"action-open-account": action},
        read_observation=lambda observation_id, kind: {
            "screen_image": b"png",
            "ui_tree": b"<hierarchy/>",
        }[kind],
    )
    outputs = adapter.NotesnookOutputs(
        context,
        device,
        "container.notesnook.authenticated-workspace.001",
    )
    record = {
        "target_ref": "container.notesnook.authenticated-workspace.001",
        "workspace_state": "authenticated",
        "account_email": "analyst@example.test",
        "sync_label": "Synced 3m ago",
    }

    outputs.write_account(
        record=record,
        action_id="action-open-account",
        observation_id="observation-account-sheet",
    )

    assert [item.relative_path for item in artifacts.records] == [
        "artifacts/notesnook/account/account.json",
        "artifacts/notesnook/account/account-screen.png",
        "artifacts/notesnook/account/account-screen.xml",
    ]
    assert json.loads(
        (tmp_path / "artifacts/notesnook/account/account.json").read_text(
            encoding="utf-8"
        )
    ) == record
    assert all(
        item.context["source_action_id"] == action.action_id
        for item in artifacts.records
    )
    assert [item.item_type for item in context.items] == ["account"]


def test_materialize_record_writer_links_optional_source_observation(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action(
        "action-open-version",
        status="success",
        context={"before_observation_id": "observation-history"},
    )
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    artifacts = context.artifacts
    device = SimpleNamespace(
        actions={"action-open-version": action},
        read_observation=lambda observation_id, kind: {
            "screen_image": b"png",
            "ui_tree": b"<hierarchy/>",
        }[kind],
    )
    outputs = adapter.NotesnookOutputs(
        context,
        device,
        "container.notesnook.authenticated-workspace.001",
    )
    record = {
        "target_ref": "container.notesnook.authenticated-workspace.001",
        "note_ref": "note-000001",
        "version_ref": "version-000001",
        "title": "Alpha Overview",
    }

    outputs.write_record(
        "materialize/notes/note-000001/history/version-000001.json",
        record,
        action_id="action-open-version",
        observation_id="observation-version",
        include_observation=True,
    )

    assert [item.relative_path for item in artifacts.records] == [
        (
            "artifacts/notesnook/materialize/notes/note-000001/history/"
            "version-000001.json"
        ),
        (
            "artifacts/notesnook/materialize/notes/note-000001/history/"
            "version-000001-screen.png"
        ),
        (
            "artifacts/notesnook/materialize/notes/note-000001/history/"
            "version-000001-screen.xml"
        ),
    ]
    assert all(
        item.context["note_ref"] == "note-000001"
        for item in artifacts.records
    )
    assert [item.item_type for item in context.items] == ["revision"]


def test_note_outputs_share_one_attempt_until_collection_finishes(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action = journal.record_action("read-note", status="success")
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    outputs = adapter.NotesnookOutputs(
        context,
        SimpleNamespace(actions={"read-note": action}),
        "container.notesnook.authenticated-workspace.001",
    )
    record = {
        "target_ref": "container.notesnook.authenticated-workspace.001",
        "note_ref": "note-000001",
    }

    outputs.write_record(
        "materialize/notes/note-000001/current.json",
        {**record, "content_status": "partial"},
        action_id="read-note",
    )
    outputs.write_record(
        "materialize/notes/note-000001/note.json",
        record,
        action_id="read-note",
    )
    outputs.write_record(
        "materialize/summary.json",
        {"target_ref": record["target_ref"], "status": "partial"},
        action_id="read-note",
    )

    assert [item.item_type for item in context.items] == ["note"]
    assert len(context.attempts) == 1
    assert context.attempts[0].procedure_status is None
    assert len(context.artifacts.records) == 3

    outputs.finalize()

    assert context.attempts[0].acquisition_status.value == "partial"
    assert context.outcomes[0].attempt_id == context.attempts[0].attempt_id
    assert {
        artifact.attempt_id for artifact in context.artifacts.records
    } == {context.outcomes[0].attempt_id}


def test_materialize_record_writer_rejects_unsafe_path(tmp_path):
    outputs = adapter.NotesnookOutputs(
        SimpleNamespace(artifacts=SimpleNamespace()),
        SimpleNamespace(actions={}),
        "container.notesnook.authenticated-workspace.001",
    )

    with pytest.raises(adapter.NotesnookAdapterError, match="output path"):
        outputs.write_record(
            "../escape.json",
            {},
            action_id="missing",
        )


def test_attachment_output_retains_original_filename(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    action_id = "action-pull-note-000001-attachment-000001"
    action = journal.record_action(
        action_id,
        status="success",
        context={"before_observation_id": "observation-attachments"},
    )
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    artifacts = context.artifacts
    device = SimpleNamespace(
        actions={action_id: action},
        read_observation=lambda observation_id, kind: {
            "screen_image": b"png",
            "ui_tree": b"<hierarchy/>",
        }[kind],
    )
    outputs = adapter.NotesnookOutputs(
        context,
        device,
        "container.notesnook.authenticated-workspace.001",
    )
    record = {
        "target_ref": "container.notesnook.authenticated-workspace.001",
        "note_ref": "note-000001",
        "attachment_ref": "attachment-000001",
        "filename": "AURA-Readiness-Disposable.md",
    }

    outputs.write_attachment(
        note_ref="note-000001",
        attachment_ref="attachment-000001",
        filename="AURA-Readiness-Disposable.md",
        payload=b"hello",
        record=record,
        action_id=action_id,
        observation_id="observation-attachment-detail",
    )

    prefix = (
        "artifacts/notesnook/materialize/notes/note-000001/"
        "attachments/attachment-000001/"
    )
    assert [item.relative_path for item in artifacts.records] == [
        f"{prefix}attachment-screen.png",
        f"{prefix}attachment-screen.xml",
        f"{prefix}AURA-Readiness-Disposable.md",
        f"{prefix}attachment.json",
    ]
    assert [item.item_type for item in context.items] == ["attachment"]


class AccountDevice:
    def __init__(self, *, local_only=False, sync_observations=None):
        ordinary = notes_tree()
        if local_only:
            ET.SubElement(
                ordinary,
                "node",
                {
                    "package": "com.streetwriters.notesnook",
                    "class": "android.widget.TextView",
                    "text": "You are not logged in",
                    "visible-to-user": "true",
                },
            )
            trees = (ordinary,)
        else:
            trees = (
                ordinary,
                side_menu_tree(),
                account_tree(),
                *(sync_observations or (
                    account_tree(sync_label="Synced just now"),
                )),
                side_menu_tree(),
                notes_tree(),
            )
        self.trees = iter(trees)
        self.observations = {}
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        return self.observations[observation_id] if kind == "ui_tree" else b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def element_selector(self, element_id):
        return self.matcher.element_selector(element_id)

    def click(self, action_id, selector, observation_id):
        self.calls.append(("click", action_id, selector["selector_id"]))

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))


class SyncingAccountDevice(AccountDevice):
    def __init__(self):
        super().__init__(sync_observations=(
            account_tree(sync_label="Syncing   .."),
            account_tree(sync_label="Synced just now"),
        ))


class StaleSyncAccountDevice(AccountDevice):
    sync_label = "Sync failed 22m (Offline) .."

    def __init__(self):
        super().__init__()
        self.trees = iter((
            notes_tree(),
            side_menu_tree(),
            account_tree(sync_label=self.sync_label),
            side_menu_tree(),
            notes_tree(),
        ))

    def observe(self, observation_id):
        if observation_id.startswith("observation-account-sheet-sync-"):
            root = account_tree(sync_label=self.sync_label)
            self.observations[observation_id] = ET.tostring(root)
            self.calls.append(("observe", observation_id))
            return {"observation_id": observation_id}
        return super().observe(observation_id)


class DelayedSideMenuDevice(AccountDevice):
    def __init__(self):
        super().__init__()
        self.trees = iter((
            notes_tree(),
            notes_tree(),
            side_menu_tree(),
            account_tree(),
            account_tree(sync_label="Synced just now"),
            side_menu_tree(),
            notes_tree(),
        ))


class NonNotesAccountDevice(AccountDevice):
    def __init__(self):
        super().__init__()
        entry = trash_list_tree()
        ET.SubElement(
            entry,
            "node",
            {
                "package": "com.streetwriters.notesnook",
                "class": "android.view.ViewGroup",
                "resource-id": "left",
                "clickable": "true",
                "focusable": "true",
                "visible-to-user": "true",
                "bounds": "[0,70][140,210]",
            },
        )
        self.trees = iter((
            entry,
            side_menu_tree(),
            notes_tree(),
            side_menu_tree(),
            account_tree(),
            account_tree(sync_label="Synced just now"),
            side_menu_tree(),
            notes_tree(),
        ))


class AccountOutputs:
    def __init__(self):
        self.writes = []

    def write_account(self, **kwargs):
        self.writes.append(kwargs)


def test_account_context_navigates_read_only_and_returns_to_notes():
    device = AccountDevice()
    outputs = AccountOutputs()

    record = collector.collect_account_context(
        device,
        outputs,
        "container.notesnook.authenticated-workspace.001",
        "controlled_online",
    )

    assert record == {
        "target_ref": "container.notesnook.authenticated-workspace.001",
        "workspace_state": "authenticated",
        "account_email": "analyst@example.test",
        "acquisition_environment": "controlled_online",
        "sync_label": "Synced just now",
        "sync_gate_passed": True,
    }
    assert [call[1] for call in device.calls if call[0] in {"click", "back"}] == [
        "action-open-side-menu",
        "action-open-account",
        "action-sync-now",
        "action-close-account",
        "action-close-side-menu",
    ]
    assert len(outputs.writes) == 1
    assert outputs.writes[0]["action_id"] == "action-sync-now"
    assert outputs.writes[0]["observation_id"].endswith("sync-000001")


def test_account_context_waits_for_synced_label():
    device = SyncingAccountDevice()
    outputs = AccountOutputs()
    record = collector.collect_account_context(
        device,
        outputs,
        "container.notesnook.authenticated-workspace.001",
        "controlled_online",
    )

    assert record["sync_label"] == "Synced just now"
    assert record["sync_gate_passed"] is True
    assert [
        call[1] for call in device.calls
        if call[0] == "click" and call[1] == "action-sync-now"
    ] == ["action-sync-now"]
    assert outputs.writes[0]["observation_id"].endswith("sync-000002")


def test_account_context_accepts_unchanged_matching_terminal_sync_label():
    device = StaleSyncAccountDevice()
    outputs = AccountOutputs()
    record = collector.collect_account_context(
        device,
        outputs,
        "container.notesnook.authenticated-workspace.001",
        "device_only",
    )

    assert record["sync_label"] == device.sync_label
    assert record["sync_gate_passed"] is True
    assert outputs.writes[0]["record"] == record


def test_account_context_retains_mismatched_terminal_sync_state():
    outputs = AccountOutputs()
    record = collector.collect_account_context(
        AccountDevice(sync_observations=(
            account_tree(sync_label="Synced just now"),
        )),
        outputs,
        "container.notesnook.authenticated-workspace.001",
        "device_only",
    )

    assert record["sync_label"] == "Synced just now"
    assert record["sync_gate_passed"] is False
    assert outputs.writes[0]["record"] == record


def test_account_context_rechecks_delayed_side_menu():
    record = collector.collect_account_context(
        DelayedSideMenuDevice(),
        AccountOutputs(),
        "container.notesnook.authenticated-workspace.001",
        "controlled_online",
    )

    assert record["workspace_state"] == "authenticated"


def test_account_context_returns_from_non_notes_screen_before_collection():
    device = NonNotesAccountDevice()

    record = collector.collect_account_context(
        device,
        AccountOutputs(),
        "container.notesnook.authenticated-workspace.001",
        "controlled_online",
    )

    assert record["workspace_state"] == "authenticated"
    assert [call[1] for call in device.calls if call[0] == "click"][:2] == [
        "action-open-entry-side-menu",
        "action-open-notes",
    ]


def test_account_context_skips_logged_out_local_workspace():
    device = AccountDevice(local_only=True)
    outputs = AccountOutputs()

    record = collector.collect_account_context(
        device,
        outputs,
        "container.notesnook.local-workspace.001",
        "device_only",
    )

    assert record == {
        "target_ref": "container.notesnook.local-workspace.001",
        "workspace_state": "local_only",
        "acquisition_environment": "device_only",
        "sync_gate_passed": False,
    }
    assert not [call for call in device.calls if call[0] in {"click", "back"}]
    assert outputs.writes == []


def test_account_context_recovers_attachment_sheet_with_bounded_back():
    device = AccountDevice()
    device.trees = iter((
        ET.fromstring('<hierarchy><node package="com.streetwriters.notesnook" '
                      'text="Attachments" visible-to-user="true"/></hierarchy>'),
        notes_tree(), side_menu_tree(), account_tree(),
        account_tree(sync_label="Synced just now"), side_menu_tree(), notes_tree(),
    ))
    record = collector.collect_account_context(
        device, AccountOutputs(), "notes.notesnook.all", "controlled_online",
    )
    assert record["workspace_state"] == "authenticated"
    assert device.calls[1] == ("back", "action-recover-notes-entry-1")


def test_account_context_stops_after_four_back_actions_on_unknown_screen():
    device = AccountDevice()
    device.trees = iter([ET.fromstring('<hierarchy><node package="com.streetwriters.notesnook"/></hierarchy>')] * 6)
    with pytest.raises(collector.NotesnookCollectorError, match="entry"):
        collector.collect_account_context(
            device, AccountOutputs(), "notes.notesnook.all", "device_only",
        )
    assert sum(call[0] == "back" for call in device.calls) == 4


def test_failure_output_preserves_last_screen_and_same_attempt_result(tmp_path):
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-failure"})
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    action = journal.record_action("download_attachment", status="success")
    device = SimpleNamespace(
        last_action=action,
        observe=lambda name: {"observation_id": name},
        read_observation=lambda observation_id, kind: {
            "screen_image": b"actual failure screen", "ui_tree": b"<hierarchy/>"
        }[kind],
    )
    outputs = adapter.NotesnookOutputs(context, device, "notes.notesnook.all")
    outputs.begin_item("attachment:note-1:file-1", "notesnook.attachments", "attachment",
                       note_ref="note-1", attachment_ref="file-1")
    failed_action = journal.record_action("select_directory", status="failed")
    outputs.preserve_failure("attachment directory selection failed")
    outputs.finalize()
    assert context.attempts[0].procedure_status.value == "interrupted"
    assert context.attempts[0].acquisition_status.value == "not_acquired"
    outcome = context.outcomes[0]
    assert outcome.reason == "attachment directory selection failed"
    assert outcome.observation_id == "observation-000001"
    assert outcome.action_id == context.observations[0].action_id
    retained = next(row for row in journal.actions if row.action_id == outcome.action_id)
    assert retained.context["source_action_id"] == failed_action.action_id
    assert {row.kind for row in context.artifacts.records} == {"screen_image", "ui_hierarchy"}
    assert (tmp_path / context.artifacts.records[0].relative_path).read_bytes() == b"actual failure screen"


def test_materialize_transport_failure_preserves_screen_before_return(tmp_path, monkeypatch):
    journal = EventJournal(tmp_path / "events.jsonl", {})
    context = output_context(tmp_path, journal, Route.MATERIALIZE)
    action = journal.record_action("open_attachment", status="success")
    device = SimpleNamespace(last_action=action,
        observe=lambda name: {"observation_id": name},
        read_observation=lambda name, kind: b"png" if kind == "screen_image" else b"<hierarchy/>")
    outputs = adapter.NotesnookOutputs(context, device, "notes.notesnook.all")
    def broken(*args):
        outputs.begin_item("attachment:n:a", "notesnook.attachments", "attachment")
        raise OSError("transport failed")
    monkeypatch.setattr(collector, "_materialize_notes", broken)
    report = collector.materialize(device, outputs, "notes.notesnook.all")
    assert report["status"] == "partial"
    assert report["failure"] == "transport failed"
    assert context.outcomes[0].observation_id == "observation-000001"


class HistoryDevice:
    def __init__(self):
        self.trees = iter((
            history_tree(),
            history_version_tree(),
            history_tree(),
            history_version_tree(),
            history_tree(),
            history_version_tree(),
            history_tree(),
            active_note_tree(),
        ))
        self.observations = {
            "observation-note-menu": ET.tostring(note_menu_tree()),
        }
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        return self.observations[observation_id] if kind == "ui_tree" else b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def element_selector(self, element_id):
        return self.matcher.element_selector(element_id)

    def click(self, action_id, selector, observation_id):
        self.calls.append(("click", action_id, selector["selector_id"]))

    def click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("click_bounds", action_id, bounds))

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))


class MaterializeOutputs:
    def __init__(self):
        self.writes = []

    def write_record(self, path, record, **kwargs):
        self.writes.append((path, record, kwargs))


class CurrentNoteDevice:
    def __init__(self, *trees):
        self.trees = iter(trees)
        self.observations = {
            "observation-notes": ET.tostring(active_note_tree()),
        }
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        return self.observations[observation_id] if kind == "ui_tree" else b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("click_bounds", action_id, bounds))

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))


def test_current_note_rechecks_editor_and_writes_live_body():
    skeleton = ET.fromstring(
        '<hierarchy><node package="com.streetwriters.notesnook" '
        'class="android.webkit.WebView" visible-to-user="true"/></hierarchy>'
    )
    device = CurrentNoteDevice(
        skeleton,
        current_note_tree(),
        active_note_tree(),
    )
    outputs = MaterializeOutputs()

    returned_id, partial = collector._collect_current_note(
        device,
        outputs,
        "container.notesnook.workspace.001",
        "note-000001",
        (0, 400, 1080, 700),
        "observation-notes",
    )

    assert returned_id == "observation-materialize-notes-after-note-000001-current"
    assert partial is False
    path, record, options = outputs.writes[0]
    assert path == "materialize/notes/note-000001/current.json"
    assert record["title"] == "Boundary Note"
    assert record["body_text"] == (
        "AURA_DATASET_ID=N06\nDocument-only local workspace boundary."
    )
    assert record["word_count"] == 6
    assert record["content_status"] == "complete"
    assert options["include_observation"] is True


def test_current_note_without_visible_body_is_partial():
    device = CurrentNoteDevice(
        current_note_tree(body=False),
        active_note_tree(),
    )
    outputs = MaterializeOutputs()

    _, partial = collector._collect_current_note(
        device,
        outputs,
        "container.notesnook.workspace.001",
        "note-000001",
        (0, 400, 1080, 700),
        "observation-notes",
    )

    assert partial is True
    assert outputs.writes[0][1]["content_status"] == "partial"


def test_history_navigation_preserves_duplicate_versions_without_actions():
    device = HistoryDevice()
    outputs = MaterializeOutputs()

    count, returned_menu_id = collector._collect_history(
        device,
        outputs,
        "container.notesnook.workspace.001",
        "note-000001",
        "observation-note-menu",
    )

    assert count == 3
    assert returned_menu_id == (
        "observation-materialize-notes-after-note-000001-history"
    )
    assert [call[1] for call in device.calls if call[0] == "click"] == [
        "action-open-note-000001-history",
    ]
    assert [
        call[1] for call in device.calls if call[0] == "click_bounds"
    ] == [
        "action-open-note-000001-version-000001",
        "action-open-note-000001-version-000002",
        "action-open-note-000001-version-000003",
    ]
    assert not any(
        action in {"Restore", "Delete permanently"}
        for _, action, *_ in device.calls
    )
    version_records = [
        record
        for path, record, _ in outputs.writes
        if path.endswith(".json") and "version-" in path
    ]
    assert [record["version_ref"] for record in version_records] == [
        "version-000001",
        "version-000002",
        "version-000003",
    ]


class AttachmentDevice:
    def __init__(self):
        self.trees = iter((
            attachment_list_tree(),
            attachment_detail_tree(),
            current_aura_tree(),
            documents_tree(),
            attachment_list_tree(),
            attachment_list_tree(),
            active_note_tree(),
        ))
        self.observations = {
            "observation-note-menu": ET.tostring(note_menu_tree()),
        }
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )
        self.before = ((
            "/sdcard/Download/AURA/AURA-Readiness-Disposable.md",
            5,
            1,
        ),)
        self.after = ((
            "/sdcard/Download/AURA/AURA-Readiness-Disposable.md",
            5,
            2,
        ),)

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        return self.observations[observation_id] if kind == "ui_tree" else b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def element_selector(self, element_id):
        return self.matcher.element_selector(element_id)

    def click(self, action_id, selector, observation_id):
        self.calls.append(("click", action_id, selector["selector_id"]))

    def click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("click_bounds", action_id, bounds))

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))

    def swipe(self, action_id, direction, observation_id):
        self.calls.append(("swipe", action_id, direction))

    def snapshot_files(self, action_id, observation_id, baseline=None):
        self.calls.append(("snapshot_files", action_id))
        return self.before if baseline is None else self.after

    def pull_file(self, action_id, path, observation_id):
        self.calls.append(("pull_file", action_id, path))
        return b"hello"


class AttachmentOutputs(MaterializeOutputs):
    def write_attachment(self, **kwargs):
        self.writes.append(("attachment", kwargs["record"], kwargs))


def test_attachment_navigation_downloads_changed_file_only():
    device = AttachmentDevice()
    outputs = AttachmentOutputs()

    count, returned_menu_id = collector._collect_attachments(
        device,
        outputs,
        "container.notesnook.workspace.001",
        "note-000001",
        "observation-note-menu",
    )

    assert count == 1
    assert returned_menu_id == (
        "observation-materialize-notes-after-note-000001-attachments"
    )
    attachment = next(
        record for path, record, _ in outputs.writes if path == "attachment"
    )
    assert attachment["filename"] == "AURA-Readiness-Disposable.md"
    assert attachment["device_path"].endswith(
        "/AURA-Readiness-Disposable.md"
    )
    assert attachment["size"] == 5
    assert not any(
        prohibited in str(call)
        for call in device.calls
        for prohibited in ("Reupload", "Run file check", "Rename", "Delete")
    )


class MultiPageAttachmentDevice(AttachmentDevice):
    def __init__(self):
        first = attachment_page_tree(
            ("JPG", "evidence.jpg", "12 KB"),
            ("MP3", "interview.mp3", "30 KB"),
        )
        second = attachment_page_tree(
            ("MP3", "interview.mp3", "30 KB"),
            ("MP4", "clip.mp4", "90 KB"),
            ("PDF", "report.pdf", "20 KB"),
        )
        details = {
            "evidence.jpg": ("image/jpeg", "12 KB"),
            "interview.mp3": ("audio/mpeg", "30 KB"),
            "clip.mp4": ("video/mp4", "90 KB"),
            "report.pdf": ("application/pdf", "20 KB"),
        }
        self.trees = iter((
            first,
            *sum((
                (
                    media_attachment_detail_tree(filename, *details[filename]),
                    current_aura_tree(),
                    documents_tree(),
                    first,
                )
                for filename in ("evidence.jpg", "interview.mp3")
            ), ()),
            second,
            *sum((
                (
                    media_attachment_detail_tree(filename, *details[filename]),
                    current_aura_tree(),
                    documents_tree(),
                    second,
                )
                for filename in ("clip.mp4", "report.pdf")
            ), ()),
            second,
            active_note_tree(),
        ))
        self.observations = {
            "observation-note-menu": ET.tostring(note_menu_tree()),
        }
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )
        self.filenames = {
            f"attachment-{index:06d}": filename
            for index, filename in enumerate(
                ("evidence.jpg", "interview.mp3", "clip.mp4", "report.pdf"),
                1,
            )
        }
        self.current_filename = None

    def swipe(self, action_id, direction, observation_id):
        self.calls.append(("swipe", action_id, direction))

    def snapshot_files(self, action_id, observation_id, baseline=None):
        self.calls.append(("snapshot_files", action_id))
        if baseline is None:
            self.current_filename = next(
                filename
                for attachment_ref, filename in self.filenames.items()
                if attachment_ref in action_id
            )
            return ()
        return ((
            f"/sdcard/Download/AURA/{self.current_filename}",
            1,
            1,
        ),)

    def pull_file(self, action_id, path, observation_id):
        self.calls.append(("pull_file", action_id, path))
        return b"x"


def test_attachment_multi_page_collects_each_mixed_media_item_once():
    device = MultiPageAttachmentDevice()
    outputs = AttachmentOutputs()

    count, _ = collector._collect_attachments(
        device,
        outputs,
        "container.notesnook.workspace.001",
        "note-000001",
        "observation-note-menu",
    )

    attachments = [
        record
        for path, record, _ in outputs.writes
        if path == "attachment"
    ]
    assert count == 4
    assert [
        (record["kind_label"], record["filename"])
        for record in attachments
    ] == [
        ("JPG", "evidence.jpg"),
        ("MP3", "interview.mp3"),
        ("MP4", "clip.mp4"),
        ("PDF", "report.pdf"),
    ]
    assert len([call for call in device.calls if call[0] == "swipe"]) == 2
    assert not any(
        category in str(call)
        for call in device.calls
        for category in ("Images", "Audios", "Videos", "Documents")
    )


class MaterializeDevice:
    def __init__(self):
        self.trees = iter((
            notes_tree(),
            active_note_tree(),
            active_note_tree(),
            note_menu_tree(),
            note_menu_tree(),
            active_note_tree(),
            side_menu_tree(),
            trash_list_tree(),
            trash_menu_tree(),
            trash_list_tree(),
            trash_list_tree(),
            side_menu_tree(),
            active_note_tree(),
        ))
        self.observations = {}
        self.calls = []
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        return self.observations[observation_id] if kind == "ui_tree" else b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def element_selector(self, element_id):
        return self.matcher.element_selector(element_id)

    def click(self, action_id, selector, observation_id):
        self.calls.append(("click", action_id, selector["selector_id"]))

    def click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("click_bounds", action_id, bounds))

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))

    def swipe(self, action_id, direction, observation_id):
        self.calls.append(("swipe", action_id, direction))


class MaterializeRunOutputs(MaterializeOutputs):
    def write_export(self, **kwargs):
        raise AssertionError("Export must remain independent")


def test_rewind_note_list_moves_to_the_first_stable_page():
    bottom = active_note_tree()
    next(
        node for node in bottom.iter()
        if node.get("resource-id") == "note-item-0"
    ).set("content-desc", "Bottom note")
    top = active_note_tree()
    next(
        node for node in top.iter()
        if node.get("resource-id") == "note-item-0"
    ).set("content-desc", "Top note")
    device = AccountDevice()
    device.trees = iter((bottom, top, top))
    device.swipe = lambda action_id, direction, observation_id: (
        device.calls.append(("swipe", action_id, direction))
    )

    observation_id = collector._rewind_note_list(device)

    assert observation_id.endswith("000003")
    assert [call[2] for call in device.calls if call[0] == "swipe"] == [
        "down",
        "down",
    ]


def test_materialize_aggregates_active_note_and_trash_counts(monkeypatch):
    device = MaterializeDevice()
    outputs = MaterializeRunOutputs()
    def history(device, outputs, target_ref, note_ref, observation_id):
        returned = "observation-after-history"
        device.observations[returned] = ET.tostring(active_note_tree())
        return 2, returned

    def attachments(device, outputs, target_ref, note_ref, observation_id):
        returned = "observation-after-attachments"
        device.observations[returned] = ET.tostring(active_note_tree())
        return 1, returned

    def current_note(
        device,
        outputs,
        target_ref,
        note_ref,
        card_bounds,
        observation_id,
    ):
        return observation_id, False

    monkeypatch.setattr(collector, "_collect_history", history)
    monkeypatch.setattr(collector, "_collect_attachments", attachments)
    monkeypatch.setattr(collector, "_collect_current_note", current_note)

    report = collector.materialize(
        device,
        outputs,
        "container.notesnook.workspace.001",
    )

    assert report == {
        "status": "complete",
        "reason_code": None,
        "target_ref": "container.notesnook.workspace.001",
        "route": "materialize",
        "note_count": 1,
        "history_version_count": 2,
        "attachment_count": 1,
        "trash_count": 1,
        "current_note_partial_count": 0,
    }
    summary = next(
        record
        for path, record, _ in outputs.writes
        if path == "materialize/summary.json"
    )
    assert summary == report
    assert not any(
        prohibited in str(call)
        for call in device.calls
        for prohibited in ("Restore", "Delete", "Sync now")
    )
    assert any(
        call[:2] == ("click", "action-return-to-notes")
        for call in device.calls
    )


class CollectorDevice:
    def __init__(
        self,
        after,
        *,
        already_in_aura=False,
        invalid_notes_return=False,
    ):
        logged_out = notes_tree()
        ET.SubElement(
            logged_out,
            "node",
            {
                "package": "com.streetwriters.notesnook",
                "class": "android.widget.TextView",
                "text": "You are not logged in",
                "visible-to-user": "true",
            },
        )
        self.matcher = adapter.NotesnookDeviceAdapter(
            SimpleNamespace(app_profile=PROFILE)
        )
        documents = (
            (current_aura_tree(), documents_tree())
            if already_in_aura
            else (
                documents_tree(),
                documents_tree(),
                current_aura_tree(),
                documents_tree(),
            )
        )
        self.trees = iter((
            logged_out,
            selection_tree(1),
            selection_tree(6),
            export_options_tree(),
            *documents,
            export_success_tree(
                filename=(
                    after[0][0].rsplit("/", 1)[-1]
                    if len(after) == 1
                    else "Notesnook.zip"
                )
            ),
            selection_tree(6),
            export_options_tree() if invalid_notes_return else logged_out,
        ))
        self.observations = {}
        self.after = after
        self.calls = []

    def observe(self, observation_id):
        root = next(self.trees)
        self.observations[observation_id] = ET.tostring(root)
        self.calls.append(("observe", observation_id))
        return {"observation_id": observation_id}

    def read_observation(self, observation_id, kind):
        if kind == "ui_tree":
            return self.observations[observation_id]
        return b"png"

    def matching_elements(self, root, element_id):
        return self.matcher.matching_elements(root, element_id)

    def element_selector(self, element_id):
        return self.matcher.element_selector(element_id)

    def long_click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("long_click_bounds", action_id, bounds))

    def click_bounds(self, action_id, bounds, observation_id):
        self.calls.append(("click_bounds", action_id, bounds))

    def click(self, action_id, selector, observation_id):
        self.calls.append(("click", action_id, selector["selector_id"]))

    def snapshot_files(self, action_id, observation_id, baseline=None):
        self.calls.append(("snapshot_files", action_id))
        return () if baseline is None else self.after

    def pull_file(self, action_id, path, observation_id):
        self.calls.append(("pull_file", action_id, path))
        return b"zip"

    def back(self, action_id, observation_id):
        self.calls.append(("back", action_id))


class CollectorOutputs:
    def __init__(self):
        self.writes = []

    def write_export(self, **kwargs):
        self.writes.append(kwargs)


def test_collector_collects_local_workspace_export():
    device = CollectorDevice((
        ("/sdcard/Download/AURA/Notesnook.zip", 3, 456),
    ))
    outputs = CollectorOutputs()

    report = collector.collect(
        device,
        outputs,
        "container.notesnook.local-workspace.001",
    )

    assert report["status"] == "complete"
    assert report["workspace_state"] == "local_only"
    assert report["selected_document_count"] == 6
    assert report["export_size"] == 3
    assert len(outputs.writes) == 1
    assert [call[1] for call in device.calls if call[0] == "click"] == [
        "action-open-export-options",
        "action-open-download",
        "action-open-aura-directory",
        "action-use-aura-directory",
        "action-confirm-aura-directory",
    ]
    assert [
        call[1] for call in device.calls if call[0] == "click_bounds"
    ] == ["action-select-all", "action-export-frontmatter"]


def test_collector_accepts_documentsui_already_in_aura_directory():
    device = CollectorDevice((
        ("/sdcard/Download/AURA/Notesnook.zip", 3, 456),
    ), already_in_aura=True)

    report = collector.collect(
        device,
        CollectorOutputs(),
        "container.notesnook.local-workspace.001",
    )

    assert report["status"] == "complete"
    assert [call[1] for call in device.calls if call[0] == "click"] == [
        "action-open-export-options",
        "action-use-aura-directory",
        "action-confirm-aura-directory",
    ]


def test_export_returns_to_notes_with_two_back_actions():
    device = CollectorDevice((
        ("/sdcard/Download/AURA/Notesnook.zip", 3, 456),
    ))

    report = collector.collect(
        device,
        CollectorOutputs(),
        "container.notesnook.local-workspace.001",
    )

    assert report["status"] == "complete"
    assert [call[1] for call in device.calls if call[0] == "back"] == [
        "action-close-export-result",
        "action-close-export-selection",
    ]


def test_export_preserves_output_when_notes_return_fails():
    outputs = CollectorOutputs()
    report = collector.collect(
        CollectorDevice(
            (("/sdcard/Download/AURA/Notesnook.zip", 3, 456),),
            invalid_notes_return=True,
        ),
        outputs,
        "container.notesnook.local-workspace.001",
    )

    assert report == {
        "status": "partial",
        "reason_code": "export_notes_return_failed",
        "target_ref": "container.notesnook.local-workspace.001",
    }
    assert len(outputs.writes) == 1


@pytest.mark.parametrize(
    "after",
    [
        (),
        (
            ("/sdcard/Download/AURA/one.zip", 3, 456),
            ("/sdcard/Download/AURA/two.zip", 3, 457),
        ),
    ],
)
def test_collector_requires_one_new_zip(after):
    report = collector.collect(
        CollectorDevice(after),
        CollectorOutputs(),
        "container.notesnook.local-workspace.001",
    )

    assert report == {
        "status": "partial",
        "reason_code": "single_zip_export_not_observed",
        "target_ref": "container.notesnook.local-workspace.001",
    }


def test_entrypoint_rejects_non_container_target():
    from aura.apps.notesnook import collect

    outcome = collect(SimpleNamespace(
        app_profile=PROFILE,
        base_context={
            "condition": {
                "target": {"kind": "account", "ref": "account-1"}
            }
        }
    ))

    assert outcome.status.value == "failed"
    assert outcome.reason == "notesnook_target_invalid"


def test_entrypoint_stops_when_notesnook_app_is_not_ready(monkeypatch):
    import aura.apps.notesnook as notesnook

    dispatched = []
    runtime_device = SimpleNamespace(last_action=None)
    context = SimpleNamespace(
        base_context={
            "route": Route.MATERIALIZE.value,
            "condition": {
                "acquisition_environment": "controlled_online",
                "target": {
                    "kind": "container",
                    "ref": "container.notesnook.workspace.001",
                },
            },
        },
        app_profile=PROFILE,
        device=SimpleNamespace(
            app_start=lambda package: None,
            last_action=None,
        ),
        start_app=lambda: False,
    )
    monkeypatch.setattr(
        notesnook,
        "NotesnookDeviceAdapter",
        lambda runtime_context: runtime_device,
    )
    monkeypatch.setattr(
        notesnook,
        "NotesnookOutputs",
        lambda runtime_context, device, target_ref: object(),
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect_account_context",
        lambda *args: {
            "workspace_state": "authenticated",
            "sync_gate_passed": True,
        },
    )
    monkeypatch.setattr(
        notesnook.collector,
        "materialize",
        lambda *args: dispatched.append("materialize")
        or {"status": "complete", "reason_code": None},
    )

    outcome = notesnook.collect(context)

    assert outcome.status.value == "failed"
    assert outcome.reason == "notesnook_app_start_not_verified"
    assert dispatched == []


def test_entrypoint_runs_account_context_before_export(monkeypatch):
    import aura.apps.notesnook as notesnook

    calls = []
    device = SimpleNamespace(last_action=None)
    monkeypatch.setattr(
        notesnook,
        "NotesnookDeviceAdapter",
        lambda context: device,
    )
    monkeypatch.setattr(
        notesnook,
        "NotesnookOutputs",
        lambda context, runtime_device, target_ref: SimpleNamespace(
            finalize=lambda: None
        ),
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect_account_context",
        lambda runtime_device, outputs, target_ref, acquisition_environment:
        calls.append("account")
        or {
            "workspace_state": "authenticated",
            "sync_gate_passed": True,
        },
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect",
        lambda runtime_device, outputs, target_ref: calls.append("export")
        or {
            "status": "complete",
            "reason_code": None,
            "workspace_state": "authenticated",
        },
    )
    context = SimpleNamespace(
        base_context={
            "route": Route.EXPORT.value,
            "condition": {
                "acquisition_environment": "controlled_online",
                "target": {
                    "kind": "container",
                    "ref": "container.notesnook.workspace.001",
                }
            },
        },
        app_profile=PROFILE,
        device=SimpleNamespace(),
        start_app=lambda: calls.append("start") or True,
    )

    outcome = notesnook.collect(context)

    assert outcome.status.value == "complete"
    assert calls == ["start", "account", "export"]


def test_entrypoint_does_not_run_export_for_materialize(monkeypatch):
    import aura.apps.notesnook as notesnook

    calls = []
    device = SimpleNamespace(last_action=None)
    monkeypatch.setattr(
        notesnook,
        "NotesnookDeviceAdapter",
        lambda context: device,
    )
    monkeypatch.setattr(
        notesnook,
        "NotesnookOutputs",
        lambda context, runtime_device, target_ref: SimpleNamespace(
            finalize=lambda: None
        ),
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect_account_context",
        lambda runtime_device, outputs, target_ref, acquisition_environment: {
            "workspace_state": "authenticated",
            "sync_gate_passed": True,
        },
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect",
        lambda *args: calls.append("export"),
    )
    monkeypatch.setattr(
        notesnook.collector,
        "materialize",
        lambda *args: calls.append("materialize")
        or {
            "status": "complete",
            "reason_code": None,
            "note_count": 1,
            "history_version_count": 2,
            "attachment_count": 1,
            "trash_count": 1,
        },
    )
    context = SimpleNamespace(
        base_context={
            "route": Route.MATERIALIZE.value,
            "condition": {
                "acquisition_environment": "controlled_online",
                "target": {
                    "kind": "container",
                    "ref": "container.notesnook.workspace.001",
                }
            },
        },
        app_profile=PROFILE,
        device=SimpleNamespace(),
        start_app=lambda: True,
        incomplete_outcomes=(SimpleNamespace(),),
    )

    outcome = notesnook.collect(context)

    assert outcome.status.value == "complete"
    assert outcome.reason is None
    assert outcome.details["trash_count"] == 1
    assert calls == ["materialize"]


@pytest.mark.parametrize("acquisition_environment", [None, "online"])
def test_entrypoint_rejects_invalid_acquisition_environment(
    monkeypatch,
    acquisition_environment,
):
    import aura.apps.notesnook as notesnook

    calls = []
    context = SimpleNamespace(
        base_context={
            "route": Route.EXPORT.value,
            "condition": {
                "acquisition_environment": acquisition_environment,
                "target": {
                    "kind": "container",
                    "ref": "container.notesnook.workspace.001",
                },
            },
        },
        app_profile=PROFILE,
        device=SimpleNamespace(),
        start_app=lambda: calls.append("start") or True,
    )

    outcome = notesnook.collect(context)

    assert outcome.status.value == "partial"
    assert outcome.reason == "notesnook_acquisition_environment_invalid"
    assert calls == []


def test_entrypoint_does_not_dispatch_route_on_sync_mismatch(monkeypatch):
    import aura.apps.notesnook as notesnook

    calls = []
    device = SimpleNamespace(last_action="action-sync-now")
    monkeypatch.setattr(
        notesnook,
        "NotesnookDeviceAdapter",
        lambda context: device,
    )
    monkeypatch.setattr(
        notesnook,
        "NotesnookOutputs",
        lambda context, runtime_device, target_ref: SimpleNamespace(
            finalize=lambda: None,
            preserve_failure=lambda reason: None,
        ),
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect_account_context",
        lambda runtime_device, outputs, target_ref, acquisition_environment: {
            "workspace_state": "authenticated",
            "acquisition_environment": acquisition_environment,
            "sync_label": "Synced just now",
            "sync_gate_passed": False,
        },
    )
    monkeypatch.setattr(
        notesnook.collector,
        "collect",
        lambda *args: calls.append("export"),
    )
    context = SimpleNamespace(
        base_context={
            "route": Route.EXPORT.value,
            "condition": {
                "acquisition_environment": "device_only",
                "target": {
                    "kind": "container",
                    "ref": "container.notesnook.workspace.001",
                },
            },
        },
        app_profile=PROFILE,
        device=SimpleNamespace(),
        start_app=lambda: calls.append("start") or True,
        incomplete_outcomes=(SimpleNamespace(),),
    )

    outcome = notesnook.collect(context)

    assert outcome.status.value == "partial"
    assert outcome.reason == "notesnook_sync_condition_mismatch"
    assert calls == ["start"]
