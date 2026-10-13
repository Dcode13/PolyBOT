export type Mode = "live" | "paper";
export type Position = {
  id: string; title: string; slug: string | null; eventSlug: string | null;
  outcome: string; size: number | null; avgPrice: number | null; currentPrice: number | null;
  value: number | null; cost: number | null; pnl: number | null; pnlPercent: number | null;
  status: string; redeemable: boolean; category: string; source: string;
};
export type Trade = {
  id: string; time: string; side: string; title: string; outcome: string;
  size: number | null; price: number | null; cash: number | null; pnl: number | null;
  status: string; source: string;
};
export type Snapshot = {
  generatedAt: string; mode: Mode; historySource?: string;
  account: {
    wallet: string | null; cash: number | null; positionsValue: number | null;
    equity: number | null; realizedPnl: number | null; unrealizedPnl: number | null;
    totalPnl: number | null; openOrdersCount: number | null; positionsCount: number;
    activePositionsCount: number; redeemablePositionsCount: number; pnlScope: string; connected: boolean;
    positionsComplete: boolean; historyComplete: boolean; equityAsOf: string | null;
  };
  positions: Position[]; trades: Trade[];
  equityHistory: {time: string; equity: number}[];
  bot: {
    status: string; lastScan: {scanned?: number; analyzed?: number; qualified?: unknown[]; errors?: unknown[]; halted?: string} | null;
    lastScanAt: string | null; stopped: boolean; pendingCount: number; ledgerAvailable: boolean;
    ledgerCash: number | null; ledgerEquity: number | null;
    rejections: {reason: string; count: number}[];
  };
  risk: Record<string, number | boolean | string>;
  errors: {code: string; message: string}[];
  stale?: boolean;
};
