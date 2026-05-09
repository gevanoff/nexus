const axios = require('axios');

const TELEGRAM_TOKEN = String(process.env.TELEGRAM_TOKEN || '').trim();
const GATEWAY_BEARER_TOKEN = String(process.env.GATEWAY_BEARER_TOKEN || '').trim();
const GATEWAY_BASE_URL = String(process.env.GATEWAY_BASE_URL || 'http://gateway:8800').replace(/\/+$/, '');
const TIMEOUT_MS = Number.parseInt(process.env.TELEGRAM_HEALTHCHECK_TIMEOUT_MS || '5000', 10);

function fail(message) {
  console.error(message);
  process.exit(1);
}

async function main() {
  if (!TELEGRAM_TOKEN) {
    fail('TELEGRAM_TOKEN is missing');
  }
  if (!GATEWAY_BEARER_TOKEN) {
    fail('GATEWAY_BEARER_TOKEN is missing');
  }

  const telegram = await axios.get(`https://api.telegram.org/bot${TELEGRAM_TOKEN}/getMe`, {
    timeout: TIMEOUT_MS,
  });
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

  console.log('ok');
}

main().catch((err) => {
  fail(err?.message || String(err));
});
