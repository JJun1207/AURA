"""Default-folder receive: never touch Edit controls or overwrite previous files."""
import sys
from types import SimpleNamespace
import pytest
from aura import windows_bluetooth as bt

@pytest.mark.parametrize('case', ['saved', 'collision', 'pending', 'save_error', 'no_names'])
def test_default_folder_finish_with_three_unreadable_edits(tmp_path, monkeypatch, case):
    destination = tmp_path / 'Downloads'
    destination.mkdir()
    old = destination / 'chat.zip'
    if case == 'collision': old.write_bytes(b'previous export')
    state = {'clicked': 0, 'closed': False, 'captures': []}
    class Node:
        def __init__(self, kind, label):
            self.element_info = SimpleNamespace(control_type=kind)
            self.label = label
        def window_text(self):
            assert self.element_info.control_type != 'Edit', 'Do not query unrelated Edit controls'
            return self.label
        def is_enabled(self): return True
        def set_edit_text(self, value): pytest.fail('Default folder must not change')
        def click_input(self):
            state['clicked'] += 1
            if case == 'saved':
                with old.open('xb') as f: f.write(b'new export')
                state['closed'] = True
    class Window:
        def descendants(self):
            if case == 'save_error' and state['clicked']:
                return [Node('Text', 'Bluetooth file transfer not completed')]
            return [Node('Text', 'Save the received file'), *([] if case == 'no_names' else [Node('ListItem', 'chat.zip')]),
                    *[Node('Edit', '') for _ in range(3)], Node('Button', 'Finish')]
    class App:
        def __init__(self, **kw): pass
        def connect(self, **kw): return self
        def window(self, **kw): return Window()
    monkeypatch.setattr(bt, '_IS_WINDOWS', True)
    monkeypatch.setattr(bt.time, 'sleep', lambda _: None)
    ticks = iter(i / 10 for i in range(100))
    monkeypatch.setattr(bt.time, 'monotonic', lambda: next(ticks))
    monkeypatch.setattr(bt, '_receiver_handle_exists', lambda handle: not state['closed'])
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(Application=App,
        findwindows=SimpleNamespace(find_elements=lambda **kw: [SimpleNamespace(handle=42)])))
    result = bt.finish_windows_bluetooth_receive(timeout_sec=1, receiver_handle=42,
        existing_names=['chat.zip'] if case == 'collision' else [],
        capture_window=lambda w, stage: state['captures'].append(stage))
    if case == 'saved':
        assert result['ok'] and result['reason'] == 'receiver_save_completed'
        assert old.read_bytes() == b'new export' and state['clicked'] == 1
        assert state['captures'] == ['finish']
    else:
        assert not result['ok']
        assert result['reason'] == {'collision':'receive_filename_conflict', 'pending':'receiver_save_unconfirmed',
            'save_error':'receive_failed', 'no_names':'receive_file_names_unavailable'}[case]
        assert state['clicked'] == (1 if case in {'pending','save_error'} else 0)
        if case == 'collision': assert old.read_bytes() == b'previous export'
        else: assert not old.exists()

@pytest.mark.parametrize('case', ['saved', 'access_time', 'old_source', 'source_changed', 'copy_changed', 'remove_error'])
def test_move_verifies_preserved_bytes_before_removing_new_source(tmp_path, monkeypatch, case):
    import os
    from aura.artifacts import ArtifactStore
    from aura.journal import EventJournal
    from aura.apps.whatsapp.collector import _move_preserved_receipt, WhatsAppCollectorError
    source = tmp_path / 'received.zip'
    source.write_bytes(b'validated receipt bytes')
    before = source.stat()
    journal = EventJournal(tmp_path / 'run/events.jsonl')
    store = ArtifactStore(tmp_path / 'run', journal)
    action = journal.record_action('receive', status='success', attempt_id='attempt-1')
    artifact = store.retain_file(source, 'export/received.zip', kind='app_export', attempt_id='attempt-1', action=action)
    destination = store.run_dir / artifact.relative_path
    if case == 'access_time': os.utime(source, ns=(before.st_atime_ns + 1000000000, before.st_mtime_ns))
    if case == 'source_changed': source.write_bytes(b'new different source')
    if case == 'copy_changed': destination.write_bytes(b'damaged copy')
    if case == 'remove_error':
        from pathlib import Path
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == source: raise PermissionError('File in use')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', unlink)
    context = SimpleNamespace(run_dir=store.run_dir, journal=journal)
    baseline = {source: (before.st_size, before.st_mtime_ns)} if case == 'old_source' else {}
    if case in {'saved','access_time'}:
        _move_preserved_receipt(context, source, artifact, baseline, before, 'attempt-1', {})
        assert not source.exists() and destination.read_bytes() == b'validated receipt bytes'
    else:
        with pytest.raises(WhatsAppCollectorError, match='.+'):
            _move_preserved_receipt(context, source, artifact, baseline, before, 'attempt-1', {})
        assert source.exists() and destination.exists()
