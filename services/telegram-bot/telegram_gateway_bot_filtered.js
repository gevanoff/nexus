const grammy = require('grammy');
const { createTelegramGroupRouter } = require('./telegram_group_routing');

const OriginalBot = grammy.Bot;
const ROUTING_LOG_LEVEL = String(process.env.LOG_LEVEL || 'info').toLowerCase();

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
  if (level === 'error') console.error(line);
  else if (level === 'warn') console.warn(line);
  else console.log(line);
}

const router = createTelegramGroupRouter({ log: routingLog });

function wrapMiddleware(middleware, options = {}) {
  if (typeof middleware !== 'function') return middleware;
  return async (ctx, next) => {
    if (!router.shouldHandleContext(ctx, options)) return undefined;
    return middleware(ctx, next);
  };
}

class NexusRoutingBot extends OriginalBot {
  constructor(...args) {
    super(...args);
    const originalGetMe = this.api.getMe.bind(this.api);
    this.api.getMe = async (...getMeArgs) => {
      const me = await originalGetMe(...getMeArgs);
      router.rememberBotIdentity(me);
      return me;
    };
  }

  on(filter, ...middleware) {
    const filters = Array.isArray(filter) ? filter : [filter];
    const shouldWrap = filters.some((entry) => entry === 'message:text' || entry === 'channel_post:text');
    if (!shouldWrap) return super.on(filter, ...middleware);
    return super.on(filter, ...middleware.map((entry) => wrapMiddleware(entry, { allowBareCommands: true })));
  }

  command(command, ...middleware) {
    return super.command(command, ...middleware.map((entry) => wrapMiddleware(entry, { allowBareCommands: true })));
  }
}

grammy.Bot = NexusRoutingBot;

routingLog('info', 'Telegram routing controls initialized', router.config);

require('./telegram_gateway_bot');
