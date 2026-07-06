const grammy = require('grammy');

const OriginalBot = grammy.Bot;

const ROUTING_LOG_LEVEL = String(process.env.LOG_LEVEL || 'info').toLowerCase();
const TELEGRAM_ALLOWED_CHATS = parseCsvSet(process.env.TELEGRAM_ALLOWED_CHATS || '');
const TELEGRAM_REQUIRE_MENTION = parseBoolean(process.env.TELEGRAM_REQUIRE_MENTION, false);
const TELEGRAM_EXCLUSIVE_BOT_MENTIONS = parseBoolean(process.env.TELEGRAM_EXCLUSIVE_BOT_MENTIONS, true);
const TELEGRAM_MENTION_PATTERNS = compileMentionPatterns(process.env.TELEGRAM_MENTION_PATTERNS || '');

let botIdentity = {
  id: '',
  username: '',
};

function parseBoolean(value, fallback) {
  if (value === undefined || value === null || String(value).trim() === '') {
    return fallback;
  }
  return ['1', 'true', 'yes', 'y', 'on'].includes(String(value).trim().toLowerCase());
}

function parseCsvSet(value) {
  return new Set(
    String(value || '')
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean),
  );
}

function escapeRegex(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function compileMentionPatterns(value) {
  return String(value || '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      try {
        if (entry.startsWith('re:')) {
          return { source: entry, regex: new RegExp(entry.slice(3), 'i') };
        }
        const literal = entry.startsWith('@') ? escapeRegex(entry) : `\\b${escapeRegex(entry)}\\b`;
        return { source: entry, regex: new RegExp(literal, 'i') };
      } catch (err) {
        routingLog('warn', 'Ignoring invalid Telegram mention pattern', {
          pattern: entry,
          error: err?.message || String(err),
        });
        return null;
      }
    })
    .filter(Boolean);
}

function shouldLog(level) {
  const levels = ['error', 'warn', 'info', 'debug'];
  const current = levels.indexOf(ROUTING_LOG_LEVEL);
  const target = levels.indexOf(level);
  if (current === -1 || target === -1) return true;
  return target <= current;
}

function routingLog(level, message, meta = {}) {
  if (!shouldLog(level)) return;
  const line = JSON.stringify({
    level,
    message,
    time: new Date().toISOString(),
    component: 'telegram-router',
    ...meta,
  });
  if (level === 'error') {
    console.error(line);
  } else if (level === 'warn') {
    console.warn(line);
  } else {
    console.log(line);
  }
}

function rememberBotIdentity(me) {
  if (!me || typeof me !== 'object') return;
  botIdentity = {
    id: me.id ? String(me.id) : botIdentity.id,
    username: me.username ? String(me.username).toLowerCase() : botIdentity.username,
  };
}

function chatId(ctx) {
  return String(ctx.chat?.id || '');
}

function chatType(ctx) {
  return String(ctx.chat?.type || '');
}

function textFromContext(ctx) {
  return String(ctx.message?.text || ctx.channelPost?.text || '');
}

function isGroupChat(ctx) {
  const type = chatType(ctx);
  return type === 'group' || type === 'supergroup';
}

function isAllowedChat(ctx) {
  if (!TELEGRAM_ALLOWED_CHATS.size) return true;
  return TELEGRAM_ALLOWED_CHATS.has(chatId(ctx));
}

function commandTarget(text) {
  const match = String(text || '').trim().match(/^\/[a-z0-9_]+@([a-z0-9_]+)/i);
  return match ? String(match[1] || '').toLowerCase() : '';
}

function ownUsername() {
  return botIdentity.username || '';
}

function isCommandAddressedToAnotherBot(text) {
  const target = commandTarget(text);
  return Boolean(target && ownUsername() && target !== ownUsername());
}

function otherBotMentions(text) {
  if (!TELEGRAM_EXCLUSIVE_BOT_MENTIONS) return [];
  const own = ownUsername();
  const mentions = [];
  const re = /@([a-z0-9_]{5,32})/gi;
  let match;
  while ((match = re.exec(String(text || ''))) !== null) {
    const username = String(match[1] || '').toLowerCase();
    if (!username || username === own) continue;
    if (username.endsWith('bot')) mentions.push(`@${username}`);
  }
  return [...new Set(mentions)];
}

function isReplyToThisBot(ctx) {
  const replyUserId = ctx.message?.reply_to_message?.from?.id;
  return Boolean(replyUserId && botIdentity.id && String(replyUserId) === botIdentity.id);
}

function mentionsThisBot(text) {
  const own = ownUsername();
  if (!own) return false;
  return String(text || '').toLowerCase().includes(`@${own}`);
}

function matchesMentionPattern(text) {
  return TELEGRAM_MENTION_PATTERNS.some((entry) => entry.regex.test(String(text || '')));
}

function isSlashCommand(text) {
  return /^\/[a-z0-9_]+(?:@[a-z0-9_]+)?(?:\s|$)/i.test(String(text || '').trim());
}

function isAddressedToThisBot(ctx, text, { allowBareCommands = true } = {}) {
  if (mentionsThisBot(text)) return true;
  if (isReplyToThisBot(ctx)) return true;
  if (matchesMentionPattern(text)) return true;
  if (allowBareCommands && isSlashCommand(text) && !isCommandAddressedToAnotherBot(text)) return true;
  return false;
}

function shouldHandleContext(ctx, options = {}) {
  const text = textFromContext(ctx);
  const cid = chatId(ctx);
  if (!text.trim()) return false;

  if (!isAllowedChat(ctx)) {
    routingLog('info', 'Ignoring Telegram message from non-allowed chat', {
      chatId: cid,
      chatType: chatType(ctx),
    });
    return false;
  }

  if (isCommandAddressedToAnotherBot(text)) {
    routingLog('info', 'Ignoring Telegram command addressed to another bot', {
      chatId: cid,
      commandTarget: commandTarget(text),
    });
    return false;
  }

  const addressedOtherBots = otherBotMentions(text);
  if (addressedOtherBots.length) {
    routingLog('info', 'Ignoring Telegram message addressed to another bot', {
      chatId: cid,
      mentionedBots: addressedOtherBots,
    });
    return false;
  }

  if (isGroupChat(ctx) && TELEGRAM_REQUIRE_MENTION && !isAddressedToThisBot(ctx, text, options)) {
    routingLog('debug', 'Ignoring unaddressed Telegram group message', {
      chatId: cid,
      chatType: chatType(ctx),
      userId: ctx.from?.id,
    });
    return false;
  }

  return true;
}

function wrapMiddleware(middleware, options = {}) {
  if (typeof middleware !== 'function') return middleware;
  return async (ctx, next) => {
    if (!shouldHandleContext(ctx, options)) return undefined;
    return middleware(ctx, next);
  };
}

class NexusRoutingBot extends OriginalBot {
  constructor(...args) {
    super(...args);
    const originalGetMe = this.api.getMe.bind(this.api);
    this.api.getMe = async (...getMeArgs) => {
      const me = await originalGetMe(...getMeArgs);
      rememberBotIdentity(me);
      return me;
    };
  }

  on(filter, ...middleware) {
    const filters = Array.isArray(filter) ? filter : [filter];
    const shouldWrap = filters.some((entry) => entry === 'message:text' || entry === 'channel_post:text');
    if (!shouldWrap) {
      return super.on(filter, ...middleware);
    }
    return super.on(filter, ...middleware.map((entry) => wrapMiddleware(entry, { allowBareCommands: true })));
  }

  command(command, ...middleware) {
    return super.command(command, ...middleware.map((entry) => wrapMiddleware(entry, { allowBareCommands: true })));
  }
}

grammy.Bot = NexusRoutingBot;

routingLog('info', 'Telegram routing controls initialized', {
  allowedChatCount: TELEGRAM_ALLOWED_CHATS.size,
  requireMention: TELEGRAM_REQUIRE_MENTION,
  exclusiveBotMentions: TELEGRAM_EXCLUSIVE_BOT_MENTIONS,
  mentionPatternCount: TELEGRAM_MENTION_PATTERNS.length,
});

require('./telegram_gateway_bot');
