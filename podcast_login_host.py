"""Ephemeral, user-operated Pi login desktop. No credentials or URLs in logs."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import tempfile
import time

from aiohttp import web
from podcast_login_crypto import seal
from podcast_publish import S3Store
from podcast_login_portal import create_app

BASE = Path(__file__).resolve().parent


def browser_environment(directory):
    keep = {'PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'XDG_RUNTIME_DIR', 'PLAYWRIGHT_BROWSERS_PATH'}
    env = {key:value for key,value in os.environ.items() if key in keep}
    env.update(DISPLAY=':99', PI_LOGIN_PROFILE=str(directory/'profile'),
               PI_LOGIN_SAVE_REQUEST=str(directory/'save-request'),
               PI_LOGIN_STATE_OUT=str(directory/'state.json'),
               PI_LOGIN_STATUS_OUT=str(directory/'status.json'), PI_LOGIN_TTL_SECONDS='1800')
    return env


async def host():
    run_id = os.environ.get('GITHUB_RUN_ID','')
    if os.name != 'posix' or not re.fullmatch(r'\d+',run_id):
        raise RuntimeError('login_configuration_failed')
    for name in ('AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY'):
        os.environ[name] = os.environ[name].strip()
    store = S3Store(os.environ['PODCAST_BUCKET'], 'podcast/login/'+run_id, os.environ['PODCAST_S3_ENDPOINT'])
    processes = []
    app_runner = None
    published_connection = False
    # This function owns this uniquely-created directory and every process below.
    with tempfile.TemporaryDirectory(prefix='podcast-login-',dir=os.environ.get('RUNNER_TEMP')) as temporary:
        directory = Path(temporary).resolve()
        directory.chmod(0o700)
        env = browser_environment(directory)
        def spawn(command, output=subprocess.DEVNULL):
            process = subprocess.Popen(command,env=env,stdout=output,stderr=output,
                                       stdin=subprocess.DEVNULL,start_new_session=True)
            processes.append(process)
            return process
        try:
            spawn(['Xvfb', ':99', '-screen', '0', '1440x1000x24', '-nolisten', 'tcp'])
            for _ in range(50):
                if Path('/tmp/.X11-unix/X99').exists():break
                await asyncio.sleep(.1)
            else:raise RuntimeError('display_start_failed')
            spawn(['x11vnc','-display',':99','-localhost','-rfbport','5900','-nopw','-forever','-shared','-noxdamage'])
            browser = spawn(['node',str(BASE/'podcast/pi_interactive.mjs')])
            token = secrets.token_urlsafe(32)
            expires = time.time()+1800
            origin = {'origin':''}
            app = create_app(token=token,expires_at=expires,save_request=directory/'save-request',
                             status_file=directory/'status.json',novnc_root=BASE/'podcast/node_modules/@novnc/novnc',origin=origin)
            app_runner = web.AppRunner(app,access_log=None)
            await app_runner.setup()
            await web.TCPSite(app_runner,'127.0.0.1',6080).start()
            log_path = directory/'tunnel.log'
            with log_path.open('wb') as log:
                tunnel = spawn(['cloudflared','tunnel','--no-autoupdate','--url','http://127.0.0.1:6080','--protocol','http2'],log)
                url = None
                for _ in range(90):
                    if tunnel.poll() is not None:raise RuntimeError('tunnel_start_failed')
                    found = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',log_path.read_text(errors='replace'))
                    if found:url=found.group(0);break
                    await asyncio.sleep(1)
                if not url:raise RuntimeError('tunnel_start_failed')
                origin['origin'] = url
                connection = seal({'kind':'connection','run_id':run_id,'expires_at':expires,'url':url,'token':token})
                try:await asyncio.to_thread(store.put,'connection.enc',json.dumps(connection).encode())
                except Exception:raise RuntimeError('private_handoff_storage_failed') from None
                published_connection = True
                print('{"status":"waiting_for_user_login"}',flush=True)
                while time.time()<expires:
                    if tunnel.poll() is not None:raise RuntimeError('tunnel_stopped')
                    if browser.poll() is not None:
                        if browser.returncode or not (directory/'state.json').is_file():
                            raise RuntimeError('login_not_saved')
                        raw = (directory/'state.json').read_bytes()
                        if len(raw)>5*1024*1024:raise RuntimeError('login_state_too_large')
                        state = json.loads(raw)
                        payload = seal({'kind':'state','run_id':run_id,'expires_at':time.time()+1800,'storage_state':state})
                        try:await asyncio.to_thread(store.put,'state.enc',json.dumps(payload).encode())
                        except Exception:raise RuntimeError('private_session_storage_failed') from None
                        print('{"status":"encrypted_session_saved"}',flush=True)
                        await asyncio.sleep(15)
                        return
                    await asyncio.sleep(1)
                raise RuntimeError('login_expired')
        finally:
            if app_runner:
                await app_runner.cleanup()
            for process in reversed(processes):
                if process.poll() is None:
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
            for process in reversed(processes):
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:os.killpg(process.pid,signal.SIGKILL)
                    except ProcessLookupError:pass
                    process.wait(timeout=5)
            if published_connection:
                try:await asyncio.to_thread(store.delete,'connection.enc')
                except Exception:pass # Encrypted metadata expires even if network cleanup fails.


if __name__=='__main__':
    try:asyncio.run(host())
    except Exception as error:
        allowed = {'login_configuration_failed','display_start_failed','tunnel_start_failed','tunnel_stopped','login_not_saved','login_state_too_large','login_expired','private_handoff_storage_failed','private_session_storage_failed'}
        print(json.dumps({'status':'login_session_closed_without_completion','code':str(error) if str(error) in allowed else 'login_setup_failed'}))
        raise SystemExit(1)
