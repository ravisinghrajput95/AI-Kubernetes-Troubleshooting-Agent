/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly react_PUBLIC_API_BASE_URL?: string;
  /** Set to opt into tests that require a live backend. */
  readonly VITE_API_INTEGRATION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

