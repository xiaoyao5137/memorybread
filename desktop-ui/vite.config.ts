import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import packageJson from './package.json'
import { customerLogUploadPlugin } from './dev/customerLogUpload.mjs'

export default defineConfig(({ mode }) => ({
  plugins: [react(), customerLogUploadPlugin()],
  define: {
    __APP_VERSION__: JSON.stringify(packageJson.version),
  },

  // 指向 Tauri 开发服务器
  server: {
    port:        1420,
    strictPort:  true,
    // Tests are not application modules. Their edits must not reload a live
    // consultation window and discard its draft. Keep Vitest watch mode intact.
    watch: mode === 'test' ? undefined : {
      ignored: ['**/__tests__/**', '**/test/**', '**/tests/**', '**/*.{test,spec}.{ts,tsx,js,jsx,mts,mjs}', '**/src-tauri/**'],
    },
  },

  // Vitest 测试配置
  test: {
    globals:     true,
    environment: 'jsdom',
    setupFiles:  ['./src/__tests__/setup.ts'],
    css:         false,
    // 构建产物里可能混入扩展测试文件的副本（node:test 格式），不应被 vitest 收集
    exclude: [
      '**/node_modules/**',
      '**/dist/**',
      '**/.git/**',
      'src-tauri/target/**',
    ],
  },
}))
