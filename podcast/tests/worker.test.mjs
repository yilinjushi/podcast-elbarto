import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {Miniflare, convertV4MiniflareOptions} from 'miniflare';

test('private RSS and media support GET, HEAD, Range, revocation and no private data access', async () => {
  const token = 'test-token-'.repeat(5);
  const storeToken = 'different-store-token-'.repeat(3);
  const mf = new Miniflare(convertV4MiniflareOptions({modules: true, scriptPath: new URL('../worker.mjs', import.meta.url).pathname.replace(/^\/([A-Z]:)/, '$1'), compatibilityDate: '2026-09-22', compatibilityFlags: ['nodejs_compat'], r2Buckets: ['AUDIO'], bindings: {FEED_TOKEN: token, STORE_TOKEN: storeToken, OBJECT_PREFIX: 'podcast'}}));
  try {
    const bucket = await mf.getR2Bucket('AUDIO');
    const name = 'a'.repeat(64);
    await bucket.put(`podcast/audio/${name}.mp3`, new Uint8Array([0,1,2,3,4,5,6,7,8,9]));
    await bucket.put('podcast/data/feed.xml', '<rss/>');
    const base = `https://example.com/p/${token}/`;
    assert.equal((await mf.dispatchFetch('https://example.com/p/wrong/feed.xml')).status, 404);
    assert.equal((await mf.dispatchFetch(base + 'data/publish-intent.json')).status, 404);
    assert.equal((await mf.dispatchFetch(base + 'feed.xml')).status, 200);
    const url = base + `audio/${name}.mp3`;
    const head = await mf.dispatchFetch(url, {method: 'HEAD'});
    assert.equal(head.headers.get('Content-Length'), '10'); assert.equal(await head.text(), '');
    for (const [range, expected] of [['bytes=2-5', [2,3,4,5]], ['bytes=-3', [7,8,9]], ['bytes=8-', [8,9]]]) {
      const response = await mf.dispatchFetch(url, {headers: {Range: range}});
      assert.equal(response.status, 206); assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], expected);
    }
    for (const range of ['bytes=20-', 'bytes=0-1,3-4', 'bytes=-0']) assert.equal((await mf.dispatchFetch(url, {headers: {Range: range}})).status, 416);
    await bucket.delete(`podcast/audio/${name}.mp3`);
    assert.equal((await mf.dispatchFetch(url)).status, 404);
    const privateUrl = 'https://example.com/_store/objects/data/private-state.zip';
    assert.equal((await mf.dispatchFetch(privateUrl)).status,401);
    assert.equal((await mf.dispatchFetch(privateUrl,{headers:{Authorization:`Bearer ${token}`}})).status,401);
    const auth = {Authorization:`Bearer ${storeToken}`};
    const body = 'private zip test';
    const putHeaders = {...auth, 'Content-Length':String(Buffer.byteLength(body)),
      'X-Content-SHA256':createHash('sha256').update(body).digest('hex')};
    assert.equal((await mf.dispatchFetch(privateUrl,{method:'PUT',headers:putHeaders,body})).status,204);
    assert.equal(await (await mf.dispatchFetch(privateUrl,{headers:auth})).text(),body);
    const listing = await (await mf.dispatchFetch('https://example.com/_store/list?prefix=data/',{headers:auth})).json();
    assert.ok(listing.objects.some(object=>object.key==='data/private-state.zip'));
    assert.equal((await mf.dispatchFetch('https://example.com/_store/objects/other/secrets',{headers:auth})).status,404);
    assert.equal((await mf.dispatchFetch(privateUrl,{method:'PUT',headers:{...putHeaders,'X-Content-SHA256':'0'.repeat(64)},body})).status,503);
    assert.equal((await mf.dispatchFetch(privateUrl,{method:'DELETE',headers:auth})).status,204);
    assert.equal((await mf.dispatchFetch(privateUrl,{headers:auth})).status,404);
  } finally { await mf.dispose(); }
});
