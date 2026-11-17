import './logsafe-boot.js';
import express from 'express';
import bodyParser from 'body-parser';
import dotenv from 'dotenv';
import cors from 'cors';
import whatsappRoutes from './routes/whatsapp.routes.js';


// Load environment variables
dotenv.config();

// Initialize Express app
const app = express();
const PORT = process.env.PORT || 3000;

// Middleware for CORS
app.use(cors({
  origin: '*', // Allow all origins
  methods: ['GET', 'POST'], // Allow specific HTTP methods
  allowedHeaders: ['Content-Type', 'Authorization', 'x-api-key'] // Allow specific headers
}));

// Middleware — 40mb limit so base64 document payloads (/api/send-document) fit
app.use(bodyParser.json({ limit: '40mb' }));
app.use(bodyParser.urlencoded({ extended: true, limit: '40mb' }));

// Simple API key auth middleware
const apiKeyAuth = (req, res, next) => {
  // Skip auth for QR and status endpoints
  if (req.path === '/qr' || req.path === '/status') return next();

  const apiKey = req.headers['x-api-key'];
  const validKey = process.env.API_KEY;

  if (!validKey) {
    console.warn('WARNING: API_KEY env variable not set. All requests are allowed.');
    return next();
  }

  if (apiKey !== validKey) {
    return res.status(401).json({ success: false, message: 'Unauthorized: invalid or missing API key' });
  }

  next();
};

// Use routes
app.use('/api/', apiKeyAuth, whatsappRoutes);

// Test route
app.get('/test', (req, res) => {
  res.json({ message: 'Server is working!' });
});

// Start server
app.listen(PORT, () => {
  console.log(`Server running on port ${PORT}`);
});