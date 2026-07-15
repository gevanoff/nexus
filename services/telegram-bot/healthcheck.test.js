const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { checkGatewayCompletion, recentGatewayFailure, validateCompletion } = require('./healthcheck');

test('gateway completion health check sends a real minimal chat request', async () => {
  let request;
  const client = {
    async post(url, payload, options) {
      request = { url, payload, options };
      return { status: 200, data: { choices: [{ message: { content: 'OK' } }] } };
    },
  };

  await checkGatewayCompletion(client);

  assert.match(request.url, /\/v1\/chat\/completions$/);
  assert.equal(request.payload.stream, false);
  assert.equal(request.payload.max_tokens, 1);
  assert.equal(request.payload.messages[0].role, 'user');
  assert.match(request.options.headers.Authorization, /^Bearer /);
});

test('gateway completion health check rejects an unusable success response', () => {
  assert.throws(
    () => validateCompletion({ status: 200, data: { choices: [] } }),
    /returned no choices/,
  );
});

test('recent gateway failure reads a fresh bot request failure marker', () => {
  const previousPath = process.env.TELEGRAM_GATEWAY_STATE_PATH;
  const temporaryDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'nexus-telegram-health-'));
  const statePath = path.join(temporaryDirectory, 'gateway-state.json');
  process.env.TELEGRAM_GATEWAY_STATE_PATH = statePath;
  fs.writeFileSync(statePath, JSON.stringify({ ok: false, checked_at: 1000, error: 'timeout' }));

  delete require.cache[require.resolve('./healthcheck')];
  const reloaded = require('./healthcheck');
  assert.equal(reloaded.recentGatewayFailure(1500)?.error, 'timeout');
  assert.equal(reloaded.recentGatewayFailure(400000), null);

  if (previousPath === undefined) delete process.env.TELEGRAM_GATEWAY_STATE_PATH;
  else process.env.TELEGRAM_GATEWAY_STATE_PATH = previousPath;
  fs.rmSync(temporaryDirectory, { recursive: true, force: true });
});
