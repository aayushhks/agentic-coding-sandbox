/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base origin of the API when hosted apart from the dashboard (e.g. https://api.up.railway.app). */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
