#!/usr/bin/env python3
"""Run AURA sequentially on one device; preserve every repetition, including partial results."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import signal
import subprocess
import sys
from datetime import datetime, timezone
from zipfile import ZipFile

from aura.cli import APP_PACKAGES, _app_id, _connected_device
from aura.configuration import load_configuration
from aura.session import SessionStore


def now():
    return datetime.now(timezone.utc).isoformat()


def completion_problem(session, root):
    if session.status not in {'complete', 'partial'} or session.interruption or session.error:
        return f'Session did not finish normally: {session.status}; {session.interruption or session.error}'
    if not session.executions or any(r.get('status') not in {'complete', 'partial'} for r in session.executions):
        return 'An execution failed or did not finish'
    if any(r.get('status') not in {'complete', 'partial', 'excluded'} for r in session.execution_plan):
        return 'The execution plan is unfinished'
    final = session.final_state or {}
    if final.get('status') != 'observed':
        return 'Final device state was not observed'
    for result, keys in ((session.wifi_restoration, ('wifi_enabled',)),
                         (session.notification_restoration, ('dnd_enabled', 'hide_all'))):
        target = {key: getattr(session.initial, key) for key in keys}
        if (not result or result.get('status') != 'restored' or result.get('target') != target
                or result.get('observed') != target
                or any(final.get('observed', {}).get(key) != value for key, value in target.items())):
            return 'Initial Wi-Fi/notification settings were not restored and confirmed'
    for run in session.executions:
        if run['status'] != 'partial' or (run.get('app_id'), run.get('method')) != ('whatsapp', 'export'):
            continue
        # Read the full attempts, not the truncated console preview. A recovered
        # earlier transfer failure must not invalidate the successful final attempt.
        run_id = run['run_id']
        SessionStore._validate_id(run_id)
        path = root / 'runs' / run_id / 'acquisition.json'
        if path.is_file():
            acquisition = json.loads(path.read_text(encoding='utf-8'))
        else:
            with ZipFile(root / 'runs' / f'{run_id}.zip') as archive:
                acquisition = json.loads(archive.read('acquisition.json'))
        latest = {a['acquisition_item_id']: a for a in acquisition['attempts']}
        for attempt in latest.values():
            reason = attempt.get('reason') or ''
            if reason.startswith(('whatsapp_bluetooth_host_', 'whatsapp_export_receipt_',
                                  'whatsapp_export_source_', 'whatsapp_retry_',
                                  'whatsapp_export_zip_', 'whatsapp_export_transcript_',
                                  'whatsapp_chat_collection_exception')):
                return f'Windows Export requires attention: {reason}'
    return None


def run_repetitions(root, apps, config_path, *, count=3, serial=None):
    root, config_path = Path(root).resolve(), Path(config_path).resolve()
    apps = tuple(_app_id(app) for app in apps)
    if type(count) is not int or count < 1 or not apps or len(set(apps)) != len(apps) or any(app not in APP_PACKAGES for app in apps):
        raise ValueError('Choose a positive count and distinct supported applications')
    store = SessionStore(root / 'runs')
    if store.active_path.exists():
        raise RuntimeError('An active device session already exists; wait for it to finish')
    configuration = load_configuration(config_path)
    config_bytes = config_path.read_bytes()
    config_hash = hashlib.sha256(config_bytes).hexdigest()
    serial, _ = _connected_device(automation=False, expected_serial=serial)
    batch = root / 'experiment-runs' / ('repetitions-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
    batch.mkdir(parents=True, exist_ok=False)
    frozen = batch / 'session-config.json'
    frozen.write_bytes(config_bytes)
    manifest = batch / 'repetitions.json'
    report = dict(schema_version=1, status='running', serial=serial, selected_apps=list(apps),
                  requested_repetitions=count, started_at=now(), config_source=str(config_path),
                  config_snapshot=str(frozen), config_sha256=config_hash, repetitions=[])
    cancelled = False

    def persist():
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(manifest)
        (batch / 'sessions.txt').write_text(''.join(r['session_record'] + '\n' for r in report['repetitions']
                                                 if r.get('session_record')), encoding='utf-8')

    def interrupt(signum, frame):
        nonlocal cancelled
        cancelled = True
        print('AURA: Repetitions cancelled. Waiting for the current acquisition to finish cleanup.', flush=True)

    print(f'AURA: Repetition records: {manifest}', flush=True)
    persist()
    previous_handler = signal.signal(signal.SIGINT, interrupt)
    try:
        for number in range(1, count + 1):
            if cancelled:
                break
            if store.active_path.exists():
                raise RuntimeError('An active session remains; no further acquisition was started')
            if hashlib.sha256(frozen.read_bytes()).hexdigest() != config_hash:
                raise RuntimeError('The frozen session configuration changed')
            current_serial, _ = _connected_device(automation=False, expected_serial=serial)
            if current_serial != serial:
                raise RuntimeError(f'Connected device changed: {serial} -> {current_serial}')
            before = set(store.archive_root.glob('*/session.json'))
            row = dict(repetition=number, status='running', started_at=now())
            report['repetitions'].append(row)
            persist()
            if cancelled:
                row['status'] = 'interrupted'
                break
            print(f'AURA: Starting repetition {number}/{count} on {serial}.', flush=True)
            # Inherit the console/process group so Ctrl+C reaches AURA as well.
            # Do not use subprocess.run: its KeyboardInterrupt cleanup may kill
            # the child before AURA finishes restoring settings and writing records.
            process = subprocess.Popen([sys.executable, '-m', 'aura', 'acquire', *apps,
                                        '--serial', serial, '--config', str(frozen)], cwd=root)
            exit_code = process.wait()
            row.update(exit_code=exit_code, completed_at=now())
            paths = set(store.archive_root.glob('*/session.json')) - before
            if len(paths) != 1:
                raise RuntimeError(f'Expected one newly finalized session, found {len(paths)}; preserve runs for inspection')
            path = paths.pop()
            row['session_record'] = str(path)
            data = path.read_bytes()
            checksum = path.with_name('session.sha256').read_text(encoding='utf-8').split()
            if not checksum or hashlib.sha256(data).hexdigest() != checksum[0]:
                raise RuntimeError('Session checksum mismatch')
            session = SessionStore._parse(json.loads(data))
            row.update(session_id=session.session_id, session_status=session.status, serial=session.serial,
                       executions=[{key: execution[key] for key in ('run_id','app_id','method','acquisition_environment',
                                    'status','elapsed_seconds','reason') if key in execution} for execution in session.executions])
            if session.serial != serial or tuple(session.selected_apps) != apps:
                raise RuntimeError('Finalized session does not match the requested device/applications')
            recorded = session.configuration or session.authorization
            if not recorded or recorded.get('document_sha256') != configuration['document_sha256']:
                raise RuntimeError('Finalized session used a different configuration')
            problem = completion_problem(session, root)
            if exit_code not in {0, 1} or problem or store.active_path.exists():
                raise RuntimeError(problem or f'Acquisition exit code {exit_code}, or an active session remains')
            row['status'] = 'finished'
            persist()
            print(f'AURA: Repetition {number}/{count} finished: {session.status}.', flush=True)
        report['status'] = 'interrupted' if cancelled else 'completed'
    except (Exception, KeyboardInterrupt) as error:
        report.update(status='interrupted' if cancelled or isinstance(error, KeyboardInterrupt) else 'stopped',
                      reason=str(error) or type(error).__name__)
        if report['repetitions'] and report['repetitions'][-1]['status'] == 'running':
            report['repetitions'][-1].update(status=report['status'], reason=report['reason'], completed_at=now())
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        report['completed_at'] = now()
        persist()
    return manifest, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apps', nargs='+', type=_app_id, choices=tuple(APP_PACKAGES))
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--count', type=int, default=3, help='new sequential repetitions (default: 3)')
    parser.add_argument('--serial', help='target ADB serial; required when multiple devices are connected')
    args = parser.parse_args(argv)
    try:
        path, report = run_repetitions(Path.cwd(), tuple(args.apps), args.config, count=args.count, serial=args.serial)
    except (Exception, KeyboardInterrupt) as error:
        print(f'AURA: Repetitions could not start: {error}', file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 2
    print(json.dumps({'status': report['status'], 'manifest': str(path), 'reason': report.get('reason'),
                      'sessions': [r.get('session_record') for r in report['repetitions']]}, ensure_ascii=False, indent=2))
    if report['status'] == 'interrupted': return 130
    if report['status'] != 'completed': return 2
    return 1 if any(r['session_status'] == 'partial' for r in report['repetitions']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
