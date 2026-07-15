const assert = require('node:assert/strict');
const test = require('node:test');

const { createTelegramMemoryClient, messagesWithMemory } = require('./telegram_memory');


function context() {
  return {
    chat: { id: 1234, type: 'private' },
    from: { id: 5678 },
    message: { message_id: 90 },
  };
}


test('memory client sends immutable Telegram identity fields', async () => {
  let request;
  const client = createTelegramMemoryClient({
    env: {
      TELEGRAM_MEMORY_ENABLED: 'true',
      GATEWAY_BASE_URL: 'http://gateway:8800',
      GATEWAY_BEARER_TOKEN: 'token',
    },
    axiosInstance: {
      async post(url, body, options) {
        request = { url, body, options };
        return { data: { enabled: true, context: 'Likes tea.' } };
      },
    },
  });

  const result = await client.getContext(context(), '999', 'What do I like?');

  assert.equal(result, 'Likes tea.');
  assert.equal(request.body.chat_id, '1234');
  assert.equal(request.body.telegram_user_id, '5678');
  assert.equal(request.body.bot_id, '999');
  assert.equal(request.body.telegram_message_id, '90');
  assert.match(request.options.headers.Authorization, /^Bearer /);
});


test('memory failures are non-fatal to Telegram chat', async () => {
  const warnings = [];
  const client = createTelegramMemoryClient({
    env: {
      TELEGRAM_MEMORY_ENABLED: 'true',
      GATEWAY_BASE_URL: 'http://gateway:8800',
      GATEWAY_BEARER_TOKEN: 'token',
    },
    axiosInstance: { async post() { throw new Error('offline'); } },
    log: (...args) => warnings.push(args),
  });

  assert.equal(await client.getContext(context(), '999', 'hello'), '');
  assert.equal(await client.recordTurn(context(), '999', 'hello', 'hi'), false);
  assert.equal(warnings.length, 2);
});


test('shared long-term memory is isolated from local history', () => {
  const messages = messagesWithMemory(
    [{ role: 'system', content: 'persona' }, { role: 'assistant', content: 'earlier' }],
    'new question',
    'The user likes tea.',
  );

  assert.equal(messages[0].content, 'persona');
  assert.match(messages[1].content, /fallible context/);
  assert.equal(messages[2].content, 'earlier');
  assert.equal(messages[3].content, 'new question');
});
