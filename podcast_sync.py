"""Private podcast state transport; never use Git/artifacts for these files.

Call restore before queue work and save afterwards, including failed runs. All
hosts MUST share workflow concurrency: this module does not provide a distributed
lock. No authentication files or browser profiles are transported. Missing audio
never removes checkpoints and never authorizes another Pi submission.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time
import zipfile

from podcast_publish import LocalStore, S3Store, atomic

MAX_TEXT = 20 * 1024 * 1024
TTL = 86400
META = '__sync__.json'
TASK = r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}'
WORK_FILES = {'input.json', 'result.json', 'rewrite.json', 'checkpoint.json',
              'quality.json', 'episode.json', 'adapted-en.md', 'segments.json',
              'approved-script.md', 'script-review.json',
              'automatic-quality.json', 'capture-quality.json'}
SEGMENT_FILES = {'source.json', 'input.json', 'checkpoint.json', 'result.json',
                 'rewrite.json', 'adapted-en.md'}
AUTH_KEYS = {'cookie', 'cookies', 'password', 'authorization', 'access_token',
             'refresh_token', 'storage_state', 'storagestate', 'auth_token', 'headers'}


def safe_task(value):
    return (isinstance(value, str) and bool(re.fullmatch(TASK, value))
            and not re.fullmatch(r'(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])', value))


def allowed(name):
    if '\\' in name or any(p in ('', '.', '..') for p in name.split('/')):
        return False
    if name in ('state/podcast_queue.json', 'state/podcast_budget.json'):
        return True
    parts = name.split('/')
    if len(parts) >= 4 and parts[:2] == ['state', 'podcast_sources']:
        return parts[-1] in ('source.json', 'source.md') and all(safe_task(p) for p in parts[2:-1])
    if (len(parts) == 4 and parts[0] == 'podcast-work' and safe_task(parts[1])
            and re.fullmatch(r'segment-[0-9]{3,6}', parts[2])):
        return parts[3] in SEGMENT_FILES
    return (len(parts) == 3 and parts[0] == 'podcast-work'
            and safe_task(parts[1]) and parts[2] in WORK_FILES)


def allowed_audio(name):
    return name == 'episode.mp3' or bool(re.fullmatch(r'segment-[0-9]{3,6}/episode\.mp3', name))


def local_managed_names(root):
    result = set()
    for base in ('state', 'podcast-work'):
        directory = root / base
        for path in directory.rglob('*') if directory.exists() else []:
            name = path.relative_to(root).as_posix()
            parts = name.split('/')
            audio = (len(parts) >= 3 and parts[0] == 'podcast-work'
                     and safe_task(parts[1]) and allowed_audio('/'.join(parts[2:])))
            if allowed(name) or audio:
                if safe_path(root, name).is_file():
                    result.add(name)
    return result


def safe_path(root, name):
    root = Path(root).resolve()
    path = root / name
    current = root
    for part in Path(name).parts:
        current = current / part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('symlinks are forbidden in private state')
    if not path.resolve().is_relative_to(root):
        raise ValueError('private state path escapes root')
    return path


def validate_text(name, data):
    text = data.decode('utf-8')
    if name.endswith('.json'):
        def scan(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key.lower() in AUTH_KEYS:
                        raise ValueError('authentication fields forbidden in private state')
                    scan(child)
            elif isinstance(value, list):
                for child in value:
                    scan(child)
        scan(json.loads(text))


def read_package(raw):
    if len(raw) > MAX_TEXT:
        raise ValueError('private package exceeds size limit')
    entries = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_TEXT:
            raise ValueError('expanded private state exceeds size limit')
        for info in infos:
            name = info.filename
            if name in entries or (name != META and not allowed(name)):
                raise ValueError('unexpected or duplicate private state path')
            if info.flag_bits & 1 or stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
                raise ValueError('encrypted or linked private state forbidden')
            data = archive.read(info)
            if len(data) != info.file_size:
                raise ValueError('invalid private state length')
            if name != META:
                validate_text(name, data)
            entries[name] = data
    metadata = json.loads(entries.pop(META))
    if metadata.get('version') != 1 or set(metadata['files']) != set(entries):
        raise ValueError('invalid private state manifest')
    for name, data in entries.items():
        if hashlib.sha256(data).hexdigest() != metadata['files'][name]:
            raise ValueError('private state hash mismatch')
    audio = metadata.get('audio', [])
    if len({item['task'] for item in audio}) > 1:
        raise ValueError('only one active audio task is allowed')
    identities = set()
    for item in audio:
        if not safe_task(item['task']) or not re.fullmatch(r'[a-f0-9]{64}', item['sha256']):
            raise ValueError('invalid staged audio identity')
        item.setdefault('path', 'episode.mp3')  # support earlier single-clip packages
        identity = (item['task'], item['path'])
        if not allowed_audio(item['path']) or identity in identities:
            raise ValueError('invalid or duplicate staged audio path')
        identities.add(identity)
        if not isinstance(item['bytes'], int) or not 0 < item['bytes'] <= 1_000_000_000:
            raise ValueError('invalid staged audio size')
        if not isinstance(item['created_at'], (float, int)) or not 0 < item['created_at'] <= time.time() + 60:
            raise ValueError('invalid staged audio creation time')
    return metadata, entries


def save(root, store, max_bytes=1_000_000_000, now=None):
    root = Path(root).resolve()
    now = time.time() if now is None else now
    entries = {}
    total = 0
    for base in ('state', 'podcast-work'):
        directory = root / base
        if directory.is_symlink():
            raise ValueError('symlinks are forbidden in private state')
        for path in directory.rglob('*') if directory.exists() else []:
            name = path.relative_to(root).as_posix()
            if not allowed(name):
                continue
            path = safe_path(root, name)
            if not path.is_file():
                continue
            total += path.stat().st_size
            if total >= MAX_TEXT:
                raise ValueError('expanded private state exceeds size limit')
            data = path.read_bytes()
            validate_text(name, data)
            entries[name] = data
    audio = []
    audio_payloads = {}
    superseded_audio = set()
    queue_data = entries.get('state/podcast_queue.json')
    if queue_data:
        queue = json.loads(queue_data)
        tasks = queue.get('tasks', {})
        if not isinstance(tasks, dict):
            tasks = {}
        for task_id, task in tasks.items():
            replacement = tasks.get(task.get('superseded_by'))
            if (task.get('status') == 'failed' and task.get('reason') == 'superseded_by_reviewed_script'
                    and isinstance(replacement, dict) and replacement.get('id') == task.get('superseded_by')
                    and replacement.get('supersedes') == task_id and task_id != replacement.get('id')
                    and replacement.get('article_id') == task.get('article_id')
                    and replacement.get('body_hash') == task.get('body_hash')
                    and replacement.get('mode') == 'punctuation_only'
                    and re.fullmatch(r'[a-f0-9]{64}', replacement.get('script_sha256', ''))):
                superseded_audio.add(task_id)
    work = root / 'podcast-work'
    paths = list(work.glob('*/episode.mp3')) + list(work.glob('*/segment-*/episode.mp3')) if work.exists() else []
    for path in paths:
        relative = path.relative_to(work)
        task = relative.parts[0]
        media_path = Path(*relative.parts[1:]).as_posix()
        if not safe_task(task) or not allowed_audio(media_path):
            raise ValueError('invalid task directory')
        path = safe_path(root, path.relative_to(root).as_posix())
        if now - path.stat().st_mtime >= TTL:
            path.unlink()
            continue
        if task in superseded_audio:
            continue
        if audio and task != audio[0]['task']:
            raise ValueError('only one active audio task is allowed')
        if path.stat().st_size + sum(map(len, audio_payloads.values())) > max_bytes:
            raise ValueError('quota_wait: staged audio exceeds byte budget')
        audio_data = path.read_bytes()
        if not audio_data:
            raise ValueError('empty staged audio')
        digest = hashlib.sha256(audio_data).hexdigest()
        audio_payloads['staging/' + digest + '.mp3'] = audio_data
        audio.append({'task': task, 'path': media_path, 'sha256': digest,
                      'bytes': len(audio_data), 'created_at': min(path.stat().st_mtime, now)})
    metadata = {'version': 1, 'files': {k: hashlib.sha256(v).hexdigest() for k, v in entries.items()}, 'audio': audio}
    entries[META] = json.dumps(metadata, sort_keys=True).encode()
    if sum(map(len, entries.values())) > MAX_TEXT:
        raise ValueError('expanded private state exceeds size limit')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    package = buffer.getvalue()
    if len(package) > MAX_TEXT:
        raise ValueError('private package exceeds size limit')
    listing = store.list()
    # Budget the transient peak: old package plus replacement audio can coexist.
    projected = sum(listing.values()) + (len(package) - listing.get('data/private-state.zip', 0))
    for key, data in audio_payloads.items():
        projected += len(data) - listing.get(key, 0)
    if projected > max_bytes:
        raise ValueError('quota_wait: controlled prefix exceeds byte budget')
    # Publish checkpoints before media transfers. A failed or interrupted upload
    # must leave the confirmed Pi message IDs recoverable; missing media can be
    # fetched again from those replies without submitting the article again.
    store.put('data/private-state.zip', package, 'application/zip')
    stored_package = store.get('data/private-state.zip')
    if stored_package is None or hashlib.sha256(stored_package).digest() != hashlib.sha256(package).digest():
        raise ValueError('private state upload verification failed')
    # Retire prior task staging before uploading another task: even a crash must
    # not leave two retained audio tasks. Its old checkpoint remains in the zip.
    for old in store.list('staging/'):
        if old not in audio_payloads:
            store.delete(old)
    for key, audio_data in audio_payloads.items():
        digest = hashlib.sha256(audio_data).hexdigest()
        existing = store.get(key)
        if existing is None or hashlib.sha256(existing).hexdigest() != digest:
            store.put(key, audio_data, 'audio/mpeg')
        check = store.get(key)
        if check is None or len(check) != len(audio_data) or hashlib.sha256(check).hexdigest() != digest:
            raise ValueError('staged upload verification failed')
    for old in store.list('staging/'):
        if old not in audio_payloads:
            store.delete(old)
    return {'status': 'saved', 'files': len(entries) - 1,
            'staged_tasks': sorted({a['task'] for a in audio}), 'staged_files': len(audio)}


def restore(root, store, now=None, max_bytes=1_000_000_000):
    root = Path(root).resolve()
    now = time.time() if now is None else now
    raw = store.get('data/private-state.zip')
    if raw is None:
        if local_managed_names(root):
            raise ValueError('local state divergence: restore requires a clean managed destination')
        return {'status': 'empty', 'missing_audio': []}
    metadata, entries = read_package(raw)
    if sum(item['bytes'] for item in metadata['audio']) > max_bytes:
        raise ValueError('quota_wait: staged audio exceeds byte budget')
    destinations = set(entries) | {f"podcast-work/{item['task']}/{item['path']}" for item in metadata['audio']}
    if local_managed_names(root) - destinations:
        raise ValueError('local state divergence: extra managed files require a clean destination')
    # Validate every destination before writing any file.
    for name in entries:
        safe_path(root, name)
    recovered = []
    missing = []
    audio_payloads = []
    unexpired_hashes = {item['sha256'] for item in metadata['audio'] if now - item['created_at'] < TTL}
    for item in metadata['audio']:
        destination = safe_path(root, f"podcast-work/{item['task']}/{item['path']}")
        key = 'staging/' + item['sha256'] + '.mp3'
        data = None if now - item['created_at'] >= TTL else store.get(key)
        if data is not None and (len(data) != item['bytes'] or hashlib.sha256(data).hexdigest() != item['sha256']):
            raise ValueError('staged audio hash mismatch')
        if data is None:
            missing.append(item['task'])
            destination.unlink(missing_ok=True)
            if now - item['created_at'] >= TTL and item['sha256'] not in unexpired_hashes:
                store.delete(key)
        else:
            audio_payloads.append((destination, data, item['created_at']))
            recovered.append(item['task'])
    for name, data in entries.items():
        atomic(safe_path(root, name), data)
    for destination, data, created_at in audio_payloads:
        atomic(destination, data)
        os.utime(destination, (created_at, created_at))
    return {'status': 'restored', 'files': len(entries), 'restored_audio': sorted(set(recovered)),
            'missing_audio': sorted(set(missing)), 'resubmission_allowed': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['restore', 'save'])
    parser.add_argument('--root', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--store', choices=['local', 's3', 'http'], default='local')
    parser.add_argument('--local-root', default='state/private-podcast-store')
    parser.add_argument('--bucket', default=os.getenv('PODCAST_BUCKET'))
    parser.add_argument('--prefix', default='podcast')
    parser.add_argument('--endpoint-url', default=os.getenv('PODCAST_S3_ENDPOINT'))
    parser.add_argument('--max-bytes', type=int, default=1_000_000_000)
    args = parser.parse_args()
    if args.max_bytes <= 0:
        parser.error('--max-bytes must be positive')
    if args.store == 's3' and not args.bucket:
        parser.error('PODCAST_BUCKET is required for s3')
    if args.store == 'http':
        from podcast_http_store import HttpStore
        store = HttpStore()
    else:
        store = LocalStore(args.local_root) if args.store == 'local' else S3Store(args.bucket, args.prefix, args.endpoint_url)
    result = save(args.root, store, args.max_bytes) if args.command == 'save' else restore(args.root, store, max_bytes=args.max_bytes)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
