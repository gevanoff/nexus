const axios = require('axios');
const fs = require('node:fs');

const TELEGRAM_TOKEN = String(process.env.TELEGRAM_TOKEN || process.env.TELEGRAM_TOKEN_FALLBACK || '').trim();
const GATEWAY_BEARER_TOKEN = String(process.env.GATEWAY_BEARER_TOKEN || '').trim();
const GATEWAY_BASE_URL = String(process.env.GATEWAY_BASE_URL || 'http://gateway:8800').replace(/\/+$/, '');
const GATEWAY_MODEL = String(process.env.GATEWAY_MODEL || 'fast').trim();
const TIMEOUT_MS = Number.parseInt(process.env.TELEGRAM_HEALTHCHECK_TIMEOUT_MS || '5000', 10);
const NETWORK_RETRIES = Number.parseInt(process.env.TELEGRAM_HEALTHCHECK_NETWORK_RETRIES || '2', 10);
const RETRY_DELAY_MS = Number.parseInt(process.env.TELEGRAM_HEALTHCHECK_RETRY_DELAY_MS || '250', 10);
const GATEWAY_STATE_PATH = String(process.env.TELEGRAM_GATEWAY_STATE_PATH || '/tmp/nexus-telegram-gateway-state.json').trim();
const GATEWAY_FAILURE_MAX_AGE_MS = Number.parseInt(process.env.TELEGRAM_GATEWAY_FAILURE_MAX_AGE_MS || '300000', 10);

function fail(message) {
  console.error(message);
  process.exit(1);
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isRetryableRequestError(err) {
  if (!axios.isAxiosError(err)) return false;
  if (!err.response) {
    return ['ENOTFOUND', 'EAI_AGAIN', 'EAI_NODATA', 'ECONNREFUSED', 'ECONNRESET', 'ETIMEDOUT'].includes(
      String(err.code || '').toUpperCase(),
    );
  }
  return err.response.status === 429 || err.response.status >= 500;
}

async function requestWithRetry(operation, {
  retries = NETWORK_RETRIES,
  delayMs = RETRY_DELAY_MS,
  sleep = wait,
} = {}) {
  let error;
  for (let attempt = 0; attempt <= retries; attempt += 1) {
    try {
      return await operation();
    } catch (err) {
      error = err;
      if (!isRetryableRequestError(err) || attempt >= retries) break;
      await sleep(delayMs * (attempt + 1));
    }
  }
  throw error;
}

function recentGatewayFailure(now = Date.now()) {
  let state;
  try {
    state = JSON.parse(fs.readFileSync(GATEWAY_STATE_PATH, 'utf8'));
  } catch (err) {
    if (err?.code === 'ENOENT') return null;
    throw err;
  }
  const checkedAt = Number(state?.checked_at || 0);
  if (state?.ok !== false || !checkedAt || now - checkedAt > GATEWAY_FAILURE_MAX_AGE_MS) {
    return null;
  }
  return state;
}

function validateCompletion(response) {
  if (response.status < 200 || response.status >= 300) {
    throw new Error(`Gateway completion failed with status ${response.status}`);
  }
  if (!Array.isArray(response.data?.choices) || response.data.choices.length === 0) {
    throw new Error(`Gateway model ${GATEWAY_MODEL} completion returned no choices`);
  }
}

async function checkGatewayCompletion(client = axios) {
  const response = await client.post(`${GATEWAY_BASE_URL}/v1/chat/completions`, {
    model: GATEWAY_MODEL,
    messages: [{ role: 'user', content: 'Reply OK' }],
    max_tokens: 1,
    stream: false,
  }, {
    headers: {
      Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
      'Content-Type': 'application/json',
    },
    timeout: TIMEOUT_MS,
  });
  validateCompletion(response);
}

async function main() {
  if (!TELEGRAM_TOKEN) {
    fail('TELEGRAM_TOKEN is missing');
  }
  if (!GATEWAY_BEARER_TOKEN) {
    fail('GATEWAY_BEARER_TOKEN is missing');
  }

  const telegram = await requestWithRetry(
    () => axios.get(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe`, {
      timeout: TIMEOUT_MS,
    }),
  );
  if (telegram.status !== 200 || telegram.data?.ok !== true) {
    fail(`Telegram getMe failed with status ${telegram.status}`);
  }

  const gateway = await axios.get(`${GATEWAY_BASE_URL}/v1/models`, {
    headers: {
      Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
    },
    timeout: TIMEOUT_MS,
  });
  if (gateway.status < 200 || gateway.status >= 300) {
    fail(`Gateway health failed with status ${gateway.status}`);
  }
  const model = Array.isArray(gateway.data?.data)
    ? gateway.data.data.find((item) => item?.id === GATEWAY_MODEL)
    : null;
  const backend = String(model?.backend || '').trim();
  if (!model || !backend) {
    fail(`Gateway model ${GATEWAY_MODEL} has no backend mapping`);
  }
  const status = await axios.get(`${GATEWAY_BASE_URL}/v1/gateway/status`, {
    headers: { Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}` },
    timeout: TIMEOUT_MS,
  });
  const backendHealth = status.data?.backend_health?.[backend];
  if (backendHealth?.ready !== true) {
    fail(`Gateway model ${GATEWAY_MODEL} backend ${backend} is not ready`);
  }
  const gatewayFailure = recentGatewayFailure();
  if (gatewayFailure) {
    fail(`Last Telegram chat request failed: ${gatewayFailure.error || 'gateway request failed'}`);
  }
  await checkGatewayCompletion();

  console.log('ok');
}

if (require.main === module) {
  main().catch((err) => {
    fail(err?.message || String(err));
  });
}

module.exports = {
  checkGatewayCompletion,
  isRetryableRequestError,
  recentGatewayFailure,
  requestWithRetry,
  validateCompletion,
};
