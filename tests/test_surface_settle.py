"""A visible screen marker can precede actionable buttons and loaded content."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from aura.apps.chrome import collector as chrome
from aura.apps.samsung_browser import collector as samsung
from aura.profiles import ProfileStore


@pytest.mark.parametrize("app,surface", [
    ("chrome", "downloads"),
    ("chrome", "recent-tabs"),
    ("samsung_browser", "downloads"),
    ("samsung_browser", "bookmarks"),
])
def test_browser_entry_waits_between_menu_actions_and_before_reading(monkeypatch, app, surface):
    module = chrome if app == "chrome" else samsung
    store = ProfileStore(Path(__file__).resolve().parents[1] / "profiles")
    profile = store.load_app(app, "156.0.8078.79" if app == "chrome" else "30.0.5.27")
    state = {"animating": False, "closed": False, "actions": []}

    def exists(selector):
        if app == "chrome":
            return selector.get("resourceId", "").endswith("close_menu_id") and not state["closed"]
        return True  # Samsung is already on its main screen.

    def click(name, selector, **kwargs):
        assert not state["animating"], "next click arrived during the previous screen transition"
        state.update(animating=True, closed=True)
        state["actions"].append(name)
        return SimpleNamespace(action_id=name)

    def settle(seconds):
        if seconds >= profile.timings["transition_settle"]:
            state["animating"] = False

    monkeypatch.setattr(module.time, "sleep", settle)
    context = SimpleNamespace(app_profile=profile, device=SimpleNamespace(exists=exists),
                              ui=SimpleNamespace(click=click))
    if surface == "bookmarks":
        module._open_bookmarks(context)
    else:
        module._open_surface(context, f"{app}.menu.{surface}", f"{app}.screen.{surface}")
    assert not state["animating"], "collection started before the screen content was ready"
    assert state["actions"][-1].endswith(surface)
