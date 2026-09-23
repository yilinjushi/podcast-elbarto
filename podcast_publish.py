"""Single-writer, crash-recoverable podcast publisher (no deployment).

The caller must enforce one writer across hosts. The OS lock prevents concurrent
local writers. Persist state_dir between runs; never cache or commit its contents.
RSS is a derived view; feed-manifest.json is authoritative. Recovery repairs both.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from urllib.parse import quote, urlsplit
import xml.etree.ElementTree as ET


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def safe_key(key):
    if not key or '\\' in key or key.startswith('/') or any(p in ('..', '.') for p in key.split('/')):
        raise ValueError('unsafe object key')
    return key


class LocalStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, key):
        path = (self.root / safe_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('object escapes store')
        return path

    def get(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, key, data, content_type='application/octet-stream'):
        atomic(self.path(key), data)

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def list(self, prefix=''):
        return {p.relative_to(self.root).as_posix(): p.stat().st_size
                for p in self.root.rglob('*') if p.is_file()
                and p.relative_to(self.root).as_posix().startswith(prefix)}


class S3Store:
    """Optional boto3 adapter. Credentials use boto3's standard environment chain.

    prefix must identify the entire controlled project namespace; byte accounting
    includes its data as well as audio. Account-wide budget headroom is external.
    """
    def __init__(self, bucket, prefix, endpoint_url=None):
        import boto3
        self.client = boto3.client('s3', endpoint_url=endpoint_url)
        self.bucket = bucket
        self.prefix = safe_key(prefix.strip('/')) + '/'

    def get(self, key):
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self.prefix + safe_key(key))['Body'].read()
        except self.client.exceptions.ClientError as error:
            if error.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None
            raise

    def put(self, key, data, content_type='application/octet-stream'):
        self.client.put_object(Bucket=self.bucket, Key=self.prefix + safe_key(key), Body=data,
                               ContentType=content_type, CacheControl='private, no-store')

    def delete(self, key):
        self.client.delete_object(Bucket=self.bucket, Key=self.prefix + safe_key(key))

    def list(self, prefix=''):
        result = {}
        pages = self.client.get_paginator('list_objects_v2').paginate(
            Bucket=self.bucket, Prefix=self.prefix + prefix)
        for page in pages:
            for obj in page.get('Contents', []):
                result[obj['Key'][len(self.prefix):]] = obj['Size']
        return result


@contextlib.contextmanager
def writer_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'publisher.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def probe_audio(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', str(path)], check=True, capture_output=True, text=True)
    data = json.loads(result.stdout)
    duration = float(data['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('invalid audio duration')
    streams = [s for s in data['streams'] if s.get('codec_type') == 'audio']
    if len(streams) != 1 or streams[0].get('codec_name') != 'mp3':
        raise ValueError('publisher currently accepts verified MP3 only')
    subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path), '-f', 'null', '-'],
                   check=True, capture_output=True)
    return duration


class Publisher:
    def __init__(self, store, state_dir, base_url, max_bytes=9_000_000_000, max_episodes=20,
                 title='Complete English Articles'):
        if not 1 <= max_episodes <= 20 or max_bytes <= 0:
            raise ValueError('invalid retention or byte limit')
        url = urlsplit(base_url)
        if url.scheme not in ('http', 'https') or not url.netloc or url.query or url.fragment:
            raise ValueError('base_url must be an absolute URL with optional private path, no query')
        self.store, self.state = store, Path(state_dir)
        self.base_url, self.max_bytes = base_url.rstrip('/'), max_bytes
        self.max_episodes, self.title = max_episodes, title

    def manifest(self):
        raw = self.store.get('data/feed-manifest.json')
        return json.loads(raw) if raw else {'version': 1, 'next_sequence': 1, 'episodes': [], 'retired': []}

    def rss(self, manifest):
        rss = ET.Element('rss', {'version': '2.0'})
        channel = ET.SubElement(rss, 'channel')
        for key, value in [('title', self.title), ('link', self.base_url),
                           ('description', '完整易听英文改写版'), ('language', 'en')]:
            ET.SubElement(channel, key).text = value
        for episode in sorted(manifest['episodes'], key=lambda e: e['sequence'], reverse=True):
            item = ET.SubElement(channel, 'item')
            for key, value in [('title', episode['title']), ('description', episode['description']),
                               ('pubDate', format_datetime(datetime.fromisoformat(
                                   episode.get('first_published') or '1970-01-01T00:00:00+00:00'))),
                               ('link', episode.get('source_url', ''))]:
                ET.SubElement(item, key).text = value
            ET.SubElement(item, 'guid', {'isPermaLink': 'false'}).text = episode['guid']
            ET.SubElement(item, 'enclosure', {'url': self.base_url + '/' + quote(episode['media_key'], safe='/'),
                                             'length': str(episode['bytes']), 'type': 'audio/mpeg'})
        return ET.tostring(rss, encoding='utf-8', xml_declaration=True)

    def commit(self, manifest):
        # RSS first ensures a retired object is unlinked before it is deleted.
        self.store.put('data/feed.xml', self.rss(manifest), 'application/rss+xml')
        self.store.put('data/feed-manifest.json', encode(manifest), 'application/json')

    def journal(self, intent):
        self.store.put('data/publish-intent.json', encode(intent), 'application/json')
        atomic(self.state / 'publish-intent.json', encode(intent))

    def recover(self):
        with writer_lock(self.state):
            self._expire_staging()
            raw = self.store.get('data/publish-intent.json')
            if raw:
                return self._resume(json.loads(raw))
            manifest = self.manifest()
            self.commit(manifest)
            self._clean_orphans(manifest)
            return {'status': 'idle'}

    def _clean_orphans(self, manifest):
        keep = {e['media_key'] for e in manifest['episodes']}
        listing = self.store.list('audio/')
        for episode in manifest['episodes']:
            if listing.get(episode['media_key']) != episode['bytes']:
                raise ValueError('published media missing or wrong size; manual repair required')
        for key in listing:
            if key not in keep:
                self.store.delete(key)

    def _expire_staging(self):
        staged = self.state / 'pending.mp3'
        if staged.exists() and time.time() - staged.stat().st_mtime > 86400:
            staged.unlink()

    def publish(self, episode, audio_path, quality_path):
        with writer_lock(self.state):
            self._expire_staging()
            pending = self.store.get('data/publish-intent.json')
            if pending:
                intent = json.loads(pending)
                candidate = Path(audio_path).read_bytes()
                if hashlib.sha256(candidate).hexdigest() == intent['episode']['sha256']:
                    atomic(self.state / 'pending.mp3', candidate)
                self._resume(intent)
            manifest = self.manifest()
            self._clean_orphans(manifest)
            episode_id = str(episode['id'])
            if episode_id in manifest['retired']:
                return {'status': 'retired', 'id': episode_id}
            for current in manifest['episodes']:
                if current['id'] == episode_id:
                    candidate_hash = hashlib.sha256(Path(audio_path).read_bytes()).hexdigest()
                    if candidate_hash != current['sha256']:
                        raise ValueError('immutable_conflict: article already published with different audio')
                    return {'status': 'published', 'id': episode_id, 'episode': current}
            quality = json.loads(Path(quality_path).read_text(encoding='utf-8'))
            if not all(quality.get(gate, {}).get('approved') is True
                       for gate in ('source_to_adapted', 'adapted_to_audio')):
                raise ValueError('needs_review: both independent content checks must be approved')
            audio = Path(audio_path).read_bytes()
            digest = hashlib.sha256(audio).hexdigest()
            if quality.get('audio_sha256') != digest:
                raise ValueError('quality approval does not match audio hash')
            duration = probe_audio(audio_path)
            reviewed_duration = float(quality.get('ffprobe', {}).get('duration', 0))
            if not math.isfinite(reviewed_duration) or abs(duration - reviewed_duration) > 0.1:
                raise ValueError('quality duration does not match ffprobe')
            ident = hashlib.sha256(episode_id.encode()).hexdigest()
            new = {'id': episode_id, 'title': str(episode['title']),
                   'description': '完整易听英文改写版。' + str(episode.get('description', '')),
                   'source_url': str(episode.get('source_url', '')), 'guid': 'urn:sha256:' + ident,
                   'sequence': manifest['next_sequence'], 'first_published': None,
                   'media_key': 'audio/' + ident + '.mp3', 'sha256': digest, 'bytes': len(audio),
                   'duration': duration}
            reduced = json.loads(json.dumps(manifest))
            while len(reduced['episodes']) >= self.max_episodes:
                old = min(reduced['episodes'], key=lambda e: e['sequence'])
                reduced['episodes'].remove(old)
                reduced['retired'].append(old['id'])
            final = json.loads(json.dumps(reduced))
            final['episodes'].append(new)
            final['next_sequence'] = new['sequence'] + 1
            # Evaluate full controlled namespace, reclaiming only known audio.
            listing = self.store.list()
            keep = {e['media_key'] for e in reduced['episodes']}
            projected = sum(size for key, size in listing.items()
                            if not key.startswith('audio/') and key not in ('data/feed.xml', 'data/feed-manifest.json')
                            or key in keep)
            projected += len(audio) + len(encode(final)) + len(self.rss(final))
            # Reserve room for the small durable intent, including both manifests.
            if projected + 4 * len(encode(final)) + 4096 > self.max_bytes:
                raise ValueError('quota_wait: controlled store byte budget exceeded')
            staged = self.state / 'pending.mp3'
            atomic(staged, audio)
            intent = {'episode': new, 'reduced': reduced, 'final': final, 'stage': 'retire',
                      'created_at': time.time()}
            self.journal(intent)
            return self._resume(intent)

    def _resume(self, intent):
        new = intent['episode']
        if intent['stage'] == 'retire':
            self.commit(intent['reduced'])
            self._clean_orphans(intent['reduced'])  # deletion failure stops upload
            intent['stage'] = 'upload'
            self.journal(intent)
        if intent['stage'] == 'upload':
            existing = self.store.get(new['media_key'])
            if existing is None or len(existing) != new['bytes'] or hashlib.sha256(existing).hexdigest() != new['sha256']:
                staged = self.state / 'pending.mp3'
                if not staged.exists():
                    raise ValueError('retry_wait: resume publish with the original approved audio file')
                audio = staged.read_bytes()
                if len(audio) != new['bytes'] or hashlib.sha256(audio).hexdigest() != new['sha256']:
                    raise ValueError('staged audio corrupted; cannot resume')
                listing = self.store.list()
                projected = sum(listing.values()) - listing.get(new['media_key'], 0) + len(audio)
                projected += max(0, len(encode(intent['final'])) - listing.get('data/feed-manifest.json', 0))
                projected += max(0, len(self.rss(intent['final'])) - listing.get('data/feed.xml', 0))
                if projected > self.max_bytes:
                    raise ValueError('quota_wait: controlled store byte budget exceeded')
                self.store.put(new['media_key'], audio, 'audio/mpeg')
                existing = self.store.get(new['media_key'])
            if existing is None or len(existing) != new['bytes'] or hashlib.sha256(existing).hexdigest() != new['sha256']:
                raise ValueError('uploaded audio verification failed')
            intent['stage'] = 'commit'
            self.journal(intent)
        if intent['stage'] == 'commit':
            # Before the first public RSS write, use the current time, not the
            # potentially days-old upload intent. If RSS was written but the
            # manifest failed, preserve that first visible publication date.
            previous_feed = self.store.get('data/feed.xml')
            already_visible = False
            if previous_feed:
                root = ET.fromstring(previous_feed)
                already_visible = any(item.findtext('guid') == new['guid']
                                      for item in root.findall('./channel/item'))
            if not already_visible or not new.get('first_published'):
                new['first_published'] = datetime.now(timezone.utc).isoformat()
                for entry in intent['final']['episodes']:
                    if entry['id'] == new['id']:
                        entry['first_published'] = new['first_published']
                self.journal(intent)
            self.commit(intent['final'])
            self._clean_orphans(intent['final'])
            (self.state / 'pending.mp3').unlink(missing_ok=True)
            self.store.delete('data/publish-intent.json')
            (self.state / 'publish-intent.json').unlink(missing_ok=True)
        return {'status': 'published', 'id': new['id'], 'episode': new}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', choices=['local', 's3'], default='local')
    parser.add_argument('--root', default='state/podcast-store')
    parser.add_argument('--state-dir', default='state/podcast-publisher')
    parser.add_argument('--base-url', default=os.getenv('PODCAST_BASE_URL'))
    parser.add_argument('--bucket', default=os.getenv('PODCAST_BUCKET'))
    parser.add_argument('--prefix', default='podcast')
    parser.add_argument('--endpoint-url', default=os.getenv('PODCAST_S3_ENDPOINT'))
    parser.add_argument('--max-bytes', type=int, default=9_000_000_000)
    parser.add_argument('--episode')
    parser.add_argument('--audio')
    parser.add_argument('--quality')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    if not args.base_url:
        parser.error('PODCAST_BASE_URL or --base-url is required')
    store = LocalStore(args.root) if args.store == 'local' else S3Store(args.bucket, args.prefix, args.endpoint_url)
    publisher = Publisher(store, args.state_dir, args.base_url, args.max_bytes)
    if args.recover:
        result = publisher.recover()
    else:
        if not all((args.episode, args.audio, args.quality)):
            parser.error('--episode, --audio and --quality are required')
        result = publisher.publish(json.loads(Path(args.episode).read_text(encoding='utf-8')), args.audio, args.quality)
    print(json.dumps({'status': result['status'], 'id': result.get('id')}))


if __name__ == '__main__':
    main()
