"""Collection refactoring must retain evidence, duplicates and failure outcomes."""
import json
from pathlib import Path

import pytest

from aura import collection
from aura.artifacts import ArtifactStore
from aura.journal import EventJournal
from aura.models import AcquisitionStatus
from aura.profiles import ProfileStore
from aura.runtime import RunContext
from fakes import FakeDevice


@pytest.mark.parametrize('failure', [None, 'overlap', 'error', 'interrupt'])
def test_shared_finalization_preserves_records_and_linked_outcome(tmp_path, failure):
    store = ProfileStore(Path(__file__).parents[1] / 'profiles')
    journal = EventJournal(tmp_path / 'events.jsonl')
    context = RunContext(run_dir=tmp_path, base_context={}, device=FakeDevice(),
                         journal=journal, artifacts=ArtifactStore(tmp_path, journal),
                         app_profile=store.load_app('chrome', '156.0.8078.79'),
                         system_ui_profile=store.load_system_ui('samsung'), ui=None)
    context.begin_identification('chrome.downloads')
    attempt = context.item_attempt('chrome:downloads', 'chrome.downloads', 'download_collection',
                                   source={'surface': 'downloads'})
    source = journal.record_action('open_downloads', status='success')
    action, screen = collection.retain_ui_observation(
        context, attempt, 'chrome/downloads/window-0001', '<hierarchy/>',
        name='observe_chrome_ui', source_action=source, linked={'surface': 'downloads', 'window': 1})
    records = [{'title': 'same'}, {'title': 'same'}]
    error = RuntimeError('original failure') if failure == 'error' else KeyboardInterrupt() if failure == 'interrupt' else None
    metadata = {'unconfirmed_overlaps': [{'reason': 'overlap_unconfirmed'}]} if failure == 'overlap' else {}

    def finish():
        return collection.finish_record_collection(
            context, attempt, 'chrome.downloads', records, action,
            surface='downloads', prefix='chrome/downloads', record_kind='chrome_downloads',
            retain_name='retain_chrome_records', completion='repeated_hierarchy',
            error=error, metadata=metadata)

    if error is not None:
        with pytest.raises(type(error)) as caught:
            finish()
        assert caught.value is error
    else:
        count, _ = finish()
        assert count == 2
    saved = json.loads((tmp_path / 'artifacts/chrome/downloads.json').read_text())
    assert saved == {'record_kind': 'chrome_downloads', 'record_count': 2, 'records': records, **metadata}
    assert (tmp_path / 'artifacts/chrome/downloads/window-0001.xml').read_text() == '<hierarchy/>'
    assert context.observations[0].observation_id == screen.observation_id
    assert context.attempts[0].acquisition_status == (AcquisitionStatus.PARTIAL if failure else AcquisitionStatus.ACQUIRED)
    identified = next(item for item in context.identifications if item.target_id == 'chrome.downloads')
    assert identified.traversal_complete is (error is None)
    assert context.attempts[0].reason == ('original failure' if failure == 'error' else 'KeyboardInterrupt' if failure == 'interrupt' else 'overlap_unconfirmed' if failure == 'overlap' else None)
    context.validate_records()
