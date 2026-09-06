// Fixed Chinese presentation mapping for machine reason codes. Unknown codes
// fall back to a readable literal — the API contract stays the source of truth.

export const STAGE_LABELS: Record<string, string> = {
  FORMING: "整理中",
  NEAR_CLOSE_RESISTANCE: "接近收市阻力",
  CLOSE_BREAK_ABOVE: "收市升穿阻力",
  EXTENDED: "過度延伸",
};

export const REASON_LABELS: Record<string, string> = {
  MOMENTUM_GATE_FAILED: "動量條件未達標",
  BASE_RANGE_GATE_FAILED: "整理區間過寬",
  BASE_NET_RETURN_GATE_FAILED: "整理期淨變幅過大",
  STRUCTURE_BROKEN: "已跌穿整理區支撐",
  INSUFFICIENT_WINDOW_HISTORY: "視窗歷史不足",
  UNDEFINED_CONTRACTION: "收窄指標無法計算（分母為零）",
  UNAVAILABLE_FEATURE: "指標不可用",
  DATA_UNAVAILABLE: "資料不可用，未能評估",
  NO_DATA: "供應商無有效資料",
  STALE_DATA: "缺少目標交易日收市價",
  MISSING_REQUIRED_SESSION: "缺少必需的交易日",
  INVALID_CLOSE: "收市價無效（NaN／非正數）",
  CONFLICTING_DUPLICATE: "同一交易日有矛盾數值",
  INSUFFICIENT_HISTORY: "歷史長度不足",
  UNSUPPORTED_INSTRUMENT: "不支援的市場或資產類型",
  SUSPICIOUS_FLAT_SERIES: "可疑橫行序列，需人工覆核",
  ADJUSTMENT_REVIEW_REQUIRED: "價格調整口徑需覆核",
  EXECUTION_INCOMPATIBLE: "執行環境與接受時不相容",
};

export function reasonLabel(code: string): string {
  return REASON_LABELS[code] ?? `未知原因（${code}）`;
}

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "—";
  return STAGE_LABELS[stage] ?? stage;
}

export const REVIEW_LABELS: Record<string, string> = {
  worth_reviewing: "值得睇",
  borderline: "一般",
  not_useful: "唔值得睇",
};

export const CATEGORY_LABELS: Record<string, string> = {
  evaluated: "有效評估",
  excluded: "排除",
  data_error: "資料失敗",
};
