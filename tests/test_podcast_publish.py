import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from podcast_publish import LocalStore, Publisher


class FaultStore(LocalStore):
    fail = None
    uploads = 0

    def put(self, key, data, content_type='application/octet-stream'):
        if self.fail == ('put', key):
            self.fail = None
            raise OSError('injected put failure')
        if key.startswith('audio/'):
            self.uploads += 1
        super().put(key, data, content_type)

    def delete(self, key):
        if self.fail == ('delete', key):
            self.fail = None
            raise OSError('injected delete failure')
        super().delete(key)


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FaultStore(self.root / 'objects')
        self.publisher = Publisher(self.store, self.root / 'state', 'https://example.test/private')
        self.audio = self.root / 'input.mp3'
        self.audio.write_bytes(b'test audio bytes')
        self.quality = self.root / 'quality.json'
        self.quality.write_text(json.dumps({
            'source_to_adapted': {'approved': True}, 'adapted_to_audio': {'approved': True},
            'audio_sha256': hashlib.sha256(self.audio.read_bytes()).hexdigest(),
            'ffprobe': {'duration': 5}}))
        mock = patch('podcast_publish.probe_audio', return_value=5)
        mock.start()
        self.addCleanup(mock.stop)

    def publish(self, number):
        return self.publisher.publish({'id': str(number), 'title': 'A & <B>',
                                       'source_url': 'https://example.test/?x=1&y=2'}, self.audio, self.quality)

    def fill(self):
        for number in range(20):
            self.publish(number)

    def verify(self):
        manifest = self.publisher.manifest()
        self.assertEqual(len(manifest['episodes']), 20)
        self.assertEqual(len(self.store.list('audio/')), 20)
        self.assertEqual(manifest['retired'], ['0'])
        rss = ET.fromstring(self.store.get('data/feed.xml'))
        self.assertEqual(len(rss.findall('./channel/item')), 20)
        self.assertEqual(rss.find('./channel/item/title').text, 'A & <B>')

    def test_twenty_one_idempotent_and_retired(self):
        self.fill()
        first = self.publisher.manifest()['episodes'][0]
        self.publish(20)
        self.verify()
        self.assertIsNone(self.store.get(first['media_key']))
        uploads = self.store.uploads
        self.assertEqual(self.publish(20)['status'], 'published')
        self.assertEqual(self.publish(0)['status'], 'retired')
        self.assertEqual(self.store.uploads, uploads)

    def test_delete_failure_prevents_upload_and_recovers(self):
        self.fill()
        old = self.publisher.manifest()['episodes'][0]
        self.store.fail = ('delete', old['media_key'])
        with self.assertRaises(OSError):
            self.publish(20)
        self.assertEqual(self.store.uploads, 20)
        self.assertEqual(len(self.publisher.manifest()['episodes']), 19)
        self.publisher.recover()
        self.verify()

    def test_upload_failure_and_new_runner_recovers(self):
        self.fill()
        key = 'audio/' + hashlib.sha256(b'20').hexdigest() + '.mp3'
        self.store.fail = ('put', key)
        with self.assertRaises(OSError):
            self.publish(20)
        self.assertEqual(len(self.store.list('audio/')), 19)
        self.publisher = Publisher(self.store, self.root / 'fresh-runner', 'https://example.test/private')
        self.publish(20)  # new runner restores same approved input, not a second episode
        self.verify()

    def test_final_manifest_failure_does_not_upload_again(self):
        self.fill()
        original = self.store.put
        def fail_final(key, data, content_type='application/octet-stream'):
            if key == 'data/feed-manifest.json' and len(json.loads(data)['episodes']) == 20:
                self.store.put = original
                raise OSError('final manifest failed')
            original(key, data, content_type)
        self.store.put = fail_final
        with self.assertRaises(OSError):
            self.publish(20)
        self.assertEqual(self.store.uploads, 21)
        self.publisher = Publisher(self.store, self.root / 'fresh-runner', 'https://example.test/private')
        self.publisher.recover()
        self.assertEqual(self.store.uploads, 21)
        self.verify()

    def test_quality_hash_and_budget_fail_before_retirement(self):
        self.fill()
        self.audio.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'hash'):
            self.publish(20)
        self.assertEqual(len(self.publisher.manifest()['episodes']), 20)

    def test_missing_published_object_fails_closed(self):
        self.publish(1)
        current = self.publisher.manifest()['episodes'][0]
        self.store.delete(current['media_key'])
        with self.assertRaisesRegex(ValueError, 'missing'):
            self.publish(2)
        self.assertEqual(len(self.publisher.manifest()['episodes']), 1)

    def test_both_content_approvals_required(self):
        quality = json.loads(self.quality.read_text())
        quality['adapted_to_audio']['approved'] = False
        self.quality.write_text(json.dumps(quality))
        with self.assertRaisesRegex(ValueError, 'needs_review'):
            self.publish(1)
        self.assertEqual(len(self.store.list('audio/')), 0)

    def test_budget_fails_before_retirement(self):
        self.fill()
        self.publisher.max_bytes = 100
        with self.assertRaisesRegex(ValueError, 'quota_wait'):
            self.publish(20)
        self.assertEqual(len(self.publisher.manifest()['episodes']), 20)

    def test_same_id_different_audio_is_immutable_conflict(self):
        initial = self.publish(1)['episode']
        self.audio.write_bytes(b'different render')
        with self.assertRaisesRegex(ValueError, 'immutable_conflict'):
            self.publish(1)
        self.assertEqual(self.publisher.manifest()['episodes'][0], initial)
        self.assertEqual(self.store.uploads, 1)

    def test_upload_failure_does_not_reserve_publication_date(self):
        key = 'audio/' + hashlib.sha256(b'1').hexdigest() + '.mp3'
        self.store.fail = ('put', key)
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 22, tzinfo=timezone.utc)
            with self.assertRaises(OSError):
                self.publish(1)
        pending = json.loads(self.store.get('data/publish-intent.json'))
        self.assertIsNone(pending['episode']['first_published'])
        later = datetime(2026, 9, 25, tzinfo=timezone.utc)
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            result = self.publisher.recover()
        self.assertEqual(result['episode']['first_published'], later.isoformat())

    def test_failed_rss_write_dates_first_successful_retry(self):
        original = self.store.put
        def fail_feed(key, data, content_type='application/octet-stream'):
            if key == 'data/feed.xml' and ET.fromstring(data).find('./channel/item') is not None:
                self.store.put = original
                raise OSError('RSS write failed')
            original(key, data, content_type)
        self.store.put = fail_feed
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 22, tzinfo=timezone.utc)
            with self.assertRaises(OSError):
                self.publish(1)
        later = datetime(2026, 9, 25, tzinfo=timezone.utc)
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = later
            result = self.publisher.recover()
        self.assertEqual(result['episode']['first_published'], later.isoformat())
        self.assertEqual(self.store.uploads, 1)

    def test_manifest_failure_preserves_first_visible_rss_date(self):
        original = self.store.put
        def fail_manifest(key, data, content_type='application/octet-stream'):
            if key == 'data/feed-manifest.json' and json.loads(data)['episodes']:
                self.store.put = original
                raise OSError('manifest write failed')
            original(key, data, content_type)
        self.store.put = fail_manifest
        first = datetime(2026, 9, 22, tzinfo=timezone.utc)
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = first
            with self.assertRaises(OSError):
                self.publish(1)
        with patch('podcast_publish.datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 9, 25, tzinfo=timezone.utc)
            result = self.publisher.recover()
        self.assertEqual(result['episode']['first_published'], first.isoformat())
        self.assertEqual(self.store.uploads, 1)


if __name__ == '__main__':
    unittest.main()
