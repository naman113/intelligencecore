import fs from 'fs';
import path from 'path';
import winston from 'winston';
import { fileURLToPath } from 'url';
import { dirname } from 'path';
import { installConsoleGuard, maskNumbers } from './logsafe.js';

installConsoleGuard();

const { format, transports } = winston;

// ES module equivalent of __dirname
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Ensure logs directory exists
const logsDir = path.join(__dirname, 'logs');
if (!fs.existsSync(logsDir)) {
  fs.mkdirSync(logsDir);
}

// Custom format for console output
// Mask phone numbers/JIDs (last 4 digits kept) in the message and in every string meta value.
const maskFormat = format((info) => {
  info.message = maskNumbers(info.message);
  for (const k of Object.keys(info)) {
    if (typeof info[k] === 'string' && k !== 'level' && k !== 'timestamp') info[k] = maskNumbers(info[k]);
  }
  return info;
});

const consoleFormat = format.combine(
  maskFormat(),
  format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
  format.colorize(),
  format.printf(({ level, message, timestamp }) => {
    return `${timestamp} ${level}: ${message}`;
  })
);

// Custom format for file logs (JSON for better parsing)
const fileFormat = format.combine(
  maskFormat(),
  format.timestamp({ format: 'YYYY-MM-DD HH:mm:ss' }),
  format.json()
);

// Create logger instance
const logger = winston.createLogger({
  level: process.env.LOG_LEVEL || 'info',
  defaultMeta: { service: 'whatsapp-api' },
  transports: [
    // Console transport for development
    new transports.Console({
      format: consoleFormat,
    }),
    
    // File transport for all logs (info and above)
    new transports.File({
      filename: path.join(logsDir, 'combined.log'),
      format: fileFormat,
      maxsize: 5242880, // 5MB
      maxFiles: 5,
    }),
    
    // File transport for error logs only
    new transports.File({
      filename: path.join(logsDir, 'error.log'),
      level: 'error',
      format: fileFormat,
      maxsize: 5242880, // 5MB
      maxFiles: 5,
    }),
    
    // File transport for WhatsApp specific logs
    new transports.File({
      filename: path.join(logsDir, 'whatsapp.log'),
      format: fileFormat,
      maxsize: 5242880, // 5MB
      maxFiles: 5,
    })
  ],
});

// Helper function to log WhatsApp specific events
logger.whatsapp = (message, meta = {}) => {
  logger.info(message, { ...meta, category: 'whatsapp' });
};

// Helper function to log message sending
logger.messageSent = (to, message, success, error = null) => {
  const logData = {
    to,
    success,
    category: 'message',
    messageLength: message ? message.length : 0,
  };
  
  if (error) {
    logData.error = error.message || String(error);
    logger.error(`Failed to send message to ${to}`, logData);
  } else {
    logger.info(`Message sent to ${to}`, logData);
  }
};

// Helper function to log bulk message operations
logger.bulkOperation = (totalMessages, successCount, failedCount, errors = []) => {
  logger.info(`Bulk message operation: ${successCount}/${totalMessages} successful, ${failedCount} failed`, {
    category: 'bulk',
    totalMessages,
    successCount,
    failedCount,
    errors: errors.length > 0 ? errors : undefined,
  });
};

export default logger;