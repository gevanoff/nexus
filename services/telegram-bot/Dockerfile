FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY telegram_gateway_bot.js telegram_group_routing.js telegram_memory.js openai_stream.js healthcheck.js ./
RUN chown -R node:node /app \
  && chmod 755 /app \
  && chmod 644 /app/package.json /app/telegram_gateway_bot.js /app/telegram_group_routing.js /app/telegram_memory.js /app/openai_stream.js /app/healthcheck.js

USER node

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=1 \
  CMD node /app/healthcheck.js

CMD ["node", "telegram_gateway_bot.js"]
