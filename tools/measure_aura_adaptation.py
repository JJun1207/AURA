"""Compare source snapshots; reuse/LOC are descriptive, never estimates of hours."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path


def _files(root, pattern):
    return {str(path.relative_to(root)): path.read_text(encoding='utf-8')
            for path in root.glob(pattern) if path.is_file()}


def _functions(text):
    result = {}
    def visit(node, prefix=''):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                if not isinstance(child, ast.ClassDef):
                    result[name] = ast.dump(child, include_attributes=False)
                visit(child, name + '.')
            else:
                visit(child, prefix)
    visit(ast.parse(text))
    return result


def _shared_calls(text, module='collection'):
    tree = ast.parse(text)
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (module, f'aura.{module}'):
            names.update({item.asname or item.name: item.name for item in node.names})
    return Counter(names[node.func.id] for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id in names)


def _leaves(value, prefix=''):
    if isinstance(value, dict) and value:
        return {path: leaf for key, child in value.items()
                for path, leaf in _leaves(child, f'{prefix}.{key}' if prefix else key).items()}
    if isinstance(value, list) and value:
        return {path: leaf for i, child in enumerate(value)
                for path, leaf in _leaves(child, f'{prefix}[{i}]').items()}
    return {prefix: value}


def _changes(before, after):
    return {'added': sorted(after.keys() - before.keys()),
            'removed': sorted(before.keys() - after.keys()),
            'changed': sorted(key for key in before.keys() & after.keys() if before[key] != after[key])}


def _code_changes(before, after):
    functions = {key: [] for key in ('added', 'removed', 'changed')}
    lines = {'added': 0, 'removed': 0}
    files = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path, ''), after.get(path, '')
        if old == new:
            continue
        files.append(path)
        for operation, a, b, c, d in difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False).get_opcodes():
            if operation in ('replace', 'delete'):
                lines['removed'] += b-a
            if operation in ('replace', 'insert'):
                lines['added'] += d-c
        for kind, names in _changes(_functions(old), _functions(new)).items():
            functions[kind].extend(f'{path}:{name}' for name in names)
    return {'files': files, 'code_lines': lines, 'functions': functions}


def measure(before: Path, after: Path):
    old_code, new_code = (_files(root, 'src/aura/**/*.py') for root in (before, after))
    old_profiles, new_profiles = (_files(root, 'profiles/apps/*/*.json') for root in (before, after))
    apps = sorted({Path(path).parts[3] for path in old_code.keys() | new_code.keys()
                   if path.startswith('src/aura/apps/') and len(Path(path).parts) > 4})
    result = {'schema_version': 1,
              'scope': 'static_source_comparison_not_runtime_coverage_or_time_estimate',
              'line_count_scope': 'physical_source_lines_including_comments_and_formatting',
              'function_count_scope': 'AST_definitions_including_nested_functions_not_independent_effort_units',
              'before': str(before.resolve()), 'after': str(after.resolve()), 'effort': None, 'apps': {}}
    result['source_hashes'] = {
        label: {p: hashlib.sha256((root/p).read_bytes()).hexdigest() for p in sorted({**code, **profiles})}
        for label, root, code, profiles in [('before', before, old_code, old_profiles), ('after', after, new_code, new_profiles)]}
    for app in apps:
        prefix = f'src/aura/apps/{app}/'
        old, new = ({p: t for p, t in values.items() if p.startswith(prefix)} for values in (old_code, new_code))
        entry = _code_changes(old, new)
        calls = Counter()
        for text in new.values():
            calls.update(_shared_calls(text))
        entry['shared_collection_calls'] = dict(sorted(calls.items()))
        parsing_calls = Counter()
        for text in new.values():
            parsing_calls.update(_shared_calls(text, 'parsing'))
        entry['shared_parsing_calls'] = dict(sorted(parsing_calls.items()))
        entry['profiles'] = []
        for path in sorted(old_profiles.keys() | new_profiles.keys()):
            if path.startswith(f'profiles/apps/{app}/') and old_profiles.get(path) != new_profiles.get(path):
                old = _leaves(json.loads(old_profiles[path])) if path in old_profiles else {}
                new = _leaves(json.loads(new_profiles[path])) if path in new_profiles else {}
                entry['profiles'].append({'path': path, **_changes(old, new)})
        result['apps'][app] = entry
    # Registration is shared infrastructure, not an eighth app.
    result['shared_code'] = _code_changes(
        {p: t for p, t in old_code.items() if len(Path(p).parts) <= 4},
        {p: t for p, t in new_code.items() if len(Path(p).parts) <= 4})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', type=Path, required=True)
    parser.add_argument('--after', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--effort', type=Path, help='Optional independently recorded effort JSON; copied verbatim without estimates')
    args = parser.parse_args()
    for root in (args.before, args.after):
        if not (root/'src/aura').is_dir() or not (root/'profiles/apps').is_dir():
            parser.error(f'not an AURA snapshot: {root}')
    result = measure(args.before, args.after)
    if args.effort:
        result['effort'] = {'source': str(args.effort.resolve()), 'recorded': json.loads(args.effort.read_text(encoding='utf-8'))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
