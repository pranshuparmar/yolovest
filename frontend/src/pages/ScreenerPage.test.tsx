import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

const h = vi.hoisted(() => ({ reviewMutate: vi.fn() }));

vi.mock("../hooks/queries", () => ({
  useReviewHoldings: () => ({
    mutate: h.reviewMutate,
    isPending: false,
    isError: false,
    data: {
      recommendations: [
        {
          symbol: "TCS", action: "BUY", signal_type: "BUY", confidence: 0.72,
          reasoning: "ML BUY signal", last_price: 100, target_price: 120,
          stop_loss_price: 95, target_pct: 20, sl_pct: -5, day_change_pct: 1.2,
          week_change_pct: 3.4, vol_ratio: 1.5,
          held: false, quantity: 0, average_price: 0, pnl_pct: 0,
        },
        {
          symbol: "INFY", action: "HOLD", signal_type: "HOLD", confidence: 0.4,
          reasoning: "No strong directional signal", last_price: 200,
          held: false, quantity: 0, average_price: 0, pnl_pct: 0,
        },
      ],
    },
  }),
}));

import { ScreenerPage } from "./ScreenerPage";

describe("ScreenerPage", () => {
  it("scans a parsed (uppercased, deduped) symbol list and renders results", () => {
    render(
      <MemoryRouter>
        <ScreenerPage />
      </MemoryRouter>,
    );
    fireEvent.change(screen.getByPlaceholderText(/Paste symbols/i), {
      target: { value: "tcs, infy  reliance\ntcs" },
    });
    fireEvent.click(screen.getByText("Scan"));

    expect(h.reviewMutate).toHaveBeenCalledWith(["TCS", "INFY", "RELIANCE"]);
    expect(screen.getByText("TCS")).toBeInTheDocument();
    expect(screen.getByText("INFY")).toBeInTheDocument();
    expect(screen.getByText("BUY")).toBeInTheDocument();
    expect(screen.getByText("HOLD")).toBeInTheDocument();
  });
});
