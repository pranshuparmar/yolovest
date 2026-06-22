import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

// Hoisted mutation spies so the vi.mock factory can reference them.
const h = vi.hoisted(() => ({
  reviewMutate: vi.fn(),
  addWatchlistMutate: vi.fn(),
  createAlertMutate: vi.fn(),
  manualTradeMutate: vi.fn(),
}));

vi.mock("../hooks/queries", () => ({
  useUniverseSymbols: () => ({ data: ["TCS", "RELIANCE"] }),
  useRecentTradedSymbols: () => ({ data: [] }),
  useSymbolQuickContext: () => ({
    data: { ltp: 100, bars: [], sector: "IT", quarantine: { is_quarantined: false } },
  }),
  useReviewHoldings: () => ({
    mutate: h.reviewMutate,
    reset: vi.fn(),
    isPending: false,
    isError: false,
    data: {
      recommendations: [
        {
          symbol: "TCS", action: "BUY", signal_type: "BUY", confidence: 0.7,
          reasoning: "ML BUY signal at 70% confidence",
          target_price: 120, stop_loss_price: 95,
          held: false, quantity: 0, average_price: 0, last_price: 100, pnl_pct: 0,
        },
      ],
    },
  }),
  useAddUserWatchlistSymbol: () => ({
    mutate: h.addWatchlistMutate, reset: vi.fn(), isPending: false, isSuccess: false,
  }),
  useCreateAlert: () => ({
    mutate: h.createAlertMutate, reset: vi.fn(), isPending: false, isSuccess: false,
  }),
  useManualTrade: () => ({
    mutate: h.manualTradeMutate, reset: vi.fn(), isPending: false,
    isSuccess: false, isError: false, data: undefined,
  }),
}));

vi.mock("../hooks/useLtpStream", () => ({ useLtpStream: () => new Map() }));

import { QuickReviewFloater } from "./QuickReviewFloater";

function openAndReview(sym = "TCS") {
  render(
    <MemoryRouter>
      <QuickReviewFloater />
    </MemoryRouter>,
  );
  fireEvent.click(screen.getByLabelText("Open Quick ML Review"));
  const input = screen.getByPlaceholderText(/Type a symbol/i);
  fireEvent.change(input, { target: { value: sym } });
  fireEvent.keyDown(input, { key: "Enter" });
}

describe("QuickReviewFloater — act on a review", () => {
  it("runs the review and surfaces Watch / Alert / Trade on a BUY reco", () => {
    openAndReview();
    expect(h.reviewMutate).toHaveBeenCalledWith(["TCS"]);
    expect(screen.getByText("☆ Watch")).toBeInTheDocument();
    expect(screen.getByText("🔔 Alert")).toBeInTheDocument();
    expect(screen.getByText("Trade…")).toBeInTheDocument();
    // TCS is in the (mocked) universe → no off-universe caveat.
    expect(screen.queryByText("outside universe")).not.toBeInTheDocument();
  });

  it("flags a symbol outside the tracked universe", () => {
    openAndReview("ZOMATO"); // not in the mocked universe
    expect(screen.getByText("outside universe")).toBeInTheDocument();
  });

  it("Watch adds the symbol to the user watchlist", () => {
    openAndReview();
    fireEvent.click(screen.getByText("☆ Watch"));
    expect(h.addWatchlistMutate).toHaveBeenCalledWith({ symbol: "TCS" });
  });

  it("Alert creates a price alert at the target (above current)", () => {
    openAndReview();
    fireEvent.click(screen.getByText("🔔 Alert"));
    expect(h.createAlertMutate).toHaveBeenCalledWith({
      symbol: "TCS", target_price: 120, direction: "above",
    });
  });

  it("Trade is gated behind an explicit confirm panel (no silent live order)", () => {
    openAndReview();
    // No live order placed just from showing the reco.
    expect(h.manualTradeMutate).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Trade…"));
    fireEvent.click(screen.getByText("Place live trade"));
    expect(h.manualTradeMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        symbol: "TCS", signal_type: "BUY", target_price: 120,
        stop_loss_price: 95, position_size: 1,
      }),
    );
  });
});
