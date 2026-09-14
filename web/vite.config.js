import { defineConfig } from 'vite';

const API_TARGET = process.env.API_TARGET || 'http://localhost:8080';

const proxy = Object.fromEntries(
  ['/routes', '/airports', '/aircraft', '/countries', '/health'].map((p) => [
    p,
    { target: API_TARGET, changeOrigin: true },
  ])
);

export default defineConfig({
  base: '/',
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy,
  },
});
