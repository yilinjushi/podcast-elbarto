import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import podcast_cloud as cloud


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / 'temp'
        self.env = {key:'secret-do-not-print' for key in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','PODCAST_BUCKET','PODCAST_S3_ENDPOINT','PODCAST_BASE_URL']}
        self.env.update({'RUNNER_TEMP':str(self.auth), 'X_SESSION_JSON':json.dumps({'cookies':[], 'origins':[]}), 'PI_STORAGE_STATE_JSON':json.dumps({'cookies':[], 'origins':[]})})
        self.task = 'x-123-' + 'a' * 64 + '-v1'
        self.calls = []

    def subprocess(self, command, **kwargs):
        self.calls.append(command)
        self.assertTrue(kwargs['capture_output'])
        self.assertNotIn('X_SESSION_JSON', kwargs['env'])
        self.assertNotIn('PI_STORAGE_STATE_JSON', kwargs['env'])
        self.assertEqual(kwargs['env']['PODCAST_DURABLE_SYNC'], 'true')
        self.assertEqual(kwargs['env']['PODCAST_ROOT'], str(self.root.resolve()))
        self.assertTrue(kwargs['env']['PODCAST_PYTHON'])
        if 'PI_STORAGE_STATE' in kwargs['env']:
            self.assertTrue(Path(kwargs['env']['PI_STORAGE_STATE']).exists())
        output = '{"status":"available"}' if Path(command[1]).name == 'pi_probe.mjs' else 'private title https://private/article secret-do-not-print'
        return type('Result', (), {'returncode':0,'stdout':output,'stderr':'secret-do-not-print'})()

    def test_capture_restores_scans_runs_saves_and_cleans_auth(self):
        with patch.object(cloud.subprocess,'run',side_effect=self.subprocess):
            cloud.orchestrate('capture', root=self.root, env=self.env)
        self.assertEqual([Path(call[1]).name for call in self.calls], ['podcast_sync.py','podcast_bookmarks.py','podcast_run.py','podcast_sync.py'])
        self.assertIn('restore',self.calls[0])
        self.assertIn('--count',self.calls[1])
        self.assertIn('save',self.calls[-1])
        self.assertEqual(list(self.auth.iterdir()), [])
        self.assertEqual(json.loads((self.root/'state/podcast_queue.json').read_text()), {'schema_version':1,'tasks':{}})

    def test_runner_failure_still_saves_without_leaking_output(self):
        def fail(command, **kwargs):
            result = self.subprocess(command, **kwargs)
            if Path(command[1]).name == 'podcast_run.py':
                result.returncode = 1
            return result
        output = io.StringIO()
        with redirect_stdout(output), patch.object(cloud.subprocess,'run',side_effect=fail):
            with self.assertRaisesRegex(cloud.CloudError,'podcast_task_failed'):
                cloud.orchestrate('capture', root=self.root, env=self.env)
        self.assertIn('save',self.calls[-1])
        self.assertEqual(output.getvalue(),'')
        self.assertEqual(list(self.auth.iterdir()), [])

    def test_restore_failure_never_saves_or_runs(self):
        def fail(command, **kwargs):
            result = self.subprocess(command, **kwargs)
            result.returncode = 1
            return result
        with patch.object(cloud.subprocess,'run',side_effect=fail):
            with self.assertRaisesRegex(cloud.CloudError,'restore_failed'):
                cloud.orchestrate('capture', root=self.root, env=self.env)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(list(self.auth.iterdir()), [])
        self.assertFalse((self.root/'state/podcast_queue.json').exists())

    def test_resume_publish_never_scan_or_approve(self):
        for action, flag in [('resume','--resume'),('publish','--publish-ready')]:
            self.calls = []
            with patch.object(cloud.subprocess,'run',side_effect=self.subprocess):
                cloud.orchestrate(action, self.task, root=self.root, env=self.env)
            self.assertEqual(len(self.calls),3)
            self.assertIn(flag,self.calls[1])
            self.assertNotIn('--approve',self.calls[1])
            self.assertFalse(any('podcast_bookmarks.py' in part for call in self.calls for part in call))

    def test_preflight_owner_and_visibility_are_fail_closed(self):
        event = self.root/'event.json'
        event.write_text(json.dumps({'repository':{'private':False,'owner':{'login':'podcast-owner'}}}))
        self.env.update({'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY_OWNER':'podcast-owner','PODCAST_ALLOWED_OWNER':'podcast-owner','GITHUB_EVENT_PATH':str(event)})
        cloud.preflight('capture',self.env)
        self.env['GITHUB_REPOSITORY_OWNER'] = 'foreign-owner'
        with self.assertRaises(cloud.CloudError):
            cloud.preflight('capture',self.env)
        self.env['GITHUB_REPOSITORY_OWNER'] = 'podcast-owner'
        event.write_text(json.dumps({'repository':{'private':True,'owner':{'login':'podcast-owner'}}}))
        with self.assertRaises(cloud.CloudError):
            cloud.preflight('capture',self.env)

    def test_only_created_auth_files_removed_after_bad_second_auth(self):
        self.auth.mkdir()
        existing = self.auth/'unrelated.json'
        existing.write_text('keep')
        self.env['X_SESSION_JSON'] = 'not-json-secret'
        with patch.object(cloud.subprocess,'run') as run:
            with self.assertRaisesRegex(cloud.CloudError,'invalid_auth'):
                cloud.orchestrate('capture',root=self.root,env=self.env)
            run.assert_not_called()
        self.assertEqual(list(self.auth.iterdir()),[existing])

    def test_http_backend_requires_only_scoped_store_credentials(self):
        for name in ['AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','PODCAST_BUCKET','PODCAST_S3_ENDPOINT']:
            self.env.pop(name)
        self.env.update({'PODCAST_STORE_BACKEND':'http','PODCAST_SERVICE_ORIGIN':'https://podcast.example','PODCAST_STORE_TOKEN':'private-scoped-token'})
        with patch.object(cloud.subprocess,'run',side_effect=self.subprocess):
            cloud.orchestrate('publish',self.task,root=self.root,env=self.env)
        for command in self.calls:
            self.assertEqual(command[command.index('--store')+1],'http')
        self.env.pop('PODCAST_STORE_TOKEN')
        with self.assertRaisesRegex(cloud.CloudError,'missing_configuration'):
            cloud.preflight('publish',self.env)

    def test_unknown_store_backend_fails_closed(self):
        self.env['PODCAST_STORE_BACKEND'] = 'unexpected'
        with self.assertRaisesRegex(cloud.CloudError,'invalid_store_backend'):
            cloud.preflight('capture',self.env)

    def test_probe_never_scans_submits_or_mutates_store(self):
        with patch.object(cloud.subprocess,'run',side_effect=self.subprocess):
            result = cloud.orchestrate('probe',root=self.root,env=self.env)
        self.assertEqual(result, {'status':'completed','action':'probe'})
        self.assertEqual(len(self.calls),1)
        self.assertEqual(Path(self.calls[0][1]).name,'pi_probe.mjs')
        self.assertEqual(list(self.auth.iterdir()), [])

    def test_failed_probe_returns_fixed_code_and_cleans_auth(self):
        def fail(command, **kwargs):
            result = self.subprocess(command, **kwargs)
            result.returncode = 1
            return result
        with patch.object(cloud.subprocess,'run',side_effect=fail):
            with self.assertRaisesRegex(cloud.CloudError,'pi_cloud_probe_error'):
                cloud.orchestrate('probe',root=self.root,env=self.env)
        self.assertEqual(len(self.calls),1)
        self.assertEqual(list(self.auth.iterdir()), [])

    def test_probe_cleanup_failure_is_not_reported_as_success(self):
        with patch.object(cloud.subprocess,'run',side_effect=self.subprocess), patch.object(Path,'unlink',side_effect=OSError('private details')):
            with self.assertRaisesRegex(cloud.CloudError,'^auth_cleanup_failed$'):
                cloud.orchestrate('probe',root=self.root,env=self.env)

    def test_automatic_probes_before_restoring_scanning_and_running(self):
        with patch.object(cloud.subprocess,'run',side_effect=self.subprocess):
            cloud.orchestrate('automatic',root=self.root,env=self.env)
        self.assertEqual([Path(call[1]).name for call in self.calls], ['pi_probe.mjs','podcast_sync.py','podcast_bookmarks.py','podcast_automatic.py','podcast_sync.py'])

    def test_login_failure_is_classified_without_exposing_browser_details(self):
        def login(command, **kwargs):
            result = self.subprocess(command, **kwargs)
            result.returncode = 2
            result.stdout = '{"status":"needs_login"}'
            return result
        with patch.object(cloud.subprocess,'run',side_effect=login):
            with self.assertRaisesRegex(cloud.CloudError,'^pi_cloud_needs_login$'):
                cloud.orchestrate('probe',root=self.root,env=self.env)
        self.assertEqual(list(self.auth.iterdir()), [])

    def test_probe_blockers_have_distinct_fixed_codes(self):
        cases = {
            'needs_onboarding': 'pi_cloud_needs_onboarding',
            'access_challenge': 'pi_cloud_access_challenge',
            'rate_limited': 'pi_cloud_rate_limited',
        }
        for status, code in cases.items():
            self.calls = []
            def blocked(command, **kwargs):
                result = self.subprocess(command, **kwargs)
                result.returncode = 2
                result.stdout = json.dumps({'status': status})
                return result
            with self.subTest(status=status), patch.object(cloud.subprocess, 'run', side_effect=blocked):
                with self.assertRaisesRegex(cloud.CloudError, f'^{code}$'):
                    cloud.orchestrate('probe', root=self.root, env=self.env)
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(list(self.auth.iterdir()), [])

    def test_probe_status_and_return_code_must_agree(self):
        for status, returncode in [('available', 2), ('needs_onboarding', 0), ('unknown', 2)]:
            self.calls = []
            def inconsistent(command, **kwargs):
                result = self.subprocess(command, **kwargs)
                result.returncode = returncode
                result.stdout = json.dumps({'status': status})
                return result
            with self.subTest(status=status, returncode=returncode), patch.object(cloud.subprocess, 'run', side_effect=inconsistent):
                with self.assertRaisesRegex(cloud.CloudError, '^pi_cloud_probe_error$'):
                    cloud.orchestrate('probe', root=self.root, env=self.env)
            self.assertEqual(list(self.auth.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
