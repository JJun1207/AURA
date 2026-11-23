import pytest

from aura import ui


@pytest.mark.parametrize('before,target', [(False, True), (True, False), (False, False), (True, True)])
def test_state_change_keeps_read_verify_and_record_order(before, target):
    calls = []

    def read():
        calls.append('read')
        return before

    def verify():
        calls.append('verify')
        return target

    def record(status, **details):
        calls.append((status, details))

    assert ui.apply_state(read, target, lambda: calls.append('change'), verify, record) is target
    assert calls == ['read', *(['change'] if before is not target else []), 'verify',
                     ('success', {'before': before, 'target': target, 'after': target})]


@pytest.mark.parametrize('stage', ['read', 'change', 'verify', 'interrupt'])
def test_state_change_preserves_failure_and_interrupt_boundaries(stage):
    failure = KeyboardInterrupt() if stage == 'interrupt' else RuntimeError(stage)
    records = []

    def read():
        if stage == 'read':
            raise failure
        return False

    def change():
        if stage in {'change', 'interrupt'}:
            raise failure

    def verify():
        raise failure

    with pytest.raises(type(failure)) as raised:
        ui.apply_state(read, True, change, verify, lambda status, **fields: records.append((status, fields)))
    assert raised.value is failure
    assert records == ([] if stage in {'read', 'interrupt'} else [
        ('failed', {'before': False, 'target': True, 'after': None, 'error': failure})])
