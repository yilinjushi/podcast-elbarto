import { chromium } from 'playwright';
import { access, chmod, mkdir, open, rename, rm } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const allowedHost = host => ['pi.ai', 'inflection.ai'].some(domain => host === domain || host.endsWith('.' + domain));
export function filterPiState(state) {
  return {
    cookies: (state.cookies || []).filter(cookie => allowedHost(String(cookie.domain).replace(/^\./, '').toLowerCase())),
    origins: (state.origins || []).filter(origin => {
      try { const url = new URL(origin.origin); return url.protocol === 'https:' && allowedHost(url.hostname.toLowerCase()); }
      catch { return false; }
    }),
  };
}

async function exists(file) {
  try { await access(file); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function privateJson(file, value) {
  const temporary = file + '.' + process.pid + '.tmp';
  let handle;
  try {
    handle = await open(temporary, 'wx', 0o600);
    await handle.writeFile(JSON.stringify(value));
    await handle.sync();
    await handle.close(); handle = undefined;
    await chmod(temporary, 0o600);
    await rename(temporary, file);
    await chmod(file, 0o600);
  } finally {
    await handle?.close();
    await rm(temporary, { force: true });
  }
}

async function ready(page) {
  if (/just a moment|attention required|请稍候|稍候|checking your browser|verify you are human/i.test(await page.title())) return false;
  if (await page.locator('iframe[src*="challenges.cloudflare.com"]').count()) return false;
  const url = new URL(page.url());
  if (url.protocol !== 'https:' || url.hostname !== 'pi.ai' || !/^\/talk(?:\/|$)/.test(url.pathname)) return false;
  const composer = page.getByTestId('chat-composer-textbox');
  return await composer.isVisible() && await composer.isEnabled() && await composer.isEditable()
    && !await page.getByTestId('memory-onboarding-modal').isVisible();
}

// The caller protects the panel that creates saveRequest. Browser interaction
// belongs entirely to the user. There is no automatic login, challenge handling,
// or export on detection of a logged-in page.
export async function interactivePi({
  profile = process.env.PI_LOGIN_PROFILE,
  saveRequest = process.env.PI_LOGIN_SAVE_REQUEST,
  stateOut = process.env.PI_LOGIN_STATE_OUT,
  statusOut = process.env.PI_LOGIN_STATUS_OUT,
  ttlSeconds = Number(process.env.PI_LOGIN_TTL_SECONDS || 1800),
  browserType = chromium,
  now = Date.now,
  sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
} = {}) {
  let context;
  let status = 'error';
  let validPaths = false;
  try {
    const files = [profile, saveRequest, stateOut, statusOut];
    if (files.some(file => typeof file !== 'string' || !path.isAbsolute(file))
      || new Set(files.map(file => path.resolve(file))).size !== 4
      || !Number.isFinite(ttlSeconds) || ttlSeconds < 1 || ttlSeconds > 1800) throw new Error('Invalid configuration');
    validPaths = true;
    // Refuse stale requests and existing exported sessions; each run is fresh.
    if (await exists(saveRequest) || await exists(stateOut)) throw new Error('Stale run');
    await mkdir(profile, { recursive: true, mode: 0o700 });
    await chmod(profile, 0o700);
    await privateJson(statusOut, { status: 'needs_login' });
    const deadline = now() + ttlSeconds * 1000;
    context = await browserType.launchPersistentContext(profile, { headless: false, viewport: { width: 1440, height: 1000 } });
    const page = context.pages()[0] || await context.newPage();
    // DISPLAY is inherited from the host. No stealth flags or channel fallback.
    try { await page.goto('https://pi.ai/talk', { waitUntil: 'domcontentloaded', timeout: Math.min(60000, ttlSeconds * 1000) }); }
    catch { /* The user may still navigate manually within the time limit. */ }
    status = 'needs_login';
    while (now() < deadline) {
      if (await exists(saveRequest)) {
        await rm(saveRequest); // One explicit request permits one verification.
        if (await ready(page)) {
          const state = filterPiState(await context.storageState({ indexedDB: true }));
          await privateJson(stateOut, state);
          status = 'saved';
          break;
        }
        await privateJson(statusOut, { status: 'needs_login' });
      }
      await sleep(Math.min(500, Math.max(0, deadline - now())));
    }
  } catch { status = 'error'; }
  finally {
    try { await context?.close(); } catch { /* No page data in logs. */ }
    if (validPaths) {
      try { await privateJson(statusOut, { status }); }
      catch { status = 'error'; }
    }
  }
  return { status };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const result = await interactivePi();
  process.exitCode = result.status === 'saved' ? 0 : result.status === 'needs_login' ? 2 : 1;
}
