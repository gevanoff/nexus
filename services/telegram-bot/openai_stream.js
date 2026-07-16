'use strict';

function streamError(payload) {
  const detail = payload && typeof payload === 'object' ? payload : {};
  const err = new Error(String(detail.message || 'Gateway streaming request failed'));
  err.code = detail.code || 'GATEWAY_STREAM_ERROR';
  err.gatewayError = detail;
  return err;
}

function consumeEventBlock(block, state) {
  for (const line of String(block || '').split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data:')) continue;
    const data = trimmed.slice(5).trim();
    if (!data || data === '[DONE]') continue;

    let event;
    try {
      event = JSON.parse(data);
    } catch {
      continue;
    }
    if (!event || typeof event !== 'object') continue;
    if (event.error && typeof event.error === 'object') {
      throw streamError(event.error);
    }

    const choice = Array.isArray(event.choices) ? event.choices[0] : null;
    const delta = choice && typeof choice.delta === 'object' ? choice.delta : null;
    const message = choice && typeof choice.message === 'object' ? choice.message : null;
    if (delta && typeof delta.content === 'string') state.answer += delta.content;
    if (message && typeof message.content === 'string') state.answer += message.content;
  }
}

async function collectOpenAIChatStream(readable) {
  const state = { answer: '' };
  let buffer = '';
  let receivedData = false;
  try {
    for await (const chunk of readable) {
      if (!chunk || !chunk.length) continue;
      receivedData = true;
      buffer += Buffer.isBuffer(chunk) ? chunk.toString('utf8') : String(chunk);
      buffer = buffer.replace(/\r\n/g, '\n');
      while (true) {
        const splitAt = buffer.indexOf('\n\n');
        if (splitAt < 0) break;
        const block = buffer.slice(0, splitAt);
        buffer = buffer.slice(splitAt + 2);
        consumeEventBlock(block, state);
      }
    }
    if (buffer.trim()) consumeEventBlock(buffer, state);
    return state.answer;
  } catch (err) {
    if (err && typeof err === 'object') err.nexusStreamStarted = receivedData;
    throw err;
  }
}

module.exports = { collectOpenAIChatStream };
