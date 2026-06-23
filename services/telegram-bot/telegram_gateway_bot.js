const { Bot, InputFile } = require('grammy');
const axios = require('axios');

const TELEGRAM_TOKEN = process.env.TELEGRAM_TOKEN;
const GATEWAY_PORT = Number.parseInt(process.env.GATEWAY_PORT || '8800', 10);
const GATEWAY_SCHEME = process.env.GATEWAY_SCHEME || 'https';
const GATEWAY_HOST = process.env.GATEWAY_HOST || '127.0.0.1';
const GATEWAY_BASE_URL_RAW = String(process.env.GATEWAY_BASE_URL || '').trim();
const GATEWAY_BASE_URL = GATEWAY_BASE_URL_RAW || `${GATEWAY_SCHEME}://${GATEWAY_HOST}:${GATEWAY_PORT}`;
const GATEWAY_URL = `${GATEWAY_BASE_URL}/v1/chat/completions`;
const GATEWAY_BEARER_TOKEN = process.env.GATEWAY_BEARER_TOKEN;
const GATEWAY_MODEL = process.env.GATEWAY_MODEL || 'fast';
const SYSTEM_PROMPT = process.env.SYSTEM_PROMPT || '';
const MAX_HISTORY = Number.parseInt(process.env.MAX_HISTORY || '20', 10);
const TELEGRAM_MAX_MESSAGE = Number.parseInt(process.env.TELEGRAM_MAX_MESSAGE || '3900', 10);
const LOG_LEVEL = String(process.env.LOG_LEVEL || 'info').toLowerCase();
const LOG_PREVIEW_CHARS = Number.parseInt(process.env.LOG_PREVIEW_CHARS || '320', 10);
const GATEWAY_SOCKET_TIMEOUT_MS = 60000;
const FALLBACK_CLARIFICATION_REPLY = 'I need a bit more context to answer reliably. Please clarify what you want to know or share the earlier message.';
const THINK_BLOCK_RE = /<think>[\s\S]*?<\/think>/gi;
const THINK_TAG_RE = /<\/?think>/gi;
const INTERNAL_SCRATCHPAD_PATTERNS = [
  /\bmaybe the user is asking\b/i,
  /\bthe previous messages? (?:are|were) not clear\b/i,
  /\b(?:i need to|i should|let me)\b[\s\S]{0,80}\bclarif(?:y|ication)\b/i,
  /\b(?:i need to|i should|let me)\b[\s\S]{0,80}\bask\b[\s\S]{0,80}\buser\b/i,
  /\b(?:internal reasoning|chain[- ]of[- ]thought|scratchpad)\b/i,
];

if (!TELEGRAM_TOKEN) {
  throw new Error('Missing TELEGRAM_TOKEN');
}

if (!GATEWAY_BEARER_TOKEN) {
  throw new Error('Missing GATEWAY_BEARER_TOKEN');
}

if (Number.isNaN(MAX_HISTORY) || MAX_HISTORY < 1) {
  throw new Error('MAX_HISTORY must be a positive integer');
}

if (Number.isNaN(TELEGRAM_MAX_MESSAGE) || TELEGRAM_MAX_MESSAGE < 500) {
  throw new Error('TELEGRAM_MAX_MESSAGE must be a positive integer >= 500');
}

if (!GATEWAY_BASE_URL_RAW && (Number.isNaN(GATEWAY_PORT) || GATEWAY_PORT < 1 || GATEWAY_PORT > 65535)) {
  throw new Error('GATEWAY_PORT must be a valid TCP port');
}

if (Number.isNaN(LOG_PREVIEW_CHARS) || LOG_PREVIEW_CHARS < 0) {
  throw new Error('LOG_PREVIEW_CHARS must be a non-negative integer');
}

if (Number.isNaN(GATEWAY_SOCKET_TIMEOUT_MS) || GATEWAY_SOCKET_TIMEOUT_MS < 1000) {
  throw new Error('GATEWAY_SOCKET_TIMEOUT_MS must be a positive integer >= 1000');
}

const bot = new Bot(TELEGRAM_TOKEN);
const histories = new Map();

const COMMANDS = [
  { command: 'start', description: 'Start the bot and show welcome message' },
  { command: 'help', description: 'Show available commands' },
  { command: 'reset', description: 'Clear conversation history for this chat' },
  { command: 'history', description: 'Export conversation history as a text file' },
  { command: 'me', description: 'Show bot profile information' },
  { command: 'whoami', description: 'Show your chat membership status' },
  { command: 'chatinfo', description: 'Show chat metadata' },
  { command: 'link', description: 'Link this private chat to your Nexus account: /link code' },
  { command: 'poll', description: 'Create a poll: /poll Question | option 1 | option 2' },
  { command: 'image', description: 'Generate an image: /image prompt' },
  { command: 'scan', description: 'Run OCR against an image URL: /scan https://...' },
  { command: 'speech', description: 'Generate speech audio: /speech text' },
  { command: 'music', description: 'Generate music: /music prompt' },
];

function shouldLog(level) {
  const levels = ['error', 'warn', 'info', 'debug'];
  const current = levels.indexOf(LOG_LEVEL);
  const target = levels.indexOf(level);
  if (current === -1 || target === -1) {
    return true;
  }
  return target <= current;
}

function log(level, message, meta = {}) {
  if (!shouldLog(level)) {
    return;
  }
  const entry = {
    level,
    message,
    time: new Date().toISOString(),
    ...meta,
  };
  const line = JSON.stringify(entry);
  if (level === 'error') {
    console.error(line);
  } else if (level === 'warn') {
    console.warn(line);
  } else {
    console.log(line);
  }
}

function previewText(text) {
  const content = String(text || '');
  if (!LOG_PREVIEW_CHARS) {
    return undefined;
  }
  if (content.length <= LOG_PREVIEW_CHARS) {
    return content;
  }
  return `${content.slice(0, LOG_PREVIEW_CHARS)}…`;
}

function getHistory(chatId) {
  if (!histories.has(chatId)) {
    const initial = [];
    if (SYSTEM_PROMPT) {
      initial.push({ role: 'system', content: SYSTEM_PROMPT });
    }
    histories.set(chatId, initial);
  }
  return histories.get(chatId);
}

function trimHistory(history) {
  const system = history[0]?.role === 'system' ? [history[0]] : [];
  const rest = system.length ? history.slice(1) : history;
  const trimmed = rest.slice(-MAX_HISTORY);
  return [...system, ...trimmed];
}

function buildHelpText() {
  const commandLines = COMMANDS.map((entry) => `/${entry.command} - ${entry.description}`);
  return [
    'Available commands:',
    ...commandLines,
    '',
    'Send any other message to chat with the Gateway.',
  ].join('\n');
}

function splitMessage(text, maxLen) {
  const chunks = [];
  const normalized = String(text || '');
  if (normalized.length <= maxLen) {
    return [normalized];
  }

  const paragraphs = normalized.split(/\n{2,}/);
  let current = '';

  for (const paragraph of paragraphs) {
    const candidate = current ? `${current}\n\n${paragraph}` : paragraph;
    if (candidate.length <= maxLen) {
      current = candidate;
      continue;
    }

    if (current) {
      chunks.push(current);
      current = '';
    }

    if (paragraph.length <= maxLen) {
      current = paragraph;
      continue;
    }

    let start = 0;
    while (start < paragraph.length) {
      chunks.push(paragraph.slice(start, start + maxLen));
      start += maxLen;
    }
  }

  if (current) {
    chunks.push(current);
  }

  return chunks;
}

function splitReplySegments(text) {
  const segments = [];
  const lines = String(text || '').split(/\n+/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      continue;
    }
    const parts = trimmed.match(/[^.!?]+[.!?]?/g);
    if (Array.isArray(parts) && parts.length) {
      for (const part of parts) {
        const segment = part.trim();
        if (segment) {
          segments.push(segment);
        }
      }
      continue;
    }
    segments.push(trimmed);
  }
  return segments;
}

function normalizeSegment(text) {
  return String(text || '').toLowerCase().replace(/\s+/g, ' ').trim();
}

function collapseRepeatedSegments(text) {
  const segments = splitReplySegments(text);
  if (segments.length < 4) {
    return { text: String(text || '').trim(), repeatedSegment: '', repeatedCount: 0 };
  }

  const counts = new Map();
  for (const segment of segments) {
    const key = normalizeSegment(segment);
    if (!key) {
      continue;
    }
    counts.set(key, (counts.get(key) || 0) + 1);
  }

  let repeatedSegment = '';
  let repeatedCount = 0;
  for (const [key, count] of counts.entries()) {
    if (count > repeatedCount) {
      repeatedSegment = key;
      repeatedCount = count;
    }
  }

  if (repeatedCount < 4 || repeatedCount / Math.max(1, segments.length) < 0.5) {
    return { text: String(text || '').trim(), repeatedSegment: '', repeatedCount: 0 };
  }

  const collapsed = [];
  let previousKey = '';
  for (const segment of segments) {
    const key = normalizeSegment(segment);
    if (!key) {
      continue;
    }
    if (key === previousKey) {
      continue;
    }
    collapsed.push(segment.trim());
    previousKey = key;
  }

  return {
    text: collapsed.join('\n').trim(),
    repeatedSegment,
    repeatedCount,
  };
}

function looksLikeInternalScratchpad(text) {
  const normalized = String(text || '').trim();
  if (!normalized) {
    return true;
  }
  let matches = 0;
  for (const pattern of INTERNAL_SCRATCHPAD_PATTERNS) {
    if (pattern.test(normalized)) {
      matches += 1;
    }
  }
  return matches >= 2;
}

function stripLeadingScratchpadParagraphs(text) {
  const paragraphs = String(text || '').split(/\n{2,}/).map((paragraph) => paragraph.trim()).filter(Boolean);
  if (paragraphs.length < 2) {
    return { text: String(text || '').trim(), droppedCount: 0 };
  }

  let index = 0;
  while (index < paragraphs.length - 1 && looksLikeInternalScratchpad(paragraphs[index])) {
    index += 1;
  }

  if (!index) {
    return { text: String(text || '').trim(), droppedCount: 0 };
  }

  return {
    text: paragraphs.slice(index).join('\n\n').trim(),
    droppedCount: index,
  };
}

function sanitizeAssistantReply(text) {
  const raw = String(text || '');
  const strippedThinkBlocks = THINK_BLOCK_RE.test(raw);
  let content = raw.replace(THINK_BLOCK_RE, ' ').replace(THINK_TAG_RE, ' ').trim();
  const strippedScratchpad = stripLeadingScratchpadParagraphs(content);
  content = strippedScratchpad.text;
  const repetition = collapseRepeatedSegments(content);
  content = repetition.text;

  let replacedWithFallback = false;
  if (!content || looksLikeInternalScratchpad(content)) {
    content = FALLBACK_CLARIFICATION_REPLY;
    replacedWithFallback = true;
  }

  return {
    content,
    meta: {
      strippedThinkBlocks,
      droppedScratchpadParagraphs: strippedScratchpad.droppedCount,
      repeatedSegment: repetition.repeatedSegment,
      repeatedCount: repetition.repeatedCount,
      replacedWithFallback,
    },
  };
}

function extractTextFromContentParts(content) {
  if (typeof content === 'string') {
    return content;
  }
  if (Array.isArray(content)) {
    const parts = [];
    for (const item of content) {
      if (typeof item === 'string' && item.trim()) {
        parts.push(item.trim());
        continue;
      }
      if (!item || typeof item !== 'object') {
        continue;
      }
      const kind = String(item.type || '').trim().toLowerCase();
      const text = typeof item.text === 'string' ? item.text.trim() : '';
      if (text && (!kind || kind === 'text' || kind === 'output_text' || kind === 'input_text')) {
        parts.push(text);
      }
    }
    return parts.join('\n').trim();
  }
  if (content && typeof content === 'object') {
    if (typeof content.text === 'string' && content.text.trim()) {
      return content.text.trim();
    }
    if (Array.isArray(content.content)) {
      return extractTextFromContentParts(content.content);
    }
  }
  return '';
}

function extractAssistantText(payload) {
  if (!payload || typeof payload !== 'object') {
    return '';
  }

  const message = payload.choices?.[0]?.message;
  if (message && typeof message === 'object') {
    const contentText = extractTextFromContentParts(message.content);
    if (contentText) {
      return contentText;
    }
    if (typeof message.refusal === 'string' && message.refusal.trim()) {
      return message.refusal.trim();
    }
  }

  if (Array.isArray(payload.output)) {
    for (const item of payload.output) {
      if (!item || typeof item !== 'object') {
        continue;
      }
      const contentText = extractTextFromContentParts(item.content);
      if (contentText) {
        return contentText;
      }
    }
  }

  if (typeof payload.output_text === 'string' && payload.output_text.trim()) {
    return payload.output_text.trim();
  }

  return '';
}

async function replyLongText(ctx, text) {
  const content = String(text || '');
  if (!content.trim()) {
    await ctx.reply(FALLBACK_CLARIFICATION_REPLY);
    return;
  }

  const chunks = splitMessage(content, TELEGRAM_MAX_MESSAGE);
  if (chunks.length > 12) {
    const buffer = Buffer.from(content, 'utf8');
    await ctx.replyWithDocument(new InputFile(buffer, `chat-${ctx.chat.id}-response.txt`), {
      caption: 'Response was too long for chat; sending as a file.',
    });
    return;
  }

  for (const chunk of chunks) {
    await ctx.reply(chunk);
  }
}

async function handleHistoryExport(ctx, history) {
  if (!history.length) {
    await ctx.reply('No history available yet.');
    return;
  }
  const lines = history
    .filter((entry) => entry.role && entry.content)
    .map((entry) => `[${entry.role}] ${entry.content}`)
    .join('\n\n');
  const buffer = Buffer.from(lines, 'utf8');
  await ctx.replyWithDocument(new InputFile(buffer, `chat-${ctx.chat.id}-history.txt`), {
    caption: 'Conversation history.',
  });
}

async function handlePoll(ctx, args) {
  const segments = args
    .split('|')
    .map((segment) => segment.trim())
    .filter(Boolean);
  const [question, ...options] = segments;
  if (!question || options.length < 2) {
    await ctx.reply('Usage: /poll Question | option 1 | option 2 (at least two options required).');
    return;
  }
  await ctx.api.sendPoll(ctx.chat.id, question, options, { is_anonymous: false });
}

async function fetchBinary(url) {
  const res = await axios.get(url, {
    responseType: 'arraybuffer',
    timeout: GATEWAY_SOCKET_TIMEOUT_MS,
  });
  return { buffer: Buffer.from(res.data), contentType: res.headers['content-type'] || '' };
}

function isJsonContentType(contentType) {
  return String(contentType || '').toLowerCase().includes('application/json');
}

async function sendAck(ctx, text) {
  try {
    await ctx.reply(text);
  } catch (err) {
    log('warn', 'Failed to send ack', {
      chatId: ctx.chat?.id,
      error: err?.message || String(err),
    });
  }
}

function buildGatewayUrl(path) {
  if (!path) {
    return '';
  }
  if (path.startsWith('http://') || path.startsWith('https://')) {
    return path;
  }
  if (!path.startsWith('/')) {
    return `${GATEWAY_BASE_URL}/${path}`;
  }
  return `${GATEWAY_BASE_URL}${path}`;
}

async function handleImageCommand(ctx, prompt) {
  if (!prompt.trim()) {
    await ctx.reply('Usage: /image <prompt>');
    return true;
  }

  const res = await axios.post(
    `${GATEWAY_BASE_URL}/v1/images/generations`,
    { prompt, size: '1024x1024', n: 1, response_format: 'b64_json' },
    {
      headers: {
        Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
        'Content-Type': 'application/json',
      },
      timeout: GATEWAY_SOCKET_TIMEOUT_MS,
    },
  );

  const first = res.data?.data?.[0];
  const b64 = first?.b64_json;
  if (typeof b64 === 'string' && b64.trim()) {
    const buffer = Buffer.from(b64, 'base64');
    await ctx.replyWithPhoto(new InputFile(buffer, 'image.png'));
    return true;
  }

  const url = first?.url;
  if (typeof url === 'string' && url.trim()) {
    await ctx.reply(`[Image] Generated image URL: ${url.trim()}`);
    return true;
  }

  await ctx.reply('[Image] generation returned no usable image.');
  return true;
}

async function handleSpeechCommand(ctx, prompt) {
  if (!prompt.trim()) {
    await ctx.reply('Usage: /speech <text>');
    return true;
  }

  const res = await axios.post(`${GATEWAY_BASE_URL}/v1/audio/speech`, { text: prompt }, {
    headers: {
      Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
      'Content-Type': 'application/json',
    },
    timeout: GATEWAY_SOCKET_TIMEOUT_MS,
    responseType: 'arraybuffer',
  });

  const contentType = res.headers['content-type'] || '';
  const byteLength = res.data ? Buffer.byteLength(res.data) : 0;
  log('info', 'Speech response metadata', {
    chatId: ctx.chat?.id,
    status: res.status,
    contentType,
    byteLength,
  });
  if (isJsonContentType(contentType)) {
    const payload = JSON.parse(Buffer.from(res.data).toString('utf8'));
    const url = payload?.audio_url || payload?.url;
    if (url) {
      const { buffer, contentType: fetchedType } = await fetchBinary(buildGatewayUrl(String(url)));
      if (!buffer.length) {
        await ctx.reply('[Speech] synthesis returned empty audio.');
        return true;
      }
      const ext = String(fetchedType || '').includes('wav') ? 'wav' : 'mp3';
      await ctx.replyWithAudio(new InputFile(buffer, `speech.${ext}`));
      return true;
    }
    await ctx.reply('[Speech] synthesis returned no audio URL.');
    return true;
  }

  const buffer = Buffer.from(res.data || []);
  if (!buffer.length) {
    await ctx.reply('[Speech] synthesis returned empty audio.');
    return true;
  }

  const ext = String(contentType).includes('wav') ? 'wav' : 'mp3';
  await ctx.replyWithAudio(new InputFile(buffer, `speech.${ext}`));
  return true;
}

async function handleMusicCommand(ctx, prompt) {
  const style = prompt.trim();
  if (!style) {
    await ctx.reply('Usage: /music <prompt>');
    return true;
  }

  const res = await axios.post(
    `${GATEWAY_BASE_URL}/v1/music/generations`,
    { prompt: style },
    {
      headers: {
        Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
        'Content-Type': 'application/json',
      },
      timeout: GATEWAY_SOCKET_TIMEOUT_MS,
    },
  );

  const audioUrl = res.data?.audio_url;
  if (!audioUrl) {
    await ctx.reply('[Music] generation returned no audio URL.');
    return true;
  }

  const full = buildGatewayUrl(String(audioUrl));
  const { buffer, contentType } = await fetchBinary(full);
  if (!buffer.length) {
    await ctx.reply('[Music] generation returned empty audio.');
    return true;
  }
  const ext = contentType.includes('wav') ? 'wav' : 'mp3';
  await ctx.replyWithAudio(new InputFile(buffer, `music.${ext}`));
  return true;
}

function extractOcrText(payload) {
  if (payload && typeof payload === 'object') {
    if (typeof payload.text === 'string' && payload.text.trim()) {
      return payload.text.trim();
    }
    if (Array.isArray(payload.data)) {
      const parts = [];
      for (const item of payload.data) {
        if (!item || typeof item !== 'object') continue;
        for (const key of ['text', 'raw_text', 'transcript', 'generated_text']) {
          if (typeof item[key] === 'string' && item[key].trim()) {
            parts.push(item[key].trim());
            break;
          }
        }
        if (Array.isArray(item.lines)) {
          for (const line of item.lines) {
            if (line && typeof line.text === 'string' && line.text.trim()) {
              parts.push(line.text.trim());
            }
          }
        }
      }
      if (parts.length) return parts.join('\n');
    }
  }
  return '';
}

async function handleScanCommand(ctx, imageUrl) {
  const url = imageUrl.trim();
  if (!url) {
    await ctx.reply('Usage: /scan <image_url>');
    return true;
  }

  const res = await axios.post(
    `${GATEWAY_BASE_URL}/v1/ocr`,
    { image_url: url },
    {
      headers: {
        Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
        'Content-Type': 'application/json',
      },
      timeout: GATEWAY_SOCKET_TIMEOUT_MS,
    },
  );

  let text = extractOcrText(res.data);
  if (!text) {
    text = JSON.stringify(res.data, null, 2);
  }
  const warning = res.data?._gateway?.ocr_warning;
  if (typeof warning === 'string' && warning.trim()) {
    text = `${text}\n\n[Scan note] ${warning.trim()}`;
  }
  await replyLongText(ctx, text);
  return true;
}

async function handleLinkCommand(ctx, code) {
  const rawCode = String(code || '').trim();
  if (!rawCode) {
    await ctx.reply('Usage: /link <code>');
    return true;
  }
  if (String(ctx.chat?.type || '') !== 'private') {
    await ctx.reply('Run /link in a direct chat with the bot so notifications go to your private Telegram chat.');
    return true;
  }

  const payload = {
    code: rawCode,
    chat_id: String(ctx.chat.id),
    chat_type: String(ctx.chat?.type || ''),
    telegram_user_id: ctx.from?.id ? String(ctx.from.id) : '',
    username: ctx.from?.username || '',
  };

  try {
    const res = await axios.post(
      `${GATEWAY_BASE_URL}/v1/telegram/link`,
      payload,
      {
        headers: {
          Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
          'Content-Type': 'application/json',
        },
        timeout: GATEWAY_SOCKET_TIMEOUT_MS,
      },
    );
    const linkedUser = String(res.data?.username || '').trim();
    if (linkedUser) {
      await ctx.reply(`Linked this Telegram chat to Nexus user '${linkedUser}'.`);
    } else {
      await ctx.reply('Linked this Telegram chat to your Nexus account.');
    }
    return true;
  } catch (err) {
    let detail = 'link_failed';
    if (axios.isAxiosError(err)) {
      detail = String(err.response?.data?.detail || err.message || 'link_failed');
    } else if (err) {
      detail = err.message || String(err);
    }
    if (detail === 'code_invalid' || detail === 'code_expired') {
      await ctx.reply('That link code is invalid or expired. Generate a new one from Nexus Settings and try again.');
      return true;
    }
    await ctx.reply(`Unable to link this chat right now (${detail}).`);
    return true;
  }
}

function parseSlashCommand(text) {
  const raw = String(text || '').trim();
  const match = raw.match(/^\/([a-z0-9_]+)(?:@[a-z0-9_]+)?(?:\s+([\s\S]*))?$/i);
  if (!match) return null;
  return {
    name: String(match[1] || '').toLowerCase(),
    args: String(match[2] || '').trim(),
  };
}

async function maybeHandleSlashCommand(ctx, text) {
  const raw = String(text || '').trim();
  if (!raw.startsWith('/')) {
    return false;
  }

  const command = parseSlashCommand(raw);
  if (!command) {
    return false;
  }

  if (command.name === 'image') {
    const prompt = command.args;
    await sendAck(ctx, 'Generating image…');
    return handleImageCommand(ctx, prompt);
  }
  if (command.name === 'speech' || command.name === 'tts') {
    const prompt = command.args;
    await sendAck(ctx, 'Synthesizing speech…');
    return handleSpeechCommand(ctx, prompt);
  }
  if (command.name === 'music') {
    const prompt = command.args;
    await sendAck(ctx, 'Generating music…');
    return handleMusicCommand(ctx, prompt);
  }
  if (command.name === 'scan') {
    const imageUrl = command.args;
    await sendAck(ctx, 'Scanning image…');
    return handleScanCommand(ctx, imageUrl);
  }
  if (command.name === 'link') {
    return handleLinkCommand(ctx, command.args);
  }

  return false;
}

async function queryGateway(history, message) {
  const payload = {
    model: GATEWAY_MODEL,
    messages: [...history, { role: 'user', content: message }],
    stream: false,
  };

  try {
    const res = await axios.post(GATEWAY_URL, payload, {
      headers: {
        Authorization: `Bearer ${GATEWAY_BEARER_TOKEN}`,
        'Content-Type': 'application/json',
      },
      timeout: GATEWAY_SOCKET_TIMEOUT_MS,
    });

    return extractAssistantText(res.data);
  } catch (err) {
    if (axios.isAxiosError(err)) {
      log('error', 'Gateway request failed', {
        error: err.message,
        code: err.code,
        status: err.response?.status,
        statusText: err.response?.statusText,
        url: err.config?.url,
        timeout: err.config?.timeout,
        response: err.response?.data,
      });
    } else {
      log('error', 'Gateway request failed', { error: err?.message || String(err) });
    }
    throw err;
  }
}


bot.command('start', async (ctx) => {
  await ctx.reply('Welcome! Send a message to chat with the Gateway. Use /link <code> from Nexus Settings to connect this private chat for notifications.');
});

bot.command('help', async (ctx) => {
  await ctx.reply(buildHelpText());
});

bot.command('reset', async (ctx) => {
  histories.delete(ctx.chat.id);
  await ctx.reply('Conversation reset.');
});

bot.command('history', async (ctx) => {
  await handleHistoryExport(ctx, getHistory(ctx.chat.id));
});

bot.command('me', async (ctx) => {
  const me = await bot.api.getMe();
  await ctx.reply(`Bot: ${me.first_name}${me.username ? ` (@${me.username})` : ''} | ID: ${me.id}`);
});

bot.command('whoami', async (ctx) => {
  if (!ctx.from?.id) {
    await ctx.reply('Unable to determine your user ID.');
    return;
  }
  const member = await ctx.api.getChatMember(ctx.chat.id, ctx.from.id);
  await ctx.reply(`You are ${member.status} in this chat.${member.user?.username ? ` (@${member.user.username})` : ''}`);
});

bot.command('chatinfo', async (ctx) => {
  const chat = await ctx.api.getChat(ctx.chat.id);
  const name = chat.title || chat.username || chat.first_name || 'this chat';
  const description = chat.description ? `\nDescription: ${chat.description}` : '';
  await ctx.reply(`Chat: ${name}\nType: ${chat.type}\nID: ${chat.id}${description}`);
});

bot.command('poll', async (ctx) => {
  const args = String(ctx.match || '').trim();
  await handlePoll(ctx, args);
});

async function handleIncomingText(ctx, text, source) {
  const userText = String(text || '');
  if (!userText.trim()) {
    return;
  }

  log('info', 'Incoming Telegram message', {
    chatId: ctx.chat?.id,
    userId: ctx.from?.id,
    username: ctx.from?.username,
    source,
    textPreview: previewText(userText),
  });

  const history = getHistory(ctx.chat.id);

  try {
    await ctx.api.sendChatAction(ctx.chat.id, 'typing');
  } catch (err) {
    log('warn', 'Failed to send chat action', {
      chatId: ctx.chat?.id,
      error: err?.message || String(err),
    });
  }

  try {
    if (await maybeHandleSlashCommand(ctx, userText)) {
      return;
    }
    const gatewayAnswer = await queryGateway(history, userText);
    const sanitized = sanitizeAssistantReply(gatewayAnswer);
    const answer = sanitized.content;
    history.push({ role: 'user', content: userText });
    history.push({ role: 'assistant', content: answer });
    histories.set(ctx.chat.id, trimHistory(history));
    log('info', 'Sending Telegram reply', {
      chatId: ctx.chat?.id,
      userId: ctx.from?.id,
      textPreview: previewText(answer),
      strippedThinkBlocks: sanitized.meta.strippedThinkBlocks,
      droppedScratchpadParagraphs: sanitized.meta.droppedScratchpadParagraphs,
      repeatedSegmentCount: sanitized.meta.repeatedCount,
      replacedWithFallback: sanitized.meta.replacedWithFallback,
    });
    await replyLongText(ctx, answer);
  } catch (err) {
    log('error', 'Chat handling failed', {
      chatId: ctx.chat?.id,
      userId: ctx.from?.id,
      error: err?.message || String(err),
    });
    await ctx.reply('Error talking to the gateway.');
  }
}

bot.on('message:text', async (ctx) => {
  await handleIncomingText(ctx, ctx.message?.text, 'message');
});

bot.on('channel_post:text', async (ctx) => {
  await handleIncomingText(ctx, ctx.channelPost?.text, 'channel_post');
});

bot.catch((err) => {
  const error = err?.error || err?.message || err;
  log('error', 'Telegram bot error', {
    error: error?.message || String(error),
    stack: error?.stack,
  });
});

async function startBot() {
  const me = await bot.api.getMe();
  log('info', 'Telegram bot authenticated', {
    botId: me.id,
    username: me.username,
  });

  await bot.api.setMyCommands(COMMANDS);
  bot.start();
  console.log('Telegram gateway bot is running.');
}

startBot().catch((err) => {
  log('error', 'Telegram bot startup failed', {
    error: err?.message || String(err),
    stack: err?.stack,
  });
  process.exit(1);
});
