import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// vitest is configured without globals, so register React Testing Library's
// between-test DOM cleanup ourselves.
afterEach(() => {
  cleanup();
});
