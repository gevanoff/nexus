function parseBoolean(value, fallback) {
  if (value === undefined || value === null || String(value).trim() === '') return fallback;
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

function literalMentionRegex(value) {
  const escaped = escapeRegex(value);
  if (!escaped) return null;
  return new RegExp(`(?:^|[^a-z0-9_])${escaped}(?=$|[^a-z0-9_])`, 'i');
}

function compileMentionPatterns(value, log) {
  return String(value || '')
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      try {
        return {
          source: entry,
          regex: entry.startsWith('re:') ? new RegExp(entry.slice(3), 'i') : literalMentionRegex(entry),
        };
      } catch (err) {
        log('warn', 'Ignoring invalid Telegram mention pattern', {
          pattern: entry,
          error: err?.message || String(err),
        });
        return null;
      }
    })
    .filter((entry) => entry?.regex);
}

function createTelegramGroupRouter({ env = process.env, log = () => {} } = {}) {
  const allowedChats = parseCsvSet(env.TELEGRAM_ALLOWED_CHATS || '');
  const requireMention = parseBoolean(env.TELEGRAM_REQUIRE_MENTION, true);
  const exclusiveBotMentions = parseBoolean(env.TELEGRAM_EXCLUSIVE_BOT_MENTIONS, true);
  const mentionPatterns = compileMentionPatterns(env.TELEGRAM_MENTION_PATTERNS || '', log);
  let botIdentity = { id: '', username: '', firstName: '' };

  function rememberBotIdentity(me) {
    if (!me || typeof me !== 'object') return;
    botIdentity = {
      id: me.id ? String(me.id) : botIdentity.id,
      username: me.username ? String(me.username).toLowerCase() : botIdentity.username,
      firstName: me.first_name ? String(me.first_name).trim() : botIdentity.firstName,
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

  function isSharedChat(ctx) {
    return ['group', 'supergroup', 'channel'].includes(chatType(ctx));
  }

  function commandTarget(text) {
    const match = String(text || '').trim().match(/^\/[a-z0-9_]+@([a-z0-9_]+)/i);
    return match ? String(match[1] || '').toLowerCase() : '';
  }

  function isCommandAddressedToAnotherBot(text) {
    const target = commandTarget(text);
    return Boolean(target && botIdentity.username && target !== botIdentity.username);
  }

  function otherBotMentions(text) {
    if (!exclusiveBotMentions) return [];
    const mentions = [];
    const re = /(?:^|[^a-z0-9_])@([a-z0-9_]{5,32})/gi;
    let match;
    while ((match = re.exec(String(text || ''))) !== null) {
      const username = String(match[1] || '').toLowerCase();
      if (!username || username === botIdentity.username) continue;
      if (username.endsWith('bot')) mentions.push(`@${username}`);
    }
    return [...new Set(mentions)];
  }

  function isReplyToThisBot(ctx) {
    const replyUserId = ctx.message?.reply_to_message?.from?.id;
    return Boolean(replyUserId && botIdentity.id && String(replyUserId) === botIdentity.id);
  }

  function mentionsThisBot(text) {
    if (botIdentity.username) {
      const usernamePattern = literalMentionRegex(`@${botIdentity.username}`);
      if (usernamePattern?.test(String(text || ''))) return true;
    }
    if (botIdentity.firstName) {
      const namePattern = literalMentionRegex(botIdentity.firstName);
      if (namePattern?.test(String(text || ''))) return true;
    }
    return false;
  }

  function matchesMentionPattern(text) {
    return mentionPatterns.some((entry) => entry.regex.test(String(text || '')));
  }

  function isSlashCommand(text) {
    return /^\/[a-z0-9_]+(?:@[a-z0-9_]+)?(?:\s|$)/i.test(String(text || '').trim());
  }

  function isAddressedToThisBot(ctx, text, { allowBareCommands = true } = {}) {
    if (mentionsThisBot(text) || isReplyToThisBot(ctx) || matchesMentionPattern(text)) return true;
    return allowBareCommands && isSlashCommand(text) && !isCommandAddressedToAnotherBot(text);
  }

  function shouldHandleContext(ctx, options = {}) {
    const text = textFromContext(ctx);
    const cid = chatId(ctx);
    if (!text.trim()) return false;
    if (allowedChats.size && !allowedChats.has(cid)) {
      log('info', 'Ignoring Telegram message from non-allowed chat', { chatId: cid, chatType: chatType(ctx) });
      return false;
    }
    if (isCommandAddressedToAnotherBot(text)) {
      log('info', 'Ignoring Telegram command addressed to another bot', {
        chatId: cid,
        commandTarget: commandTarget(text),
      });
      return false;
    }
    const addressedOtherBots = otherBotMentions(text);
    if (addressedOtherBots.length) {
      log('info', 'Ignoring Telegram message addressed to another bot', {
        chatId: cid,
        mentionedBots: addressedOtherBots,
      });
      return false;
    }
    if (isSharedChat(ctx) && requireMention && !isAddressedToThisBot(ctx, text, options)) {
      log('debug', 'Ignoring unaddressed Telegram shared-chat message', {
        chatId: cid,
        chatType: chatType(ctx),
        userId: ctx.from?.id,
      });
      return false;
    }
    return true;
  }

  return {
    config: {
      allowedChatCount: allowedChats.size,
      requireMention,
      exclusiveBotMentions,
      mentionPatternCount: mentionPatterns.length,
    },
    isAddressedToThisBot,
    rememberBotIdentity,
    shouldHandleContext,
  };
}

module.exports = { createTelegramGroupRouter };
