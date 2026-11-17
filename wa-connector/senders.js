// senders.js — who sent a message, as far as WhatsApp has revealed it (Quick 017).
//
// Modern WhatsApp may address a sender by a private ID (LID, "...@lid") instead
// of their phone number (PN). The gateway's allowlist is written in phone
// numbers, so the connector forwards BOTH identities it has: `phone` (the PN,
// or empty while WhatsApp has not revealed it) and `lid` (the bare LID). The
// gateway decides; the connector never drops a message for being unresolved.
//
// LID<->PN pairs live in Baileys' own store (sock.signalRepository.lidMapping).
// It is kept in the auth keys, i.e. the wa-sessions volume, so every pair
// written here survives restarts and later messages resolve on the spot.
//
// Nothing here imports Baileys or opens a socket: `sock` is passed in, so this
// module unit-tests with a stub (wa-connector/test/senders.test.js).

const noop = () => {};
let log = { error: noop, info: noop };

// whatsapp.js hands in its winston logger; tests leave the silent default.
export function setLogger(logger) {
  log = {
    error: (...a) => logger?.error?.(...a),
    info: (...a) => (logger?.whatsapp || logger?.info)?.(...a),
  };
}

// Reduce a JID to its bare number: "919000000102:38@s.whatsapp.net" -> "919000000102".
export function bareNumber(jid) {
  return String(jid || '').split('@')[0].split(':')[0];
}

export const isLid = (jid) => typeof jid === 'string' && jid.endsWith('@lid');
export const isPn = (jid) => typeof jid === 'string' && jid.endsWith('@s.whatsapp.net');

const lidJid = (jid) => `${bareNumber(jid)}@lid`;
const pnJid = (jid) => `${bareNumber(jid)}@s.whatsapp.net`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ── LIDs we have seen ──────────────────────────────────────────────────
// Every LID a message arrived from, kept so a reply to the bare LID digits
// (a thread id the gateway created before Quick 017) goes to <lid>@lid and
// never to "<lid>@s.whatsapp.net", a phone number that does not exist. Pairs
// with a phone live in Baileys' store; this list is the LIDs with or without
// one. Persisted next to the auth keys (wa-sessions), so clearSession wipes it
// together with the pairing.
const SEEN_LIDS_MAX = 5000;
const seenLids = new Set();
let seenFile = null;
let seenFs = null;
let saveTimer = null;

export function initSeenLids(dir, fsImpl) {
  seenFile = dir && fsImpl ? `${dir}/seen-lids.json` : null;
  seenFs = seenFile ? fsImpl : null;
  seenLids.clear();
  if (!seenFile) return;
  try {
    if (fsImpl.existsSync(seenFile)) {
      for (const l of JSON.parse(fsImpl.readFileSync(seenFile, 'utf8')) || []) {
        if (/^\d+$/.test(String(l))) seenLids.add(String(l));
      }
    }
  } catch (error) {
    log.error(`seen-lids.json unreadable, starting empty: ${error.message}`);
  }
}

export function rememberLid(lid) {
  const l = bareNumber(lid).replace(/\D/g, '');
  if (!l || seenLids.has(l)) return;
  seenLids.add(l);
  while (seenLids.size > SEEN_LIDS_MAX) seenLids.delete(seenLids.values().next().value);
  if (!seenFile || saveTimer) return;
  saveTimer = setTimeout(flushSeenLids, 2000);
  saveTimer.unref?.();
}

export function flushSeenLids() {
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = null;
  if (!seenFile) return;
  try {
    seenFs.writeFileSync(seenFile, JSON.stringify([...seenLids]));
  } catch (error) {
    log.error(`saving seen-lids.json failed: ${error.message}`);
  }
}

export const isSeenLid = (lid) => seenLids.has(bareNumber(lid).replace(/\D/g, ''));

// How long to wait before asking the store a second time. Right after pairing
// the history and contact sync is still writing pairs; one short wait catches
// a pair that lands moments after the message (seen on the first client copy).
export const RETRY_DELAY_MS = 1200;

// The LID->PN store lookup. Returns a bare PN or ''. Never throws.
export async function lookupPN(sock, jid) {
  if (!isLid(jid)) return '';
  try {
    const pn = await sock?.signalRepository?.lidMapping?.getPNForLID?.(jid);
    return pn ? bareNumber(pn) : '';
  } catch (error) {
    log.error(`LID->PN lookup failed: ${error.message}`);
    return '';
  }
}

// In a GROUP, key.remoteJid is the group itself and the human who typed is
// key.participant (with the other form in key.participantAlt). Map that onto
// the DM shape so one resolver serves both.
export function senderKeyFor(key) {
  const jid = key?.remoteJid || '';
  if (!jid.endsWith('@g.us')) return key;
  return {
    remoteJid: key.participant || key.participantAlt || '',
    remoteJidAlt: key.participant ? (key.participantAlt || null) : null,
  };
}

// Resolve a sender key to {phone, lid, resolved, via}.
//   phone    bare PN, or '' when WhatsApp has not revealed it
//   lid      bare LID when the sender came as (or alongside) a LID, else null
//   resolved true when phone is known
//   via      'pn' | 'alt' | 'store' | 'retry' | 'none' (for tests and logs)
// A pair seen on the key is written to the store (learnPairs) so the next
// LID-only message from the same person resolves.
export async function resolveSender(sock, key, { retryDelayMs = RETRY_DELAY_MS } = {}) {
  const raw = key?.remoteJid || '';
  const alt = key?.remoteJidAlt || '';

  if (!isLid(raw)) {
    // A phone-number JID (or anything that is not a LID: keep it as before).
    const lid = isLid(alt) ? bareNumber(alt) : null;
    if (lid) rememberLid(lid);
    if (lid && isPn(raw)) await learnPairs(sock, [{ lid: alt, pn: raw }]);
    return { phone: bareNumber(raw), lid, resolved: Boolean(bareNumber(raw)), via: 'pn' };
  }

  const lid = bareNumber(raw);
  rememberLid(lid);
  if (isPn(alt)) {
    await learnPairs(sock, [{ lid: raw, pn: alt }]);
    return { phone: bareNumber(alt), lid, resolved: true, via: 'alt' };
  }

  let phone = await lookupPN(sock, raw);
  if (phone) return { phone, lid, resolved: true, via: 'store' };

  // One brief retry while the mapping store may still be syncing.
  if (retryDelayMs > 0 && sock?.signalRepository?.lidMapping) {
    await sleep(retryDelayMs);
    phone = await lookupPN(sock, raw);
    if (phone) return { phone, lid, resolved: true, via: 'retry' };
  }
  return { phone: '', lid, resolved: false, via: 'none' };
}

// The old single-number shape: the phone, or the bare LID so nothing is lost.
// Used for the inbox ring buffer and the log line.
export async function resolveSenderNumber(sock, key, opts) {
  const who = await resolveSender(sock, key, opts);
  return who.phone || who.lid || '';
}

// ── Learning LID<->PN pairs ────────────────────────────────────────────

// One {lid, pn} pair from two JIDs in either order, or null.
function pairOf(a, b) {
  if (isLid(a) && isPn(b)) return { lid: lidJid(a), pn: pnJid(b) };
  if (isPn(a) && isLid(b)) return { lid: lidJid(b), pn: pnJid(a) };
  return null;
}

// Contacts (contacts.upsert / contacts.update / messaging-history.set) carry
// `id` in either form plus `lid` and/or `phoneNumber`. Group participants are
// contacts too (GroupParticipant = Contact & {admin}).
export function pairsFromContacts(contacts) {
  const out = [];
  for (const c of contacts || []) {
    if (!c) continue;
    const p = pairOf(c.id, c.phoneNumber) || pairOf(c.id, c.lid) || pairOf(c.lid, c.phoneNumber);
    if (p) out.push(p);
  }
  return out;
}

// Message keys carry the other form on remoteJidAlt (DM) or participantAlt (group).
export function pairsFromKeys(keys) {
  const out = [];
  for (const k of keys || []) {
    if (!k) continue;
    const p = pairOf(k.remoteJid, k.remoteJidAlt) || pairOf(k.participant, k.participantAlt);
    if (p) out.push(p);
  }
  return out;
}

// Write pairs to Baileys' store (persisted in the session keys). Deduped,
// already-known pairs are skipped by the store itself. Returns how many pairs
// were offered. Never throws: learning must never cost a message.
export async function learnPairs(sock, pairs) {
  const store = sock?.signalRepository?.lidMapping;
  if (!store?.storeLIDPNMappings || !pairs?.length) return 0;
  const seen = new Set();
  const clean = [];
  for (const p of pairs) {
    const pair = p && pairOf(p.lid, p.pn);
    if (!pair) continue;
    const k = `${pair.lid}|${pair.pn}`;
    if (seen.has(k)) continue;
    seen.add(k);
    clean.push(pair);
  }
  if (!clean.length) return 0;
  try {
    await store.storeLIDPNMappings(clean);
    return clean.length;
  } catch (error) {
    log.error(`storing ${clean.length} LID mapping(s) failed: ${error.message}`);
    return 0;
  }
}

// Subscribe to every event through which Baileys 7 reveals pairs. rc.9 stores
// pairs from message envelopes itself, but NOT the lid/phoneNumber that
// history-sync and app-state contacts carry, and it declares
// 'lid-mapping.update' without emitting it yet; both are covered here.
export function attachLearners(sock) {
  const ev = sock?.ev;
  if (!ev?.on) return;
  const learn = async (pairs, source) => {
    const n = await learnPairs(sock, pairs);
    if (n) log.info(`learned ${n} LID mapping(s) from ${source}`);
  };
  ev.on('contacts.upsert', (contacts) => learn(pairsFromContacts(contacts), 'contacts'));
  ev.on('contacts.update', (contacts) => learn(pairsFromContacts(contacts), 'contacts'));
  // `lidPnMappings` is not sent by rc.9; later versions add it (waha#2305).
  ev.on('messaging-history.set', ({ contacts, messages, lidPnMappings } = {}) => learn(
    [...pairsFromContacts(contacts), ...pairsFromKeys((messages || []).map((m) => m?.key)),
      ...(Array.isArray(lidPnMappings) ? lidPnMappings : [])],
    'history sync'));
  ev.on('lid-mapping.update', (u) => {
    const list = Array.isArray(u) ? u : [u];
    return learn(list.map((x) => x && { lid: x.lid, pn: x.pn }), 'lid-mapping.update');
  });
  ev.on('groups.upsert', (groups) => learn(
    (groups || []).flatMap((g) => pairsFromContacts(g?.participants)), 'group metadata'));
  ev.on('group-participants.update', (u) => learn(pairsFromContacts(u?.participants), 'group participants'));
}

// ── Outbound addresses ─────────────────────────────────────────────────

// Where a send to `target` goes. Per the v7 guide a user can be messaged by
// LID or PN, so:
//   group / broadcast / newsletter JID   unchanged
//   "<lid>@lid", or a bare number known as a LID (Baileys' store has its
//   reverse pair, or a message came from it)   its PN JID when the store has
//   the pair, else "<lid>@lid"
//   anything else (a phone number, bare or "@s.whatsapp.net")   "<digits>@s.whatsapp.net"
// A LID never gets the phone-number suffix: that address is nobody.
export async function resolveRecipient(sock, target) {
  const raw = String(target ?? '').trim();
  if (/@(g\.us|broadcast|newsletter)$/.test(raw)) return raw;
  const user = bareNumber(raw).replace(/\D/g, '');
  if (!user) return raw;
  const pn = await lookupPN(sock, `${user}@lid`);
  if (pn) return `${pn}@s.whatsapp.net`;
  if (isLid(raw) || isSeenLid(user)) return `${user}@lid`;
  return `${user}@s.whatsapp.net`;
}

// The documented one-way lookup (PN -> LID): Baileys reads its store and asks
// WhatsApp (USync) for the rest, storing what it finds. The gateway calls this
// for its allowlisted numbers so a sender who arrives only as a LID can be
// matched before WhatsApp reveals their phone. Returns [{phone, lid}].
export async function lookupLids(sock, phones) {
  const store = sock?.signalRepository?.lidMapping;
  const pns = [...new Set((phones || [])
    .map((p) => String(p ?? '').replace(/\D/g, ''))
    .filter((p) => p.length >= 6 && p.length <= 15))].slice(0, 50).map((p) => `${p}@s.whatsapp.net`);
  if (!store?.getLIDsForPNs || !pns.length) return [];
  let pairs = null;
  try {
    pairs = await store.getLIDsForPNs(pns); // rc.9 returns null when USync found nothing
  } catch (error) {
    log.error(`PN->LID lookup failed: ${error.message}`);
    return [];
  }
  const out = [];
  const seen = new Set();
  for (const p of pairs || []) {
    const phone = bareNumber(p?.pn);
    const lid = bareNumber(p?.lid);
    if (!phone || !lid || !isLid(p?.lid) || seen.has(phone)) continue;
    seen.add(phone);
    rememberLid(lid);
    out.push({ phone, lid });
  }
  return out;
}

// ── What the gateway receives ──────────────────────────────────────────

// The webhook payload for one inbound message. `phone` keeps its meaning (the
// sender's phone number) and is '' when WhatsApp hid it; `lid` and `resolved`
// let the gateway match its allowlist and keep one thread per person.
export function buildPayload({ msg, who, text, timestamp, isGroup, chatJid, subject, mentioned, media, quoted }) {
  const payload = {
    phone: who?.phone || '',
    lid: who?.lid || null,
    resolved: Boolean(who?.resolved),
    name: msg?.pushName || null,
    message: text || '',
    messageId: msg?.key?.id || null,
    timestamp,
    isGroup: Boolean(isGroup),
    chatId: isGroup ? chatJid : null,
    groupSubject: subject || null,
    mentioned: Boolean(mentioned),
  };
  if (media) payload.document = media;
  if (quoted) payload.quoted = quoted;
  return payload;
}
