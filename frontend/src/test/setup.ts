// Vitest global setup: register jest-dom matchers (toBeInTheDocument, etc.)
// so component tests can assert on the DOM. Pure-logic tests don't need it
// but it's harmless to load.
import "@testing-library/jest-dom/vitest";

// globals:false means RTL's automatic afterEach(cleanup) isn't registered, so
// unmount rendered trees between tests here — without it, renders pile up and
// queries find duplicate elements across tests.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => cleanup());
