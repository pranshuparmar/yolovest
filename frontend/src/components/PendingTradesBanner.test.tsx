import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

// Mock the data + mutation hooks so we can render the banner with known
// pending trades and assert the header's money math. Two trades:
// 100×10 + 200×5 = ₹2,000 total investment, 15 total qty.
vi.mock("../hooks/queries", () => ({
  usePendingTrades: () => ({
    data: [
      {
        id: 1, symbol: "TCS", signal_type: "BUY", entry_price: 100,
        target_price: 110, stop_loss_price: 95, product: "CNC",
        position_size: 10, confidence_score: 0.6,
        holding_period: "short_term", expected_holding_days: 3,
      },
      {
        id: 2, symbol: "INFY", signal_type: "BUY", entry_price: 200,
        target_price: 220, stop_loss_price: 190, product: "CNC",
        position_size: 5, confidence_score: 0.7,
        holding_period: "short_term", expected_holding_days: 3,
      },
    ],
  }),
  useApprovePendingTrade: () => ({ mutate: vi.fn(), isPending: false }),
  useRejectPendingTrade: () => ({ mutate: vi.fn(), isPending: false }),
  useClearTodaysSignals: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("../hooks/useLtpStream", () => ({ useLtpStream: () => new Map() }));

import { PendingTradesBanner } from "./PendingTradesBanner";

describe("PendingTradesBanner", () => {
  it("shows the trade count, total qty, and computed total investment", () => {
    const { container } = render(
      <MemoryRouter>
        <PendingTradesBanner />
      </MemoryRouter>,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("2 trades awaiting approval");
    expect(text).toContain("Total qty");
    // 100*10 + 200*5 = 2,000 — proves the investment reduce, not a static label
    expect(text).toContain("₹2,000");
  });
});
