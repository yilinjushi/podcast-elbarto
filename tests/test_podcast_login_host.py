import os
from pathlib import Path
import unittest
from unittest.mock import patch
from podcast_login_host import browser_environment


class LoginHostTests(unittest.TestCase):
    def test_children_receive_no_cloud_or_github_credentials(self):
        with patch.dict(os.environ,{'AWS_SECRET_ACCESS_KEY':'private','PODCAST_LOGIN_KEY':'private','GITHUB_TOKEN':'private','PATH':'safe-path'},clear=True):
            env=browser_environment(Path('/tmp/owned-login'))
        self.assertEqual(env['PATH'],'safe-path')
        self.assertEqual(env['DISPLAY'],':99')
        self.assertFalse(any('private' in value for value in env.values()))
        self.assertNotIn('AWS_SECRET_ACCESS_KEY',env)
        self.assertTrue(env['PI_LOGIN_PROFILE'].endswith('profile'))


if __name__=='__main__':unittest.main()
