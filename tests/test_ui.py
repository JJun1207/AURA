import json

import pytest

from aura.device import AndroidDevice
from aura.journal import EventJournal
from aura.ui import UiRuntime, UiTimeout
from fakes import FakeDevice


def test_click_waits_for_target_and_expected_state(tmp_path):
    device = FakeDevice(
        existing=[{"text": "Export"}],
        appear_after_click={"text": "Save"},
    )
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal, poll_interval=0).click(
        "open_export",
        {"text": "Export"},
        expected={"text": "Save"},
        context={"screen": "settings"},
    )

    assert action.status == "success"
    assert action.context["screen"] == "settings"
    assert device.clicks == [{"text": "Export"}]


def test_click_records_timeout_before_raising(tmp_path):
    device = FakeDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})
    ui = UiRuntime(device, journal, default_timeout=0, poll_interval=0)

    with pytest.raises(UiTimeout, match="missing"):
        ui.click("missing", {"text": "Missing"})

    event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["action"] == "missing"
    assert event["status"] == "failed"
    assert event["details"]["stage"] == "target_not_found"
    assert device.clicks == []


def test_click_rejects_ambiguous_selector(tmp_path):
    selector = {"text": "Export"}
    device = FakeDevice(existing=[selector, selector])
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    with pytest.raises(UiTimeout, match="exactly one"):
        UiRuntime(device, journal, default_timeout=0).click("ambiguous", selector)

    assert device.clicks == []


@pytest.mark.parametrize(
    "selector",
    [
        {"x": 540, "y": 1200},
        {"point": (540, 1200)},
        {"bounds": (0, 0, 1080, 2400)},
    ],
)
def test_click_rejects_coordinate_selector(tmp_path, selector):
    device = FakeDevice(existing=[selector])
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    with pytest.raises(ValueError, match="coordinate"):
        UiRuntime(device, journal).click("forbidden", selector)

    assert device.clicks == []


def test_click_bounds_uses_supported_anchor_points():
    class UiClient:
        def __init__(self):
            self.points = []

        @staticmethod
        def window_size():
            return 100, 200

        def click(self, x, y):
            self.points.append((x, y))

    ui_client = UiClient()
    device = AndroidDevice(ui_client, object(), serial=None)

    device.click_bounds((10, 20, 90, 120), anchor="center")
    device.click_bounds((10, 20, 90, 120), anchor="lower_third")

    assert ui_client.points == [(50, 70), (50, 85)]


def test_long_click_bounds_uses_rectangle_center():
    class UiClient:
        def __init__(self):
            self.points = []

        @staticmethod
        def window_size():
            return 100, 200

        def long_click(self, x, y, duration=0.5):
            self.points.append((x, y, duration))

    ui_client = UiClient()

    AndroidDevice(ui_client, object(), serial=None).long_click_bounds(
        (10, 20, 90, 120)
    )

    assert ui_client.points == [(50, 70, 0.8)]


@pytest.mark.parametrize(
    "bounds",
    [
        (1, 2, 3),
        (10.0, 20, 30, 40),
        (-1, 10, 20, 30),
        (10, 10, 10, 30),
        (10, 10, 101, 30),
    ],
)
def test_click_bounds_rejects_invalid_rectangles(bounds):
    class UiClient:
        @staticmethod
        def window_size():
            return 100, 200

        @staticmethod
        def click(x, y):
            raise AssertionError(f"must not click invalid point {(x, y)}")

    device = AndroidDevice(UiClient(), object(), serial=None)

    with pytest.raises(ValueError):
        device.click_bounds(bounds)


def test_ui_runtime_click_bounds_records_derived_action(tmp_path):
    class BoundsDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.boundary_clicks = []

        def click_bounds(self, bounds, *, anchor="center"):
            self.boundary_clicks.append((tuple(bounds), anchor))

    device = BoundsDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal).click_bounds(
        "enter_chatroom",
        (10, 20, 90, 120),
        anchor="lower_third",
        context={"screen": "default_chat_list"},
    )

    assert action.status == "success"
    assert action.context["screen"] == "default_chat_list"
    assert device.boundary_clicks == [((10, 20, 90, 120), "lower_third")]
    event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["details"] == {
        "bounds": [10, 20, 90, 120],
        "anchor": "lower_third",
    }


def test_ui_runtime_long_click_bounds_records_derived_action(tmp_path):
    class BoundsDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.boundary_long_clicks = []

        def long_click_bounds(self, bounds):
            self.boundary_long_clicks.append(tuple(bounds))

    device = BoundsDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal).long_click_bounds(
        "select_note",
        (10, 20, 90, 120),
        context={"screen": "notes"},
    )

    assert action.status == "success"
    assert action.context["screen"] == "notes"
    assert device.boundary_long_clicks == [(10, 20, 90, 120)]
    event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["details"] == {"bounds": [10, 20, 90, 120]}


def test_back_confirms_expected_state(tmp_path):
    device = FakeDevice(appear_after_back={"text": "Chats"})
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal, poll_interval=0).back(
        "return_to_chats",
        expected={"text": "Chats"},
    )

    assert action.status == "success"
    assert device.back_count == 1


def test_swipe_can_require_hierarchy_change(tmp_path):
    device = FakeDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal, poll_interval=0).swipe(
        "next_page",
        "up",
        expect_change=True,
    )

    assert action.status == "success"
    assert device.swipes == [("up", 0.2)]


def test_android_device_normalizes_ui_and_adb_clients(tmp_path):
    class Element:
        def __init__(self):
            self.clicked = 0
            self.count = 1

        def exists(self, timeout=0):
            return timeout == 0

        def click(self):
            self.clicked += 1

        def long_click(self):
            self.clicked += 1

    class XPathSelector:
        def __init__(self, elements):
            self.elements = elements

        def all(self):
            return self.elements

    class UiClient:
        def __init__(self):
            self.element = Element()
            self.xpath_element = Element()
            self.selector = None
            self.xpath_query = None
            self.swipe_args = None

        def __call__(self, **selector):
            self.selector = selector
            return self.element

        def xpath(self, query):
            self.xpath_query = query
            return XPathSelector([self.xpath_element])

        def app_start(self, package_name):
            self.started = package_name

        def app_stop(self, package_name):
            self.stopped = package_name

        def press(self, key):
            self.pressed = key

        def window_size(self):
            return 100, 200

        def swipe(self, *args):
            self.swipe_args = args

        def dump_hierarchy(self, compressed=False):
            return f"<ui compressed='{compressed}'/>"

        def screenshot(self, format="pillow"):
            if format != "pillow":
                raise ValueError("unsupported screenshot format")

            class Image:
                def save(self, stream, format):
                    assert format == "PNG"
                    stream.write(b"png")

            return Image()

    class Sync:
        def pull(self, remote_path, local_path):
            self.args = (remote_path, local_path)

    class AdbClient:
        def __init__(self):
            self.sync = Sync()

        def shell(self, command):
            return f"out:{command}"

    ui_client = UiClient()
    adb_client = AdbClient()
    device = AndroidDevice(ui_client, adb_client, serial="device-1")

    assert device.exists({"text": "Export"})
    assert device.count({"text": "Export"}) == 1
    device.click({"text": "Export"})
    device.click_xpath("//android.widget.Button[@text='Save']")
    device.swipe("up", duration=0.4)
    destination = tmp_path / "pulled.bin"
    device.pull("/sdcard/file.bin", destination)

    assert ui_client.selector == {"text": "Export"}
    assert ui_client.element.clicked == 1
    assert ui_client.xpath_query == "//android.widget.Button[@text='Save']"
    assert ui_client.xpath_element.clicked == 1
    assert ui_client.swipe_args == (50, 120, 50, 80, 0.4)
    assert adb_client.sync.args == ("/sdcard/file.bin", str(destination))
    assert device.hierarchy() == "<ui compressed='False'/>"
    assert device.screenshot() == b"png"
    assert device.shell("id") == "out:id"


def test_android_device_swipe_uses_requested_distance_ratio():
    class UiClient:
        swipe_args = None

        @staticmethod
        def window_size():
            return 100, 200

        def swipe(self, *args):
            self.swipe_args = args

    ui_client = UiClient()
    device = AndroidDevice(ui_client, object(), serial="device-1")

    device.swipe("down", duration=0.3, distance_ratio=0.42)

    assert ui_client.swipe_args == (50, 58, 50, 142, 0.3)


def test_android_device_swipe_bounds_uses_derived_rectangle_points():
    class UiClient:
        swipe_args = None

        @staticmethod
        def window_size():
            return 100, 200

        def swipe(self, *args):
            self.swipe_args = args

    ui_client = UiClient()
    device = AndroidDevice(ui_client, object(), serial="device-1")

    device.swipe_bounds(
        (10, 20, 90, 120),
        "left",
        duration=0.3,
        distance_ratio=0.5,
    )

    assert ui_client.swipe_args == (70, 70, 30, 70, 0.3)


def test_ui_runtime_swipe_bounds_records_derived_action(tmp_path):
    class BoundsDevice(FakeDevice):
        def __init__(self):
            super().__init__()
            self.boundary_swipes = []

        def swipe_bounds(
            self,
            bounds,
            direction,
            duration=0.2,
            distance_ratio=0.8,
        ):
            self.boundary_swipes.append(
                (tuple(bounds), direction, duration, distance_ratio)
            )

    device = BoundsDevice()
    journal = EventJournal(tmp_path / "events.jsonl", {"run_id": "run-1"})

    action = UiRuntime(device, journal).swipe_bounds(
        "next_offline_cards",
        (10, 20, 90, 120),
        "left",
        duration=0.3,
        distance_ratio=0.5,
        context={"screen": "notion_home"},
    )

    assert action.status == "success"
    assert device.boundary_swipes == [
        ((10, 20, 90, 120), "left", 0.3, 0.5)
    ]
    event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    )
    assert event["details"] == {
        "bounds": [10, 20, 90, 120],
        "direction": "left",
        "duration": 0.3,
        "distance_ratio": 0.5,
    }


@pytest.mark.parametrize("match_count", [0, 2])
def test_android_device_click_xpath_requires_exactly_one_target(match_count):
    class Element:
        def __init__(self):
            self.clicked = 0

        def click(self):
            self.clicked += 1

    element = Element()

    class UiClient:
        @staticmethod
        def xpath(query):
            assert query == "//*[@text='Save']/ancestor::*[@clickable='true'][1]"

            class XPathSelector:
                @staticmethod
                def all():
                    return [element] * match_count

            return XPathSelector()

    device = AndroidDevice(UiClient(), object(), serial=None)

    with pytest.raises(RuntimeError, match="exactly one"):
        device.click_xpath(
            "//*[@text='Save']/ancestor::*[@clickable='true'][1]"
        )

    assert element.clicked == 0
