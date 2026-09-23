import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from podcast_publish import LocalStore, atomic
from podcast_sync import META, MAX_TEXT, save, restore


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'input'
        self.target = Path(self.temp.name) / 'restored'
        self.store = LocalStore(Path(self.temp.name) / 'objects')
        atomic(self.root / 'state/podcast_queue.json', b'{"tasks": []}')
        atomic(self.root / 'state/podcast_sources/task_1/source.md', '正文 text'.encode())
        atomic(self.root / 'podcast-work/task_1/checkpoint.json', b'{"messageSid":"existing"}')

    def audio(self):
        path = self.root / 'podcast-work/task_1/episode.mp3'
        atomic(path, b'real bytes for transport (not an MP3 decoder test)')
        return path

    def test_verified_superseded_media_stays_local_but_not_staged(self):
        old=self.audio()
        atomic(self.root/'podcast-work/task_2/episode.mp3',b'new audio')
        tasks={'task_1':{'id':'task_1','status':'failed','reason':'superseded_by_reviewed_script','superseded_by':'task_2','article_id':'x-1','body_hash':'same'},'task_2':{'id':'task_2','supersedes':'task_1','article_id':'x-1','body_hash':'same','mode':'punctuation_only','script_sha256':'a'*64}}
        atomic(self.root/'state/podcast_queue.json',json.dumps({'tasks':tasks}).encode())
        result=save(self.root,self.store)
        self.assertEqual(result['staged_tasks'],['task_2'])
        self.assertTrue(old.exists())
        restore(self.target,self.store)
        self.assertFalse((self.target/'podcast-work/task_1/episode.mp3').exists())
        self.assertTrue((self.target/'podcast-work/task_1/checkpoint.json').exists())
        os.utime(old,(time.time()-86401,time.time()-86401))
        save(self.root,self.store)
        self.assertFalse(old.exists())
        self.assertTrue((self.root/'podcast-work/task_1/checkpoint.json').exists())
        atomic(self.root/'podcast-work/task_3/episode.mp3',b'other active')
        with self.assertRaisesRegex(ValueError,'one active'):save(self.root,self.store)

    def test_unverified_supersession_does_not_hide_second_active_task(self):
        self.audio()
        atomic(self.root/'podcast-work/task_2/episode.mp3',b'new audio')
        tasks={'task_1':{'id':'task_1','status':'failed','reason':'superseded_by_reviewed_script','superseded_by':'task_2','article_id':'x-1','body_hash':'same'},'task_2':{'id':'task_2','supersedes':'different_task','article_id':'x-1','body_hash':'same','mode':'punctuation_only','script_sha256':'a'*64}}
        atomic(self.root/'state/podcast_queue.json',json.dumps({'tasks':tasks}).encode())
        with self.assertRaisesRegex(ValueError,'one active'):save(self.root,self.store)

    def test_roundtrip_and_auth_files_excluded(self):
        path = self.audio()
        atomic(self.root / 'state/auth.json', b'{"cookies":"secret"}')
        atomic(self.root / 'podcast-work/task_1/storage-state.json', b'secret')
        result = save(self.root, self.store)
        self.assertEqual(result['staged_tasks'], ['task_1'])
        result = restore(self.target, self.store)
        self.assertEqual(result['restored_audio'], ['task_1'])
        self.assertEqual((self.target / 'podcast-work/task_1/episode.mp3').read_bytes(), path.read_bytes())
        self.assertFalse((self.target / 'state/auth.json').exists())
        self.assertEqual((self.target / 'state/podcast_sources/task_1/source.md').read_text(encoding='utf-8'), '正文 text')

    def test_auth_fields_rejected(self):
        atomic(self.root / 'podcast-work/task_1/checkpoint.json', b'{"cookies": []}')
        with self.assertRaisesRegex(ValueError, 'authentication'):
            save(self.root, self.store)

    def test_missing_audio_preserves_checkpoint(self):
        self.audio()
        save(self.root, self.store)
        for key in self.store.list('staging/'):
            self.store.delete(key)
        result = restore(self.target, self.store)
        self.assertEqual(result['missing_audio'], ['task_1'])
        self.assertFalse(result['resubmission_allowed'])
        self.assertTrue((self.target / 'podcast-work/task_1/checkpoint.json').exists())

    def test_expired_audio_removed_but_checkpoint_restored(self):
        self.audio()
        save(self.root, self.store)
        result = restore(self.target, self.store, now=time.time() + 86401)
        self.assertEqual(result['missing_audio'], ['task_1'])
        self.assertEqual(self.store.list('staging/'), {})
        self.assertTrue((self.target / 'podcast-work/task_1/checkpoint.json').exists())

    def test_zip_traversal_rejected_before_any_write(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../escaped', 'no')
        self.store.put('data/private-state.zip', stream.getvalue())
        with self.assertRaisesRegex(ValueError, 'path'):
            restore(self.target, self.store)
        self.assertFalse(self.target.exists())

    def test_expanded_limit_and_budget(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('podcast-work/task_1/adapted-en.md', b'x' * (MAX_TEXT + 1))
        self.store.put('data/private-state.zip', stream.getvalue())
        with self.assertRaisesRegex(ValueError, 'size limit'):
            restore(self.target, self.store)
        self.store.delete('data/private-state.zip')
        self.store.put('audio/published.mp3', b'x' * 1000)
        with self.assertRaisesRegex(ValueError, 'quota_wait'):
            save(self.root, self.store, max_bytes=1000)
        self.assertEqual(self.store.get('audio/published.mp3'), b'x' * 1000)

    def test_single_active_audio_and_unreferenced_cleanup(self):
        self.audio()
        atomic(self.root / 'podcast-work/task_2/episode.mp3', b'other')
        with self.assertRaisesRegex(ValueError, 'one active'):
            save(self.root, self.store)
        (self.root / 'podcast-work/task_2/episode.mp3').unlink()
        self.store.put('staging/unreferenced.mp3', b'old')
        save(self.root, self.store)
        self.assertIsNone(self.store.get('staging/unreferenced.mp3'))
        self.assertEqual(len(self.store.list('staging/')), 1)

    def test_audio_hash_corruption_blocks_restore(self):
        self.audio()
        save(self.root, self.store)
        key = next(iter(self.store.list('staging/')))
        self.store.put(key, b'corrupt')
        with self.assertRaisesRegex(ValueError, 'hash'):
            restore(self.target, self.store)
        self.assertFalse((self.target / 'state/podcast_queue.json').exists())

    def test_zip_symlink_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            info = zipfile.ZipInfo('podcast-work/task_1/input.json')
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            archive.writestr(info, '../../../auth.json')
        self.store.put('data/private-state.zip', stream.getvalue())
        with self.assertRaisesRegex(ValueError, 'linked'):
            restore(self.target, self.store)

    def test_save_discards_expired_audio_only(self):
        path = self.audio()
        os.utime(path, (time.time() - 86401, time.time() - 86401))
        result = save(self.root, self.store)
        self.assertEqual(result['staged_tasks'], [])
        self.assertFalse(path.exists())
        self.assertTrue((self.root / 'podcast-work/task_1/checkpoint.json').exists())

    def test_segment_checkpoints_and_audio_roundtrip(self):
        atomic(self.root / 'podcast-work/task_1/segments.json', b'{"completedSegments":1}')
        for number in (1, 2):
            base = self.root / f'podcast-work/task_1/segment-{number:03d}'
            atomic(base / 'source.json', b'{"text":"part"}')
            atomic(base / 'checkpoint.json', f'{{"messageSid":"sid{number}"}}'.encode())
            atomic(base / 'rewrite.json', b'{"rewrite":"spoken part"}')
            atomic(base / 'episode.mp3', f'audio {number}'.encode())
        result = save(self.root, self.store)
        self.assertEqual(result['staged_tasks'], ['task_1'])
        self.assertEqual(result['staged_files'], 2)
        restore(self.target, self.store)
        for number in (1, 2):
            base = self.target / f'podcast-work/task_1/segment-{number:03d}'
            self.assertIn(f'sid{number}', (base / 'checkpoint.json').read_text())
            self.assertEqual((base / 'episode.mp3').read_bytes(), f'audio {number}'.encode())
        self.assertTrue((self.target / 'podcast-work/task_1/segments.json').exists())

    def test_missing_segment_audio_keeps_every_sid(self):
        for number in (1, 2):
            base = self.root / f'podcast-work/task_1/segment-{number:03d}'
            atomic(base / 'checkpoint.json', f'{{"messageSid":"sid{number}"}}'.encode())
            atomic(base / 'episode.mp3', f'audio {number}'.encode())
        save(self.root, self.store)
        self.store.delete(next(iter(self.store.list('staging/'))))
        result = restore(self.target, self.store)
        self.assertEqual(result['missing_audio'], ['task_1'])
        self.assertFalse(result['resubmission_allowed'])
        self.assertEqual(len(list((self.target / 'podcast-work').glob('*/segment-*/checkpoint.json'))), 2)

    def test_finished_episode_retires_remote_segments(self):
        base = self.root / 'podcast-work/task_1/segment-001'
        atomic(base / 'checkpoint.json', b'{"messageSid":"keep"}')
        atomic(base / 'episode.mp3', b'segment audio')
        save(self.root, self.store)
        old = next(iter(self.store.list('staging/')))
        (base / 'episode.mp3').unlink()
        self.audio()
        save(self.root, self.store)
        self.assertIsNone(self.store.get(old))
        self.assertEqual(len(self.store.list('staging/')), 1)
        restore(self.target, self.store)
        self.assertTrue((self.target / 'podcast-work/task_1/segment-001/checkpoint.json').exists())

    def test_segment_audio_path_is_not_arbitrary(self):
        atomic(self.root / 'podcast-work/task_1/segment-invalid/episode.mp3', b'audio')
        with self.assertRaisesRegex(ValueError, 'invalid task'):
            save(self.root, self.store)

    def test_audio_upload_failure_still_persists_new_checkpoint(self):
        self.audio()
        original = self.store.put
        def fail_audio(key, data, content_type='application/octet-stream'):
            if key.startswith('staging/'):
                raise OSError('simulated audio interruption')
            original(key, data, content_type)
        self.store.put = fail_audio
        with self.assertRaises(OSError):
            save(self.root, self.store)
        result = restore(self.target, self.store)
        self.assertEqual(result['missing_audio'], ['task_1'])
        self.assertIn('existing', (self.target / 'podcast-work/task_1/checkpoint.json').read_text())

    def test_restore_rejects_extra_local_task_without_deletion(self):
        save(self.root, self.store)
        extra = self.target / 'podcast-work/old-task/checkpoint.json'
        atomic(extra, b'{"messageSid":"do-not-delete"}')
        with self.assertRaisesRegex(ValueError, 'local state divergence'):
            restore(self.target, self.store)
        self.assertEqual(extra.read_bytes(), b'{"messageSid":"do-not-delete"}')
        self.assertFalse((self.target / 'state/podcast_queue.json').exists())

    def test_restore_same_package_is_idempotent(self):
        self.audio()
        save(self.root, self.store)
        first = restore(self.target, self.store)
        second = restore(self.target, self.store)
        self.assertEqual(first, second)

    def test_zip_upload_readback_is_verified(self):
        original = self.store.put
        def corrupt_zip(key, data, content_type='application/octet-stream'):
            original(key, b'corrupt' if key == 'data/private-state.zip' else data, content_type)
        self.store.put = corrupt_zip
        self.audio()
        with self.assertRaisesRegex(ValueError, 'upload verification'):
            save(self.root, self.store)
        self.assertEqual(self.store.list('staging/'), {})


if __name__ == '__main__':
    unittest.main()
