FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --no-audit --no-fund

COPY telegram_gateway_bot.js ./

USER node

CMD ["node", "telegram_gateway_bot.js"]
