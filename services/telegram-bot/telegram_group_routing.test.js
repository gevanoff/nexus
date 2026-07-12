const assert = require('node:assert/strict');
const test = require('node:test');

const { createTelegramGroupRouter } = require('./telegram_group_routing');

function context(text, { type = 'supergroup', chatId = '-1001', replyUserId } = {}) {
  const message = { text };
  if (replyUserId) message.reply_to_message = { from: { id: replyUserId } };
  return { chat: { id: chatId, type }, from: { id: 7 }, message };
}

function configuredRouter(env = {}) {
  const router = createTelegramGroupRouter({
    env: { TELEGRAM_MENTION_PATTERNS: 'Hermes,Mercurious', ...env },
  });
  router.rememberBotIdentity({ id: 42, username: 'NexusBridgeBot', first_name: 'Nexus' });
  return router;
}

test('shared chats require addressing by default while private chats do not', () => {
  const router = configuredRouter();
  assert.equal(router.config.requireMention, true);
  assert.equal(router.shouldHandleContext(context('ambient room chatter')), false);
  assert.equal(router.shouldHandleContext(context('ambient direct message', { type: 'private', chatId: '7' })), true);
  assert.equal(router.shouldHandleContext(context('channel announcement', { type: 'channel' })), false);
});

test('username, Telegram display name, configured nicknames, and replies activate the bot', () => {
  const router = configuredRouter();
  assert.equal(router.shouldHandleContext(context('@NexusBridgeBot please answer')), true);
  assert.equal(router.shouldHandleContext(context('Nexus, please answer')), true);
  assert.equal(router.shouldHandleContext(context('Hermes, please answer')), true);
  assert.equal(router.shouldHandleContext(context('please answer', { replyUserId: 42 })), true);
  assert.equal(router.shouldHandleContext(context('hermesian architecture')), false);
});

test('commands for this bot work and messages addressed to other bots are ignored', () => {
  const router = configuredRouter();
  assert.equal(router.shouldHandleContext(context('/help')), true);
  assert.equal(router.shouldHandleContext(context('/help@NexusBridgeBot')), true);
  assert.equal(router.shouldHandleContext(context('/help@OtherHelperBot')), false);
  assert.equal(router.shouldHandleContext(context('@OtherHelperBot please answer')), false);
});

test('chat allowlist is enforced before activation checks', () => {
  const router = configuredRouter({ TELEGRAM_ALLOWED_CHATS: '-1002' });
  assert.equal(router.shouldHandleContext(context('@NexusBridgeBot answer', { chatId: '-1001' })), false);
  assert.equal(router.shouldHandleContext(context('@NexusBridgeBot answer', { chatId: '-1002' })), true);
});
