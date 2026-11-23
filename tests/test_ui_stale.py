"""Exercise the installed uiautomator2 click path against a transient RPC read."""
import json
from types import SimpleNamespace

import pytest
from uiautomator2._selector import Selector, UiObject
from uiautomator2.exceptions import RPCUnknownError

from aura.device import AndroidDevice
from aura.journal import EventJournal
from aura.ui import UiRuntime, UiTimeout
from fakes import UnobstructedAdb


@pytest.mark.parametrize("stale_reads,remaining_targets,tap_error,outcome", [
    (1, 1, False, "success"),
    (2, 1, False, "rpc_error"),
    (1, 0, False, "target_error"),
    (1, 2, False, "target_error"),
    (0, 1, True, "rpc_error"),
])
def test_click_recovers_only_before_input_and_rechecks_unique_target(
    tmp_path, stale_reads, remaining_targets, tap_error, outcome,
):
    reads, taps, waits = [], [], []

    class Rpc:
        def count(self, selector):
            return remaining_targets if reads else 1

        def waitForExists(self, *args, **kwargs):
            return True

        def objInfo(self, selector):
            reads.append(dict(selector))
            if len(reads) <= stale_reads:
                raise RPCUnknownError(
                    "Unknown RPC error: -32001 androidx.test.uiautomator.StaleObjectException",
                    (dict(selector),),
                    "androidx.test.uiautomator.StaleObjectException\n"
                    "\tat com.wetest.uia2.stub.AutomatorServiceImpl.objInfo(AutomatorServiceImpl.java:840)",
                )
            return {"bounds": {"left": 924, "top": 92, "right": 1068, "bottom": 236}}

    class Client:
        wait_timeout = 0
        window_size = staticmethod(lambda: (1080, 2400))

        def __init__(self):
            self.jsonrpc = Rpc()

        def __call__(self, **selector):
            return UiObject(self, Selector(**selector))

        def click(self, x, y):
            taps.append((x, y))
            if tap_error:
                raise RPCUnknownError(
                    "androidx.test.uiautomator.StaleObjectException",
                    (x, y), "AutomatorServiceImpl.click(AutomatorServiceImpl.java:400)",
                )

    journal = EventJournal(tmp_path / "events.jsonl")
    ui = UiRuntime(AndroidDevice(Client(), UnobstructedAdb(), serial=None), journal,
                   default_timeout=0, poll_interval=0.2, sleep=waits.append)
    selector = {"resourceId": "com.android.chrome:id/close_menu_id", "packageName": "com.android.chrome"}
    if outcome == "success":
        assert ui.click("close_chrome_surface", selector).status == "success"
    else:
        with pytest.raises(UiTimeout if outcome == "target_error" else RPCUnknownError):
            ui.click("close_chrome_surface", selector)

    assert len(taps) == int(outcome == "success" or tap_error)
    assert len(reads) == (2 if stale_reads and remaining_targets == 1 else 1)
    assert waits == ([0.2] if stale_reads else [])
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    retries = [event for event in events if event.get("status") == "retrying"]
    assert len(retries) == int(bool(stale_reads))
    if retries:
        assert retries[0]["details"]["stage"] == "stale_target_before_click"
        assert retries[0]["details"]["selector"] == selector
