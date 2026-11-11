import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"

export default defineConfig({
  plugins: [react()],
  server: {
    host: "localhost",
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.SECUREPAY_API ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
})
