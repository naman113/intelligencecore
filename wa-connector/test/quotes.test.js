// Quick 021: replies quote the message they answer (quotes.js). The quote is checked
// against the REAL Baileys 7.0.0-rc.9 builder (generateWAMessageFromContent, the same
// function sendMessage runs), so the contextInfo WhatsApp receives is what these tests
// assert. No socket, no network. All numbers and ids are made up.
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { generateWAMessageFromContent, proto } from '@whiskeysockets/baileys';
import {
  rememberInbound, recallInbound, clearQuoteCache, quoteCacheSize, buildQuoted,
  participantJid, QUOTE_CACHE_MAX, QUOTE_TTL_MS,
} from '../quotes.js';
import { rememberLid } from '../senders.js';

const PHONE = '15550001111';
const LID = '987650000012345';
const OTHER = '15550002222';
const GROUP = '120363000000000001@g.us';
const ME = '15559990000:7@s.whatsapp.net';
const sock = (map = {}) => ({
  user: { id: ME },
  signalRepository: { lidMapping: {
    async getPNForLID(jid) {
      const pn = map[jid.split('@')[0]];
      return pn ? `${pn}:0@s.whatsapp.net` : null;
    },
  } },
});

// What Baileys would put on the wire for a text reply with this quote.
function wire(to, quoted) {
  const m = generateWAMessageFromContent(
    to, { extendedTextMessage: { text: 'answer' } }, { quoted, userJid: ME });
  return m.message.extendedTextMessage.contextInfo;
}
const validate = (to, q) => wire(to, q);

function inbound({ id, remoteJid, remoteJidAlt, participant, text = 'how many cases are open' }) {
  return {
    key: { remoteJid, remoteJidAlt, participant, fromMe: false, id },
    message: proto.Message.fromObject({ conversation: text }),
  };
}

beforeEach(() => clearQuoteCache());

test('DM from a phone JID: the reply quotes it in the same chat, author = that person', async () => {
  rememberInbound(inbound({ id: 'A1', remoteJid: `${PHONE}@s.whatsapp.net` }));
  const to = `${PHONE}@s.whatsapp.net`;
  const q = await buildQuoted(sock(), to, { id: 'A1' }, { validate });
  const ctx = wire(to, q);
  assert.equal(ctx.stanzaId, 'A1');
  assert.equal(ctx.participant, to);
  assert.equal(ctx.quotedMessage.conversation, 'how many cases are open');
  assert.equal(ctx.remoteJid, undefined, 'same chat: no cross-chat remoteJid');
});

test('DM that came from <lid>@lid, reply sent to the phone (Quick 017): rebuilt on the phone', async () => {
  rememberInbound(inbound({ id: 'A2', remoteJid: `${LID}@lid` }));
  const to = `${PHONE}@s.whatsapp.net`;
  const q = await buildQuoted(sock({ [LID]: PHONE }), to, { id: 'A2' }, { validate });
  const ctx = wire(to, q);
  assert.equal(ctx.stanzaId, 'A2');
  assert.equal(ctx.participant, to);
  assert.equal(ctx.remoteJid, undefined);
  // the alt form on the key is enough too
  rememberInbound(inbound({ id: 'A3', remoteJid: `${LID}@lid`, remoteJidAlt: `${PHONE}@s.whatsapp.net` }));
  assert.ok(await buildQuoted(sock(), to, { id: 'A3' }, { validate }));
});

test('DM reply to an unresolved private ID goes to <lid>@lid and quotes there', async () => {
  rememberInbound(inbound({ id: 'A4', remoteJid: `${LID}@lid` }));
  const to = `${LID}@lid`;
  const ctx = wire(to, await buildQuoted(sock(), to, { id: 'A4' }, { validate }));
  assert.equal(ctx.participant, to);
  assert.equal(ctx.remoteJid, undefined);
});

test('a quote is never put on a message to someone else', async () => {
  rememberInbound(inbound({ id: 'A5', remoteJid: `${PHONE}@s.whatsapp.net` }));
  assert.equal(await buildQuoted(sock(), `${OTHER}@s.whatsapp.net`, { id: 'A5' }, { validate }), null);
  assert.equal(await buildQuoted(sock(), GROUP, { id: 'A5' }, { validate }), null);
  rememberInbound(inbound({ id: 'G0', remoteJid: GROUP, participant: `${LID}@lid` }));
  assert.equal(await buildQuoted(sock(), `${PHONE}@s.whatsapp.net`, { id: 'G0' }, { validate }), null);
});

test('group: the cached message as WhatsApp delivered it (participant in LID form)', async () => {
  rememberInbound(inbound({ id: 'G1', remoteJid: GROUP, participant: `${LID}@lid`, text: '@bot status?' }));
  const ctx = wire(GROUP, await buildQuoted(sock(), GROUP, { id: 'G1', chat: GROUP }, { validate }));
  assert.equal(ctx.stanzaId, 'G1');
  assert.equal(ctx.participant, `${LID}@lid`);
  assert.equal(ctx.quotedMessage.conversation, '@bot status?');
  assert.equal(ctx.remoteJid, undefined);
});

test('not cached (restart): minimal quote from the gateway text; group needs the sender', async () => {
  const to = `${PHONE}@s.whatsapp.net`;
  const dm = wire(to, await buildQuoted(sock(), to, { id: 'R1', text: 'hi there' }, { validate }));
  assert.equal(dm.stanzaId, 'R1');
  assert.equal(dm.quotedMessage.conversation, 'hi there');
  assert.equal(dm.participant, to);

  const g = await buildQuoted(sock(), GROUP, { id: 'R2', chat: GROUP, participant: PHONE, text: 'status?' },
    { validate });
  assert.equal(wire(GROUP, g).participant, `${PHONE}@s.whatsapp.net`);
  const lidSender = await buildQuoted(sock(), GROUP,
    { id: 'R3', chat: GROUP, participant: `${LID}@lid`, text: 'status?' }, { validate });
  assert.equal(wire(GROUP, lidSender).participant, `${LID}@lid`);

  assert.equal(await buildQuoted(sock(), GROUP, { id: 'R4', chat: GROUP, text: 'x' }, { validate }), null,
    'no sender: unquoted');
  assert.equal(await buildQuoted(sock(), to, { id: 'R5' }, { validate }), null, 'no text: unquoted');
  assert.equal(await buildQuoted(sock(), GROUP, { id: 'R6', chat: 'other@g.us', participant: PHONE, text: 'x' },
    { validate }), null, 'another group: unquoted');
});

test('bad input never throws and never quotes', async () => {
  assert.equal(await buildQuoted(sock(), `${PHONE}@s.whatsapp.net`, null), null);
  assert.equal(await buildQuoted(sock(), '', { id: 'X' }), null);
  // a cached entry without content is never kept
  rememberInbound({ key: { id: 'E1', remoteJid: `${PHONE}@s.whatsapp.net` } });
  assert.equal(recallInbound('E1'), null);
  // content Baileys cannot name is never quoted (it would quote an empty message)
  rememberInbound({ key: { id: 'E4', remoteJid: `${PHONE}@s.whatsapp.net` },
    message: { messageContextInfo: {} } });
  assert.equal(await buildQuoted(sock(), `${PHONE}@s.whatsapp.net`, { id: 'E4' }, { validate }), null);
  // the real builder throws on a quote without content: dropped, not thrown
  const broken = (to, q) => wire(to, { ...q, message: undefined });
  assert.throws(() => broken(`${PHONE}@s.whatsapp.net`, { key: { id: 'E2' } }), TypeError);
  assert.equal(await buildQuoted(sock(), `${PHONE}@s.whatsapp.net`, { id: 'E2', text: 'x' },
    { validate: broken }), null);
  // a lookup that throws is caught too
  const throwing = { signalRepository: { lidMapping: { getPNForLID() { throw new Error('down'); } } } };
  rememberInbound(inbound({ id: 'E3', remoteJid: `${LID}@lid` }));
  assert.equal(await buildQuoted(throwing, `${PHONE}@s.whatsapp.net`, { id: 'E3' }, { validate }), null);
});

test('own messages are never cached; the cache is bounded and expires', () => {
  rememberInbound({ key: { id: 'M0', remoteJid: `${PHONE}@s.whatsapp.net`, fromMe: true },
    message: { conversation: 'x' } });
  assert.equal(recallInbound('M0'), null);
  const t0 = 1_000_000;
  for (let i = 0; i < QUOTE_CACHE_MAX + 5; i++) {
    rememberInbound(inbound({ id: `C${i}`, remoteJid: `${PHONE}@s.whatsapp.net` }), t0);
  }
  assert.equal(quoteCacheSize(), QUOTE_CACHE_MAX);
  assert.equal(recallInbound('C0', t0), null, 'oldest dropped first');
  assert.ok(recallInbound(`C${QUOTE_CACHE_MAX + 4}`, t0));
  assert.equal(recallInbound(`C${QUOTE_CACHE_MAX + 4}`, t0 + QUOTE_TTL_MS + 1), null, 'expired');
});

test('participantJid: LID, phone, seen LID, device suffix', () => {
  assert.equal(participantJid(`${LID}@lid`), `${LID}@lid`);
  assert.equal(participantJid(`${PHONE}:3@s.whatsapp.net`), `${PHONE}@s.whatsapp.net`);
  assert.equal(participantJid(PHONE), `${PHONE}@s.whatsapp.net`);
  rememberLid('987650000099999');
  assert.equal(participantJid('987650000099999'), '987650000099999@lid');
  assert.equal(participantJid(''), '');
});

test('documents and images carry the quote too (the same builder, other content types)', async () => {
  rememberInbound(inbound({ id: 'D1', remoteJid: `${PHONE}@s.whatsapp.net`, text: 'send the report' }));
  const to = `${PHONE}@s.whatsapp.net`;
  const q = await buildQuoted(sock(), to, { id: 'D1' }, { validate });
  const doc = generateWAMessageFromContent(to,
    { documentMessage: { fileName: 'r.pdf', mimetype: 'application/pdf' } }, { quoted: q, userJid: ME });
  assert.equal(doc.message.documentMessage.contextInfo.stanzaId, 'D1');
  const img = generateWAMessageFromContent(to,
    { imageMessage: { caption: 'chart' } }, { quoted: q, userJid: ME });
  assert.equal(img.message.imageMessage.contextInfo.quotedMessage.conversation, 'send the report');
});
