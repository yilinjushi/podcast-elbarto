import { timingSafeEqual } from 'node:crypto';

export function byteRange(value, size) {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) throw new Error('range');
  let start, end;
  if (!match[1]) {
    const suffix = Number(match[2]);
    if (!Number.isSafeInteger(suffix) || suffix <= 0) throw new Error('range');
    start = Math.max(0, size - suffix); end = size - 1;
  } else {
    start = Number(match[1]); end = match[2] ? Number(match[2]) : size - 1;
  }
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start >= size || end < start) throw new Error('range');
  return {offset: start, length: Math.min(end, size - 1) - start + 1};
}

async function authorized(provided, expected) {
  if (!expected || expected.length < 32) return false;
  const hash = (text) => crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  const [a, b] = await Promise.all([hash(provided), hash(expected)]);
  return timingSafeEqual(new Uint8Array(a), new Uint8Array(b));
}

function storeKey(key) {
  return /^data\/(?:feed\.xml|feed-manifest\.json|publish-intent\.json|private-state\.zip)$/.test(key)
    || /^(?:audio|staging)\/[a-f0-9]{64}\.mp3$/.test(key);
}

async function privateStore(request, env, url, headers) {
  // This application credential can access only this Worker's bucket binding,
  // not Cloudflare account settings or any other bucket. Never place it in URLs.
  const bearer = request.headers.get('Authorization') || '';
  if (!bearer.startsWith('Bearer ') || !await authorized(bearer.slice(7), env.STORE_TOKEN)) {
    // Do not disguise failed authentication as an empty/missing state archive.
    return new Response('Unauthorized', {status: 401, headers});
  }
  const root = `${env.OBJECT_PREFIX || 'podcast'}/`;
  if (url.pathname === '/_store/list' && request.method === 'GET') {
    const prefix = url.searchParams.get('prefix') || '';
    if (!['', 'audio/', 'staging/', 'data/'].includes(prefix)) return new Response(null, {status: 400, headers});
    const cursor = url.searchParams.get('cursor') || undefined;
    if (cursor && cursor.length > 2048) return new Response(null, {status: 400, headers});
    const listing = await env.AUDIO.list({prefix: root + prefix, cursor, limit: 1000});
    return Response.json({objects: listing.objects.map(object => ({key: object.key.slice(root.length), size: object.size})),
      cursor: listing.truncated ? listing.cursor : null}, {headers});
  }
  const key = url.pathname.startsWith('/_store/objects/') ? url.pathname.slice('/_store/objects/'.length) : '';
  if (!storeKey(key) || url.search) return new Response('Not found', {status: 404, headers});
  const objectKey = root + key;
  if (request.method === 'GET') {
    const object = await env.AUDIO.get(objectKey);
    if (!object) return new Response('Not found', {status: 404, headers});
    return new Response(object.body, {headers: {...headers, 'Content-Length': String(object.size),
      'Content-Type': object.httpMetadata?.contentType || 'application/octet-stream'}});
  }
  if (request.method === 'DELETE') {
    await env.AUDIO.delete(objectKey);
    return new Response(null, {status: 204, headers});
  }
  if (request.method !== 'PUT') return new Response(null, {status: 405, headers});
  const rawLength = request.headers.get('Content-Length') || '';
  const length = Number(rawLength);
  const maximum = key.endsWith('.zip') ? 20 * 1024 * 1024 : key.endsWith('.mp3') ? 100_000_000 : 2_000_000;
  if (!/^\d+$/.test(rawLength) || !Number.isSafeInteger(length) || length < 1 || length > maximum || !request.body) {
    return new Response(null, {status: 413, headers});
  }
  const sha256 = request.headers.get('X-Content-SHA256') || '';
  if (!/^[a-f0-9]{64}$/.test(sha256)) return new Response(null, {status: 400, headers});
  // Bound the whole controlled namespace. Writers are serialized by the single
  // workflow concurrency group; this check is not a distributed quota lock.
  let total = 0, existing = 0, cursor;
  do {
    const listing = await env.AUDIO.list({prefix: root, cursor, limit: 1000});
    for (const object of listing.objects) {
      total += object.size;
      if (object.key === objectKey) existing = object.size;
    }
    cursor = listing.truncated ? listing.cursor : undefined;
  } while (cursor);
  if (total - existing + length > 1_000_000_000) return new Response(null, {status: 507, headers});
  const stream = new FixedLengthStream(length);
  const upload = request.body.pipeTo(stream.writable);
  await Promise.all([env.AUDIO.put(objectKey, stream.readable, {sha256,
    httpMetadata: {contentType: request.headers.get('Content-Type') || 'application/octet-stream', cacheControl: 'private, no-store'}}), upload]);
  return new Response(null, {status: 204, headers});
}

export default {
  async fetch(request, env) {
    const baseHeaders = {'Cache-Control': 'private, no-store', 'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'};
    const headers = {...baseHeaders};
    try {
      const url = new URL(request.url);
      if (url.pathname.startsWith('/_store/')) return await privateStore(request, env, url, baseHeaders);
      const parts = url.pathname.split('/');
      if (parts[1] !== 'p' || !await authorized(parts[2] || '', env.FEED_TOKEN)) return new Response('Not found', {status: 404, headers});
      if (!['GET', 'HEAD'].includes(request.method)) return new Response(null, {status: 405, headers: {...headers, Allow: 'GET, HEAD'}});
      const resource = parts.slice(3).join('/');
      let key;
      if (resource === 'feed.xml') key = 'data/feed.xml';
      else if (/^audio\/[a-f0-9]{64}\.mp3$/.test(resource)) key = resource;
      else return new Response('Not found', {status: 404, headers});
      key = `${env.OBJECT_PREFIX || 'podcast'}/${key}`;
      const meta = await env.AUDIO.head(key);
      if (!meta) return new Response('Not found', {status: 404, headers});
      headers['Content-Type'] = resource === 'feed.xml' ? 'application/rss+xml; charset=utf-8' : 'audio/mpeg';
      headers['Accept-Ranges'] = 'bytes';
      headers.ETag = meta.httpEtag;
      // HEAD describes the entire representation; Range only applies to GET.
      let range = null;
      if (request.method === 'GET') {
        const ifRange = request.headers.get('If-Range');
        if (!ifRange || ifRange === meta.httpEtag) {
          try { range = byteRange(request.headers.get('Range'), meta.size); }
          catch { return new Response(null, {status: 416, headers: {...headers, 'Content-Range': `bytes */${meta.size}`}}); }
        }
      }
      headers['Content-Length'] = String(range ? range.length : meta.size);
      if (range) headers['Content-Range'] = `bytes ${range.offset}-${range.offset + range.length - 1}/${meta.size}`;
      if (request.method === 'HEAD') return new Response(null, {headers});
      const object = await env.AUDIO.get(key, range ? {range, onlyIf: {etagMatches: meta.etag}} : {onlyIf: {etagMatches: meta.etag}});
      if (!object) return new Response('Not found', {status: 404, headers: baseHeaders});
      if (!('body' in object)) return new Response(null, {status: 412, headers: {'Cache-Control': 'no-store'}});
      return new Response(object.body, {status: range ? 206 : 200, headers});
    } catch {
      // Never log private subscription paths or secrets.
      return new Response('Service unavailable', {status: 503, headers: baseHeaders});
    }
  },
};
