/// <reference types="vite/client" />

declare const __APP_VERSION__: string

interface ImportMetaEnv {
  readonly VITE_MEMORYBREAD_DEBUG_MODE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
