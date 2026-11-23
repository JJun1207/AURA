"""Comparison excludes system bars; retained UI evidence stays intact."""
from fakes import browser_profile
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from aura.apps.notesnook.adapter import NotesnookDeviceAdapter
from aura.apps.telegram.adapter import TelegramDeviceAdapter
from aura.apps.notion.support import observe
from aura.apps.whatsapp.collector import _settled_root


def screen(tick, content="Ready", dialog="Pair"):
    return f'''<hierarchy rotation="0">
      <node package="example.app" text="{content}" bounds="[0,80][1080,2200]"/>
      <node package="com.android.systemui" bounds="[0,0][1080,80]">
        <node package="com.android.systemui" resource-id="com.android.systemui:id/status_bar_container">
          <node text="{tick}" content-desc="signal-{tick}" bounds="[0,0][20,80]"/>
        </node>
      </node>
      <node package="com.android.systemui" resource-id="com.android.systemui:id/navigation_bar_frame" content-desc="nav-{tick}"/>
      <node package="com.android.systemui" resource-id="com.android.systemui:id/resolver_list" text="{dialog}"/>
    </hierarchy>'''


@pytest.mark.parametrize("app", ["startup", "notesnook", "telegram", "notion", "whatsapp"])
def test_ui_stability_ignores_bars_and_coordinates_but_waits_for_content(app):
    reads = []

    def hierarchy():
        # The app changes once, then settles; the system bars keep changing.
        value = screen(len(reads), "Loading" if not reads else "Ready").replace(
            '[0,80][1080,2200]', f'[0,{80 + len(reads)}][1080,{2200 + len(reads)}]')
        reads.append(value)
        return value

    context = SimpleNamespace(
        device=SimpleNamespace(hierarchy=hierarchy, screenshot=lambda: b"original-screen", app_start=lambda *a: None),
        app_profile=SimpleNamespace(package_name="example.app", timings={**browser_profile('telegram', '13.1.1').timings, "transition_settle": 0}),
        ui=SimpleNamespace(wait_until=lambda predicate, **kwargs: any(predicate() for _ in range(5)), wait_for=lambda *a, **kwargs: True),
        journal=SimpleNamespace(append=lambda *args, **kwargs: None),
    )
    if app == "startup":
        from aura.runtime import RunContext
        assert RunContext.start_app(context)
        value = reads[-1]
    elif app == "notesnook":
        value = NotesnookDeviceAdapter._settled_hierarchy(SimpleNamespace(context=context))
    elif app == "telegram":
        value = TelegramDeviceAdapter._settled_hierarchy(SimpleNamespace(context=context))
    elif app == "notion":
        value = observe(context, "observation", SimpleNamespace(event_id="event"))["tree"].decode()
    else:
        value = _settled_root(context, lambda root: root[0].get("text"), "not_ready")[1]
    assert len(reads) == 3
    assert value == reads[-1], "The retained XML must still contain the original bars"
    assert 'signal-2' in value


def test_content_comparison_keeps_dialogs_and_input_tree():
    from aura.ui import hierarchy_signature

    original = ET.fromstring(screen(1))
    unchanged = ET.tostring(original)
    baseline = hierarchy_signature(original)
    assert baseline == hierarchy_signature(screen(2))
    assert baseline != hierarchy_signature(screen(2, content="Other item"))
    assert baseline != hierarchy_signature(screen(2, dialog="Finish"))
    assert ET.tostring(original) == unchanged


def test_shared_swipe_does_not_accept_only_a_system_bar_change(tmp_path):
    from aura.journal import EventJournal
    from aura.ui import UiRuntime, UiTimeout
    from fakes import FakeDevice

    device = FakeDevice(hierarchy_before=screen(1), hierarchy_after_swipe=screen(2))
    ui = UiRuntime(device, EventJournal(tmp_path / "events.jsonl", {}), default_timeout=0)
    with pytest.raises(UiTimeout):
        ui.swipe("scroll", "up", expect_change=True)
