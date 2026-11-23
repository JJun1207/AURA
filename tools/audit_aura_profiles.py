#!/usr/bin/env python3
"""Static profile/collector consistency audit; does not operate a device."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import re
import sys
import textwrap
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from aura.cli import COLLECTORS
from aura.profiles import ProfileStore
from aura.authorization import profile_digest


def audit_profiles(profiles_root: Path) -> dict:
    started = perf_counter()
    rows = []
    store = ProfileStore(profiles_root)
    for app_dir in sorted((profiles_root / 'apps').iterdir()):
        if not app_dir.is_dir():
            continue
        app = app_dir.name
        contracts = {}
        if app not in COLLECTORS:
            rows.append({'app_id': app, 'errors': ['collector unavailable']})
            continue
        contract_factory = getattr(inspect.getmodule(COLLECTORS[app]), "identification_contracts", None)
        tree = ast.parse(textwrap.dedent(inspect.getsource(COLLECTORS[app])))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'require_identification_rules' and contract_factory is None:
                contracts.update(ast.literal_eval(node.args[0]))
        sources = [ast.parse(p.read_text()) for p in (ROOT / 'src/aura/apps' / app).glob('*.py')]
        required = set()
        literal_strings = set()
        for source in sources:
            for node in ast.walk(source):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literal_strings.add(node.value)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {'_selector', '_selector_spec'} and len(node.args) > 1:
                    key = node.args[1]
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        required.add(key.value)
        paths = sorted(app_dir.glob('*.json'), key=lambda p: tuple(int(x) for x in re.findall(r'\d+', p.stem)))
        documents = {path: json.loads(path.read_text()) for path in paths}
        known_selectors = {key for doc in documents.values() for key in doc.get('selectors', {})}
        required.update(known_selectors & literal_strings)
        previous = None
        for path, doc in documents.items():
            row = {'app_id': app, 'app_version': path.stem, 'profile_path': str(path),
                   'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'errors': [],
                   'scope': 'static_contracts_rules_and_literal_selector_references_not_live_UI_validation'}
            try:
                profile = store.load_app(app, path.stem)
                row['profile_sha256'] = profile_digest(profile)
                row['execution_scopes'] = [{'method': method.value, 'acquisition_environment': environment,
                                           'target_ids': [t.target_id for t in profile.targets if t.method is method and environment in t.acquisition_environments]}
                                          for method in profile.routes for environment in ('device_only', 'controlled_online')
                                          if any(t.method is method and environment in t.acquisition_environments for t in profile.targets)]
                try:
                    profile.require_identification_rules(contract_factory(profile) if contract_factory else contracts)
                except ValueError as error:
                    row['errors'].append(str(error))
                for target in profile.targets:
                    if target.remote_access_environments is None:
                        row['errors'].append(f'{target.target_id}: remote_access_environments missing')
                    if target.environment_rationale is None:
                        row['errors'].append(f'{target.target_id}: environment_rationale missing')
                row['target_count'] = len(profile.targets)
                row['selector_count'] = len(profile.selectors)
            except (ValueError, OSError) as error:
                row['errors'].append(str(error))
            missing = sorted(required - doc.get('selectors', {}).keys())
            if missing:
                row['errors'].append('missing literal selectors: ' + ', '.join(missing))
            if previous:
                row['previous_version'] = previous['app_version']
                row['changes'] = {section: sorted(key for key in set(previous.get(section, {})) | set(doc.get(section, {}))
                                                 if previous.get(section, {}).get(key) != doc.get(section, {}).get(key))
                                  for section in ('selectors', 'timings', 'parameters')}
                row['changes']['targets'] = doc.get('targets') != previous.get('targets')
                row['changes']['android_versions'] = doc.get('android_versions') != previous.get('android_versions')
            previous = doc
            rows.append(row)
    return {'valid': bool(rows) and not any(row['errors'] for row in rows), 'generated_at': datetime.now(timezone.utc).isoformat(),
            'processing_elapsed_seconds': perf_counter() - started, 'timing_scope': 'static_audit_not_profile_development_time', 'profiles': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, default=ROOT / 'profiles')
    parser.add_argument('--json', type=Path)
    args = parser.parse_args()
    result = audit_profiles(args.profiles)
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.json:
        args.json.write_text(encoded)
    else:
        print(encoded, end='')
    return 0 if result['valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
