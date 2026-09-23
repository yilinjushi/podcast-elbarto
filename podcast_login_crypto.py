"""Authenticated, run-bound short-lived login payloads; no I/O or secret logs."""
import base64
import binascii
import json
import math
import os
import re
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b'x-bookmarks-podcast-login-v1'
MAX_BYTES = 5 * 1024 * 1024
MAX_ENVELOPE_BYTES = 4 * ((MAX_BYTES + 16 + 2) // 3) + 256


class LoginCryptoError(ValueError):
    """Fixed message only; never include attacker-controlled or secret content."""


def _fail():
    raise LoginCryptoError('invalid_login_envelope') from None


def _decode(value):
    if not isinstance(value,str):_fail()
    try:
        decoded=base64.b64decode(value,validate=True)
    except (ValueError,binascii.Error):_fail()
    if base64.b64encode(decoded).decode('ascii')!=value:_fail()
    return decoded


def _key(key):
    value=key if key is not None else os.environ.get('PODCAST_LOGIN_KEY','')
    decoded=_decode(value)
    if len(decoded)!=32:_fail()
    return decoded


def _object(pairs):
    result={}
    for name,value in pairs:
        if name in result:_fail()
        result[name]=value
    return result


def _json(raw,limit):
    if not isinstance(raw,(bytes,str)):_fail()
    if len(raw.encode('utf-8') if isinstance(raw,str) else raw)>limit:_fail()
    try:
        value=json.loads(raw,object_pairs_hook=_object,parse_constant=lambda _: _fail())
    except (ValueError,UnicodeError,RecursionError):_fail()
    if not isinstance(value,dict):_fail()
    return value


def _validate(payload,kind,run_id,now):
    if kind not in {'connection','state'} or not isinstance(run_id,str) or not re.fullmatch(r'[0-9]+',run_id):_fail()
    if payload.get('kind')!=kind or payload.get('run_id')!=run_id:_fail()
    expires=payload.get('expires_at')
    if isinstance(expires,bool) or not isinstance(expires,(int,float)) or not math.isfinite(expires):_fail()
    if isinstance(now,bool) or not isinstance(now,(int,float)) or not math.isfinite(now) or now<0 or expires<=now:_fail()


def seal(payload,*,key=None,now=None):
    """Return a JSON-serializable v1 envelope. Key is canonical base64 (32 bytes)."""
    try:
        raw=json.dumps(payload,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
        checked=_json(raw,MAX_BYTES)
        _validate(checked,checked.get('kind'),checked.get('run_id'),time.time() if now is None else now)
        nonce=os.urandom(12)
        ciphertext=AESGCM(_key(key)).encrypt(nonce,raw,AAD)
        return {'version':1,'nonce':base64.b64encode(nonce).decode('ascii'),'ciphertext':base64.b64encode(ciphertext).decode('ascii')}
    except Exception:_fail()


def open_envelope(envelope,*,expected_kind,run_id,now,key=None):
    """Authenticate and validate expiry/run/kind before returning plaintext.

    Prevents cross-run reuse. Same-run single-use enforcement belongs to the
    caller's state machine; cryptography alone cannot track previous opens.
    """
    try:
        if isinstance(envelope,dict):
            envelope=json.dumps(envelope,separators=(',',':'),allow_nan=False)
        parsed=_json(envelope,MAX_ENVELOPE_BYTES)
        if set(parsed)!={'version','nonce','ciphertext'} or type(parsed['version']) is not int or parsed['version']!=1:_fail()
        nonce=_decode(parsed['nonce']);ciphertext=_decode(parsed['ciphertext'])
        if len(nonce)!=12 or not 16<=len(ciphertext)<=MAX_BYTES+16:_fail()
        raw=AESGCM(_key(key)).decrypt(nonce,ciphertext,AAD)
        payload=_json(raw,MAX_BYTES)
        _validate(payload,expected_kind,run_id,now)
        return payload
    except Exception:_fail()


# Requested public operation name; this module performs no filesystem access.
open = open_envelope
