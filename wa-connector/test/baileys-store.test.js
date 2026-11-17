// Quick 017: senders.js against the REAL Baileys 7.0.0-rc.9 LID mapping store
// on the real multi-file auth state (the same useMultiFileAuthState whatsapp.js
// uses for wa-sessions), in a temp folder. Proves the pairs the connector learns
// are written as files in the session folder and are read back by a fresh
// process, which is what makes a later LID-only message resolve after a
// restart or a container rebuild that keeps the volume. No network, no pairing.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { useMultiFileAuthState, addTransactionCapability } from '@whiskeysockets/baileys';
import { LIDMappingStore } from '@whiskeysockets/baileys/lib/Signal/lid-mapping.js';
import { learnPairs, lookupLids, resolveRecipient, resolveSender } from '../senders.js';

const PHONE = '15550001111';
const LID = '987650000012345';
const quiet = { trace() {}, debug() {}, info() {}, warn() {}, error() {}, child() { return quiet; } };

async function sockAt(dir, pnToLid = null) {
  const { state } = await useMultiFileAuthState(dir);
  const keys = addTransactionCapability(state.keys, quiet, { maxCommitRetries: 1, delayBetweenTriesMs: 1 });
  return { signalRepository: { lidMapping: new LIDMappingStore(keys, quiet, pnToLid) } };
}

function tmp() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'q017-baileys-'));
}

test('a learned pair is written to the session folder and read back by a fresh process', async () => {
  const dir = tmp();
  try {
    const first = await sockAt(dir);
    assert.equal(await learnPairs(first, [{ lid: `${LID}@lid`, pn: `${PHONE}@s.whatsapp.net` }]), 1);
    const files = fs.readdirSync(dir);
    assert.ok(files.includes(`lid-mapping-${PHONE}.json`), files.join(','));
    assert.ok(files.includes(`lid-mapping-${LID}_reverse.json`), files.join(','));

    const second = await sockAt(dir); // new process, same volume, empty cache
    assert.equal(await second.signalRepository.lidMapping.getPNForLID(`${LID}@lid`), `${PHONE}:0@s.whatsapp.net`);
    const who = await resolveSender(second, { remoteJid: `${LID}@lid` }, { retryDelayMs: 0 });
    assert.deepEqual(who, { phone: PHONE, lid: LID, resolved: true, via: 'store' });
    assert.equal(await resolveRecipient(second, LID), `${PHONE}@s.whatsapp.net`);
    assert.equal(await resolveRecipient(second, `${LID}@lid`), `${PHONE}@s.whatsapp.net`);
    // A phone number is never mistaken for a LID by the reverse lookup.
    assert.equal(await resolveRecipient(second, PHONE), `${PHONE}@s.whatsapp.net`);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('an unknown LID stays unresolved on the real store and is addressed as <lid>@lid', async () => {
  const dir = tmp();
  try {
    const sock = await sockAt(dir);
    const who = await resolveSender(sock, { remoteJid: '987650000054321@lid' }, { retryDelayMs: 1 });
    assert.equal(who.resolved, false);
    assert.equal(await resolveRecipient(sock, '987650000054321@lid'), '987650000054321@lid');
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('lookupLids uses rc.9 getLIDsForPNs (store, then the USync function) and persists the result', async () => {
  const dir = tmp();
  try {
    let asked = null;
    const usync = async (pns) => {
      asked = pns;
      return pns.filter((pn) => pn.startsWith(PHONE)).map((pn) => ({ pn, lid: `${LID}@lid` }));
    };
    const sock = await sockAt(dir, usync);
    assert.deepEqual(await lookupLids(sock, [PHONE, '15550002222']), [{ phone: PHONE, lid: LID }]);
    assert.deepEqual(asked, [`${PHONE}@s.whatsapp.net`, '15550002222@s.whatsapp.net']);
    const fresh = await sockAt(dir);
    assert.equal(await fresh.signalRepository.lidMapping.getPNForLID(`${LID}@lid`), `${PHONE}:0@s.whatsapp.net`);

    // USync finds nothing: rc.9 returns null, lookupLids returns [].
    const none = await sockAt(tmp(), async () => []);
    assert.deepEqual(await lookupLids(none, ['15550003333']), []);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
