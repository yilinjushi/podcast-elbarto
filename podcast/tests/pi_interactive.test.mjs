import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, rm, access } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { filterPiState, interactivePi } from '../pi_interactive.mjs';

test('export allowlist excludes lookalikes and unrelated SSO, retains indexedDB', () => {
  const result = filterPiState({ cookies: ['.pi.ai', 'a.pi.ai', 'inflection.ai', 'pi.ai.evil.test', 'notpi.ai', 'google.com'].map(domain => ({ domain })), origins: [{ origin: 'https://pi.ai', indexedDB: [{ name: 'fixture' }] }, { origin: 'https://auth.inflection.ai' }, { origin: 'http://pi.ai' }, { origin: 'https://evil.test' }] });
  assert.deepEqual(result.cookies.map(c => c.domain), ['.pi.ai', 'a.pi.ai', 'inflection.ai']);
  assert.equal(result.origins.length, 2);
  assert.deepEqual(result.origins[0].indexedDB, [{ name: 'fixture' }]);
});

async function fixture(run, options = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'pi-interactive-test-'));
  const config = { profile: path.join(directory, 'profile'), saveRequest: path.join(directory, 'save'), stateOut: path.join(directory, 'state.json'), statusOut: path.join(directory, 'status.json'), ttlSeconds: 1 };
  const calls = [];
  let time = 0;
  const page = {
    goto: async url => calls.push(['goto', url]), url: () => 'https://pi.ai/talk',
    title: async () => options.challenge ? 'Just a moment...' : 'Pi',
    locator: () => ({ count: async () => 0 }),
    getByTestId: id => ({ isVisible: async () => id === 'chat-composer-textbox' ? true : !!options.modal, isEnabled: async () => true, isEditable: async () => true }),
  };
  const context = { pages: () => [page], close: async () => calls.push(['close']), storageState: async arg => { calls.push(['export', arg]); return { cookies: [{ domain: '.pi.ai', value: 'fixture' }, { domain: 'unrelated.test', value: 'fixture' }], origins: [] }; } };
  config.browserType = { launchPersistentContext: async (profile, settings) => { calls.push(['launch', settings]); return context; } };
  config.now = () => time;
  config.sleep = async milliseconds => { if (options.request && time === 0) await writeFile(config.saveRequest, ''); time += milliseconds; };
  try { await run(config, calls); }
  finally { await rm(directory, { recursive: true, force: true }); }
}

test('ready composer never auto-exports and TTL exits', async () => fixture(async (config, calls) => {
  assert.deepEqual(await interactivePi(config), { status: 'needs_login' });
  assert.equal(calls.some(c => c[0] === 'export'), false);
  await assert.rejects(access(config.stateOut));
  assert.deepEqual(JSON.parse(await readFile(config.statusOut)), { status: 'needs_login' });
}));

test('explicit request exports filtered state with indexedDB and headed default Chromium', async () => fixture(async (config, calls) => {
  assert.deepEqual(await interactivePi(config), { status: 'saved' });
  assert.equal(calls[0][1].headless, false);
  assert.deepEqual(calls.find(c => c[0] === 'export'), ['export', { indexedDB: true }]);
  assert.deepEqual(JSON.parse(await readFile(config.stateOut)).cookies.map(c => c.domain), ['.pi.ai']);
  assert.deepEqual(JSON.parse(await readFile(config.statusOut)), { status: 'saved' });
  await assert.rejects(access(config.saveRequest));
}, { request: true }));

test('challenge or onboarding cannot export even on explicit save', async () => {
  for (const blocker of [{ challenge: true }, { modal: true }]) await fixture(async (config, calls) => {
    assert.deepEqual(await interactivePi(config), { status: 'needs_login' });
    assert.equal(calls.some(c => c[0] === 'export'), false);
  }, { ...blocker, request: true });
});

test('stale save request fails closed without browser launch', async () => fixture(async (config, calls) => {
  await writeFile(config.saveRequest, '');
  assert.deepEqual(await interactivePi(config), { status: 'error' });
  assert.deepEqual(calls, []);
}));
