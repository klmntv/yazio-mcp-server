// Executed only through Playwriter direct CDP, in a task-owned BrowserOS tab.
const fs = await import('node:fs');
const https = await import('node:https');
const net = await import('node:net');
const options = YAZIO_LOGIN_OPTIONS;
const pages = context.pages();
if (!pages.length) throw new Error('Open a BrowserOS window before starting login.');
const browserCdp = await getCDPSession({ page: pages[0] });
const nextPage = context.waitForEvent('page', { timeout: 10000 });
nextPage.catch(() => {});
const created = await browserCdp.send('Browser.createTab', { url: 'about:blank', background: true });
state.yazioLogin = { phase: 'starting', ownedPages: [], targetId: created.tab.targetId };
try {
  await nextPage;
  for (const candidate of context.pages()) {
    if (candidate.isClosed()) continue;
    const cdp = await getCDPSession({ page: candidate });
    const info = await cdp.send('Target.getTargetInfo');
    if (info.targetInfo.targetId === created.tab.targetId) {
      state.page = candidate;
      state.yazioLogin.ownedPages.push(candidate);
      break;
    }
  }
  if (!state.yazioLogin.ownedPages.length) throw new Error('Could not identify the new tab.');
  state.page.on('popup', page => state.yazioLogin.ownedPages.push(page));
  if (options.webAddress) {
    if (!net.isIP(options.webAddress)) throw new Error('Expected a literal public DNS address.');
    // Optional per-tab DNS repair. TLS still checks www.yazio.com's certificate.
    await state.page.route('https://www.yazio.com/**', async route => {
      const request = route.request();
      try {
        const result = await new Promise((resolve, reject) => {
          const outgoing = https.request(request.url(), {
            method: request.method(), headers: request.headers(),
            lookup: (_host, lookupOptions, callback) => {
              const record = { address: options.webAddress, family: net.isIP(options.webAddress) };
              if (lookupOptions.all) callback(null, [record]);
              else callback(null, record.address, record.family);
            },
            timeout: 20000,
          }, response => {
            const parts = [];
            response.on('data', part => parts.push(part));
            response.on('end', () => resolve({
              status: response.statusCode,
              headers: Object.fromEntries(Object.entries(response.headers)
                .filter(([key, value]) => value !== undefined && key !== 'transfer-encoding')
                .map(([key, value]) => [key, Array.isArray(value) ? value.join('\n') : value])),
              body: Buffer.concat(parts),
            }));
          });
          outgoing.on('timeout', () => outgoing.destroy(new Error('YAZIO timed out')));
          outgoing.on('error', reject);
          const body = request.postDataBuffer();
          if (body) outgoing.write(body);
          outgoing.end();
        });
        await route.fulfill(result);
      } catch {
        await route.abort();
      }
    });
  }
  state.page.on('response', async response => {
    try {
      const url = new URL(response.url());
      if (url.origin !== 'https://www.yazio.com' || url.pathname !== '/api/auth/token') return;
      if (!response.ok() || response.request().method() !== 'POST') return;
      const request = response.request().postDataJSON();
      if (request.grant_type !== 'sign_in_with_apple') return;
      const tokens = await response.json();
      if (typeof tokens.access_token !== 'string' || !tokens.access_token) return;
      // Never store the Apple JWT or a password. Verification happens in Python.
      fs.writeFileSync(options.outputFile, JSON.stringify({
        access_token: tokens.access_token,
        refresh_token: tokens.refresh_token,
        expires_in: tokens.expires_in ?? 3600,
        issuer: 'web', client_id: request.client_id,
        web_address: options.webAddress ?? null,
      }), { mode: 0o600 });
      fs.chmodSync(options.outputFile, 0o600);
      state.yazioLogin.phase = 'authorized';
    } catch {
      state.yazioLogin.phase = 'capture_failed';
    }
  });
  await state.page.goto('https://www.yazio.com/en/app/account/login', {
    waitUntil: 'domcontentloaded', timeout: 30000,
  });
  state.yazioLogin.phase = 'ready';
  if (options.show) await state.page.bringToFront();
  console.log(JSON.stringify({ phase: state.yazioLogin.phase }));
} catch (error) {
  await browserCdp.send('Browser.closeTab', { targetId: created.tab.targetId });
  throw error;
}
