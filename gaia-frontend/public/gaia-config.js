// GAIA dashboard settings (loaded by gaia-app.html).
window.GAIA_CONFIG = {
  // Backend base URL. '/api' goes through the Vite dev proxy (vite.config.js).
  // For a static build, point this at the backend directly, e.g. 'http://127.0.0.1:8000'.
  apiBase: '/api',

  // Satellite basemap. With a Google Maps Platform key that has the "Map Tiles API"
  // enabled, the map uses Google satellite imagery (the same imagery as Google Earth).
  // Without a key it falls back to Esri World Imagery (no key needed).
  googleMapsKey: '',
};
