import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {byteRange} from '../worker.mjs';

test('range validation clamps ends and rejects unsafe numbers and empty objects', () => {
  assert.deepEqual(byteRange('bytes=8-999', 10), {offset: 8, length: 2});
  assert.deepEqual(byteRange('bytes=-99', 10), {offset: 0, length: 10});
  for (const [range, size] of [['bytes=0-',0], ['bytes=9007199254740992-',10], ['bytes=4-2',10], ['items=0-2',10]]) {
    assert.throws(() => byteRange(range,size));
  }
});

test('HEAD/get deletion and backend errors do not retain media length headers', async () => {
  const token = 'unit-test-'.repeat(5);
  const url = `https://example.com/p/${token}/audio/${'a'.repeat(64)}.mp3`;
  for (const [mode,status] of [['deleted',404],['error',503],['changed',412]]) {
    const env = {FEED_TOKEN: token, AUDIO: {
      head: async () => ({size: 100, httpEtag: '"one"', etag: 'one'}),
      get: async () => { if (mode==='error') throw new Error('private'); return mode==='deleted' ? null : {}; },
    }};
    const response = await worker.fetch(new Request(url,{headers:{Range:'bytes=2-5'}}),env);
    assert.equal(response.status,status);
    assert.equal(response.headers.get('Content-Length'),null);
    assert.equal(response.headers.get('Content-Range'),null);
    assert.doesNotMatch(await response.text(),/private/);
  }
});

test('a feed token never grants access to private archives or staging', async () => {
  const token='unit-test-'.repeat(5);
  const env={FEED_TOKEN:token,AUDIO:{head:async()=>{throw new Error('must not access R2');}}};
  for (const resource of ['data/private-state.zip','staging/test.mp3','data/feed-manifest.json','checkpoint.json']) {
    assert.equal((await worker.fetch(new Request(`https://example.com/p/${token}/${resource}`),env)).status,404);
  }
  assert.equal((await worker.fetch(new Request(`https://example.com/p/${token}/feed.xml`,{method:'PUT',body:'x'}),env)).status,405);
});
