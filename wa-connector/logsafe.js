// Quick 046: connector log hygiene.
//  * libsignal (a Baileys dependency) console.info()s whole SessionEntry objects
//    ("Closing session: SessionEntry {...}"), which carry session private keys.
//    installConsoleGuard() drops those calls before they reach stdout.
//  * Phone numbers and JIDs are masked to their last 4 digits in every log line:
//    winston (logger.js), the console, and the Baileys pino logger.
import pino from 'pino';

// A run of 7+ digits not glued to letters/digits: phone numbers, LIDs, the
// digits part of a JID ("15550001111:12@s.whatsapp.net"). Keeps the last 4.
const DIGIT_RUN = /(?<![A-Za-z0-9])\+?\d{7,}(?![A-Za-z0-9])/g;

export function maskNumbers(text) {
  return String(text).replace(DIGIT_RUN, (m) => `***${m.slice(-4)}`);
}

// libsignal's own messages that print session objects (src/session_record.js,
// session_cipher.js, session_builder.js in @whiskeysockets/libsignal-node).
const SESSION_NOISE = /^(Closing|Opening) session:|^Removing old closed session:|^Session already (closed|open)|^Migrating session to:|^Closing open session in favor|^Decrypted message with closed session/;

export function isSessionDump(args) {
  if (typeof args[0] === 'string' && SESSION_NOISE.test(args[0])) return true;
  return args.some((a) => a && typeof a === 'object' && a.constructor?.name === 'SessionEntry');
}

function maskArg(a) {
  return typeof a === 'string' ? maskNumbers(a) : a;
}

let installed = false;
export function installConsoleGuard(target = console) {
  if (installed && target === console) return;
  if (target === console) installed = true;
  for (const m of ['log', 'info', 'warn', 'error', 'debug']) {
    const orig = target[m].bind(target);
    target[m] = (...args) => {
      if (isSessionDump(args)) return;
      orig(...args.map(maskArg));
    };
  }
}

// Baileys logger: warn and above only (its info/debug lines carry JIDs and
// message ids), every line passed through maskNumbers on the way out.
export function makeBaileysLogger(level = process.env.BAILEYS_LOG_LEVEL || 'warn',
                                  write = (line) => process.stdout.write(line)) {
  return pino({ level }, { write: (line) => write(maskNumbers(line)) });
}
