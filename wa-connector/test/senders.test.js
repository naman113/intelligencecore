// Quick 017: sender identity, LID<->PN learning and outbound addressing.
// Run: npm test (node --test). No Baileys socket: a stub stands in for
// sock.signalRepository.lidMapping with the rc.9 method names and shapes
// (getPNForLID -> "pn:device@s.whatsapp.net" | null, storeLIDPNMappings([{lid, pn}]),
// getLIDsForPNs([pnJid]) -> [{pn, lid}] | null). All numbers are made up.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {
  resolveSender, resolveSenderNumber, senderKeyFor, pairsFromContacts, pairsFromKeys,
  learnPairs, attachLearners, buildPayload, resolveRecipient, lookupLids,
  initSeenLids, rememberLid, isSeenLid, flushSeenLids, bareNumber,
} from '../senders.js';

const PHONE = '15550001111';
const LID = '987650000012345';

function stubSock({ map = {}, appearOnCall = 0, appearPn = null, usync = {}, throwOn = null } = {}) {
  const stored = [];
  let getCalls = 0;
  const lidMapping = {
    async getPNForLID(jid) {
      if (throwOn === 'get') throw new Error('store down');
      if (!jid.endsWith('@lid')) return null;
      getCalls++;
      const u = bareNumber(jid);
      if (map[u]) return `${map[u]}:0@s.whatsapp.net`;
      if (appearOnCall && getCalls >= appearOnCall) return `${appearPn}:0@s.whatsapp.net`;
      return null;
    },
    async storeLIDPNMappings(pairs) {
      if (throwOn === 'store') throw new Error('disk full');
      for (const p of pairs) {
        assert.ok(p.lid.endsWith('@lid') && p.pn.endsWith('@s.whatsapp.net'), 'full JIDs, as rc.9 validates');
        stored.push(p);
        map[bareNumber(p.lid)] = bareNumber(p.pn);
      }
    },
    async getLIDsForPNs(pns) {
      const out = pns
        .filter((pn) => usync[bareNumber(pn)])
        .map((pn) => ({ pn, lid: `${usync[bareNumber(pn)]}@lid` }));
      return out.length ? out : null; // rc.9 returns null when USync found nothing
    },
  };
  return { signalRepository: { lidMapping }, ev: new EventEmitter(), stored, calls: () => getCalls };
}

// ── inbound: resolveSender / resolveSenderNumber ───────────────────────

test('PN JID: phone as before, no LID', async () => {
  const who = await resolveSender(stubSock(), { remoteJid: `${PHONE}@s.whatsapp.net` });
  assert.deepEqual(who, { phone: PHONE, lid: null, resolved: true, via: 'pn' });
});

test('PN JID with a LID alt: both forwarded and the pair stored', async () => {
  const sock = stubSock();
  const who = await resolveSender(sock, { remoteJid: `${PHONE}@s.whatsapp.net`, remoteJidAlt: `${LID}@lid` });
  assert.deepEqual(who, { phone: PHONE, lid: LID, resolved: true, via: 'pn' });
  assert.deepEqual(sock.stored, [{ lid: `${LID}@lid`, pn: `${PHONE}@s.whatsapp.net` }]);
});

test('LID with remoteJidAlt: phone from the alt, pair stored', async () => {
  const sock = stubSock();
  const who = await resolveSender(sock, { remoteJid: `${LID}@lid`, remoteJidAlt: `${PHONE}@s.whatsapp.net` });
  assert.deepEqual(who, { phone: PHONE, lid: LID, resolved: true, via: 'alt' });
  assert.equal(sock.stored.length, 1);
});

test('LID with a mapping-store hit', async () => {
  const sock = stubSock({ map: { [LID]: PHONE } });
  const who = await resolveSender(sock, { remoteJid: `${LID}:12@lid` }, { retryDelayMs: 0 });
  assert.deepEqual(who, { phone: PHONE, lid: LID, resolved: true, via: 'store' });
});

test('LID whose pair lands while syncing: one retry finds it', async () => {
  const sock = stubSock({ appearOnCall: 2, appearPn: PHONE });
  const who = await resolveSender(sock, { remoteJid: `${LID}@lid` }, { retryDelayMs: 5 });
  assert.deepEqual(who, { phone: PHONE, lid: LID, resolved: true, via: 'retry' });
  assert.equal(sock.calls(), 2, 'exactly one retry');
});

test('LID unresolved: phone empty, LID forwarded, resolveSenderNumber keeps the old fallback', async () => {
  const sock = stubSock();
  const key = { remoteJid: `${LID}@lid` };
  assert.deepEqual(await resolveSender(sock, key, { retryDelayMs: 1 }),
    { phone: '', lid: LID, resolved: false, via: 'none' });
  assert.equal(await resolveSenderNumber(sock, key, { retryDelayMs: 0 }), LID);
});

test('store errors never cost the message', async () => {
  const who = await resolveSender(stubSock({ throwOn: 'get' }), { remoteJid: `${LID}@lid` }, { retryDelayMs: 0 });
  assert.equal(who.resolved, false);
  const n = await learnPairs(stubSock({ throwOn: 'store' }), [{ lid: `${LID}@lid`, pn: `${PHONE}@s.whatsapp.net` }]);
  assert.equal(n, 0);
});

test('group: the participant is the sender, alt either way round', async () => {
  const g = '120363000000000000@g.us';
  const k1 = senderKeyFor({ remoteJid: g, participant: `${LID}@lid`, participantAlt: `${PHONE}@s.whatsapp.net` });
  assert.deepEqual(await resolveSender(stubSock(), k1), { phone: PHONE, lid: LID, resolved: true, via: 'alt' });
  const k2 = senderKeyFor({ remoteJid: g, participant: `${PHONE}@s.whatsapp.net`, participantAlt: `${LID}@lid` });
  assert.deepEqual(await resolveSender(stubSock(), k2), { phone: PHONE, lid: LID, resolved: true, via: 'pn' });
  const k3 = senderKeyFor({ remoteJid: g, participant: `${LID}@lid` });
  assert.deepEqual(await resolveSender(stubSock(), k3, { retryDelayMs: 0 }),
    { phone: '', lid: LID, resolved: false, via: 'none' });
  const dm = { remoteJid: `${PHONE}@s.whatsapp.net` };
  assert.equal(senderKeyFor(dm), dm, 'a DM key passes through untouched');
});

// ── learning pairs ─────────────────────────────────────────────────────

test('pairs from contacts, participants and keys', () => {
  assert.deepEqual(pairsFromContacts([
    { id: `${LID}@lid`, phoneNumber: `${PHONE}@s.whatsapp.net` },
    { id: '15550002222@s.whatsapp.net', lid: '987650000022222@lid' },
    { id: '987650000033333@lid', notify: 'no pair' },
    null,
    { id: '120363000000000000@g.us', phoneNumber: `${PHONE}@s.whatsapp.net` },
  ]), [
    { lid: `${LID}@lid`, pn: `${PHONE}@s.whatsapp.net` },
    { lid: '987650000022222@lid', pn: '15550002222@s.whatsapp.net' },
  ]);
  assert.deepEqual(pairsFromKeys([
    { remoteJid: `${LID}@lid`, remoteJidAlt: `${PHONE}:3@s.whatsapp.net` },
    { remoteJid: 'x@g.us', participant: '15550002222@s.whatsapp.net', participantAlt: '987650000022222@lid' },
    { remoteJid: `${LID}@lid` },
  ]), [
    { lid: `${LID}@lid`, pn: `${PHONE}@s.whatsapp.net` },
    { lid: '987650000022222@lid', pn: '15550002222@s.whatsapp.net' },
  ]);
});

test('attachLearners stores pairs from the events rc.9 emits (and lid-mapping.update when it does)', async () => {
  const sock = stubSock();
  attachLearners(sock);
  sock.ev.emit('contacts.upsert', [{ id: `${LID}@lid`, phoneNumber: `${PHONE}@s.whatsapp.net` }]);
  sock.ev.emit('contacts.update', [{ id: '987650000011111@lid', phoneNumber: '15550001112@s.whatsapp.net' }]);
  sock.ev.emit('messaging-history.set', {
    contacts: [{ id: '15550002222@s.whatsapp.net', lid: '987650000022222@lid' }],
    messages: [{ key: { remoteJid: '987650000033333@lid', remoteJidAlt: '15550003333@s.whatsapp.net' } }],
    lidPnMappings: [{ lid: '987650000044444@lid', pn: '15550004444@s.whatsapp.net' }],
  });
  sock.ev.emit('lid-mapping.update', { lid: '987650000055555@lid', pn: '15550005555@s.whatsapp.net' });
  sock.ev.emit('group-participants.update', {
    id: 'x@g.us', action: 'add',
    participants: [{ id: '987650000066666@lid', phoneNumber: '15550006666@s.whatsapp.net' }],
  });
  sock.ev.emit('groups.upsert', [{
    id: 'y@g.us', participants: [{ id: '987650000077777@lid', phoneNumber: '15550007777@s.whatsapp.net' }],
  }]);
  await new Promise((r) => setTimeout(r, 10));
  assert.deepEqual(sock.stored.map((p) => bareNumber(p.lid)).sort(), [
    LID, '987650000011111', '987650000022222', '987650000033333', '987650000044444',
    '987650000055555', '987650000066666', '987650000077777',
  ].sort());
  // ...after which a LID-only message resolves from the store.
  const who = await resolveSender(sock, { remoteJid: '987650000022222@lid' }, { retryDelayMs: 0 });
  assert.equal(who.phone, '15550002222');
});

// ── what the gateway receives ──────────────────────────────────────────

test('payload: resolved sender keeps phone, adds lid and resolved', () => {
  const msg = { key: { id: 'm1' }, pushName: 'Owner' };
  const p = buildPayload({
    msg, who: { phone: PHONE, lid: LID, resolved: true }, text: 'hi', timestamp: 't',
    isGroup: false, chatJid: `${LID}@lid`, subject: null, mentioned: true,
  });
  assert.deepEqual(p, {
    phone: PHONE, lid: LID, resolved: true, name: 'Owner', message: 'hi', messageId: 'm1',
    timestamp: 't', isGroup: false, chatId: null, groupSubject: null, mentioned: true,
  });
});

test('payload: unresolved sender has an empty phone and the LID', () => {
  const p = buildPayload({
    msg: { key: { id: 'm2' } }, who: { phone: '', lid: LID, resolved: false }, text: 'hi', timestamp: 't',
    isGroup: true, chatJid: 'g@g.us', subject: 'Ops', mentioned: false,
    quoted: { text: 'q' }, media: { filename: 'a.pdf' },
  });
  assert.equal(p.phone, '');
  assert.equal(p.lid, LID);
  assert.equal(p.resolved, false);
  assert.equal(p.chatId, 'g@g.us');
  assert.equal(p.groupSubject, 'Ops');
  assert.deepEqual(p.quoted, { text: 'q' });
  assert.deepEqual(p.document, { filename: 'a.pdf' });
});

// ── outbound addresses ─────────────────────────────────────────────────

test('recipient: group and phone numbers as before', async () => {
  const sock = stubSock();
  assert.equal(await resolveRecipient(sock, '120363000000000000@g.us'), '120363000000000000@g.us');
  assert.equal(await resolveRecipient(sock, '15550008888'), '15550008888@s.whatsapp.net');
  assert.equal(await resolveRecipient(sock, '+1 555 000 8888'), '15550008888@s.whatsapp.net');
  assert.equal(await resolveRecipient(sock, '15550008888@s.whatsapp.net'), '15550008888@s.whatsapp.net');
});

test('recipient: a LID JID goes to its phone when the store knows it, else to <lid>@lid', async () => {
  assert.equal(await resolveRecipient(stubSock({ map: { [LID]: PHONE } }), `${LID}@lid`), `${PHONE}@s.whatsapp.net`);
  assert.equal(await resolveRecipient(stubSock(), '987650000099999@lid'), '987650000099999@lid');
});

test('recipient: a bare LID (the old thread id) never gets the phone suffix', async () => {
  const bare = '987650000088888';
  // Mapped in the store: the phone wins.
  assert.equal(await resolveRecipient(stubSock({ map: { [bare]: '15550009999' } }), bare), '15550009999@s.whatsapp.net');
  // Unmapped but a message came from it: <lid>@lid.
  const sock = stubSock();
  await resolveSender(sock, { remoteJid: `${bare}@lid` }, { retryDelayMs: 0 });
  assert.ok(isSeenLid(bare));
  assert.equal(await resolveRecipient(sock, bare), `${bare}@lid`);
  assert.equal(await resolveRecipient(sock, `${bare}@s.whatsapp.net`), `${bare}@lid`);
});

test('seen LIDs survive a restart (persisted beside the auth keys)', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'q017-'));
  try {
    initSeenLids(dir, fs);
    rememberLid('987650000077770@lid');
    flushSeenLids();
    initSeenLids(dir, fs); // a fresh process reading the same volume
    assert.ok(isSeenLid('987650000077770'));
    assert.ok(!isSeenLid('15550001111'));
  } finally {
    initSeenLids(null, null);
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('lookupLids: PN -> LID through getLIDsForPNs, null-safe, input cleaned', async () => {
  const sock = stubSock({ usync: { [PHONE]: LID } });
  assert.deepEqual(await lookupLids(sock, [PHONE, '+1 555 000 1111', 'abc', '12']), [{ phone: PHONE, lid: LID }]);
  assert.ok(isSeenLid(LID));
  assert.deepEqual(await lookupLids(stubSock(), ['15550004321']), []);
  assert.deepEqual(await lookupLids(stubSock(), []), []);
  assert.deepEqual(await lookupLids({}, [PHONE]), []);
});
