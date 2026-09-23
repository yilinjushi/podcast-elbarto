import test from 'node:test';
import assert from 'node:assert/strict';
import { probePi } from '../pi_probe.mjs';

function fixture(overrides = {}) {
  const calls = [];
  const composer = {
    waitFor: async () => { if (overrides.missing) throw new Error('sensitive URL'); },
    isVisible: async () => true, isEnabled: async () => !overrides.disabled,
    isEditable: async () => true,
  };
  const page = {
    goto: async (url, options) => { calls.push(['goto', url, options]); return { status: () => overrides.status ?? 200 }; },
    title: async () => overrides.title ?? 'Pi',
    locator: () => ({ count: async () => overrides.challenge ? 1 : 0 }),
    getByTestId: id => id === 'chat-composer-textbox' ? composer : { isVisible: async () => !!overrides.modal },
    getByPlaceholder: placeholder => ({ isVisible: async () => placeholder === 'Preferred name' && !!overrides.preferredName }),
    getByRole: (role, options) => ({ isVisible: async () => role === 'button' && options?.name === 'Log in' && !!overrides.loginVisible }),
    waitForTimeout: async () => {},
  };
  const context = { newPage: async () => page, close: async () => calls.push(['context.close']) };
  const browser = { newContext: async options => { calls.push(['context', options]); return context; }, close: async () => calls.push(['browser.close']) };
  const browserType = { launch: async options => { calls.push(['launch', options]); if (overrides.launchError) throw new Error('secret-value'); return browser; } };
  return { calls, browserType };
}

test('headless bundled Chromium probes only talk with supplied state, closes resources', async () => {
  const f = fixture();
  assert.deepEqual(await probePi({ storageState: 'private-session.json', browserType: f.browserType }), { status: 'available' });
  assert.deepEqual(f.calls[0], ['launch', { headless: true }]);
  assert.equal(f.calls[1][1].storageState, 'private-session.json');
  assert.deepEqual(f.calls.filter(c => c[0] === 'goto').map(c => c[1]), ['https://pi.ai/talk']);
  assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
});

test('explicit unauthorized response requires login', async () => {
  const f = fixture({ status: 401 });
  assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'needs_login' });
  assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
});

test('access challenges and rate limits have distinct classifications', async () => {
  for (const options of [{ status: 403 }, { title: 'Just a moment...' }, { title: '请稍候…' }, { challenge: true }]) {
    const f = fixture(options);
    assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'access_challenge' });
    assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
  }
  const limited = fixture({ status: 429 });
  assert.deepEqual(await probePi({ storageState: 'private', browserType: limited.browserType }), { status: 'rate_limited' });
  assert.deepEqual(limited.calls.slice(-2), [['context.close'], ['browser.close']]);
});

test('observed preferred-name and memory onboarding are distinct from login', async () => {
  for (const options of [{ preferredName: true }, { modal: true }, { missing: true, preferredName: true }]) {
    const f = fixture(options);
    assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'needs_onboarding' });
    assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
  }
});

test('guest-ready composer is available while login remains visible', async () => {
  const f = fixture({ loginVisible: true });
  assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'available' });
  assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
});

test('ambiguous missing or disabled composer is a probe error', async () => {
  for (const options of [{ missing: true }, { disabled: true }]) {
    const f = fixture(options);
    assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'probe_error' });
    assert.deepEqual(f.calls.slice(-2), [['context.close'], ['browser.close']]);
  }
});

test('missing state never launches; operational errors reveal no raw error', async () => {
  const f = fixture({ launchError: true });
  assert.deepEqual(await probePi({ storageState: '', browserType: f.browserType }), { status: 'needs_login' });
  assert.equal(f.calls.length, 0);
  assert.deepEqual(await probePi({ storageState: 'private', browserType: f.browserType }), { status: 'probe_error' });
  const unavailable = fixture({ status: 503 });
  assert.deepEqual(await probePi({ storageState: 'private', browserType: unavailable.browserType }), { status: 'probe_error' });
});
