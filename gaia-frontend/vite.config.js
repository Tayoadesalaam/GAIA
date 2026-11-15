import react from '@vitejs/plugin-react';

// The dashboard calls the GAIA backend at /api/* — proxied to uvicorn in dev.
// Override the target with GAIA_API_URL, e.g. GAIA_API_URL=http://127.0.0.1:8001 npm run dev
const API = process.env.GAIA_API_URL || 'http://127.0.0.1:8000';

export default {
  plugins: [react()],
  server: {
    proxy: {
      '/api': { target: API, changeOrigin: true, rewrite: p => p.replace(/^\/api/, '') },
    },
  },
};
