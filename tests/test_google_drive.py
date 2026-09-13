import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from xml.sax.saxutils import quoteattr

from aura import cli
from aura.apps.google_drive import collector as drive_collector
from aura.apps.google_drive.collector import (
    activity_complete,
    collection_scope,
    detail_metadata_complete,
    parse_activity,
    parse_information,
    parse_listing,
    parse_file_inventory,
    select_download,
)
from aura.profiles import ProfileStore
from aura.models import Outcome, OutcomeStatus, Route
from aura.runtime import AcquisitionInterrupted, AcquisitionRunError, AcquisitionRuntime
from fakes import FakeDevice


class DriveTreeDevice(FakeDevice):
    """Only the device boundary is fake; listing, recursion and records are real."""
    def __init__(self, tree, *, unavailable=False, interrupted=False):
        super().__init__()
        self.tree = tree
        self.path = ("My Drive",)
        self.surface = "list"
        self.selected = None
        self.unavailable = unavailable
        self.interrupted = interrupted
        self.downloaded = False
        self.pull_destinations = []

    def hierarchy(self):
        if self.surface == "menu":
            return '<hierarchy><node text="View information"/></hierarchy>'
        if self.surface == "information":
            return '<hierarchy>' + ''.join(
                f'<node resource-id="{key}" text={quoteattr(value)}/>'
                for key, value in {
                    "kind_text": "Text", "location_text": "/".join(self.path),
                    "created_text": "1 pm", "modified_text": self.selected[2],
                    "recents_status": "No activity recorded before 1 September 2026",
                }.items()
            ) + '</hierarchy>'
        rows = []
        for index, (name, kind, modified) in enumerate(self.tree[self.path]):
            y = 100 + index * 150
            rows.append(f'<node clickable="true" bounds="[20,{y}][1000,{y+100}]">'
                        f'<node content-desc={quoteattr(kind)}/><node text="Modified"/><node text={quoteattr(modified)}/>'
                        f'<node resource-id="more_actions_button" bounds="[900,{y}][1000,{y+100}]">'
                        f'<node content-desc={quoteattr("More actions for " + name)}/></node></node>')
        return '<hierarchy><node resource-id="scrollList">' + ''.join(rows) + '</node></hierarchy>'

    def exists(self, selector):
        if selector.get("resourceId", "").endswith("menu_navigation_drives"):
            return True
        if selector.get("resourceId") == "scrollList":
            return self.surface == "list"
        if selector.get("resourceId", "").endswith("details_recyclerview"):
            return self.surface == "information"
        if selector.get("text") == "Download":
            return self.surface == "menu" and not self.unavailable
        if selector.get("text") == "View information":
            return self.surface == "menu"
        return True

    def count(self, selector):
        return int(self.exists(selector))

    def click_bounds(self, bounds, *, anchor="center"):
        self.selected = self.tree[self.path][(bounds[1] - 100) // 150]
        if bounds[0] == 900:
            self.surface = "menu"
        else:
            self.path = (*self.path, self.selected[0])

    def click(self, selector):
        if selector.get("text") == "View information":
            self.surface = "information"
        else:
            self.surface = "list"
        if selector.get("text") == "Download":
            self.downloaded = True

    def back(self):
        self.back_count += 1
        if self.surface == "list":
            self.path = self.path[:-1]
        self.surface = "list"

    def swipe(self, direction, duration=0.2):
        if self.interrupted and self.surface == "list":
            raise KeyboardInterrupt("synthetic listing interruption")

    def shell(self, command):
        if "find " in command:
            return "/sdcard/Download/report.txt\0004\000200\000" if self.downloaded else ""
        return super().shell(command)

    def pull(self, remote_path, local_path):
        self.pull_destinations.append(local_path.name)
        # Prevent an unsafe write even when exercising the pre-fix code.
        if local_path.name == "artifact":
            local_path.write_bytes(b"data")


def run_drive(tmp_path, monkeypatch, device, environment="device_only"):
    monkeypatch.setattr(drive_collector.time, "sleep", lambda seconds: None)
    store = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    result = AcquisitionRuntime(tmp_path, device).run(
        store.load_app("google_drive", "2.26.337.0.all.alldpi"),
        store.load_system_ui("samsung"), Route.MATERIALIZE,
        {"acquisition_environment": environment},
        lambda context: drive_collector._collect_drive(context, collection_scope(environment)),
        run_id="tree-drive",
    )
    return result, json.loads((result.run_dir / "acquisition.json").read_text())


def test_nested_drive_preserves_equal_names_across_and_within_parents(tmp_path, monkeypatch):
    tree = {
        ("My Drive",): [("A", "Folder", "1 pm"), ("B", "Folder", "1 pm")],
        ("My Drive", "A"): [("same.txt", "Text", "1 pm"), ("same.txt", "Text", "2 pm"), ("Nested", "Folder", "1 pm")],
        ("My Drive", "A", "Nested"): [("same.txt", "Text", "1 pm")],
        ("My Drive", "B"): [("same.txt", "Text", "1 pm")],
    }
    result, document = run_drive(tmp_path, monkeypatch, DriveTreeDevice(tree))
    inventory = json.loads((result.run_dir / "artifacts/google-drive/inventory.json").read_text())
    assert [row["drive_path"] for row in inventory["items"]] == [
        ["My Drive", "A"], ["My Drive", "A", "same.txt"], ["My Drive", "A", "same.txt"],
        ["My Drive", "A", "Nested"], ["My Drive", "A", "Nested", "same.txt"],
        ["My Drive", "B"], ["My Drive", "B", "same.txt"],
    ]
    assert document["identifications"][0]["traversal_complete"] is True
    assert document["identifications"][0]["identified_item_count"] == 7
    metadata = [item for item in document["items"] if item["item_type"] == "file_metadata"]
    assert len({item["acquisition_item_id"] for item in metadata}) == 4
    assert len(list((result.run_dir / "artifacts/google-drive/items").glob("*/metadata.json"))) == 7


def test_drive_interruption_preserves_rows_without_claiming_traversal(tmp_path, monkeypatch):
    with pytest.raises(AcquisitionInterrupted) as interrupted:
        run_drive(tmp_path, monkeypatch, DriveTreeDevice({("My Drive",): [("report.txt", "Text", "1 pm")]}, interrupted=True))
    run_dir = interrupted.value.run_dir
    document = json.loads((run_dir / "acquisition.json").read_text())
    inventory = json.loads((run_dir / "artifacts/google-drive/inventory.json").read_text())
    assert inventory["item_count"] == 1
    identification = document["identifications"][0]
    assert identification["identification_status"] == "interrupted"
    assert identification["traversal_complete"] is False
    assert identification["identified_item_count"] == 1


def test_drive_unavailable_download_restores_listing_and_keeps_metadata(tmp_path, monkeypatch):
    device = DriveTreeDevice({("My Drive",): [("report.txt", "Text", "1 pm"), ("second.txt", "Text", "2 pm")]}, unavailable=True)
    result, document = run_drive(tmp_path, monkeypatch, device, "controlled_online")
    inventory = json.loads((result.run_dir / "artifacts/google-drive/inventory.json").read_text())
    assert inventory["item_count"] == 2
    assert len(inventory["downloads"]) == 2
    assert all(row["status"] == "not_acquired" for row in inventory["downloads"])
    assert all(record["traversal_complete"] for record in document["identifications"])
    assert device.surface == "list"
    file_ids = {item["acquisition_item_id"] for item in document["items"] if item["item_type"] == "file_content"}
    attempts = [attempt for attempt in document["attempts"] if attempt["acquisition_item_id"] in file_ids]
    assert all(attempt["procedure_status"] == "interrupted" for attempt in attempts)


@pytest.mark.parametrize("environment", ["device_only", "controlled_online"])
def test_empty_drive_folder_has_verified_listing_and_zero_identifications(tmp_path, monkeypatch, environment):
    result, document = run_drive(tmp_path, monkeypatch, DriveTreeDevice({("My Drive",): []}), environment)
    active = [record for record in document["identifications"] if record["identification_status"] != "not_attempted"]
    assert len(active) == (2 if environment == "controlled_online" else 1)
    assert all(record["traversal_complete"] and record["identified_item_count"] == 0 for record in active)
    assert json.loads((result.run_dir / "artifacts/google-drive/inventory.json").read_text())["items"] == []


def test_drive_failed_menu_recovery_stops_without_claiming_traversal(tmp_path, monkeypatch):
    class StuckMenuDevice(DriveTreeDevice):
        def back(self):
            raise RuntimeError("menu stayed open")
    device = StuckMenuDevice({("My Drive",): [("report.txt", "Text", "1 pm"), ("second.txt", "Text", "2 pm")]}, unavailable=True)
    with pytest.raises(AcquisitionRunError) as failure:
        run_drive(tmp_path, monkeypatch, device, "controlled_online")
    document = json.loads((failure.value.run_dir / "acquisition.json").read_text())
    assert all(record["traversal_complete"] is False for record in document["identifications"])
    inventory = json.loads((failure.value.run_dir / "artifacts/google-drive/inventory.json").read_text())
    assert inventory["item_count"] == 1
    assert len(inventory["downloads"]) == 1


@pytest.mark.parametrize("name", ["report.txt", r"..\escaped.txt", r"C:\escaped.txt"])
def test_drive_pull_uses_fixed_host_name_and_preserves_source_name(tmp_path, monkeypatch, name):
    device = DriveTreeDevice({("My Drive",): [(name, "Text", "1 pm")]})
    monkeypatch.setattr(drive_collector, "_await_download", lambda *args: ("/sdcard/Download/" + name, 4, 200))
    result, document = run_drive(tmp_path, monkeypatch, device, "controlled_online")
    assert device.pull_destinations == ["artifact"]
    item = next(item for item in document["items"] if item["item_type"] == "file_content")
    assert item["source"]["name"] == name
    inventory = json.loads((result.run_dir / "artifacts/google-drive/inventory.json").read_text())
    assert inventory["downloads"][0]["displayed_name"] == name
    if name == "report.txt":
        assert inventory["downloads"][0]["retained_path"].endswith("/report.txt")
        assert inventory["downloads"][0]["status"] == "acquired"
    else:
        assert inventory["downloads"][0]["status"] == "not_acquired"


@pytest.mark.parametrize("matches", [0, 2])
def test_refresh_drive_row_rejects_missing_or_ambiguous_current_row(matches):
    device = DriveTreeDevice({("My Drive",): [("report.txt", "Text", "1 pm")] * matches})
    row = drive_collector.DriveRow("report.txt", "file", "Text", "1 pm", (20, 100, 1000, 200), (900, 100, 1000, 200))
    with pytest.raises(RuntimeError, match="row_not_uniquely_visible"):
        drive_collector._refresh_row(SimpleNamespace(device=device,
            ui=SimpleNamespace(wait_until=lambda predicate: predicate())), row)


@pytest.mark.parametrize("returned", ["same", "missing", "ambiguous", "changed"])
def test_refresh_drive_row_waits_for_exact_identity_after_list_shell(tmp_path, returned):
    from aura.journal import EventJournal
    from aura.ui import UiRuntime

    node = '''<node clickable="true" bounds="[24,1196][1056,1364]">
      <node content-desc="Audio"/><node text="Modified"/><node text="1 Sept"/>
      <node resource-id="more_actions_button" bounds="[924,1208][1068,1352]">
        <node content-desc="More actions for AURA-EVAL-08-audio.wav"/>
      </node></node>'''
    shell = '<hierarchy><node resource-id="scrollList">{}</node></hierarchy>'
    row, = parse_listing(shell.format(node))
    final = {"same": node, "missing": "", "ambiguous": node * 2,
             "changed": node.replace("1 Sept", "2 Sept")}[returned]

    class ReturningDevice:
        reads = 0

        def hierarchy(self):
            self.reads += 1
            return shell.format("" if self.reads < 3 else final)

    clock = [0.0]
    device = ReturningDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {})
    context = SimpleNamespace(device=device, journal=journal, ui=UiRuntime(
        device, journal,
        default_timeout=0.6, poll_interval=0.2,
        monotonic=lambda: clock[0], sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    if returned == "same":
        actual = drive_collector._refresh_row(context, row, ("My Drive", "AURA-EVAL", "04-Archive"))
        assert actual.menu_bounds == (924, 1208, 1068, 1352)
        assert actual.name == "AURA-EVAL-08-audio.wav"
    else:
        with pytest.raises(RuntimeError, match="row_not_uniquely_visible"):
            drive_collector._refresh_row(context, row)
    assert device.reads >= 3
    assert clock[0] <= 0.6
    events = [json.loads(line) for line in journal.path.read_text().splitlines()]
    ready = [event for event in events if event.get("action") == "google_drive_row_ready_after_wait"]
    if returned == "same":
        assert len(ready) == 1
        assert ready[0]["status"] == "success"
        assert ready[0]["context"]["drive_path"] == ["My Drive", "AURA-EVAL", "04-Archive", "AURA-EVAL-08-audio.wav"]
        assert ready[0]["details"]["recheck_count"] == 2
    else:
        assert ready == []


@pytest.mark.parametrize("revealed", [True, False])
def test_refresh_drive_row_scrolls_only_exact_row_obscured_by_returned_fab(tmp_path, revealed):
    from aura.journal import EventJournal
    from aura.ui import UiRuntime

    def listing(top, compose_top):
        return f'''<hierarchy><node resource-id="scrollList">
          <node clickable="true" bounds="[24,{top}][1056,{top + 168}]">
            <node content-desc="Video"/><node text="Modified"/><node text="1 Sept"/>
            <node resource-id="more_actions_button" bounds="[924,{top + 12}][1068,{top + 156}]">
              <node content-desc="More actions for AURA-EVAL-09-video.mp4"/>
            </node>
          </node></node>
          <node resource-id="fab_compose_view" bounds="[744,{compose_top}][1080,{compose_top + 288}]"/>
          <node resource-id="bottom_navigation" bounds="[0,2064][1080,2400]"/>
        </hierarchy>'''

    class ReturningDevice:
        swipes = 0

        def hierarchy(self):
            return listing(1392 if self.swipes and revealed else 1872, 1776)

        def swipe(self, direction, duration):
            assert direction == "up"
            self.swipes += 1

    device = ReturningDevice()
    context = SimpleNamespace(device=device,
        ui=UiRuntime(device, EventJournal(tmp_path / "events.jsonl", {})),
        app_profile=SimpleNamespace(timings={"transition_settle": 0}))
    row, = parse_listing(listing(1872, 1436))
    assert parse_listing(device.hierarchy()) == []
    if revealed:
        refreshed = drive_collector._refresh_row(context, row, ("My Drive", "04-Archive"))
        assert refreshed.name == row.name
        assert refreshed.menu_bounds == (924, 1404, 1068, 1548)
    else:
        with pytest.raises(RuntimeError, match="row_not_uniquely_visible"):
            drive_collector._refresh_row(context, row)
    assert device.swipes == 1


def test_refresh_drive_row_rejects_visible_match_with_obscured_duplicate():
    hierarchy = '''<hierarchy><node resource-id="scrollList">
      <node clickable="true" bounds="[24,100][1056,268]">
        <node content-desc="Video"/><node text="Modified"/><node text="1 Sept"/>
        <node resource-id="more_actions_button" bounds="[924,112][1068,256]">
          <node content-desc="More actions for duplicate.mp4"/>
        </node>
      </node>
      <node clickable="true" bounds="[24,1872][1056,2040]">
        <node content-desc="Video"/><node text="Modified"/><node text="1 Sept"/>
        <node resource-id="more_actions_button" bounds="[924,1884][1068,2028]">
          <node content-desc="More actions for duplicate.mp4"/>
        </node>
      </node></node>
      <node resource-id="fab_compose_view" bounds="[744,1776][1080,2064]"/>
    </hierarchy>'''
    row = parse_listing(hierarchy)[0]

    with pytest.raises(RuntimeError, match="row_not_uniquely_visible"):
        drive_collector._refresh_row(
            SimpleNamespace(device=SimpleNamespace(hierarchy=lambda: hierarchy)), row
        )


@pytest.mark.parametrize("capture_error", [False, True])
def test_drive_row_refresh_failure_preserves_actual_returned_screen(tmp_path, monkeypatch, capture_error):
    failure_xml = '<hierarchy><node resource-id="scrollList" text="Row disappeared after return"/></hierarchy>'

    class MissingReturnedRow(DriveTreeDevice):
        returned = False

        def click(self, selector):
            if self.surface == "information":
                self.returned = True
            super().click(selector)

        def hierarchy(self):
            return failure_xml if self.returned else super().hierarchy()

        def screenshot(self):
            if self.returned:
                if capture_error:
                    raise OSError("failure screenshot unavailable")
                return b"actual-returned-failure-screen"
            return super().screenshot()

    device = MissingReturnedRow({("My Drive",): [("AURA-EVAL-09-video.mp4", "Video", "1 Sept")]})
    with pytest.raises(AcquisitionRunError) as failed:
        run_drive(tmp_path, monkeypatch, device, "controlled_online")
    document = json.loads((failed.value.run_dir / "acquisition.json").read_text())
    outcome = next(item for item in document["outcomes"] if item["reason"] == "google_drive_row_not_uniquely_visible")
    if capture_error:
        assert outcome["observation_id"] is None
        assert outcome["details"]["failure_evidence_error"] == "OSError: failure screenshot unavailable"
        return
    observation = next(item for item in document["observations"] if item["observation_id"] == outcome["observation_id"])
    assert observation["attempt_id"] == outcome["attempt_id"]
    tree = next(item for item in document["artifacts"] if item["artifact_id"] == observation["hierarchy_artifact_id"])
    assert (failed.value.run_dir / tree["relative_path"]).read_text() == failure_xml
    assert tree["context"]["drive_path"] == ["My Drive", "AURA-EVAL-09-video.mp4"]
    screen = next(item for item in document["artifacts"] if item["artifact_id"] == observation["screen_artifact_id"])
    assert (failed.value.run_dir / screen["relative_path"]).read_bytes() == b"actual-returned-failure-screen"
    assert outcome["action_id"] == observation["action_id"]
    assert document["identifications"][0]["traversal_complete"] is False


def test_parse_listing_distinguishes_folder_and_file_rows():
    xml = """<hierarchy>
      <node resource-id="scrollList">
        <node resource-id="Test" clickable="true" bounds="[24,616][1056,784]">
          <node content-desc="Test" />
          <node content-desc="Folder" />
          <node text="Modified" />
          <node text="1:08 pm" />
          <node resource-id="more_actions_button" clickable="true"
                bounds="[924,628][1068,772]">
            <node content-desc="More actions for Test" />
          </node>
        </node>
        <node resource-id="report.pdf" clickable="true"
              bounds="[24,791][1056,959]">
          <node content-desc="report.pdf" />
          <node content-desc="PDF" />
          <node text="Modified" />
          <node text="5 Nov 2024" />
          <node resource-id="more_actions_button" clickable="true"
                bounds="[924,803][1068,947]">
            <node content-desc="More actions for report.pdf" />
          </node>
        </node>
      </node>
    </hierarchy>"""

    rows = parse_listing(xml)

    assert [row.as_record(("My Drive",)) for row in rows] == [
        {
            "name": "Test",
            "kind": "folder",
            "drive_path": ["My Drive", "Test"],
            "displayed_type": "Folder",
            "displayed_modified": "1:08 pm",
        },
        {
            "name": "report.pdf",
            "kind": "file",
            "drive_path": ["My Drive", "report.pdf"],
            "displayed_type": "PDF",
            "displayed_modified": "5 Nov 2024",
        },
    ]


def test_parse_listing_ignores_rows_with_menu_covered_by_floating_control():
    xml = """<hierarchy>
      <node resource-id="scrollList">
        <node clickable="true" bounds="[24,1350][1056,1518]">
          <node content-desc="visible.txt" />
          <node content-desc="Text" />
          <node resource-id="more_actions_button" clickable="true"
                bounds="[924,1362][1068,1506]">
            <node content-desc="More actions for visible.txt" />
          </node>
        </node>
        <node clickable="true" bounds="[24,1522][1056,1690]">
          <node content-desc="covered.png" />
          <node content-desc="Image" />
          <node resource-id="more_actions_button" clickable="true"
                bounds="[924,1534][1068,1678]">
            <node content-desc="More actions for covered.png" />
          </node>
        </node>
      </node>
      <node resource-id="com.google.android.apps.docs:id/scanner_fab"
            clickable="true" bounds="[864,1560][1032,1728]" />
    </hierarchy>"""

    assert [row.name for row in parse_listing(xml)] == ["visible.txt"]


def test_parse_information_keeps_visible_file_metadata():
    xml = """<hierarchy>
      <node resource-id="com.google.android.apps.docs:id/toolbar"
            content-desc="Showing item properties for report.pdf" />
      <node resource-id="com.google.android.apps.docs:id/kind_text" text="PDF" />
      <node resource-id="com.google.android.apps.docs:id/location_text" text="Test" />
      <node resource-id="com.google.android.apps.docs:id/size_text" text="1.6 MB" />
      <node resource-id="com.google.android.apps.docs:id/quota_text" text="1.6 MB" />
      <node resource-id="com.google.android.apps.docs:id/created_text" text="1:08 pm" />
      <node resource-id="com.google.android.apps.docs:id/modified_text"
            text="1:09 pm by Example User" />
      <node resource-id="com.google.android.apps.docs:id/private_acl">
        <node text="Not shared" />
      </node>
    </hierarchy>"""

    assert parse_information(xml) == {
        "name": "report.pdf",
        "type": "PDF",
        "location": "Test",
        "size": "1.6 MB",
        "storage_used": "1.6 MB",
        "created": "1:08 pm",
        "modified": "1:09 pm by Example User",
        "access": "Not shared",
    }


def test_detail_metadata_requires_fields_beyond_the_item_name():
    assert detail_metadata_complete({"name": "report.pdf"}) is False
    assert detail_metadata_complete(
        {
            "name": "report.pdf",
            "type": "PDF",
            "location": "My Drive",
            "created": "1:08 pm",
            "modified": "1:09 pm",
        }
    ) is True


def test_parse_activity_keeps_actor_time_and_event_together():
    xml = """<hierarchy>
      <node resource-id="com.google.android.apps.docs:id/main_content">
        <node resource-id="com.google.android.apps.docs:id/recent_event_username"
              text="Example User" />
        <node resource-id="com.google.android.apps.docs:id/recent_event_timestamp"
              text="1:09 pm" />
        <node resource-id="com.google.android.apps.docs:id/recent_event_eventType"
              text="Uploaded this file" />
      </node>
      <node resource-id="com.google.android.apps.docs:id/recents_status"
            text="No activity recorded before 1 September 2026" />
    </hierarchy>"""

    assert parse_activity(xml) == (
        [
            {
                "actor": "Example User",
                "displayed_time": "1:09 pm",
                "event": "Uploaded this file",
            }
        ],
        "No activity recorded before 1 September 2026",
    )


def test_activity_complete_rejects_transient_network_error():
    assert activity_complete(
        "No activity recorded before 1 September 2026"
    ) is True
    assert activity_complete(
        "Oops, couldn't load activity. Please try again later."
    ) is False


def test_select_download_accepts_google_drive_duplicate_suffix():
    before = (("/sdcard/Download/report.pdf", 10, 100),)
    after = (
        ("/sdcard/Download/report.pdf", 10, 100),
        ("/sdcard/Download/report (1).pdf", 20, 200),
    )
    assert select_download(before, after, "report.pdf") == (
        "/sdcard/Download/report (1).pdf",
        20,
        200,
    )


def test_select_download_accepts_google_drive_postfix_and_text_names():
    assert select_download(
        (),
        (("/sdcard/Download/1-20.hwp (1)", 20, 200),),
        "1-20.hwp",
    ) == ("/sdcard/Download/1-20.hwp (1)", 20, 200)
    assert select_download(
        (),
        (("/sdcard/Download/events.log (1).txt", 30, 300),),
        "events.log",
    ) == ("/sdcard/Download/events.log (1).txt", 30, 300)


def test_parse_file_inventory_preserves_spaces_in_download_paths():
    output = "/sdcard/Download/report final.pdf\00020\000200\000"

    assert parse_file_inventory(output, "/sdcard/Download") == (
        ("/sdcard/Download/report final.pdf", 20, 200),
    )


def test_download_inventory_batches_stat_calls():
    command = drive_collector._inventory_command("/sdcard/Download")

    assert 'stat -c "%s %Y" "$@"' in command
    assert "$(stat" not in command


def test_collection_scope_limits_network_actions_to_controlled_online():
    local = collection_scope("device_only")
    online = collection_scope("controlled_online")

    assert (local.collect_activity, local.download_files) == (False, False)
    assert (online.collect_activity, online.download_files) == (True, True)


@pytest.mark.parametrize("environment", ["device_only", "controlled_online"])
def test_metadata_attempt_records_canonical_environment(tmp_path, monkeypatch, environment):
    store = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    row = drive_collector.DriveRow("report.txt", "file", "Text", "1 pm", (0, 0, 10, 10), (8, 0, 10, 10))

    def unavailable_menu(*args):
        raise RuntimeError("item menu unavailable")

    monkeypatch.setattr(drive_collector, "_open_menu", unavailable_menu)

    def collect(context):
        drive_collector._collect_information(context, row, ("My Drive",), collection_scope(environment), 1)
        return Outcome(OutcomeStatus.PARTIAL, "google_drive_metadata_unavailable")

    result = AcquisitionRuntime(tmp_path, FakeDevice()).run(
        store.load_app("google_drive", "2.26.337.0.all.alldpi"),
        store.load_system_ui("samsung"), Route.MATERIALIZE,
        {"acquisition_environment": environment}, collect, run_id="metadata",
    )
    document = json.loads((result.run_dir / "acquisition.json").read_text())
    assert document["attempts"][0]["context"] == {
        "drive_path": ["My Drive", "report.txt"],
        "item_kind": "file",
        "acquisition_environment": environment,
    }
    assert document["attempts"][0]["procedure_status"] == "interrupted"


def test_empty_drive_traversal_completes_with_zero_data_items(tmp_path, monkeypatch):
    store = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    monkeypatch.setattr(drive_collector, "_open_root", lambda context, attempt: context.journal.record_action("open_root", status="success", attempt_id=attempt))
    result = AcquisitionRuntime(tmp_path, FakeDevice(hierarchy_before="<hierarchy/>", hierarchy_after_swipe="<hierarchy/>")).run(
        store.load_app("google_drive", "2.26.337.0.all.alldpi"), store.load_system_ui("samsung"), Route.MATERIALIZE,
        {"acquisition_environment": "device_only"}, lambda context: drive_collector._collect_drive(context, collection_scope("device_only")), run_id="empty-drive",
    )
    record = json.loads((result.run_dir / "acquisition.json").read_text())["identifications"][0]
    assert record["identification_status"] == "completed"
    assert record["traversal_complete"] is True
    assert record["identified_item_count"] == 0


def test_walk_folder_refreshes_row_bounds_after_each_item(monkeypatch):
    def listing(second_top):
        return f"""<hierarchy><node resource-id="scrollList">
          <node clickable="true" bounds="[24,100][1056,200]">
            <node content-desc="first.txt" />
            <node content-desc="Text" />
            <node text="Modified" /><node text="1 pm" />
            <node resource-id="more_actions_button" clickable="true"
                  bounds="[924,110][1068,190]">
              <node content-desc="More actions for first.txt" />
            </node>
          </node>
          <node clickable="true" bounds="[24,{second_top}][1056,{second_top + 100}]">
            <node content-desc="second.txt" />
            <node content-desc="Text" />
            <node text="Modified" /><node text="2 pm" />
            <node resource-id="more_actions_button" clickable="true"
                  bounds="[924,{second_top + 10}][1068,{second_top + 90}]">
              <node content-desc="More actions for second.txt" />
            </node>
          </node>
        </node></hierarchy>"""

    device = SimpleNamespace(current=listing(220))
    device.hierarchy = lambda: device.current
    ui = SimpleNamespace(swipe=lambda *args, **kwargs: None)
    context = SimpleNamespace(
        device=device,
        ui=ui,
        app_profile=SimpleNamespace(timings={"transition_settle": 0.0}),
    )

    monkeypatch.setattr(
        drive_collector,
        "_observe",
        lambda context, attempt, prefix, action, linked: (
            context.device.hierarchy(),
            action,
        ),
    )
    monkeypatch.setattr(
        drive_collector,
        "_linked",
        lambda context, name, attempt, source_action, linked: source_action,
    )

    def collect_information(context, row, parent, scope, sequence):
        if row.name == "first.txt":
            context.device.current = listing(420)
        return {
            "name": row.name,
            "menu_bounds": list(row.menu_bounds),
        }, None, True

    monkeypatch.setattr(
        drive_collector, "_collect_information", collect_information
    )
    records = []
    counters = {
        "items": 0,
        "folders": 0,
        "files": 0,
        "metadata_failures": 0,
        "download_failures": 0,
        "folder_failures": 0,
    }

    drive_collector._walk_folder(
        context,
        ("My Drive",),
        "attempt-1",
        collection_scope("device_only"),
        None,
        records,
        [],
        counters,
    )

    assert records == [
        {"name": "first.txt", "menu_bounds": [924, 110, 1068, 190]},
        {"name": "second.txt", "menu_bounds": [924, 430, 1068, 510]},
    ]


def test_walk_folder_refreshes_same_row_before_download(monkeypatch):
    def listing(top):
        return f"""<hierarchy><node resource-id="scrollList">
          <node clickable="true" bounds="[24,{top}][1056,{top + 100}]">
            <node content-desc="report.txt" />
            <node content-desc="Text" />
            <node text="Modified" /><node text="1 pm" />
            <node resource-id="more_actions_button" clickable="true"
                  bounds="[924,{top + 10}][1068,{top + 90}]">
              <node content-desc="More actions for report.txt" />
            </node>
          </node>
        </node></hierarchy>"""

    device = SimpleNamespace(current=listing(100))
    device.hierarchy = lambda: device.current
    context = SimpleNamespace(
        device=device,
        ui=SimpleNamespace(swipe=lambda *args, **kwargs: None),
        app_profile=SimpleNamespace(timings={"transition_settle": 0.0}),
    )
    monkeypatch.setattr(
        drive_collector,
        "_observe",
        lambda context, attempt, prefix, action, linked: (
            context.device.hierarchy(),
            action,
        ),
    )
    monkeypatch.setattr(
        drive_collector,
        "_linked",
        lambda context, name, attempt, source_action, linked: source_action,
    )

    def collect_information(context, row, parent, scope, sequence):
        context.device.current = listing(400)
        return {"name": row.name}, None, True

    monkeypatch.setattr(
        drive_collector, "_collect_information", collect_information
    )
    monkeypatch.setattr(
        drive_collector,
        "_download_file",
        lambda context, row, parent, sequence: (
            {"menu_bounds": list(row.menu_bounds)},
            None,
            True,
        ),
    )
    downloads = []
    counters = {
        "items": 0,
        "folders": 0,
        "files": 0,
        "metadata_failures": 0,
        "download_failures": 0,
        "folder_failures": 0,
    }

    drive_collector._walk_folder(
        context,
        ("My Drive",),
        "attempt-1",
        collection_scope("controlled_online"),
        None,
        [],
        downloads,
        counters,
    )

    assert downloads == [{"menu_bounds": [924, 410, 1068, 490]}]


def test_collect_uses_controlled_online_scope(monkeypatch):
    expected = Outcome(OutcomeStatus.COMPLETE, details={"file_count": 2})
    observed = []

    def collect_drive(context, scope):
        observed.append((context, scope))
        return expected

    monkeypatch.setattr(drive_collector, "_collect_drive", collect_drive)
    context = SimpleNamespace(
        app_profile=ProfileStore(Path(__file__).resolve().parents[1] / "profiles").load_app("google_drive", "2.26.337.0.all.alldpi"),
        base_context={
            "condition": {
                "target": {
                    "kind": "cloud_storage",
                    "ref": "drive.google.my-drive",
                },
                "acquisition_environment": "controlled_online",
            }
        },
        start_app=lambda: True,
    )

    assert drive_collector.collect(context) is expected
    assert observed[0][1] == collection_scope("controlled_online")

def test_google_drive_is_registered_with_cloud_storage_condition():
    assert cli.APP_PACKAGES["google_drive"] == "com.google.android.apps.docs"
    assert cli.COLLECTORS["google_drive"] is not None
    assert cli._condition(
        "google_drive", cli.Route.MATERIALIZE, "controlled_online"
    )["target"] == {"kind": "cloud_storage", "ref": "drive.google.my-drive"}


def test_google_drive_profile_separates_metadata_and_download_phases():
    root = Path(__file__).resolve().parents[1] / "profiles"
    profile = ProfileStore(root).load_app(
        "google_drive", "2.26.337.0.all.alldpi"
    )

    assert [target.target_id for target in profile.targets] == [
        "google_drive.inventory",
        "google_drive.files",
    ]
    assert profile.targets[0].acquisition_environments == ("device_only", "controlled_online")
    assert profile.targets[1].acquisition_environments == ("controlled_online",)
