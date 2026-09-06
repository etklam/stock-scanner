import type { Series } from "../api/client";

// Hand-rolled SVG chart: close line, SMA10/20/50, the selected window band and
// the close-resistance line. Geometry (SMA/nulls/window) comes from the API;
// this component only draws. Missing SMA values become GAPS, never zeros, and
// a constant series pads the domain instead of dividing by zero.

const W = 860;
const H = 320;
const PAD = { top: 14, right: 14, bottom: 26, left: 52 };

function scale(value: number, min: number, max: number, from: number, to: number): number {
  return from + ((value - min) / (max - min)) * (to - from);
}

function polyline(values: (number | null)[], xOf: (index: number) => number, yOf: (value: number) => number): string {
  // Split into contiguous non-null segments; nulls never render as 0.
  let path = "";
  let pen = false;
  values.forEach((value, index) => {
    if (value === null || !Number.isFinite(value)) {
      pen = false;
      return;
    }
    path += `${pen ? "L" : "M"}${xOf(index).toFixed(1)},${yOf(value).toFixed(1)}`;
    pen = true;
  });
  return path;
}

export function Chart({ series }: { series: Series }) {
  const { closes, sma, sessions } = series;
  const visible = [...closes, ...(sma.sma10 ?? []), ...(sma.sma20 ?? []), ...(sma.sma50 ?? [])].filter(
    (value): value is number => value !== null && Number.isFinite(value),
  );
  if (closes.length === 0 || visible.length === 0) {
    return <p className="muted">無可用價格資料。</p>;
  }
  let min = Math.min(...visible);
  let max = Math.max(...visible);
  if (series.close_resistance != null) {
    min = Math.min(min, series.close_resistance);
    max = Math.max(max, series.close_resistance);
  }
  // Constant series would make the domain empty: pad it instead.
  if (max - min < 1e-9) {
    const middle = (max + min) / 2 || 1;
    min = middle * 0.97;
    max = middle * 1.03;
  } else {
    const pad = (max - min) * 0.05;
    min -= pad;
    max += pad;
  }
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const xOf = (index: number) => PAD.left + (index / Math.max(closes.length - 1, 1)) * innerW;
  const yOf = (value: number) => PAD.top + innerH - scale(value, min, max, 0, innerH);
  const start = series.window_start ?? null;
  const end = series.window_end ?? null;
  const windowStart = start === null ? -1 : sessions.indexOf(start);
  const windowEnd = end === null ? -1 : sessions.indexOf(end);
  const hasWindow = windowStart >= 0 && windowEnd >= 0;
  const gridValues = [0, 0.5, 1].map((fraction) => min + fraction * (max - min));

  return (
    <figure className="chart" style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${series.symbol} 收市價與均線圖`}>
        {gridValues.map((value) => (
          <g key={value}>
            <line x1={PAD.left} x2={W - PAD.right} y1={yOf(value)} y2={yOf(value)} stroke="var(--line)" strokeWidth={1} />
            <text x={PAD.left - 6} y={yOf(value) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">
              {value.toFixed(2)}
            </text>
          </g>
        ))}
        {hasWindow && (
          <rect
            x={xOf(windowStart)}
            width={Math.max(xOf(windowEnd) - xOf(windowStart), 2)}
            y={PAD.top}
            height={innerH}
            fill="var(--chip)"
            opacity={0.75}
          />
        )}
        {series.close_resistance != null && (
          <g>
            <line
              x1={PAD.left}
              x2={W - PAD.right}
              y1={yOf(series.close_resistance)}
              y2={yOf(series.close_resistance)}
              stroke="var(--bad)"
              strokeDasharray="6 4"
              strokeWidth={1.5}
            />
            <text x={W - PAD.right} y={yOf(series.close_resistance) - 4} textAnchor="end" fontSize={11} fill="var(--bad)">
              收市阻力 {series.close_resistance.toFixed(2)}
            </text>
          </g>
        )}
        {([["sma10", "var(--warn)"], ["sma20", "var(--accent)"], ["sma50", "var(--good)"]] as const).map(
          ([key, color]) => {
            const values = sma[key];
            if (values === undefined) return null;
            return (
              <path
                key={key}
                d={polyline(values, xOf, yOf)}
                fill="none"
                stroke={color}
                strokeWidth={1.5}
                strokeDasharray="4 3"
              />
            );
          },
        )}
        <path d={polyline(closes, xOf, yOf)} fill="none" stroke="var(--text)" strokeWidth={1.8} />
      </svg>
      <figcaption className="legend">
        <span><span className="swatch" style={{ background: "var(--text)" }} />收市價</span>
        <span><span className="swatch" style={{ background: "var(--warn)" }} />SMA10</span>
        <span><span className="swatch" style={{ background: "var(--accent)" }} />SMA20</span>
        <span><span className="swatch" style={{ background: "var(--good)" }} />SMA50</span>
        {hasWindow && <span>灰色區域＝所選整理窗口</span>}
        <span className="muted">{series.displayed_sessions} 個時段・{series.price_basis}</span>
      </figcaption>
    </figure>
  );
}
