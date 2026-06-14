// Vitest global setup: register jest-dom matchers (toBeInTheDocument, etc.)
// so component tests can assert on the DOM. Pure-logic tests don't need it
// but it's harmless to load.
import "@testing-library/jest-dom/vitest";
