'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { Readable } = require('node:stream');

const { collectOpenAIChatStream } = require('./openai_stream');

test('collectOpenAIChatStream ignores heartbeats and joins split deltas', async () => {
  const stream = Readable.from([
    ': nexus-keepalive\n\n',
    'data: {"choices":[{"delta":{"content":"hel"}}]}\n',
    '\ndata: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
    'data: [DONE]\n\n',
  ]);

  assert.equal(await collectOpenAIChatStream(stream), 'hello');
});

test('collectOpenAIChatStream surfaces gateway SSE errors', async () => {
  const stream = Readable.from([
    'data: {"error":{"message":"backend overloaded","code":"429"}}\n\n',
  ]);

  await assert.rejects(collectOpenAIChatStream(stream), /backend overloaded/);
});

test('collectOpenAIChatStream marks failures after response data starts', async () => {
  async function* chunks() {
    yield 'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n';
    throw new Error('socket closed');
  }

  await assert.rejects(
    collectOpenAIChatStream(Readable.from(chunks())),
    (err) => err.message === 'socket closed' && err.nexusStreamStarted === true,
  );
});
