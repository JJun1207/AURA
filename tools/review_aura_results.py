#!/usr/bin/env python3
"""Batch structural provenance review; automatic timings are not examiner timings."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path
from time import perf_counter

from validate_aura_results import build_trace, validate_session


def _measure(session, run, kind, identifier, observations_by_attempt):
    started = perf_counter()
    indexes = run['indexes']
    profile_targets = {target['target_id']: target for target in (run.get('profile') or {}).get('targets', [])}
    edges = set()
    association_lookups = 0

    def edge(source, field, destination):
        if source and destination:
            edges.add((source, field, destination))

    row = {'session_id': session['device_session_id'], 'run_id': run['run']['run_id'],
           'device_serial': session.get('serial'), 'installed_app_version': run['run'].get('installed_app_version'),
           'app_id': run['run'].get('app_id'), 'device_model': run['run'].get('device_model'),
           'method': run['execution'].get('method'), 'acquisition_environment': run['execution'].get('acquisition_environment'),
           'kind': kind, 'id': identifier, 'artifact_kind': None, 'procedure_status': None,
           'acquisition_status': None, 'identification_status': None, 'reason': None,
           'screen_evidence': 'not_applicable', 'full_chain_complete': None,
           'associated_item_count': 0, 'examiner_seconds': None,
           'review_scope': 'schema2_structural_review' if session.get('schema_version') == 2 else 'legacy_limited'}
    if kind == 'identification':
        record = next(r for r in run['identifications'] if r['target_id'] == identifier)
        row.update(identification_status=record['identification_status'], reason=record.get('reason'), target_id=identifier,
                   identified_item_count=record.get('identified_item_count'))
        row['required_links_complete'] = identifier in profile_targets
        if row['required_links_complete']:
            edge('identification:' + identifier, 'target_id', 'target:' + identifier)
    else:
        trace = None
        if kind in ('artifact', 'outcome'):
            errors = []
            trace = build_trace(session, [run], identifier, run['run']['run_id'], errors)
            if errors:
                raise ValueError('; '.join(errors))
            record = trace[kind]
            attempt, item, action = trace['attempt'], trace['item'], trace['action']
            row['associated_item_count'] = len(trace.get('associated_items', []))
            row['artifact_kind'] = record.get('kind')
            edge(identifier, 'attempt_id', record.get('attempt_id'))
            edge(identifier, 'action_id', record.get('action_id'))
        else:
            record = attempt = indexes['attempts'][identifier]
            item = indexes['items'].get(attempt.get('acquisition_item_id'))
            action = None
        attempt = attempt or {}
        item = item or {}
        target = item.get('target_id')
        row.update(target_id=target, procedure_status=attempt.get('procedure_status'),
                   acquisition_status=attempt.get('acquisition_status'), reason=attempt.get('reason'))
        edge(attempt.get('attempt_id'), 'acquisition_item_id', item.get('acquisition_item_id'))
        if target in profile_targets:
            edge(item.get('acquisition_item_id'), 'target_id', 'target:' + target)
        row['required_links_complete'] = bool(attempt and item and target in profile_targets and (action or kind == 'attempt'))
        current = action
        seen = set()
        while current and current['action_id'] not in seen:
            seen.add(current['action_id'])
            source = current.get('context', {}).get('source_action_id')
            edge(current['action_id'], 'context.source_action_id', source)
            current = indexes['actions'].get(source)
        if kind in ('artifact', 'outcome'):
            direct = indexes['observations'].get(record.get('observation_id'))
            if direct:
                observations = [direct]
                row['screen_evidence'] = 'direct'
                edge(identifier, 'observation_id', direct['observation_id'])
            else:
                association_lookups = 1
                observations = observations_by_attempt.get(attempt.get('attempt_id'), [])
                row['screen_evidence'] = 'same_attempt' if observations else 'absent'
            evidence_present = False
            for observation in observations:
                oid = observation['observation_id']
                for field in ('attempt_id', 'action_id', 'screen_artifact_id', 'hierarchy_artifact_id'):
                    edge(oid, field, observation.get(field))
                screen = indexes['artifacts'].get(observation.get('screen_artifact_id'), {})
                hierarchy = indexes['artifacts'].get(observation.get('hierarchy_artifact_id'), {})
                evidence_present |= screen.get('kind') == 'screen_image' and hierarchy.get('kind') == 'ui_hierarchy'
            row['full_chain_complete'] = row['required_links_complete'] and evidence_present if session.get('schema_version') == 2 else None
    row['reference_hops'] = len(edges)
    row['reference_edges'] = [list(edge) for edge in sorted(edges)]
    row['association_lookups'] = association_lookups
    row['processing_seconds'] = perf_counter() - started
    return row


def review_sessions(paths, samples=None):
    errors, rows, sessions = [], [], []
    selected = None
    if samples is not None:
        if not isinstance(samples, list) or not all(isinstance(s, dict) and set(s) == {'session', 'run_id', 'kind', 'id'}
                                                  and all(isinstance(v, str) and v for v in s.values()) for s in samples):
            return {'valid': False, 'errors': ['sample list requires session/run_id/kind/id strings'], 'rows': []}
        selected = {(str(Path(s['session']).resolve()), s['run_id'], s['kind'], s['id']) for s in samples}
        if len(selected) != len(samples):
            errors.append('duplicate sample selection')
    matched = set()
    normalized_paths = [Path(p).resolve() for p in paths]
    if len(set(normalized_paths)) != len(normalized_paths):
        errors.append('duplicate session path')
    for path in dict.fromkeys(normalized_paths):
        started = perf_counter()
        session, runs, problems = validate_session(path)
        sessions.append({'session': str(path), 'validation_seconds': perf_counter() - started, 'valid': not problems})
        if problems:
            errors.extend(f'{path}: {problem}' for problem in problems)
            continue
        for run in runs:
            by_attempt = defaultdict(list)
            for observation in run['indexes']['observations'].values():
                by_attempt[observation['attempt_id']].append(observation)
            candidates = [(kind, key) for kind, collection in (('artifact', 'artifacts'), ('outcome', 'outcomes')) for key in run['indexes'][collection]]
            candidates += [('identification', row['target_id']) for row in run.get('identifications', [])]
            candidates += [('attempt', key) for key, row in run['indexes']['attempts'].items() if row.get('procedure_status') == 'not_attempted']
            for kind, identifier in candidates:
                key = (str(path), run['run']['run_id'], kind, identifier)
                if selected is not None and key not in selected:
                    continue
                matched.add(key)
                row = _measure(session, run, kind, identifier, by_attempt)
                rows.append({'session_path': str(path), **row})
        executed = {run['run']['run_id'] for run in runs}
        for entry in session.get('execution_plan', []):
            if entry['run_id'] in executed:
                continue
            key = (str(path), entry['run_id'], 'execution', entry['run_id'])
            if selected is not None and key not in selected:
                continue
            matched.add(key)
            rows.append({'session_path': str(path), 'session_id': session['device_session_id'], 'run_id': entry['run_id'],
                         'app_id': entry.get('app_id'), 'method': entry.get('method'), 'acquisition_environment': entry.get('acquisition_environment'),
                         'kind': 'execution', 'id': entry['run_id'], 'procedure_status': 'not_attempted',
                         'reason': entry.get('exclusion_reason') or (session.get('interruption') or {}).get('reason') or 'planned_execution_not_started',
                         'required_links_complete': None, 'full_chain_complete': None, 'screen_evidence': 'not_applicable',
                         'reference_hops': 0, 'reference_edges': [], 'association_lookups': 0, 'processing_seconds': None,
                         'examiner_seconds': None, 'review_scope': 'execution_plan_only'})
    if selected is not None:
        errors.extend('sample not found: ' + repr(key) for key in sorted(selected - matched))
    return {'valid': not errors, 'errors': errors, 'sessions': sessions, 'row_count': len(rows),
            'counts_by_kind': dict(Counter(row['kind'] for row in rows)),
            'full_chain_evaluable_count': sum(row['full_chain_complete'] is not None for row in rows),
            'full_chain_complete_count': sum(row['full_chain_complete'] is True for row in rows),
            'definitions': {'reference_hops': 'Unique directed record-field references inspected; explicit edges included. Same-attempt joins counted separately.',
                            'processing_seconds': 'Automatic in-memory tracing after package validation; not examiner interpretation time. Null for unexecuted plans.',
                            'full_chain_complete': 'Schema-2 artifact/outcome has item, attempt, profile target, retention/result action and screen+hierarchy evidence. Null when not applicable or legacy.',
                            'limits': 'Structural resolution does not prove original content, legal authority or an independent examiner result.'},
            'rows': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sessions', nargs='+', type=Path)
    parser.add_argument('--samples', type=Path, help='JSON list of session/run_id/kind/id; session paths relative to sample file')
    parser.add_argument('--json', type=Path)
    parser.add_argument('--csv', type=Path)
    args = parser.parse_args()
    samples = None
    if args.samples:
        try:
            samples = json.loads(args.samples.read_text())
            if isinstance(samples, list):
                for sample in samples:
                    if isinstance(sample, dict) and isinstance(sample.get('session'), str):
                        sample['session'] = str((args.samples.parent / sample['session']).resolve())
        except (OSError, ValueError) as error:
            parser.error(str(error))
    result = review_sessions(args.sessions, samples)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    if args.csv:
        fields = sorted({key for row in result['rows'] for key in row})
        with args.csv.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value for key, value in row.items()} for row in result['rows'])
    print(json.dumps({key: value for key, value in result.items() if key != 'rows'}, ensure_ascii=False, indent=2))
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
