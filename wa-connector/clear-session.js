#!/usr/bin/env node

import { clearSession } from './whatsapp.js';

console.log('🧹 Clearing WhatsApp session...');

if (clearSession()) {
  console.log('✅ Session cleared successfully!');
  console.log('📱 Restart the application to generate a new QR code.');
} else {
  console.log('❌ Failed to clear session.');
}
