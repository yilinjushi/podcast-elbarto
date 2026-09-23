import { chromium } from 'playwright';
import { pathToFileURL } from 'node:url';

// Read-only availability check. Never submit, dismiss onboarding, export state,
// take screenshots, or expose browser errors (which can contain session URLs).
export async function probePi({ storageState = process.env.PI_STORAGE_STATE, browserType = chromium } = {}) {
  if (!storageState) return { status: 'needs_login' };
  let browser;
  let context;
  try {
    browser = await browserType.launch({ headless: true });
    context = await browser.newContext({ storageState, viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const response = await page.goto('https://pi.ai/talk', { waitUntil: 'domcontentloaded', timeout: 60000 });
    if (response?.status() === 401) return { status: 'needs_login' };
    if (response?.status() === 403) return { status: 'access_challenge' };
    if (response?.status() === 429) return { status: 'rate_limited' };
    if (!response || response.status() >= 400) return { status: 'probe_error' };
    const challenge = async () => /just a moment|attention required|请稍候|稍候|checking your browser|verify you are human/i.test(await page.title())
      || await page.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0;
    const onboarding = async () => await page.getByPlaceholder('Preferred name', { exact: true }).isVisible()
      || await page.getByTestId('memory-onboarding-modal').isVisible();
    if (await challenge()) return { status: 'access_challenge' };
    if (await onboarding()) return { status: 'needs_onboarding' };
    const composer = page.getByTestId('chat-composer-textbox');
    try { await composer.waitFor({ state: 'visible', timeout: 45000 }); }
    catch {
      if (await challenge()) return { status: 'access_challenge' };
      if (await onboarding()) return { status: 'needs_onboarding' };
      return { status: 'probe_error' };
    }
    await page.waitForTimeout(3000); // Match the existing capture hydration wait.
    if (await challenge()) return { status: 'access_challenge' };
    if (await onboarding()) return { status: 'needs_onboarding' };
    if (!await composer.isVisible() || !await composer.isEnabled() || !await composer.isEditable()) return { status: 'probe_error' };
    return { status: 'available' };
  } catch {
    return { status: 'probe_error' };
  } finally {
    try { await context?.close(); } catch { /* Never log browser error details. */ }
    try { await browser?.close(); } catch { /* Never log browser error details. */ }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const result = await probePi();
  process.stdout.write(JSON.stringify(result) + '\n');
  process.exitCode = result.status === 'available' ? 0
    : ['needs_login', 'needs_onboarding', 'access_challenge', 'rate_limited'].includes(result.status) ? 2 : 1;
}
