#!/usr/bin/env python3
"""Copy selected AURA session metadata into a small, read-only review bundle."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import sys
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED

# Same original-file kinds counted by evaluate_aura_results.py. JSON originals
# remain excluded even though their extension resembles a structured UI record.
ORIGINAL_KINDS = {'file', 'acquired_file', 'downloaded_file', 'saved_file', 'export_file',
                  'original_artifact', 'app_materialized_file', 'app_export'}
CORE = {'acquisition.json', 'events.jsonl', 'files.json'}
README = '''AURA metadata review bundle (not a complete acquisition package)

Original session, metadata and log bytes are copied without editing their paths,
references, statuses or timings. Input order in bundle-manifest.json follows the
operator's session selection; it does not infer a repetition number from dates.

Included: session records/checksums; acquisition.json, events.jsonl, files.json;
nested JSON/JSONL records; available source ZIP checksums; optional references/notes.
Excluded: acquired original files (including JSON originals), images, XML, media,
PDFs and exported ZIP contents. Missing results/checksums are reported as warnings.

Selected ZIP entries are checked against files.json when present. Large original
ZIPs are NOT read in full to recompute their whole-archive SHA-256. Recorded source
ZIP hashes are declarations, not a new verification of omitted originals.

Keep the complete original results. This subset is for result/time comparison;
validate_aura_results.py requires the complete original packages. No source file
is deleted, renamed, rewritten or moved. No device or network is accessed.
'''


def safe_name(name, *, component=False):
    parts = name.split('/') if isinstance(name, str) else []
    if (not parts or any(not p or p in {'.', '..'} for p in parts)
            or any(c in name for c in '\\:*?"<>|') or any(ord(c) < 32 for c in name)
            or PurePosixPath(name).is_absolute()
            or (component and len(parts) != 1)):
        raise ValueError(f'Unsafe bundle path: {name!r}')
    return name


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@contextmanager
def run_members(source):
    if source.is_dir():
        members = {}
        for path in source.rglob('*'):
            if path.is_symlink():
                raise ValueError(f'Symbolic link in run directory: {path}')
            if path.is_file():
                members[path.relative_to(source).as_posix()] = path
        yield list(members), lambda name: members[name].open('rb')
    else:
        with ZipFile(source) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise ValueError(f'Duplicate ZIP member: {source}')
            for info in infos:
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f'Symbolic link in ZIP: {info.filename}')
            yield names, archive.open


def find_run(session_path, execution, runs_dir):
    run_id = safe_name(execution['run_id'], component=True)
    root = runs_dir or session_path.parent.parent.parent
    candidates = []
    if runs_dir is None and execution.get('archive_path'):
        recorded = Path(execution['archive_path'])
        candidates.append(recorded if recorded.is_absolute() else session_path.parent / recorded)
    candidates += [root / f'{run_id}.zip', root / run_id]
    if runs_dir is None and execution.get('acquisition_record'):
        recorded = Path(execution['acquisition_record'])
        if recorded.is_absolute():
            candidates.append(recorded.parent)
    return next((p for p in candidates if p.is_file() or p.is_dir()), None)


def create_bundle(sessions, output, *, runs_dir=None, references=(), notes=None):
    output = Path(output).absolute()
    if output.suffix.lower() != '.zip':
        raise ValueError('Output must have a .zip extension')
    checksum = output.with_suffix(output.suffix + '.sha256')
    if output.exists() or checksum.exists():
        raise FileExistsError(f'Output or checksum already exists: {output}')
    paths = [Path(p).resolve() for p in sessions]
    paths = [p / 'session.json' if p.is_dir() else p for p in paths]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError('Select distinct session.json files in repetition order')
    report = {'schema_version': 1, 'kind': 'aura_metadata_review_subset',
              'created_at': datetime.now(timezone.utc).isoformat(), 'sessions': [], 'files': [], 'warnings': [],
              'whole_source_archive_hashes_recomputed': False}
    seen = set()
    created = checksum_created = False
    try:
        # Exclusive creation also protects against a file appearing after the check.
        with output.open('xb') as stream:
            created = True
            with ZipFile(stream, 'w', compression=ZIP_DEFLATED, compresslevel=6) as bundle:
                def copy(reader, destination, expected=None):
                    safe_name(destination)
                    if destination.casefold() in seen:
                        raise ValueError(f'Duplicate bundle path: {destination}')
                    seen.add(destination.casefold())
                    digest, size = hashlib.sha256(), 0
                    with reader() as source, bundle.open(destination, 'w', force_zip64=True) as target:
                        for block in iter(lambda: source.read(1024 * 1024), b''):
                            target.write(block); digest.update(block); size += len(block)
                    actual = digest.hexdigest()
                    if expected and (expected.get('sha256') != actual or expected.get('size') != size):
                        raise ValueError(f'Metadata checksum/size mismatch: {destination}')
                    report['files'].append({'path': destination, 'size': size, 'sha256': actual})

                def copy_path(path, destination, expected=None):
                    if path.resolve() in {output.resolve(), checksum.resolve()}:
                        raise ValueError('An output file cannot also be an input')
                    copy(lambda: path.open('rb'), destination, expected)

                for number, path in enumerate(paths, 1):
                    raw = path.read_bytes()
                    session = json.loads(raw)
                    if session.get('status') in {'running', 'preparing', 'prepared'}:
                        raise ValueError(f'Session is still active: {path}')
                    sid = safe_name(session['device_session_id'], component=True)
                    prefix = f'sessions/{sid}'
                    print(f'AURA: Reading session {number}/{len(paths)}: {sid}', flush=True)
                    session_hash = path.with_name('session.sha256')
                    if session_hash.is_file():
                        fields = session_hash.read_text(encoding='ascii').split()
                        if not fields or fields[0].lower() != hashlib.sha256(raw).hexdigest():
                            raise ValueError(f'Session checksum mismatch: {path}')
                    else:
                        report['warnings'].append(f'Missing session.sha256: {sid}')
                    copy_path(path, prefix+'/session.json',
                              {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
                    if session_hash.is_file(): copy_path(session_hash,prefix+'/session.sha256')
                    row = {'input_order': number, 'session_id': sid, 'device_model': session.get('device_model'),
                           'source_session': str(path), 'bundle_path': prefix+'/session.json', 'runs': []}
                    report['sessions'].append(row)
                    for execution in session.get('executions', []):
                        rid = safe_name(execution['run_id'], component=True)
                        app = safe_name(execution.get('app_id') or 'unknown', component=True)
                        target_prefix = f'{prefix}/runs/{app}/{rid}'
                        source = find_run(path, execution, Path(runs_dir).resolve() if runs_dir else None)
                        run = {'run_id': rid, 'app_id': app, 'method': execution.get('method'),
                               'acquisition_environment': execution.get('acquisition_environment'),
                               'source': str(source) if source else None, 'bundle_prefix': target_prefix,
                               'recorded_archive_sha256': execution.get('sha256'), 'copied_members': 0}
                        row['runs'].append(run)
                        if source is None:
                            report['warnings'].append(f'Missing ZIP/run directory: {rid}')
                            continue
                        print(f'AURA: Copying metadata: {app} / {rid}', flush=True)
                        with run_members(source) as (names, reader):
                            if 'acquisition.json' not in names:
                                raise ValueError(f'Missing acquisition.json: {source}')
                            with reader('acquisition.json') as f: acquisition = json.load(f)
                            if (acquisition.get('run', {}).get('run_id') != rid
                                    or acquisition['run'].get('app_id') != app):
                                raise ValueError(f'Run identity mismatch: {source}')
                            originals = {a['relative_path'] for a in acquisition.get('artifacts', [])
                                         if a.get('kind') in ORIGINAL_KINDS}
                            selected = [name for name in names if name in CORE or
                                        (PurePosixPath(name).suffix.lower() in {'.json','.jsonl'} and name not in originals)]
                            expected = None
                            if 'files.json' in names:
                                with reader('files.json') as f: expected = {r['path']: r for r in json.load(f)['members']}
                            for missing in sorted(CORE-set(names)):
                                report['warnings'].append(f'Missing {missing}: {rid}')
                            for name in selected:
                                safe_name(name)
                                if expected is not None and name != 'files.json' and name not in expected:
                                    raise ValueError(f'Metadata missing from files.json: {name}')
                                copy(lambda name=name: reader(name), target_prefix+'/'+name,
                                     expected.get(name) if expected is not None else None)
                            run.update(copied_members=len(selected), omitted_members=len(names)-len(selected),
                                       source_kind='directory' if source.is_dir() else 'zip',
                                       selected_hashes_checked=expected is not None)
                        if source.is_file():
                            sidecar = source.with_suffix(source.suffix+'.sha256')
                            if sidecar.is_file(): copy_path(sidecar,target_prefix+'/'+rid+'.zip.sha256')
                            else: report['warnings'].append(f'Missing source ZIP checksum: {rid}')
                for number, reference in enumerate(references, 1):
                    reference = Path(reference)
                    if reference.suffix.lower() != '.json': raise ValueError('Reference must be a JSON file')
                    with reference.open('rb') as f: json.load(f)
                    copy_path(reference,f'references/{number:02d}/'+safe_name(reference.name,component=True))
                if notes:
                    notes = Path(notes)
                    copy_path(notes,'notes/'+safe_name(notes.name,component=True))
                bundle.writestr('README.txt',README)
                bundle.writestr('bundle-manifest.json',json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        with checksum.open('x',encoding='utf-8',newline='\n') as f:
            checksum_created = True
            f.write(digest_file(output)+'  '+output.name+'\n')
    except BaseException:
        if created: output.unlink(missing_ok=True)
        if checksum_created: checksum.unlink(missing_ok=True)
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sessions',nargs='+',type=Path,help='completed session.json files or their directories, in repetition order')
    parser.add_argument('--output',required=True,type=Path,help='new ZIP path; existing output/checksum are never overwritten')
    parser.add_argument('--runs-dir',type=Path,help='runs directory if original results were moved')
    parser.add_argument('--reference',action='append',default=[],type=Path,help='optional ground-truth JSON; repeat for multiple files')
    parser.add_argument('--notes',type=Path,help='optional plain-text repetition/release notes')
    args=parser.parse_args(argv)
    try:
        report=create_bundle(args.sessions,args.output,runs_dir=args.runs_dir,references=args.reference,notes=args.notes)
    except (OSError,ValueError,KeyError,TypeError,BadZipFile) as error:
        print(f'AURA: Bundle creation failed: {error}',file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('AURA: Cancelled; incomplete output removed. Originals unchanged.',file=sys.stderr)
        return 130
    print(json.dumps({'output':str(args.output.resolve()),'sessions':len(report['sessions']),
                      'copied_files':len(report['files']),'warnings':report['warnings']},ensure_ascii=False,indent=2))
    return 1 if report['warnings'] else 0


if __name__=='__main__':
    raise SystemExit(main())
