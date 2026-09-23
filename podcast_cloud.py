"""Cloud entry point with private state recovery and fixed, non-sensitive logs."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from podcast_sources import atomic_write, queue_lock

BASE = Path(__file__).resolve().parent


class CloudError(Exception):
    """Only fixed application-defined codes are exposed by the CLI."""


def preflight(action, env):
    backend = env.get('PODCAST_STORE_BACKEND', 's3')
    if backend not in {'s3', 'http'}:
        raise CloudError('preflight_invalid_store_backend')
    required = (['PODCAST_SERVICE_ORIGIN', 'PODCAST_STORE_TOKEN'] if backend == 'http'
                else ['AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'PODCAST_BUCKET', 'PODCAST_S3_ENDPOINT'])
    if action in {'capture', 'resume', 'probe', 'automatic'}:
        required.append('PI_STORAGE_STATE_JSON')
    if action in {'capture', 'automatic'}:
        required.append('X_SESSION_JSON')
    if action in {'publish', 'automatic'}:
        required.append('PODCAST_BASE_URL')
    if any(not env.get(key, '').strip() for key in required):
        raise CloudError('preflight_missing_configuration')
    if env.get('GITHUB_ACTIONS') == 'true':
        owner = env.get('PODCAST_ALLOWED_OWNER', '').lower()
        actual = env.get('GITHUB_REPOSITORY_OWNER', '').lower()
        if not owner or owner != actual:
            raise CloudError('preflight_repository_owner')
        try:
            repository = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))['repository']
            if repository.get('private') is not False or repository.get('owner', {}).get('login', '').lower() != owner:
                raise CloudError('preflight_repository_visibility')
        except (KeyError, OSError, ValueError, TypeError):
            raise CloudError('preflight_repository_visibility') from None


def child(command, env, failure_code, timeout=3900):
    try:
        result = subprocess.run(command, cwd=BASE, env=env, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise CloudError(failure_code) from None
    if result.returncode:
        raise CloudError(failure_code)


def probe(env):
    try:
        result = subprocess.run(['node', str(BASE / 'podcast/pi_probe.mjs')], cwd=BASE,
                                env=env, capture_output=True, text=True, timeout=180)
        status = json.loads(result.stdout).get('status')
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        raise CloudError('pi_cloud_probe_error') from None
    blockers = {
        'needs_login': 'pi_cloud_needs_login',
        'needs_onboarding': 'pi_cloud_needs_onboarding',
        'access_challenge': 'pi_cloud_access_challenge',
        'rate_limited': 'pi_cloud_rate_limited',
    }
    if status in blockers and result.returncode == 2:
        raise CloudError(blockers[status])
    if status != 'available' or result.returncode != 0:
        raise CloudError('pi_cloud_probe_error')


def write_auth(raw, directory, created):
    try:
        state = json.loads(raw)
        if not isinstance(state, dict) or not isinstance(state.get('cookies'), list) or not isinstance(state.get('origins'), list):
            raise ValueError()
    except (ValueError, TypeError):
        raise CloudError('preflight_invalid_auth_state') from None
    fd, name = tempfile.mkstemp(prefix='podcast-auth-', suffix='.json', dir=directory)
    path = Path(name)
    created.append(path)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(state, stream)
    path.chmod(0o600)
    return str(path)


def orchestrate(action, task=None, *, root=BASE, env=None):
    env = dict(os.environ if env is None else env)
    if action not in {'capture', 'resume', 'publish', 'probe', 'automatic'}:
        raise CloudError('invalid_action')
    if action in {'resume', 'publish'} and not task:
        raise CloudError('task_required')
    if task and not re.fullmatch(r'x-\d+-[a-f0-9]{64}-v\d+', task):
        raise CloudError('invalid_task')
    preflight(action, env)
    backend = env.get('PODCAST_STORE_BACKEND', 's3')
    env['PODCAST_STORE_BACKEND'] = backend
    root = Path(root).resolve()
    created = []
    restored = False
    failure = None
    try:
        auth_dir = Path(env.get('RUNNER_TEMP') or tempfile.gettempdir()).resolve()
        auth_dir.mkdir(parents=True, exist_ok=True)
        if action in {'capture', 'resume', 'probe', 'automatic'}:
            env['PI_STORAGE_STATE'] = write_auth(env['PI_STORAGE_STATE_JSON'], auth_dir, created)
            env['PI_HEADLESS'] = 'true'
            env.setdefault('PI_CHANNEL', 'chromium')
        if action in {'capture', 'automatic'}:
            env['SESSION_FILE'] = write_auth(env['X_SESSION_JSON'], auth_dir, created)
        # Raw secrets are not inherited by browser/child commands unnecessarily.
        env.pop('X_SESSION_JSON', None)
        env.pop('PI_STORAGE_STATE_JSON', None)
        env['PODCAST_SOURCE_DIR'] = str(root / 'state/podcast_sources')
        env['PODCAST_STATE_FILE'] = str(root / 'state/podcast_queue.json')
        env['PODCAST_DURABLE_SYNC'] = 'true'
        env['PODCAST_ROOT'] = str(root)
        env['PODCAST_PYTHON'] = sys.executable
        if action in {'probe', 'automatic'}:
            # No article submission or state mutation during cloud compatibility checks.
            probe(env)
            if action == 'probe':
                return {'status': 'completed', 'action': 'probe'}
        sync = [sys.executable, str(BASE / 'podcast_sync.py')]
        sync_args = ['--root', str(root), '--store', backend]
        child([*sync, 'restore', *sync_args], env, 'restore_failed', timeout=300)
        restored = True
        queue_path = root / 'state/podcast_queue.json'
        with queue_lock(queue_path):
            if not queue_path.exists():
                atomic_write(queue_path, '{"schema_version":1,"tasks":{}}\n')
        if action in {'capture', 'automatic'}:
            child([sys.executable, str(BASE / 'podcast_bookmarks.py'), '--count', '15'], env, 'bookmark_scan_failed', timeout=1800)
        runner = [sys.executable, str(BASE / 'podcast_run.py'), '--queue', str(root / 'state/podcast_queue.json'), '--work', str(root / 'podcast-work'), '--budget', str(root / 'state/podcast_budget.json')]
        if task:
            runner.extend(['--task', task])
        if action == 'resume':
            runner.append('--resume')
        elif action == 'publish':
            runner.extend(['--publish-ready', '--store', backend])
        if action == 'automatic':
            child([sys.executable, str(BASE / 'podcast_automatic.py')], env, 'automatic_task_needs_attention')
        else:
            child(runner, env, 'podcast_task_failed')
    except CloudError as error:
        failure = str(error)
    except Exception:
        failure = 'cloud_operation_failed'
    finally:
        if restored:
            try:
                child([sys.executable, str(BASE / 'podcast_sync.py'), 'save', '--root', str(root), '--store', backend], env, 'state_save_failed', timeout=300)
            except Exception:
                failure = 'state_save_failed'
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                failure = 'auth_cleanup_failed'
        if failure == 'auth_cleanup_failed':
            raise CloudError(failure)
    if failure:
        raise CloudError(failure)
    return {'status': 'completed', 'action': action}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default=os.getenv('PODCAST_ACTION'), choices=['capture', 'resume', 'publish', 'probe', 'automatic'])
    parser.add_argument('--task', default=os.getenv('PODCAST_TASK') or None)
    parser.add_argument('--root', type=Path, default=BASE)
    args = parser.parse_args()
    try:
        result = orchestrate(args.action, args.task, root=args.root)
    except CloudError as error:
        print(json.dumps({'status': 'failed', 'code': str(error)}))
        return 1
    except Exception:
        print('{"status":"failed","code":"cloud_operation_failed"}')
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
