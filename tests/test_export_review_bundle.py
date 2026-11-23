import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'tools/export_aura_review_bundle.py'


def load_tool():
    assert SCRIPT.is_file(), 'Review-bundle exporter has not been implemented'
    spec = importlib.util.spec_from_file_location('review_bundle_exporter', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def fixture_session(root, number=1, *, directory=False):
    sid = f'session-20261111-{number:06d}'
    rid = sid + '-001-chrome-materialize-device-only'
    members = {
        'acquisition.json': json.dumps({'run': {'run_id': rid, 'app_id': 'chrome'},
            'artifacts': [{'kind': 'downloaded_file', 'relative_path': 'chrome/files/original.json'}]}).encode(),
        'events.jsonl': b'{"event": "example"}\n',
        'chrome/history/기록.json': b'{"records": [{"title": "Example"}]}',
        'chrome/diagnostic.jsonl': b'{"error": "none"}\n',
        'chrome/files/original.json': b'{"attachment": true}',
        'screens/one.png': b'not a real png',
        'screens/one.xml': b'<hierarchy/>',
        'chrome/files/video.mp4': b'media',
        'chrome/files/export.zip': b'export original',
    }
    manifest = {'members': [{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                            for name, data in members.items()]}
    members['files.json'] = json.dumps(manifest).encode()
    root.mkdir(parents=True, exist_ok=True)
    if directory:
        for name, data in members.items():
            if name == 'files.json': continue  # package_run removes this from the source directory
            p=root/rid/name; p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    else:
        with ZipFile(root/f'{rid}.zip', 'w') as archive:
            for name, data in members.items(): archive.writestr(name,data)
        (root/f'{rid}.zip.sha256').write_text(hashlib.sha256((root/f'{rid}.zip').read_bytes()).hexdigest())
    path=root/'sessions'/sid/'session.json'
    write_json(path, {'device_session_id': sid,'device_model':'SM-G991N','status':'complete',
        'executions':[{'run_id':rid,'app_id':'chrome','status':'complete','method':'materialize',
                       'acquisition_environment':'device_only','elapsed_seconds':123.4,
                       'archive_path':rf'C:\old\AURA\runs\{rid}.zip'}]})
    path.with_name('session.sha256').write_text(hashlib.sha256(path.read_bytes()).hexdigest()+'  session.json\n')
    return path, rid, members


def snapshot(root):
    return {str(p.relative_to(root)):(p.read_bytes(),p.stat().st_mtime_ns)
            for p in root.rglob('*') if p.is_file()}


def test_three_sessions_keep_structure_bytes_and_originals(tmp_path):
    tool=load_tool(); runs=tmp_path/'원본 results'
    samples=[fixture_session(runs,n) for n in range(1,4)]
    before=snapshot(runs)
    reference=tmp_path/'reference.json';write_json(reference,{'groups':[]})
    notes=tmp_path/'notes.txt';notes.write_text('S21 repeats 1, 2, 3; cache retained.',encoding='utf-8')
    output=tmp_path/'검토자료 S21.zip'
    report=tool.create_bundle([p for p,_,_ in samples],output,references=[reference],notes=notes)
    assert report['warnings']==[]
    with ZipFile(output) as archive:
        assert archive.testzip() is None
        manifest=json.loads(archive.read('bundle-manifest.json'))
        assert len(manifest['sessions'])==3
        for p,rid,members in samples:
            prefix=f'sessions/{p.parent.name}'
            assert archive.read(prefix+'/session.json')==p.read_bytes()
            for name in ('acquisition.json','events.jsonl','files.json','chrome/history/기록.json','chrome/diagnostic.jsonl'):
                assert archive.read(f'{prefix}/runs/chrome/{rid}/{name}')==members[name]
        assert not any(name.endswith(('.png','.xml','.mp4','original.json','export.zip')) for name in archive.namelist())
        assert archive.read('references/01/reference.json')==reference.read_bytes()
        assert archive.read('notes/notes.txt')==notes.read_bytes()
        for row in manifest['files']:
            assert hashlib.sha256(archive.read(row['path'])).hexdigest()==row['sha256']
    assert snapshot(runs)==before
    assert output.with_suffix('.zip.sha256').read_text().split()[0]==hashlib.sha256(output.read_bytes()).hexdigest()


def test_unpackaged_failed_run_and_missing_run_are_explicit(tmp_path):
    tool=load_tool();p,rid,_=fixture_session(tmp_path/'runs',directory=True)
    output=tmp_path/'partial.zip';report=tool.create_bundle([p],output)
    assert report['warnings']  # files.json/ZIP unavailable; folder metadata remains usable
    with ZipFile(output) as archive:
        assert any(name.endswith('/chrome/history/기록.json') for name in archive.namelist())
    missing_root=tmp_path/'missing';p2,rid2,_=fixture_session(missing_root,2)
    (missing_root/f'{rid2}.zip').unlink()
    report=tool.create_bundle([p2],tmp_path/'missing.zip')
    assert any('missing' in message.lower() for message in report['warnings'])


@pytest.mark.parametrize('case',['existing_output','existing_checksum','duplicate_session','session_hash','unsafe_member','member_hash','wrong_run','broken_zip'])
def test_invalid_input_never_overwrites_or_leaves_partial_output(tmp_path,case):
    tool=load_tool();runs=tmp_path/'runs';p,rid,members=fixture_session(runs)
    output=tmp_path/'review.zip';sessions=[p]
    if case=='existing_output':output.write_bytes(b'KEEP')
    if case=='existing_checksum':output.with_suffix('.zip.sha256').write_bytes(b'KEEP')
    if case=='duplicate_session':sessions=[p,p]
    if case=='session_hash':p.with_name('session.sha256').write_text('0'*64)
    if case=='unsafe_member':members['../escape.json']=b'{}'
    if case=='member_hash':members['chrome/history/기록.json']=b'CHANGED'
    if case=='wrong_run':members['acquisition.json']=json.dumps({'run':{'run_id':'another','app_id':'chrome'},'artifacts':[]}).encode()
    if case in ('unsafe_member','member_hash','wrong_run'):
        with ZipFile(runs/f'{rid}.zip','w') as archive:
            for name,data in members.items():archive.writestr(name,data)
    if case=='broken_zip':(runs/f'{rid}.zip').write_bytes(b'broken')
    before=snapshot(runs)
    with pytest.raises((ValueError,OSError,BadZipFile)):
        tool.create_bundle(sessions,output)
    assert snapshot(runs)==before
    if case=='existing_output':assert output.read_bytes()==b'KEEP'
    else:assert not output.exists()
    if case=='existing_checksum':assert output.with_suffix('.zip.sha256').read_bytes()==b'KEEP'
