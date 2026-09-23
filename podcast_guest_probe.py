"""One fixed guest audio sample; no queue, feed, bookmark or production writes."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from podcast_cloud import preflight, write_auth
from podcast_publish import S3Store

BASE = Path(__file__).resolve().parent


def browser_environment(env, auth):
    keep = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'PLAYWRIGHT_BROWSERS_PATH',
            'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'DISPLAY'}
    child = {key: value for key, value in env.items() if key in keep}
    child.update(PI_STORAGE_STATE=auth, PI_HEADLESS='false' if env.get('DISPLAY') else 'true', PI_CHANNEL='chromium')
    return child


def run(env=None, store_factory=S3Store):
    env = dict(os.environ if env is None else env)
    preflight('probe', env)
    run_id = env.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'[0-9]+', run_id):
        raise ValueError('invalid_run')
    # Existing one-day staging lifecycle applies; never touch the feed namespace.
    store = store_factory(env['PODCAST_BUCKET'], 'podcast/staging/guest-probe/' + run_id,
                          env['PODCAST_S3_ENDPOINT'])
    with tempfile.TemporaryDirectory(prefix='pi-guest-audio-', dir=env.get('RUNNER_TEMP')) as temp:
        root = Path(temp)
        root.chmod(0o700)
        auth = write_auth(env['PI_STORAGE_STATE_JSON'], root, [])
        result = subprocess.run(['node', str(BASE / 'podcast/pi_guest_audio_probe.mjs'), str(root / 'audio')],
                                env=browser_environment(env, auth), cwd=BASE,
                                capture_output=True, text=True, timeout=600)
        if result.returncode:
            failure = json.loads(result.stdout).get('code')
            raise ValueError(failure if failure in {'PI_ACCESS_CHALLENGE', 'PI_NEEDS_ONBOARDING',
                                                   'PI_GUEST_NOT_VERIFIED'} else 'guest_audio_unavailable')
        report = json.loads((root / 'audio/result.json').read_text(encoding='utf-8'))
        audio = (root / 'audio/episode.mp3').read_bytes()
        if (report.get('guestVerified') is not True or report.get('decoded') is not True
                or report.get('textValidation', {}).get('passed') is not True
                or not 0 < len(audio) <= 5 * 1024 * 1024
                or report.get('audioSha256') != hashlib.sha256(audio).hexdigest()
                or not 1 <= report.get('durationSeconds', 0) <= 120):
            raise ValueError('guest_audio_validation_failed')
        summary = {key: report[key] for key in ('guestVerified', 'decoded', 'bytes', 'durationSeconds', 'audioSha256')}
        summary.update(status='completed', action='guest-audio', run_id=run_id)
        store.put('episode.mp3', audio, 'audio/mpeg')
        store.put('result.json', json.dumps(summary).encode(), 'application/json')
        return summary


if __name__ == '__main__':
    try:
        print(json.dumps(run()))
    except Exception as error:
        code = str(error) if str(error) in {'PI_ACCESS_CHALLENGE', 'PI_NEEDS_ONBOARDING',
                                           'PI_GUEST_NOT_VERIFIED', 'guest_audio_unavailable'} else 'guest_audio_probe_failed'
        print(json.dumps({'status': 'failed', 'code': code}))
        raise SystemExit(1)
