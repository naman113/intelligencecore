// Quick 046: libsignal session dumps are dropped; numbers/JIDs masked to the last 4 digits.
// Run: npm test. All numbers are made up.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { maskNumbers, isSessionDump, installConsoleGuard, makeBaileysLogger } from '../logsafe.js';

test('maskNumbers keeps the last 4 digits of numbers and JIDs', () => {
  assert.equal(maskNumbers('from 15550001111'), 'from ***1111');
  assert.equal(maskNumbers('15550001111:12@s.whatsapp.net'), '***1111:12@s.whatsapp.net');
  assert.equal(maskNumbers('987650000012345@lid'), '***2345@lid');
  assert.equal(maskNumbers('+15550001111 ok'), '***1111 ok');
  assert.equal(maskNumbers('port 8080, 12 msgs'), 'port 8080, 12 msgs');
  assert.equal(maskNumbers('id 3EB0A1B2C3D4E5F67890'), 'id 3EB0A1B2C3D4E5F67890');
});

class SessionEntry { constructor() { this.privKey = 'SECRET'; } }

test('libsignal session dumps are recognised', () => {
  assert.ok(isSessionDump(['Closing session:', new SessionEntry()]));
  assert.ok(isSessionDump(['Removing old closed session:', {}]));
  assert.ok(isSessionDump(['anything', new SessionEntry()]));
  assert.ok(!isSessionDump(['Server running on port 3000']));
});

test('console guard drops dumps and masks numbers', () => {
  const out = [];
  const fake = { log: (...a) => out.push(a), info: (...a) => out.push(a), warn: (...a) => out.push(a),
                 error: (...a) => out.push(a), debug: (...a) => out.push(a) };
  installConsoleGuard(fake);
  fake.info('Closing session:', new SessionEntry());
  fake.log('Inbound dm from 15550001111');
  assert.deepEqual(out, [['Inbound dm from ***1111']]);
  assert.ok(!JSON.stringify(out).includes('SECRET'));
});

test('baileys logger hides info and masks warn lines', () => {
  const lines = [];
  const log = makeBaileysLogger('warn', (l) => lines.push(l));
  log.info({ jid: '15550001111@s.whatsapp.net' }, 'noisy');
  log.warn({ jid: '15550001111@s.whatsapp.net' }, 'careful');
  assert.equal(lines.length, 1);
  assert.ok(lines[0].includes('***1111@s.whatsapp.net') && !lines[0].includes('15550001111'));
});
