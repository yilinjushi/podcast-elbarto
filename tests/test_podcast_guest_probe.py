import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import podcast_guest_probe as probe


class GuestProbeTests(unittest.TestCase):
    def test_browser_receives_no_store_credentials_or_durable_sync(self):
        env = {key: 'private' for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
               'PI_STORAGE_STATE_JSON', 'X_SESSION_JSON', 'PODCAST_LOGIN_KEY', 'PODCAST_DURABLE_SYNC')}
        env['PATH'] = 'node-path'
        actual = probe.browser_environment(env, 'auth.json')
        self.assertEqual(actual, {'PATH': 'node-path', 'PI_STORAGE_STATE': 'auth.json',
                                 'PI_HEADLESS': 'true', 'PI_CHANNEL': 'chromium'})

    def exercise(self, *, invalid=False, failure=False):
        with tempfile.TemporaryDirectory() as directory:
            env = {'RUNNER_TEMP': directory, 'GITHUB_RUN_ID': '123', 'PODCAST_BUCKET': 'test',
                   'PODCAST_S3_ENDPOINT': 'https://example.invalid', 'AWS_ACCESS_KEY_ID': 'secret',
                   'AWS_SECRET_ACCESS_KEY': 'secret',
                   'PI_STORAGE_STATE_JSON': '{"cookies":[],"origins":[]}'}
            writes = []
            class Store:
                def __init__(self, bucket, prefix, endpoint):
                    self.prefix = prefix
                    self.asserted = prefix == 'podcast/staging/guest-probe/123'
                    if not self.asserted: raise AssertionError(prefix)
                def put(self, key, data, content_type):
                    writes.append((key, data))
            def child(command, **kwargs):
                self.assertNotIn('AWS_ACCESS_KEY_ID', kwargs['env'])
                self.assertTrue(Path(kwargs['env']['PI_STORAGE_STATE']).is_file())
                output = Path(command[-1]); output.mkdir()
                audio = b'test-audio'
                (output / 'episode.mp3').write_bytes(audio)
                (output / 'result.json').write_text(json.dumps({
                    'guestVerified': not invalid, 'decoded': True,
                    'textValidation': {'passed': True}, 'durationSeconds': 10,
                    'bytes': len(audio), 'audioSha256': hashlib.sha256(audio).hexdigest(),
                }))
                return type('Result', (), {'returncode': int(failure), 'stdout': '{"code":"PI_NEEDS_ONBOARDING"}'})()
            with patch.object(probe.subprocess, 'run', side_effect=child):
                if invalid or failure:
                    with self.assertRaises(ValueError): probe.run(env, Store)
                    self.assertEqual(writes, [])
                else:
                    self.assertEqual(probe.run(env, Store)['status'], 'completed')
                    self.assertEqual([key for key, _ in writes], ['episode.mp3', 'result.json'])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_sample_only_writes_to_expiring_staging(self): self.exercise()
    def test_logged_in_or_unverified_sample_never_uploaded(self): self.exercise(invalid=True)
    def test_capture_failure_never_uploaded(self): self.exercise(failure=True)
