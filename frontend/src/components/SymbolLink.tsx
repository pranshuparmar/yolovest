import { Link } from "react-router-dom";
import clsx from "clsx";

type Props = {
  symbol: string;
  /** Extra classes appended to the default styling. Use this to keep
   * existing colour-tags (e.g. emerald for buys, amber for pending). */
  className?: string;
  children?: React.ReactNode;
};

/**
 * Single source of truth for "click symbol to open detail page".
 * Default styling adds a subtle hover treatment without overriding any
 * colour the caller supplied. Children override the rendered text,
 * useful when the symbol needs a badge/icon next to it.
 */
export function SymbolLink({ symbol, className, children }: Props) {
  return (
    <Link
      to={`/symbol/${symbol}`}
      onClick={(e) => e.stopPropagation()}
      className={clsx("hover:underline hover:text-blue-400 transition-colors", className)}
    >
      {children ?? symbol}
    </Link>
  );
}
