// controllers/whatsapp.controller.js
import { getWhatsAppInstance, getLatestQR, getInboxMessages, getGroupRoster } from '../whatsapp.js';
import fs from 'fs';
import QRCode from 'qrcode';
import logger from '../logger.js';
import { resolveRecipient, lookupLids } from '../senders.js';
import { generateWAMessageFromContent } from '@whiskeysockets/baileys';
import { buildQuoted } from '../quotes.js';

// Resolve a recipient to a JID: senders.resolveRecipient (Quick 017).
//
// A group JID passes through UNTOUCHED (the agent threads group conversations
// on the group JID and sends to it verbatim; an earlier version stripped the
// '@' out of "1203…@g.us" and made group replies unaddressable). A private ID
// (LID) goes to its phone number when Baileys' store has the pair, else to
// "<lid>@lid"; it never gets "@s.whatsapp.net", which would address a phone
// number that does not exist (the reply that never arrived on the first
// client copy). A plain phone number gets "@s.whatsapp.net" as before.
const formatPhoneNumber = (sock, number) => resolveRecipient(sock, number);

// Quick 021: the `quoted` option for a reply (quotes.js), dry-run through Baileys' own
// builder first (pure, no network) so a bad quote is dropped before anything is sent.
// Null means "send unquoted"; a quote never fails a send and a send is never retried.
const quoteFor = async (sock, jid, replyTo) => {
  if (!replyTo?.id) return null;
  const quoted = await buildQuoted(sock, jid, replyTo, {
    validate: (to, q) => generateWAMessageFromContent(
      to, { extendedTextMessage: { text: '' } }, { quoted: q, userJid: sock?.user?.id || '' }),
  });
  if (!quoted) logger.whatsapp(`Reply to message ${replyTo.id} goes out unquoted`);
  return quoted;
};
const sendOptions = (quoted) => (quoted ? { quoted } : undefined);

// Send text message
export const sendMessage = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    
    // Check if WhatsApp is connected
    if (!connectionReady || !waSocket) {
      logger.error('WhatsApp connection not ready for send message');
      return res.status(503).json({ 
        success: false, 
        message: 'WhatsApp connection not ready. Please scan the QR code first.' 
      });
    }

    const { phone, message, replyTo } = req.body;
    
    // Validate request
    if (!phone || !message) {
      logger.error(`Invalid request: phone number or message missing. Body: ${JSON.stringify(req.body)}, IP: ${req.ip}, Headers: ${JSON.stringify({ 'user-agent': req.headers['user-agent'], origin: req.headers['origin'], referer: req.headers['referer'] })}`);
      return res.status(400).json({ 
        success: false, 
        message: 'Phone number and message are required' 
      });
    }

    // Format phone number
    const formattedNumber = await formatPhoneNumber(waSocket, phone);

    // Send message (Quick 021: as a quoted reply when the gateway names the message it answers)
    const quoted = await quoteFor(waSocket, formattedNumber, replyTo);
    logger.whatsapp(`Attempting to send message to ${formattedNumber}${quoted ? ' (quoted reply)' : ''}`);
    await waSocket.sendMessage(formattedNumber, { text: message }, sendOptions(quoted));

    // Log success
    logger.messageSent(formattedNumber, message, true);

    res.status(200).json({ 
      success: true, 
      message: 'Message sent successfully' 
    });
  } catch (error) {
    // Log error
    logger.error(`Error sending message: ${error.message}`, { error: error.stack });
    logger.messageSent(req.body.phone, req.body.message, false, error);

    res.status(500).json({ 
      success: false, 
      message: 'Failed to send message', 
      error: error.message 
    });
  }
};

// NEW: Send bulk messages
export const sendBulkMessages = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    
    // Check if WhatsApp is connected
    if (!connectionReady || !waSocket) {
      logger.error('WhatsApp connection not ready for bulk messaging');
      return res.status(503).json({ 
        success: false, 
        message: 'WhatsApp connection not ready. Please scan the QR code first.' 
      });
    }

    const { messages } = req.body;
    
    // Validate request
    if (!messages || !Array.isArray(messages) || messages.length === 0) {
      logger.error(`Invalid bulk message request: messages array is empty or invalid. Body: ${JSON.stringify(req.body)}, IP: ${req.ip}, Headers: ${JSON.stringify({ 'user-agent': req.headers['user-agent'] })}`);
      return res.status(400).json({ 
        success: false, 
        message: 'A valid array of messages is required. Received empty or missing messages array.' 
      });
    }

    logger.info(`Starting bulk message operation for ${messages.length} messages`);

    // Results tracking
    const results = {
      total: messages.length,
      successful: 0,
      failed: 0,
      details: []
    };

    // Process each message with a small delay to avoid rate limiting
    for (const item of messages) {
      try {
        // Validate each message object
        if (!item.phone || !item.message) {
          throw new Error('Missing required fields (to, message)');
        }

        // Format phone number
        const formattedNumber = await formatPhoneNumber(waSocket, item.phone);
        
        // Send message with a small delay
        await waSocket.sendMessage(formattedNumber, { text: item.message });
        
        // Log success
        logger.messageSent(formattedNumber, item.message, true);
        
        // Update results
        results.successful++;
        results.details.push({
          phone: item.phone,
          status: 'success'
        });
        
        // Add a small delay to avoid flooding
        await new Promise(resolve => setTimeout(resolve, 100));
      } catch (error) {
        // Log individual message failure
        logger.messageSent(item.phone, item.message, false, error);
        
        // Update results
        results.failed++;
        results.details.push({
          phone: item.phone,
          status: 'failed',
          error: error.message
        });
      }
    }

    // Log overall bulk operation result
    logger.bulkOperation(
      results.total, 
      results.successful, 
      results.failed, 
      results.details.filter(d => d.status === 'failed').map(d => `${d.phone}: ${d.error}`)
    );

    res.status(200).json({
      success: true,
      message: `Bulk operation completed. ${results.successful} of ${results.total} messages sent successfully.`,
      results
    });
  } catch (error) {
    logger.error(`Bulk messaging operation failed: ${error.message}`, { error: error.stack });
    
    res.status(500).json({ 
      success: false, 
      message: 'Failed to process bulk messages', 
      error: error.message 
    });
  }
};

// Send a document (pdf/xlsx/docx/csv/png...) from base64 payload.
// Body: { phone, filename, mimetype, data (base64), caption? }
export const sendDocument = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();

    if (!connectionReady || !waSocket) {
      logger.error('WhatsApp connection not ready for send document');
      return res.status(503).json({ success: false, message: 'WhatsApp connection not ready' });
    }

    const { phone, filename, mimetype, data, caption, replyTo } = req.body;

    if (!phone || !filename || !data) {
      return res.status(400).json({
        success: false,
        message: 'phone, filename and data (base64) are required',
      });
    }

    const buffer = Buffer.from(data, 'base64');
    const MAX_BYTES = 25 * 1024 * 1024; // keep well under WhatsApp limits
    if (buffer.length === 0 || buffer.length > MAX_BYTES) {
      return res.status(400).json({
        success: false,
        message: `document must be 1 byte – ${MAX_BYTES} bytes (got ${buffer.length})`,
      });
    }

    const formattedNumber = await formatPhoneNumber(waSocket, phone);

    const quoted = await quoteFor(waSocket, formattedNumber, replyTo); // Quick 021

    logger.whatsapp(`Sending document "${filename}" (${buffer.length} bytes) to ${formattedNumber}${quoted ? ' (quoted reply)' : ''}`);
    await waSocket.sendMessage(formattedNumber, {
      document: buffer,
      fileName: filename,
      mimetype: mimetype || 'application/octet-stream',
      caption: caption || '',
    }, sendOptions(quoted));

    logger.info(`Document sent to ${formattedNumber}`, {
      category: 'message', type: 'document', filename, bytes: buffer.length,
    });

    res.status(200).json({ success: true, message: 'Document sent successfully' });
  } catch (error) {
    logger.error(`Error sending document: ${error.message}`, { error: error.stack });
    res.status(500).json({ success: false, message: 'Failed to send document', error: error.message });
  }
};

// Send image with optional caption
export const sendImage = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    
    if (!connectionReady || !waSocket) {
      logger.error('WhatsApp connection not ready for send image');
      return res.status(503).json({ 
        success: false, 
        message: 'WhatsApp connection not ready' 
      });
    }

    const { to, imagePath, caption, replyTo } = req.body;
    
    if (!to || !imagePath) {
      logger.error('Invalid image send request: missing required fields');
      return res.status(400).json({ 
        success: false, 
        message: 'Phone number and image path are required' 
      });
    }

    // Check if image exists
    if (!fs.existsSync(imagePath)) {
      logger.error(`Image file not found: ${imagePath}`);
      return res.status(404).json({ 
        success: false, 
        message: 'Image file not found' 
      });
    }

    // Format phone number
    const formattedNumber = await formatPhoneNumber(waSocket, to);

    // Read image file
    const image = fs.readFileSync(imagePath);
    
    // Send image (Quick 021: quoted when it answers a message)
    const quoted = await quoteFor(waSocket, formattedNumber, replyTo);
    logger.whatsapp(`Sending image to ${formattedNumber}${quoted ? ' (quoted reply)' : ''}`);
    await waSocket.sendMessage(formattedNumber, {
      image: image,
      caption: caption || ''
    }, sendOptions(quoted));

    logger.info(`Image sent to ${formattedNumber}`, { 
      category: 'message',
      type: 'image',
      imagePath,
      hasCaption: !!caption
    });

    res.status(200).json({ 
      success: true, 
      message: 'Image sent successfully' 
    });
  } catch (error) {
    logger.error(`Error sending image: ${error.message}`, { error: error.stack });
    
    res.status(500).json({ 
      success: false, 
      message: 'Failed to send image', 
      error: error.message 
    });
  }
};

// GET /api/qr-data — the pairing state as JSON, for the gateway's dashboard
// page. Unlike /qr (token-in-query, human-facing HTML) this sits under the
// normal x-api-key auth, because only the gateway calls it.
export const getQRData = async (req, res) => {
  try {
    const { connectionReady, waSocket } = getWhatsAppInstance();
    if (connectionReady) {
      const user = waSocket?.user || null;
      return res.status(200).json({
        success: true, connected: true, qr: null,
        user: user ? { id: user.id, name: user.name || null } : null,
      });
    }
    const qrString = getLatestQR();
    if (!qrString) {
      return res.status(200).json({ success: true, connected: false, qr: null });
    }
    const qrDataURL = await QRCode.toDataURL(qrString, { width: 400, margin: 2 });
    return res.status(200).json({ success: true, connected: false, qr: qrDataURL });
  } catch (error) {
    logger.error(`Error serving QR data: ${error.message}`, { error: error.stack });
    return res.status(500).json({ success: false, message: 'Failed to generate QR', error: error.message });
  }
};

// Get QR code page for scanning
export const getQR = async (req, res) => {
  try {
    // Token auth
    const token = req.query.token;
    const expectedToken = process.env.QR_TOKEN || 'whatsapp-qr-secret';
    if (token !== expectedToken) {
      return res.status(401).json({ success: false, message: 'Unauthorized. Provide ?token=YOUR_TOKEN' });
    }

    const { connectionReady } = getWhatsAppInstance();
    const qrString = getLatestQR();

    // If already connected
    if (connectionReady) {
      return res.send(`
        <!DOCTYPE html>
        <html><head><title>WhatsApp QR</title>
        <meta http-equiv="refresh" content="30">
        <style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0a0a0a;color:#25D366;}
        .container{text-align:center;padding:2rem;}
        .status{font-size:3rem;margin-bottom:1rem;}h1{margin:0;}</style></head>
        <body><div class="container"><div class="status">✅</div><h1>WhatsApp Connected</h1><p>Session is active. No QR code needed.</p></div></body></html>
      `);
    }

    // If no QR available yet
    if (!qrString) {
      return res.send(`
        <!DOCTYPE html>
        <html><head><title>WhatsApp QR</title>
        <meta http-equiv="refresh" content="5">
        <style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0a0a0a;color:#fff;}
        .container{text-align:center;padding:2rem;}
        .spinner{font-size:3rem;animation:spin 1s linear infinite;display:inline-block;}@keyframes spin{to{transform:rotate(360deg);}}
        </style></head>
        <body><div class="container"><div class="spinner">⏳</div><h1>Waiting for QR Code...</h1><p>Page will auto-refresh every 5 seconds.</p></div></body></html>
      `);
    }

    // Generate QR as data URL
    const qrDataURL = await QRCode.toDataURL(qrString, { width: 400, margin: 2 });

    res.send(`
      <!DOCTYPE html>
      <html><head><title>WhatsApp QR</title>
      <meta http-equiv="refresh" content="15">
      <style>body{font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;background:#0a0a0a;color:#fff;}
      .container{text-align:center;padding:2rem;background:#111;border-radius:16px;box-shadow:0 0 40px rgba(37,211,102,0.2);}
      img{border-radius:8px;margin:1rem 0;}
      h1{color:#25D366;margin-bottom:0.5rem;}
      p{color:#aaa;font-size:0.9rem;}
      .warning{color:#ff9800;font-weight:bold;}</style></head>
      <body><div class="container">
        <h1>📱 Scan QR Code</h1>
        <p>Open WhatsApp on your phone → Settings → Linked Devices → Link a Device</p>
        <img src="${qrDataURL}" alt="WhatsApp QR Code" />
        <p class="warning">⚠️ QR refreshes every ~20 seconds. Page auto-reloads every 15s.</p>
        <p>Generated at: ${new Date().toISOString()}</p>
      </div></body></html>
    `);
  } catch (error) {
    logger.error(`Error serving QR page: ${error.message}`, { error: error.stack });
    res.status(500).json({ success: false, message: 'Failed to generate QR page', error: error.message });
  }
};

// POC: get captured inbound messages (number, message, timestamp only)
export const getInbox = (req, res) => {
  const messages = getInboxMessages();
  res.status(200).json({
    success: true,
    count: messages.length,
    messages
  });
};

// Get connection status
// GET /api/group-members?jid=<group jid>[&force=1] — the full participant
// roster of one group, normalized to bare phone numbers. The gateway syncs
// this into its chat_members table so agents can answer "who is in this
// group?" from data instead of guessing.
export const getGroupMembers = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    if (!connectionReady || !waSocket) {
      return res.status(503).json({ success: false, message: 'WhatsApp connection not ready' });
    }

    const jid = (req.query.jid || '').toString().trim();
    if (!jid || !jid.endsWith('@g.us')) {
      return res.status(400).json({ success: false, message: 'query param jid must be a group jid (…@g.us)' });
    }

    const force = req.query.force === '1' || req.query.force === 'true';
    const roster = await getGroupRoster(waSocket, jid, { force });
    return res.status(200).json({ success: true, jid, ...roster }); // subject, size, members
  } catch (error) {
    logger.error(`Error fetching group members: ${error.message}`, { error: error.stack });
    // Baileys reports 'item-not-found' / 'forbidden' when we are not (or no
    // longer) a member of the group.
    const notFound = /not-found|item-not-found|forbidden/i.test(error.message || '');
    return res.status(notFound ? 404 : 500).json({
      success: false,
      message: 'Failed to fetch group members',
      error: error.message,
    });
  }
};

// POST /api/lid-lookup {phones: ["<digits>", ...]} — the private IDs (LIDs) of
// up to 50 phone numbers, through Baileys' documented one-way lookup
// (signalRepository.lidMapping.getLIDsForPNs: its store, then WhatsApp USync;
// found pairs are stored). The gateway asks for its allowlisted numbers when a
// sender arrives only as a LID, so that sender can be matched (Quick 017).
export const lidLookup = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    if (!connectionReady || !waSocket) {
      return res.status(503).json({ success: false, message: 'WhatsApp connection not ready' });
    }
    const phones = Array.isArray(req.body?.phones) ? req.body.phones : [];
    if (!phones.length) {
      return res.status(400).json({ success: false, message: 'phones must be a non-empty array' });
    }
    const pairs = await lookupLids(waSocket, phones);
    logger.whatsapp(`LID lookup: ${pairs.length} of ${Math.min(phones.length, 50)} number(s) resolved`);
    return res.status(200).json({ success: true, pairs });
  } catch (error) {
    logger.error(`LID lookup failed: ${error.message}`);
    return res.status(500).json({ success: false, message: 'LID lookup failed', error: error.message });
  }
};

export const getStatus = (req, res) => {
  const { connectionReady, waSocket } = getWhatsAppInstance();
  
  const status = {
    connected: connectionReady,
    timestamp: new Date().toISOString()
  };

  // Add extra info if connected
  if (connectionReady && waSocket) {
    try {
      status.user = waSocket.user || {};
    } catch (error) {
      logger.error(`Error getting user info: ${error.message}`);
    }
  }

  logger.whatsapp(`Status check: ${connectionReady ? 'Connected' : 'Disconnected'}`);
  
  res.status(200).json({ 
    success: true, 
    ...status
  });
};

// Logout and clear session
export const logout = async (req, res) => {
  try {
    const { waSocket, connectionReady } = getWhatsAppInstance();
    
    if (!connectionReady || !waSocket) {
      logger.error('WhatsApp connection not ready for logout');
      return res.status(503).json({ 
        success: false, 
        message: 'WhatsApp connection not ready' 
      });
    }

    logger.whatsapp('Logging out from WhatsApp');
    
    // Logout from WhatsApp
    await waSocket.logout();
    
    logger.whatsapp('Logged out successfully');
    
    res.status(200).json({ 
      success: true, 
      message: 'Logged out successfully' 
    });
  } catch (error) {
    logger.error(`Error during logout: ${error.message}`, { error: error.stack });
    
    res.status(500).json({ 
      success: false, 
      message: 'Failed to logout', 
      error: error.message 
    });
  }
};