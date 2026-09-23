"""Ephemeral, loopback-only noVNC login portal. Never enable HTTP access logs.

The owner process supplies a changing HTTPS tunnel origin and runs this app with
``web.run_app(app, host='127.0.0.1', port=6080, access_log=None, print=None)``.
Only the owner process may persist browser credentials; this module only creates
a save-request marker and exposes a fixed status vocabulary.
"""
import asyncio
import contextlib
import hmac
import json
from pathlib import Path, PurePosixPath
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web


COOKIE = 'podcast_login_session'
MAX_BODY = 8192
STATUS = {'saved', 'needs_login', 'waiting', 'error'}
MIME = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
        '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf'}

LANDING = '''<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Private browser login</title></head><body>
<h1>Private browser login</h1><p>Enter your temporary access token. Do not share this page or token.</p>
<form id="login"><input id="token" type="password" autocomplete="off" aria-label="Access token" required><button>Open browser</button></form><p id="message"></p>
<script nonce="NONCE">const tokenInput=document.getElementById('token');
const fragment=location.hash.slice(1); history.replaceState(null,'',location.pathname);
async function login(token){try{const r=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token}),credentials:'same-origin'});if(r.ok){location.replace('/desktop');return;}document.getElementById('message').textContent='Access unavailable.';}catch{document.getElementById('message').textContent='Access unavailable.';}finally{tokenInput.value='';}}
document.getElementById('login').addEventListener('submit',e=>{e.preventDefault();login(tokenInput.value);});
if(fragment){try{login(decodeURIComponent(fragment));}catch{document.getElementById('message').textContent='Access unavailable.';}}
</script></body></html>'''

DESKTOP = '''<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><title>Private browser</title></head><body style="margin:0">
<div><button id="save">Save browser login</button><span id="status">waiting</span></div><div id="screen" style="width:100vw;height:calc(100vh - 40px)"></div>
<script type="module" nonce="NONCE">import RFB from '/novnc/core/rfb.js';
const label=document.getElementById('status');const rfb=new RFB(document.getElementById('screen'),'wss://'+location.host+'/websockify');rfb.scaleViewport=true;rfb.resizeSession=false;
rfb.addEventListener('disconnect',()=>{label.textContent='error';});
document.getElementById('save').onclick=async()=>{try{const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',credentials:'same-origin'});if(!r.ok)label.textContent='error';}catch{label.textContent='error';}};
const poll=setInterval(async()=>{try{const r=await fetch('/status',{credentials:'same-origin'});if(!r.ok){clearInterval(poll);label.textContent='error';return;}const data=await r.json();label.textContent=['saved','needs_login','waiting','error'].includes(data.status)?data.status:'error';}catch{label.textContent='error';}},2000);
</script></body></html>'''


def create_app(token: str, expires_at: float, save_request: Path, status_file: Path,
               novnc_root: Path, origin):
    """Create the portal. ``origin`` is a callable, string, or {'origin': URL}.

    The HTTPS origin may be populated after the local server starts; an absent
    or malformed origin fails closed. Expiration is an absolute Unix timestamp.
    """
    if not isinstance(token, str) or not token:
        raise ValueError('invalid_portal_configuration')
    expires_at = float(expires_at)
    if not 0 < expires_at < float('inf'):
        raise ValueError('invalid_portal_configuration')
    save_request, status_file, novnc_root = map(Path, (save_request, status_file, novnc_root))
    sessions = {}
    sockets = set()

    def configured_origin():
        value = origin() if callable(origin) else origin.get('origin', '') if isinstance(origin, dict) else origin
        if not isinstance(value, str):
            return None
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment):
            return None
        return value

    def origin_ok(request):
        expected = configured_origin()
        return expected is not None and request.headers.get('Origin') == expected

    def authenticated(request):
        sid = request.cookies.get(COOKIE, '')
        return bool(sid) and sessions.get(sid, 0) > time.time()

    @web.middleware
    async def protect(request, handler):
        try:
            if time.time() >= expires_at:
                response = web.Response(status=410, text='Access expired.')
            elif request.method == 'POST' and not origin_ok(request):
                response = web.Response(status=403, text='Access denied.')
            elif request.path not in {'/', '/session'} and not authenticated(request):
                response = web.Response(status=401, text='Access denied.')
            else:
                response = await handler(request)
        except web.HTTPRequestEntityTooLarge:
            response = web.Response(status=413, text='Request too large.')
        except web.HTTPException as error:
            response = web.Response(status=error.status, text='Request unavailable.')
        except Exception:
            response = web.Response(status=500, text='Request unavailable.')
        if not response.prepared:
            response.headers.update({'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                                     'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff',
                                     'X-Frame-Options': 'DENY'})
        return response

    def html(template):
        nonce = secrets.token_urlsafe(24)
        return web.Response(text=template.replace('NONCE', nonce), content_type='text/html', headers={
            'Content-Security-Policy': "default-src 'none'; script-src 'self' 'nonce-" + nonce + "'; connect-src 'self'; style-src 'unsafe-inline' 'self'; font-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})

    async def landing(request):
        return html(LANDING)

    async def body(request):
        if request.content_type != 'application/json':
            raise web.HTTPBadRequest()
        raw = await request.read()
        if time.time() >= expires_at:
            raise web.HTTPGone()
        if len(raw) > MAX_BODY:
            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=len(raw))
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest() from None

    async def session(request):
        data = await body(request)
        candidate = data.get('token') if isinstance(data, dict) else None
        if not isinstance(candidate, str) or not hmac.compare_digest(candidate.encode('utf-8'), token.encode('utf-8')):
            return web.Response(status=401, text='Access denied.')
        now = time.time()
        for sid in list(sessions):
            if sessions[sid] <= now:
                sessions.pop(sid)
        if len(sessions) >= 32:
            return web.Response(status=429, text='Access unavailable.')
        sid = secrets.token_urlsafe(32)
        sessions[sid] = expires_at
        response = web.json_response({'status': 'ready'})
        response.set_cookie(COOKIE, sid, max_age=max(0, int(expires_at-now)), path='/',
                            secure=True, httponly=True, samesite='Strict')
        return response

    async def desktop(request):
        return html(DESKTOP)

    async def save(request):
        data = await body(request)
        if data != {}:
            raise web.HTTPBadRequest()
        if save_request.is_symlink():
            raise web.HTTPForbidden()
        # This is a request, not a claim that credentials have been saved.
        save_request.touch(exist_ok=True)
        return web.json_response({'status': 'waiting'})

    async def status(request):
        value = 'waiting'
        try:
            if status_file.exists():
                if status_file.is_symlink() or status_file.stat().st_size > MAX_BODY:
                    value = 'error'
                else:
                    raw = status_file.read_text(encoding='utf-8').strip()
                    try:
                        parsed = json.loads(raw)
                        value = parsed.get('status') if isinstance(parsed, dict) else parsed
                    except ValueError:
                        value = raw
        except (OSError, UnicodeError):
            value = 'error'
        return web.json_response({'status': value if isinstance(value, str) and value in STATUS else 'error'})

    async def static(request):
        raw = request.match_info['path']
        parts = PurePosixPath(raw).parts
        if not raw or '\\' in raw or '\x00' in raw or raw.startswith('/') or any(p in {'.', '..'} for p in raw.split('/')):
            raise web.HTTPNotFound()
        if novnc_root.is_symlink() or not novnc_root.is_dir():
            raise web.HTTPNotFound()
        path = novnc_root
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise web.HTTPNotFound()
        if not path.resolve().is_relative_to(novnc_root.resolve()) or not path.is_file() or path.suffix not in MIME:
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type=MIME[path.suffix])

    async def websocket(request):
        if not origin_ok(request):
            return web.Response(status=403, text='Access denied.')
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 5900), timeout=5)
        except Exception:
            return web.Response(status=503, text='Desktop unavailable.')
        if time.time() >= expires_at:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return web.Response(status=410, text='Access expired.')
        ws = web.WebSocketResponse(max_msg_size=1024*1024, heartbeat=20, protocols=('binary',))
        jobs = []
        try:
            await ws.prepare(request)
            sockets.add(ws)

            async def upstream():
                async for message in ws:
                    if message.type == WSMsgType.BINARY:
                        writer.write(message.data)
                        await writer.drain()
                    else:
                        break

            async def downstream():
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)

            async def expire():
                await asyncio.sleep(max(0, expires_at-time.time()))

            jobs = [asyncio.create_task(fn()) for fn in (upstream, downstream, expire)]
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        except Exception:
            pass
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            with contextlib.suppress(Exception):
                await ws.close()
            sockets.discard(ws)
        return ws

    async def cleanup(app):
        await asyncio.gather(*(ws.close() for ws in tuple(sockets)), return_exceptions=True)
        sessions.clear()

    app = web.Application(middlewares=[protect], client_max_size=MAX_BODY)
    app.router.add_get('/', landing)
    app.router.add_post('/session', session)
    app.router.add_get('/desktop', desktop)
    app.router.add_post('/save', save)
    app.router.add_get('/status', status)
    app.router.add_get('/novnc/{path:.*}', static)
    app.router.add_get('/websockify', websocket)
    app.on_shutdown.append(cleanup)
    return app
