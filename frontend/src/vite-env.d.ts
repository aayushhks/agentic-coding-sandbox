/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base origin of the API when hosted apart from the dashboard (e.g. https://api.example.com). */
  readonly VITE_API_BASE_URL?: string;
  /** "true" on a static host with no API: reads come from the committed /static-api/ snapshots. */
  readonly VITE_STATIC_DATA?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
