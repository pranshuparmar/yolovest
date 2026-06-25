import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ErrorBoundary } from "./ErrorBoundary";

function Boom() {
  throw new Error("kaboom");
}

describe("ErrorBoundary", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders children when there is no error", () => {
    render(
      <ErrorBoundary>
        <div>healthy content</div>
      </ErrorBoundary>
    );
    expect(screen.getByText("healthy content")).toBeInTheDocument();
  });

  it("renders a recoverable fallback (role=alert) when a child throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => {}); // expected boundary log
    render(
      <ErrorBoundary scope="page">
        <Boom />
      </ErrorBoundary>
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/something went wrong in the page/i)).toBeInTheDocument();
    expect(screen.getByText("kaboom")).toBeInTheDocument();
    expect(screen.getByText("Try again")).toBeInTheDocument();
  });

  it("uses a custom fallback when provided", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <ErrorBoundary fallback={(e) => <div>custom: {e.message}</div>}>
        <Boom />
      </ErrorBoundary>
    );
    expect(screen.getByText("custom: kaboom")).toBeInTheDocument();
  });

  it("recovers via 'Try again' once the child stops throwing", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    let shouldThrow = true;
    function Toggler() {
      if (shouldThrow) throw new Error("boom");
      return <div>recovered content</div>;
    }
    render(
      <ErrorBoundary>
        <Toggler />
      </ErrorBoundary>
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    shouldThrow = false;
    fireEvent.click(screen.getByText("Try again"));

    expect(screen.getByText("recovered content")).toBeInTheDocument();
  });
});
