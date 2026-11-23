import importlib.util
import json
from io import StringIO
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


def script():
    spec = importlib.util.spec_from_file_location("seed_telegram", Path(__file__).resolve().parents[1] / "tools/seed_telegram_messages.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Phone:
    def __init__(self, fault=None):
        self.chat = "Example channel"
        self.draft = "existing draft" if fault == "draft" else ""
        self.fault = fault
        self.sent = []
        self.clicks = 0

    def app_current(self):
        raise AssertionError("The sender must not query app_current")

    def dump_hierarchy(self):
        root = ET.Element("hierarchy")
        def node(**values):
            return ET.SubElement(root, "node", {"package": "org.telegram.messenger", "visible-to-user": "true", "enabled": "true", **values})
        node(**{"class": "android.widget.FrameLayout", "content-desc": self.chat + ", Muted\n1 subscriber", "clickable": "true"})
        node(**{"class": "android.widget.EditText", "text": self.draft or "Broadcast"})
        if self.draft:
            node(**{"content-desc": "Send", "clickable": "true"})
        for message in self.sent:
            node(**{"class": "android.view.ViewGroup", "text": message + "\nSent at 02:00"})
        return ET.tostring(root, encoding="unicode")

    def __call__(self, **selector):
        return self

    def set_text(self, text):
        self.draft = text
        if self.fault == "changed_chat":
            self.chat = "OTHER CHANNEL"

    def click(self, offset=(.5, .5)):
        self.clicks += 1
        # Pixel Telegram's wide Send view overlaps the composer at its center.
        if self.fault != "unconfirmed" and offset == (.8, .5):
            self.sent.append(self.draft)
            self.draft = ""


@pytest.mark.parametrize("fault", [None, "draft", "changed_chat", "unconfirmed"])
def test_indexed_batch_stops_without_resending_or_overwriting(fault):
    module = script()
    phone = Phone(fault)
    log = StringIO()
    if fault:
        with pytest.raises(RuntimeError):
            module.send_messages(phone, "Example channel", 41, 3, 0, .01, log)
        assert phone.sent == []
        assert phone.clicks == (1 if fault == "unconfirmed" else 0)
        if fault == "draft":
            assert phone.draft == "existing draft"
    else:
        module.send_messages(phone, "Example channel", 41, 3, 0, .01, log)
        assert phone.sent == ["Demo message #000041", "Demo message #000042", "Demo message #000043"]
        records = [json.loads(line) for line in log.getvalue().splitlines()]
        assert [r["index"] for r in records if r["event"] == "submitted"] == [41, 42, 43]
