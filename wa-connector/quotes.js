// quotes.js — a reply quotes the message it answers (Quick 021).
//
// The gateway sends `replyTo: {id, chat?, participant?, text?}` with an answer: the
// WhatsApp id of the message it answers (and, for a group, the group and the sender).
// Baileys threads a reply under a message when sendMessage gets `{ quoted: WAMessage }`
// (https://baileys.wiki/messaging/sending-messages). Its builder (rc.9
// Utils/messages.js generateWAMessageFromContent) reads quoted.key.id (stanzaId),
// quoted.message (the quoted content; without it the builder throws) and the author
// from quoted.participant || key.participant || key.remoteJid, and marks the quote as
// coming from another chat when key.remoteJid differs from the address we send to.
//
// So inbound messages we may answer (every DM, group messages that address us) are
// kept here by id for a while. A DM quote is rebuilt on the address the reply goes to
// (Quick 017 may send to the phone while the message came from "<lid>@lid"), so the
// quote stays in the same chat with that person as its author: the shape of the usual
// sendMessage(msg.key.remoteJid, ..., { quoted: msg }). A group quote is the message as
// WhatsApp delivered it (group JID, participant in whatever form it came). After a
// restart the cache is empty: a minimal quote from the gateway's text (and sender, for
// groups) is used, else the reply goes out unquoted. A quote never fails a send.
//
// Nothing here imports Baileys: the caller passes `validate` (a dry run of Baileys' own
// builder), so this module unit-tests with stubs (wa-connector/test/quotes.test.js).

import { bareNumber, isLid, isPn, isSeenLid, lookupPN } from './senders.js';

export const QUOTE_CACHE_MAX = 300;
export const QUOTE_TTL_MS = 24 * 60 * 60 * 1000;
const cache = new Map(); // message id -> { at, msg: { key, message } }

const noop = () => {};
let log = { error: noop, info: noop };
export function setQuoteLogger(logger) {
  log = {
    error: (...a) => logger?.error?.(...a),
    info: (...a) => (logger?.whatsapp || logger?.info)?.(...a),
  };
}

// Keep an inbound message we may answer. Oldest entries go first.
export function rememberInbound(msg, now = Date.now()) {
  const id = msg?.key?.id;
  if (!id || !msg.message || msg.key.fromMe) return;
  cache.delete(id);
  cache.set(id, { at: now, msg: { key: { ...msg.key }, message: msg.message } });
  while (cache.size > QUOTE_CACHE_MAX) cache.delete(cache.keys().next().value);
}

export function recallInbound(id, now = Date.now()) {
  const key = String(id || '');
  const hit = key ? cache.get(key) : null;
  if (!hit) return null;
  if (now - hit.at > QUOTE_TTL_MS) {
    cache.delete(key);
    return null;
  }
  return hit.msg;
}

export function clearQuoteCache() {
  cache.clear();
}

export const quoteCacheSize = () => cache.size;

// A sender the gateway names ("<lid>@lid", a full JID, bare digits) as a participant
// JID without a device suffix; bare digits are a LID when a message came from that LID.
export function participantJid(raw) {
  const s = String(raw ?? '').trim();
  const user = bareNumber(s).replace(/\D/g, '');
  if (!user) return '';
  if (isLid(s)) return `${user}@lid`;
  if (isPn(s)) return `${user}@s.whatsapp.net`;
  return isSeenLid(user) ? `${user}@lid` : `${user}@s.whatsapp.net`;
}

// Is `dest` (the address the reply goes to) the person who wrote this DM?
async function sameDmUser(sock, key, dest) {
  const d = bareNumber(dest);
  if (!d) return false;
  const forms = [key?.remoteJid, key?.remoteJidAlt].filter(Boolean);
  if (forms.map(bareNumber).includes(d)) return true;
  for (const jid of forms) {
    if (isLid(jid) && (await lookupPN(sock, jid)) === d) return true;
  }
  if (isLid(dest)) {
    const destPn = await lookupPN(sock, dest);
    if (destPn && forms.map(bareNumber).includes(destPn)) return true;
  }
  return false;
}

// Baileys' getContentType rule: a quote needs content it can name, or the builder
// quotes nothing (an empty message) or throws (no message at all).
const hasContent = (message) => Object.keys(message || {}).some(
  (k) => k === 'conversation' || (k.includes('Message') && k !== 'senderKeyDistributionMessage'));

// The `quoted` option for a reply to `dest`, or null (send unquoted). Never throws.
export async function buildQuoted(sock, dest, replyTo, { validate } = {}) {
  try {
    const id = replyTo?.id ? String(replyTo.id) : '';
    const to = String(dest || '');
    if (!id || !to) return null;
    const isGroup = to.endsWith('@g.us');
    const cached = recallInbound(id);
    let quoted;
    if (cached) {
      const chat = cached.key?.remoteJid || '';
      if (isGroup) {
        if (chat !== to) return null;
        quoted = { key: { ...cached.key }, message: cached.message };
      } else {
        if (chat.endsWith('@g.us') || !(await sameDmUser(sock, cached.key, to))) {
          log.info(`reply to ${id}: the quoted message is from someone else; sent unquoted`);
          return null;
        }
        quoted = { key: { remoteJid: to, fromMe: false, id }, message: cached.message };
      }
    } else {
      const text = String(replyTo?.text ?? '').trim();
      if (!text) return null;
      if (isGroup) {
        if (replyTo.chat && replyTo.chat !== to) return null;
        const participant = participantJid(replyTo.participant);
        if (!participant) return null;
        quoted = { key: { remoteJid: to, fromMe: false, id, participant },
          message: { conversation: text } };
      } else {
        quoted = { key: { remoteJid: to, fromMe: false, id }, message: { conversation: text } };
      }
    }
    if (!hasContent(quoted.message)) return null;
    if (validate) validate(to, quoted); // Baileys' own builder, no network: throws on a bad quote
    return quoted;
  } catch (error) {
    log.error(`quoted reply not built (sent unquoted): ${error.message}`);
    return null;
  }
}
