import { defineConfig } from 'vite';
import handlebars from 'vite-plugin-handlebars';
import { resolve } from 'path';

export default defineConfig({
  root: './',
  publicDir: 'public',
  plugins: [
    handlebars({
      // The plugin does not recurse — nested partial dirs must be listed.
      partialDirectory: [
        resolve(__dirname, 'partials'),
        resolve(__dirname, 'partials/tabs'),
      ],
    }),
  ],
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: './index.html',
        login: './login.html',
      },
    },
    minify: 'esbuild',
    sourcemap: false,
  },
  server: {
    // 5183 is production and 5184 is the dev container, so the Vite dev server
    // sits above both.
    port: 5185,
    proxy: {
      // Points at the dev container's published port, NOT 8000 — uvicorn binds
      // 127.0.0.1:8000 *inside* the container and is not reachable from the host.
      // Override with PLEXLECTION_API=http://localhost:8000 when running uvicorn
      // directly on the host instead of in Docker.
      //
      // Must stay on the *dev* port. 5183 now serves the production container,
      // and pointing HMR at it would mean editing the frontend against live
      // data — including anything that writes labels back to Plex.
      '/api': {
        target: process.env.PLEXLECTION_API || 'http://localhost:5184',
        changeOrigin: true,
      },
    },
  },
});
