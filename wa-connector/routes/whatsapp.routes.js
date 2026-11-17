// routes/whatsapp.routes.js
import express from 'express';
import * as whatsappController from '../controllers/whatsapp.controller.js';

const router = express.Router();

// Routes
router.get('/qr', whatsappController.getQR);
router.get('/qr-data', whatsappController.getQRData); // JSON variant for the gateway's dashboard page
router.post('/message', whatsappController.sendMessage);
router.post('/bulk-message', whatsappController.sendBulkMessages); // New route for bulk messaging
router.post('/send-image', whatsappController.sendImage);
router.post('/send-document', whatsappController.sendDocument);
router.get('/inbox', whatsappController.getInbox); // POC: view captured inbound messages
router.get('/group-members', whatsappController.getGroupMembers); // roster for the gateway's chat_members sync
router.post('/lid-lookup', whatsappController.lidLookup); // Quick 017: LIDs of allowlisted numbers
router.get('/status', whatsappController.getStatus);
router.post('/logout', whatsappController.logout);

export default router;