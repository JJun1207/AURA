import importlib
import sys
from types import SimpleNamespace


class _TextNode:
    def __init__(self, text):
        self._text = text

    def window_text(self):
        return self._text


class _WaitingWindow:
    handle = 42
    def exists(self, timeout=0):
        return not getattr(self, 'closed', False)

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
        item = _TextNode("chat.zip")
        item.element_info = SimpleNamespace(control_type="ListItem")
        return [_TextNode("Save the received file"), item, self.button]


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
        "receiver_handle": 42,
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
    monkeypatch.setattr(importlib.import_module("aura.windows_bluetooth"),
                        "_receiver_handle_exists", lambda handle: not window.button.clicked)
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
        "reason": "receiver_save_completed",
        "detail": "Receiver closed after Finish",
        "file_names": ["chat.zip"],
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



def test_finish_uses_this_chat_window_and_selects_file_to_enable_finish(monkeypatch):
    from aura import windows_bluetooth as bt

    class FileItem(_TextNode):
        element_info = SimpleNamespace(control_type="ListItem")
        def is_enabled(self): return True
        def click_input(self): window.selected = True

    class Finish(_FinishButton):
        def is_enabled(self): return window.selected

    class Window(_FinishWindow):
        selected = False
        def __init__(self): self.button = Finish()
        def descendants(self):
            return [FileItem("WhatsApp Chat with TEST.zip"), self.button]

    window = Window()
    monkeypatch.setattr(bt, "_IS_WINDOWS", True)
    monkeypatch.setattr(_ConnectedApplication, "window_value", window)
    monkeypatch.setattr(importlib.import_module("aura.windows_bluetooth"),
                        "_receiver_handle_exists", lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(
        Application=_ConnectedApplication,
        findwindows=SimpleNamespace(find_elements=lambda **kwargs: [SimpleNamespace(handle=42), SimpleNamespace(handle=99)]),
    ))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0.01, poll_interval=0, receiver_handle=42)
    assert result["ok"] is True
    assert result["file_names"] == ["WhatsApp Chat with TEST.zip"]
    assert window.selected and window.button.clicked


def test_finish_reports_actual_control_error_instead_of_empty_timeout(monkeypatch):
    from aura import windows_bluetooth as bt
    class BrokenWindow:
        def descendants(self): raise RuntimeError("UIA receiver inaccessible")
    monkeypatch.setattr(bt, "_IS_WINDOWS", True)
    monkeypatch.setattr(_ConnectedApplication, "window_value", BrokenWindow())
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0, poll_interval=0)
    assert result["ok"] is False
    assert "UIA receiver inaccessible" in result["detail"]



def test_idle_receiver_cleanup_preserves_pending_received_data(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_WaitingWindow):
        pending_data = False
        closed = False
        def descendants(self):
            return [_TextNode("Save the received file" if self.pending_data else "Waiting for a connection")]
        def close(self): self.closed = True
    window = Window()
    monkeypatch.setattr(bt, "_IS_WINDOWS", True)
    monkeypatch.setattr(_ConnectedApplication, "window_value", window)
    monkeypatch.setattr(importlib.import_module("aura.windows_bluetooth"),
                        "_receiver_handle_exists", lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(Application=_ConnectedApplication,
        findwindows=SimpleNamespace(find_elements=lambda **kwargs: [SimpleNamespace(handle=42)])))
    window.pending_data = True
    assert bt.close_waiting_windows_bluetooth_receiver(42)["reason"] == "receiver_retained_for_pending_data"
    assert not window.closed
    window.pending_data = False
    assert bt.close_waiting_windows_bluetooth_receiver(42)["reason"] == "idle_receiver_closed"
    assert window.closed


def test_failed_transfer_finish_is_not_success(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_FinishWindow):
        def descendants(self):
            return [_TextNode('Bluetooth file transfer not completed'), self.button]
    window = Window()
    captures = []
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', window)
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0, capture_window=lambda w, stage: captures.append(stage))
    assert result['ok'] is False and result['reason'] == 'receive_failed'
    assert not window.button.clicked
    assert captures == ['error']


def test_repeated_receiver_ui_errors_stop_before_receive_timeout(monkeypatch):
    from aura import windows_bluetooth as bt
    calls = []
    clock = [0.0]
    class Window:
        def descendants(self):
            calls.append(1)
            clock[0] += 1
            raise RuntimeError('Catastrophic failure')
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(bt.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(_ConnectedApplication, 'window_value', Window())
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=10, poll_interval=0)
    assert result['reason'] == 'receiver_ui_error'
    assert 'Catastrophic failure' in result['detail']
    assert len(calls) == 3


def test_closed_owned_receiver_does_not_use_another_window(monkeypatch):
    from aura import windows_bluetooth as bt
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication,
        findwindows=SimpleNamespace(find_elements=lambda **kwargs: [] if 'handle' in kwargs else [SimpleNamespace(handle=99)])))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0, receiver_handle=42)
    assert result['ok'] is False and result['reason'] == 'receiver_window_closed'


def test_failed_preparation_returns_owned_handle_and_capture(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_WaitingWindow):
        def descendants(self): return [_TextNode('Bluetooth file transfer not completed')]
    window = Window()
    captures = []
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_Application, 'window_value', window)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_Application))
    result = bt.prepare_windows_bluetooth_receiver(timeout_sec=0, capture_window=lambda w, stage: captures.append(stage))
    assert result['ok'] is False and result['reason'] == 'receive_failed'
    assert result['receiver_handle'] == 42 and captures == ['error']


def test_cleanup_closes_failed_receiver_but_preserves_pending_file(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_WaitingWindow):
        closed = False
        def descendants(self): return [_TextNode('Bluetooth file transfer not completed')]
        def close(self): self.closed = True
    window = Window()
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', window)
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication,
        findwindows=SimpleNamespace(find_elements=lambda **kwargs: [SimpleNamespace(handle=42)])))
    result = bt.close_waiting_windows_bluetooth_receiver(42)
    assert window.closed and result['reason'] == 'failed_receiver_closed'


def test_unknown_finish_screen_is_not_success(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_FinishWindow):
        def descendants(self):
            return [_TextNode('An unexpected transport problem occurred'), self.button]
    window = Window()
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', window)
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0)
    assert result['reason'] == 'receive_result_unrecognized'
    assert not window.button.clicked


def test_transient_ui_error_recovers_and_error_filename_is_not_failure(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_FinishWindow):
        calls = 0
        def descendants(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('Temporary UI failure')
            item = _TextNode('Catastrophic failure.zip')
            item.element_info = SimpleNamespace(control_type='ListItem')
            return [item, self.button]
    window = Window()
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', window)
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0.1, poll_interval=0)
    assert result['ok'] is True and window.calls == 2
    assert result['file_names'] == ['Catastrophic failure.zip']


def test_disabled_finish_during_progress_waits_for_save_screen(monkeypatch):
    from aura import windows_bluetooth as bt
    class Button(_FinishButton):
        def is_enabled(self): return window.calls > 1
    class Window(_FinishWindow):
        calls = 0
        def descendants(self):
            self.calls += 1
            return [_TextNode('Receiving files'), self.button] if self.calls == 1 else super().descendants()
    window = Window()
    window.button = Button()
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', window)
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not window.button.clicked)
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication, findwindows=_FindWindows))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=0.1, poll_interval=0)
    assert result['ok'] is True and window.calls == 2


def test_cleanup_does_not_claim_closure_when_window_remains(monkeypatch):
    from aura import windows_bluetooth as bt
    class Window(_WaitingWindow):
        def close(self): pass
    clock = iter(range(100))
    monkeypatch.setattr(bt.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(bt.time, 'sleep', lambda _: None)
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(_ConnectedApplication, 'window_value', Window())
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=_ConnectedApplication,
        findwindows=SimpleNamespace(find_elements=lambda **kwargs: [SimpleNamespace(handle=42)])))
    result = bt.close_waiting_windows_bluetooth_receiver(42)
    assert result['ok'] is False and result['reason'] == 'receiver_close_unconfirmed'
