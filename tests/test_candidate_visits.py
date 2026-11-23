import pytest
from aura import collection


def test_pending_candidates_read_live_processed_state_without_deduplicating_names():
    rows = [{'slot': 0, 'name': 'Same'}, {'slot': 1, 'name': 'Same'}, {'slot': 2, 'name': 'Other'}]
    processed = set()
    pending = collection.pending_candidates(rows, processed, key=lambda r: r['slot'])
    assert next(pending) == rows[0]
    processed.add(0)
    processed.add(2)
    assert list(pending) == [rows[1]]


def test_matching_candidates_preserve_ambiguity_for_the_caller():
    rows = [{'id': 'same', 'y': 5}, {'id': 'other', 'y': 10}, {'id': 'same', 'y': 15}]
    assert list(collection.matching_candidates(rows, {'id': 'same'}, key=lambda r:r['id'])) == [rows[0], rows[2]]
    assert list(collection.matching_candidates(rows, {'id': 'missing'}, key=lambda r:r['id'])) == []


def test_ordered_candidate_uses_new_coordinates_and_checks_entire_window():
    old = [{'name': 'Same', 'y': 0}, {'name': 'Same', 'y': 50}, {'name': 'Other', 'y': 100}]
    now = [{'name': 'Same', 'y': 10}, {'name': 'Other', 'y': 60}]
    key = lambda row: row['name']
    assert collection.ordered_candidate(old, now, 1, key=key, allow_removed_prefix=True)['y'] == 10
    with pytest.raises(ValueError):
        collection.ordered_candidate(old, now, 1, key=key)
    with pytest.raises(ValueError):
        collection.ordered_candidate(old, list(reversed(now)), 1, key=key, allow_removed_prefix=True)
    with pytest.raises(ValueError):
        collection.ordered_candidate(old, now, 0, key=key, allow_removed_prefix=True)


@pytest.mark.parametrize('always', [False, True])
@pytest.mark.parametrize('failure', [None, ValueError, KeyboardInterrupt])
def test_branch_scope_preserves_success_and_error_restoration_policy(always, failure):
    calls = []
    def level(node):
        if node == 'child':
            calls.append('child')
            if failure:
                raise failure('stop')
            return 42
        with collection.visit_scope(lambda: calls.append('enter'), lambda: calls.append('return'),
                                    restore_on_error=always):
            result = yield 'child'
            calls.append(result)
        return 'parent'
    if failure:
        with pytest.raises(failure, match='stop'):
            collection.walk_hierarchy('root', level)
        assert calls == ['enter', 'child'] + (['return'] if always else [])
    else:
        assert collection.walk_hierarchy('root', level) == 'parent'
        assert calls == ['enter', 'child', 42, 'return']


def test_branch_failed_entry_never_runs_restore():
    calls = []
    def enter():
        raise ValueError('entry')
    with pytest.raises(ValueError, match='entry'):
        with collection.visit_scope(enter, lambda: calls.append('return'), restore_on_error=True):
            pytest.fail('entered after failure')
    assert calls == []
