import path from 'node:path'
import { fileURLToPath } from 'node:url'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const sourceRoot = path.dirname(fileURLToPath(import.meta.url))

export default defineConfig({
  base: '/static/dist/liva-ui/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(sourceRoot, './src'),
    },
  },
  build: {
    outDir: '../static/dist/liva-ui',
    emptyOutDir: true,
    manifest: true,
    rollupOptions: {
      output: {
        entryFileNames: 'liva-ui.js',
        chunkFileNames: 'chunks/[name]-[hash].js',
        assetFileNames: (assetInfo) =>
          assetInfo.names?.some((name) => name.endsWith('.css'))
            ? 'liva-ui.css'
            : 'assets/[name]-[hash][extname]',
      },
    },
  },
})
