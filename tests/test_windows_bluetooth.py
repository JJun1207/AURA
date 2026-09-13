import importlib
import sys
from types import SimpleNamespace


class _TextNode:
    def __init__(self, text):
        self._text = text

    def window_text(self):
        return self._text


class _WaitingWindow:
    def exists(self, timeout=0):
        return True

    def window_text(self):
        return "Bluetooth File Transfer"

    def descendants(self):
        return [_TextNode("Waiting for a connection")]


class _Application:
    started = []
    window_value = _WaitingWindow()

    def __init__(self, *, backend):
        assert backend == "uia"

    def start(self, command):
        self.started.append(command)
        return self

    def window(self, **kwargs):
        assert kwargs == {"title_re": ".*Bluetooth.*"}
        return self.window_value


class _ClickableNode(_TextNode):
    def __init__(self, text, window):
        super().__init__(text)
        self.window = window
        self.element_info = SimpleNamespace(control_type="Hyperlink")

    def is_enabled(self):
        return True

    def click_input(self):
        self.window.waiting = True


class _ChooserWindow(_WaitingWindow):
    def __init__(self):
        self.waiting = False
        self.receive = _ClickableNode("Receive files", self)

    def descendants(self):
        if self.waiting:
            return [_TextNode("Waiting for a connection")]
        return [self.receive]


class _NextNode(_TextNode):
    def __init__(self, window):
        super().__init__("Next")
        self.window = window
        self.element_info = SimpleNamespace(control_type="Button")

    def is_enabled(self):
        return True

    def click_input(self):
        self.window.waiting = True


class _ReceiveStepNode(_TextNode):
    def __init__(self, window):
        super().__init__("Receive files")
        self.window = window
        self.element_info = SimpleNamespace(control_type="Hyperlink")

    def is_enabled(self):
        return True

    def click_input(self):
        self.window.stage = 1


class _TwoStepWindow(_WaitingWindow):
    def __init__(self):
        self.stage = 0
        self.waiting = False
        self.receive = _ReceiveStepNode(self)

    def descendants(self):
        if self.waiting:
            return [_TextNode("Waiting for a connection")]
        if self.stage == 1:
            return [_NextNode(self)]
        return [self.receive]


class _FinishButton(_TextNode):
    def __init__(self):
        super().__init__("Finish")
        self.clicked = False
        self.element_info = SimpleNamespace(control_type="Button")

    def is_enabled(self):
        return True

    def click_input(self):
        self.clicked = True


class _FinishWindow:
    def __init__(self):
        self.button = _FinishButton()

    def descendants(self):
        return [self.button]


class _ConnectedApplication:
    window_value = _FinishWindow()

    def __init__(self, *, backend):
        assert backend == "uia"

    def connect(self, *, handle):
        assert handle == 42
        return self

    def window(self, *, handle):
        assert handle == 42
        return self.window_value


class _FindWindows:
    @staticmethod
    def find_elements(**kwargs):
        assert kwargs == {
            "title_re": ".*Bluetooth.*",
            "backend": "uia",
        }
        return [SimpleNamespace(handle=42)]


def test_prepare_windows_bluetooth_receiver_verifies_waiting_screen(
    monkeypatch,
):
    windows_bluetooth = importlib.import_module("aura.windows_bluetooth")
    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    monkeypatch.setitem(
        sys.modules,
        "pywinauto",
        SimpleNamespace(Application=_Application),
    )
    _Application.started.clear()

    result = windows_bluetooth.prepare_windows_bluetooth_receiver(
        timeout_sec=0.01
    )

    assert result == {
        "ok": True,
        "reason": "receive_waiting_screen_detected",
        "detail": "",
    }
    assert _Application.started == ["fsquirt.exe -receive"]


def test_prepare_windows_bluetooth_receiver_selects_receive_files(
    monkeypatch,
):
    windows_bluetooth = importlib.import_module("aura.windows_bluetooth")
    chooser = _ChooserWindow()
    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    monkeypatch.setattr(_Application, "window_value", chooser)
    monkeypatch.setitem(
        sys.modules,
        "pywinauto",
        SimpleNamespace(Application=_Application),
    )

    result = windows_bluetooth.prepare_windows_bluetooth_receiver(
        timeout_sec=0.01
    )

    assert result["ok"] is True
    assert chooser.waiting is True


def test_finish_windows_bluetooth_receive_clicks_finish(monkeypatch):
    windows_bluetooth = importlib.import_module("aura.windows_bluetooth")
    window = _FinishWindow()
    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    monkeypatch.setattr(_ConnectedApplication, "window_value", window)
    monkeypatch.setitem(
        sys.modules,
        "pywinauto",
        SimpleNamespace(
            Application=_ConnectedApplication,
            findwindows=_FindWindows,
        ),
    )

    result = windows_bluetooth.finish_windows_bluetooth_receive(
        timeout_sec=0.01,
        poll_interval=0.0,
    )

    assert result == {
        "ok": True,
        "reason": "finish_button_clicked",
        "detail": "Finish",
    }
    assert window.button.clicked is True


def test_prepare_windows_bluetooth_receiver_advances_receive_wizard(
    monkeypatch,
):
    windows_bluetooth = importlib.import_module("aura.windows_bluetooth")
    chooser = _TwoStepWindow()
    monkeypatch.setattr(windows_bluetooth, "_IS_WINDOWS", True)
    monkeypatch.setattr(_Application, "window_value", chooser)
    monkeypatch.setitem(
        sys.modules,
        "pywinauto",
        SimpleNamespace(Application=_Application),
    )

    result = windows_bluetooth.prepare_windows_bluetooth_receiver(
        timeout_sec=0.01
    )

    assert result["ok"] is True
    assert chooser.waiting is True
