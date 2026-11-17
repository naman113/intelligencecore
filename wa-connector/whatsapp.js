// whatsapp.js
import makeWASocket, { DisconnectReason, useMultiFileAuthState, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import path from 'path';
import fs from 'fs';
import qrcode from 'qrcode-terminal';
import logger from './logger.js';
import { makeBaileysLogger } from './logsafe.js';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import {
  bareNumber, lookupPN, senderKeyFor, resolveSender, learnPairs, pairsFromContacts,
  attachLearners, buildPayload, initSeenLids, setLogger,
} from './senders.js';
import { rememberInbound, setQuoteLogger } from './quotes.js';

// ES module equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Store for active connections
let waSocket = null;
let connectionReady = false;
let reconnectAttempts = 0;
let latestQR = null;
const RECONNECT_DELAY = 5000; // 5 seconds

// POC: in-memory ring buffer of recently received inbound messages.
// Each entry holds only { number, message, timestamp } — nothing else.
const INBOX_LIMIT = 100;
const inboxMessages = [];

// Agent webhook: forward every inbound message to the agentic system.
// Configured via env: AGENT_WEBHOOK_URL (full URL) + AGENT_WEBHOOK_SECRET.
// Fire-and-forget with small retry; never blocks or crashes the connector.
const AGENT_WEBHOOK_URL = process.env.AGENT_WEBHOOK_URL || null;
const AGENT_WEBHOOK_SECRET = process.env.AGENT_WEBHOOK_SECRET || null;

async function forwardToAgent(payload) {
  if (!AGENT_WEBHOOK_URL || !AGENT_WEBHOOK_SECRET) return;

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const res = await fetch(AGENT_WEBHOOK_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-webhook-secret': AGENT_WEBHOOK_SECRET,
        },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(10000),
      });
      if (res.ok) {
        logger.whatsapp(`Forwarded message ${payload.messageId} to agent webhook`);
        return;
      }
      logger.error(`Agent webhook responded ${res.status} (attempt ${attempt})`);
    } catch (error) {
      logger.error(`Agent webhook failed (attempt ${attempt}): ${error.message}`);
    }
    await new Promise(r => setTimeout(r, attempt * 2000));
  }
}

// Extract plain text from the various WhatsApp message shapes.
function extractMessageText(message) {
  if (!message) return null;
  return (
    message.conversation ||
    message.extendedTextMessage?.text ||
    message.imageMessage?.caption ||
    message.videoMessage?.caption ||
    message.documentMessage?.caption ||
    null
  );
}

// Sender identity (phone number vs private ID / LID), the LID<->PN learning
// and outbound addressing live in senders.js (Quick 017).
setLogger(logger);
setQuoteLogger(logger);

// groupMetadata is a network round-trip and a busy group emits many messages,
// so subjects are cached for the process lifetime.
const groupSubjects = new Map();

async function groupSubject(sock, jid) {
  if (groupSubjects.has(jid)) return groupSubjects.get(jid);
  try {
    const subject = (await sock.groupMetadata(jid))?.subject || null;
    groupSubjects.set(jid, subject);
    return subject;
  } catch (error) {
    logger.error(`groupMetadata failed for ${jid}: ${error.message}`);
    return null;
  }
}

// Roster cache — SEPARATE from groupSubjects (process-lifetime) because
// membership changes; a short TTL bounds staleness while absorbing repeated
// syncs from the gateway.
const ROSTER_TTL_MS = 5 * 60 * 1000;
const groupRosters = new Map(); // jid -> { at: epochMs, data: {subject, size, members} }

// Normalize one group participant to a bare phone number + admin flag.
// GroupParticipant is Contact & {admin}: `id` may be LID form ("...@lid"),
// with the PN riding along as `phoneNumber`. Falls back to the LID->PN store,
// then to the bare LID digits so nobody silently disappears from the roster.
async function normalizeParticipant(sock, p) {
  const admin = p.admin || (p.isSuperAdmin ? 'superadmin' : p.isAdmin ? 'admin' : null);
  const id = p.id || '';
  if (id && !id.endsWith('@lid')) {
    return { number: bareNumber(id), admin, raw_id: id };
  }
  if (p.phoneNumber && !String(p.phoneNumber).endsWith('@lid')) {
    return { number: bareNumber(p.phoneNumber), admin, raw_id: id };
  }
  const pn = await lookupPN(sock, id);
  if (pn) return { number: pn, admin, raw_id: id };
  return { number: bareNumber(id), admin, raw_id: id, lid_unresolved: true };
}

// Full roster for a group. Throws on Baileys failure — the controller maps
// the error onto the HTTP envelope.
async function getGroupRoster(sock, jid, { force = false } = {}) {
  const cached = groupRosters.get(jid);
  if (!force && cached && Date.now() - cached.at < ROSTER_TTL_MS) return cached.data;

  const meta = await sock.groupMetadata(jid); // network round-trip
  // Participants carry id + phoneNumber (or lid): store the pairs so a later
  // LID-only message from any of them resolves (Quick 017).
  await learnPairs(sock, pairsFromContacts(meta?.participants));
  const members = [];
  for (const p of meta?.participants || []) {
    members.push(await normalizeParticipant(sock, p));
  }
  const data = { subject: meta?.subject || null, size: members.length, members };
  groupRosters.set(jid, { at: Date.now(), data });
  // Free freshness win: the subject cache is process-lifetime, so refresh it
  // whenever a roster fetch hands us a current subject.
  if (data.subject) groupSubjects.set(jid, data.subject);
  return data;
}

// Did this message address us? Our own id carries a device suffix
// ("...:38@s.whatsapp.net") and mentions arrive in either PN or LID form, so
// every comparison goes through bareNumber().
function isAddressingUs(sock, msg) {
  const me = bareNumber(sock?.user?.id);
  const meLid = bareNumber(sock?.user?.lid);
  const ctx = msg.message?.extendedTextMessage?.contextInfo
    || msg.message?.imageMessage?.contextInfo
    || msg.message?.documentMessage?.contextInfo;
  if (!ctx) return false;

  const mentioned = (ctx.mentionedJid || []).map(bareNumber);
  if (mentioned.includes(me) || (meLid && mentioned.includes(meLid))) return true;

  // Replying to one of our own messages counts as talking to us.
  const quotedAuthor = bareNumber(ctx.participant);
  if (quotedAuthor && (quotedAuthor === me || (meLid && quotedAuthor === meLid))) return true;

  return false;
}

// The quoted message a reply carries, reduced to anchoring context for the
// gateway: a short snippet plus who wrote it. Without this, "done" replied to
// a week-old message reaches triage with no clue what it answers — the reply
// text is all extractMessageText forwards. Quote authors arrive in LID form
// on modern WhatsApp; resolve toward a phone number like everywhere else and
// fall back to the bare LID digits rather than dropping the context.
async function quotedContext(sock, msg) {
  const ctx = msg.message?.extendedTextMessage?.contextInfo
    || msg.message?.imageMessage?.contextInfo
    || msg.message?.videoMessage?.contextInfo
    || msg.message?.documentMessage?.contextInfo;
  const quoted = ctx?.quotedMessage;
  if (!quoted) return null;
  const text = extractMessageText(quoted);
  if (!text) return null;

  const rawAuthor = ctx.participant || '';
  const author = (await lookupPN(sock, rawAuthor)) || bareNumber(rawAuthor);
  const me = bareNumber(sock?.user?.id);
  const meLid = bareNumber(sock?.user?.lid);
  const rawBare = bareNumber(rawAuthor);
  return {
    text: String(text).slice(0, 300),
    author: author || null,
    // Both forms checked: the quote may name us by PN or by LID.
    from_me: [author, rawBare].some(a => a && (a === me || (meLid && a === meLid))),
  };
}

// The gateway caps document intake at 30MB (the Anthropic document-block limit
// is 32MB); stay under it and let anything larger through as text-only.
const MAX_MEDIA_BYTES = 25 * 1024 * 1024;

async function extractMedia(msg) {
  const type = ['documentMessage', 'imageMessage'].find(t => msg.message?.[t]);
  if (!type) return null;
  const node = msg.message[type];
  try {
    const buf = await downloadMediaMessage(msg, 'buffer', {});
    if (!buf) return null;
    if (buf.length > MAX_MEDIA_BYTES) {
      logger.error(`media skipped: ${buf.length} bytes exceeds ${MAX_MEDIA_BYTES}`);
      return null;
    }
    return {
      filename: node.fileName || (type === 'imageMessage' ? 'image.jpg' : 'document.pdf'),
      mimetype: node.mimetype || 'application/octet-stream',
      data: buf.toString('base64'),
    };
  } catch (error) {
    logger.error(`media download failed: ${error.message}`);
    return null;
  }
}

// Auth session path
const sessionDir = path.join(__dirname, 'wa-sessions');
if (!fs.existsSync(sessionDir)) {
  fs.mkdirSync(sessionDir);
  logger.info(`Created WhatsApp sessions directory: ${sessionDir}`);
}
// LIDs messages arrived from, kept beside the auth keys (Quick 017).
initSeenLids(sessionDir, fs);

// Initialize WhatsApp connection
async function connectToWhatsApp() {
  logger.whatsapp('Initializing WhatsApp connection...');

  try {
    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version } = await fetchLatestBaileysVersion();
    logger.whatsapp(`Using Baileys version: ${version.join('.')}`);

    waSocket = makeWASocket({
      version,
      auth: state,
      logger: makeBaileysLogger(), // quick 046: warn+, numbers masked
      printQRInTerminal: false, // We'll handle QR manually
      browser: ['WhatsApp API', 'Chrome', '1.0.0'],
      generateHighQualityLinkPreview: true,
    });

    // Learn LID<->PN pairs from every event that reveals one (Quick 017).
    attachLearners(waSocket);

    // Handle connection updates
    waSocket.ev.on('connection.update', async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        latestQR = qr;
        logger.whatsapp('QR Code generated, please scan with your WhatsApp mobile app:');
        console.log('\n📱 Scan this QR code with your WhatsApp mobile app:\n');
        qrcode.generate(qr, { small: true });
        console.log('\n⚠️  QR code expires in 60 seconds. Scan quickly!\n');
      }

      if (connection === 'close') {
        const shouldReconnect = (lastDisconnect?.error instanceof Boom)
          ? lastDisconnect.error.output.statusCode !== DisconnectReason.loggedOut
          : true;

        const statusCode = lastDisconnect?.error instanceof Boom 
          ? lastDisconnect.error.output.statusCode 
          : null;

        logger.error(`Connection closed. Reason: ${lastDisconnect?.error?.message || 'Unknown'}`);
        logger.whatsapp(`Status Code: ${statusCode}, Should reconnect: ${shouldReconnect}`);

        connectionReady = false;

        if (shouldReconnect) {
          reconnectAttempts++;
          // No hard cap while unpaired: keep cycling so a fresh, scannable QR is
          // always available at /api/qr — even if the device is linked many hours later.
          logger.whatsapp(`Reconnection attempt ${reconnectAttempts} in ${RECONNECT_DELAY/1000} seconds...`);

          setTimeout(() => {
            connectToWhatsApp();
          }, RECONNECT_DELAY);
        } else {
          // WhatsApp invalidated the session (logged out / conflict). The saved
          // creds can never reconnect — wipe them and start over so a fresh QR
          // is served instead of sitting dead until a manual restart.
          logger.whatsapp('Logged out by WhatsApp. Clearing session and reconnecting to generate a fresh QR...');
          latestQR = null;
          clearSession();
          setTimeout(() => {
            connectToWhatsApp();
          }, RECONNECT_DELAY);
        }
      } else if (connection === 'open') {
        logger.whatsapp('WhatsApp connection established successfully!');
        connectionReady = true;
        latestQR = null; // Clear QR once connected
        reconnectAttempts = 0; // Reset reconnection attempts on successful connection
        
        // Log connection details
        try {
          const userInfo = waSocket.user || {};
          logger.whatsapp('Connected as:', {
            id: userInfo.id,
            name: userInfo.name
          });
        } catch (error) {
          logger.error(`Error getting user details: ${error.message}`);
        }
      } else if (connection === 'connecting') {
        logger.whatsapp('Connecting to WhatsApp...');
      }
    });

    // Log messages + POC inbound capture
    waSocket.ev.on('messages.upsert', async (m) => {
      if (m.type !== 'notify') return;

      logger.whatsapp(`Received ${m.messages.length} new message(s)`);

      for (const msg of m.messages) {
        // Skip our own outgoing echoes — capture only messages we receive.
        if (msg.key?.fromMe) continue;

        const chatJid = msg.key?.remoteJid || '';
        const isGroup = chatJid.endsWith('@g.us');

        const text = extractMessageText(msg.message);
        const media = await extractMedia(msg);
        // Ignore receipts, reactions and other bodiless events — but an
        // attachment with no caption is a real message and must survive.
        if (!text && !media) continue;

        // Who sent it: the phone number when WhatsApp revealed it, and the
        // private ID (LID) when it addressed the sender that way. In groups
        // this looks at the participant rather than the group itself. An
        // unresolved LID is forwarded as such; the gateway decides (Quick 017).
        // The one short store retry is for direct messages only: in a group a
        // participant's number is often hidden on purpose and a wait per
        // message would only slow the group down.
        const who = await resolveSender(waSocket, senderKeyFor(msg.key),
          isGroup ? { retryDelayMs: 0 } : undefined);
        const number = who.phone || who.lid || '';

        // WhatsApp gives the timestamp in unix seconds → ISO string.
        const tsSeconds = Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000);
        const timestamp = new Date(tsSeconds * 1000).toISOString();

        const entry = { number, message: text || `[${media.mimetype}]`, timestamp };
        inboxMessages.push(entry);
        if (inboxMessages.length > INBOX_LIMIT) inboxMessages.shift();

        // A DM is inherently addressed to us; in a group we must be mentioned
        // (or quoted). The gateway uses this to decide whether to REPLY — it
        // records and triages the message either way.
        const mentioned = isGroup ? isAddressingUs(waSocket, msg) : true;
        const subject = isGroup ? await groupSubject(waSocket, chatJid) : null;
        const quoted = await quotedContext(waSocket, msg);
        // Quick 021: keep what we may answer, so the reply can quote it (quotes.js).
        if (!isGroup || mentioned) rememberInbound(msg);

        logger.whatsapp(
          `Inbound ${isGroup ? `group "${subject || chatJid}"` : 'dm'} from ${number}` +
          `${who.resolved ? '' : ' [private ID, number not revealed yet]'}` +
          `${media ? ' [+media]' : ''}${isGroup && mentioned ? ' @us' : ''}` +
          `${quoted ? ` [reply→${quoted.from_me ? 'us' : quoted.author || '?'}]` : ''}`);

        // Forward to the agentic system (non-blocking).
        forwardToAgent(buildPayload({
          msg, who, text, timestamp, isGroup, chatJid, subject, mentioned, media, quoted,
        }));
      }
    });

    // Save session credentials on update
    waSocket.ev.on('creds.update', saveCreds);
    
    return waSocket;
  } catch (error) {
    logger.error(`Failed to initialize WhatsApp connection: ${error.message}`, { error: error.stack });
    throw error;
  }
}

function getWhatsAppInstance() {
  return { waSocket, connectionReady };
}

function getLatestQR() {
  return latestQR;
}

// POC: return captured inbound messages, newest first.
function getInboxMessages() {
  return [...inboxMessages].reverse();
}

// Function to clear session (useful for debugging)
function clearSession() {
  try {
    if (fs.existsSync(sessionDir)) {
      // Delete contents only — the directory itself may be a mounted volume
      // (Railway), and removing a mount point fails with EBUSY.
      for (const entry of fs.readdirSync(sessionDir)) {
        fs.rmSync(path.join(sessionDir, entry), { recursive: true, force: true });
      }
      initSeenLids(sessionDir, fs); // the seen-LID list went with the files
      logger.whatsapp('Session cleared successfully');
    }
    return true;
  } catch (error) {
    logger.error(`Error clearing session: ${error.message}`);
    return false;
  }
}

// Initialize connection on startup
logger.info('Starting WhatsApp API service');
connectToWhatsApp().catch(err => {
  logger.error(`Failed to start WhatsApp connection: ${err.message}`, { error: err.stack });
});

export {
  getWhatsAppInstance,
  connectToWhatsApp,
  clearSession,
  getLatestQR,
  getInboxMessages,
  getGroupRoster
};