// OSPA over time for the trackers being compared, with the replay time as
// a cursor. One y axis (metres), thin lines, a legend and end labels, a
// crosshair readout on hover, and click to move the replay there.

import { useEffect, useMemo, useRef, useState } from "react";
import { utc } from "./TimeBar";

export interface OspaSeries {
  label: string;
  color: string;
  times: number[]; // POSIX seconds
  ospa: number[];
}

interface OspaChartProps {
  series: OspaSeries[];
  window: [number, number];
  t: number;
  /** The OSPA cut-off: the axis's top (an error can't exceed it). */
  cutoffM: number;
  onSeek: (t: number) => void;
}

const HEIGHT = 150;
const PAD = { left: 44, right: 12, top: 10, bottom: 22 };

function nearest(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (times[mid]! < t) lo = mid + 1;
    else hi = mid;
  }
  if (lo > 0 && Math.abs(times[lo - 1]! - t) < Math.abs(times[lo]! - t)) return lo - 1;
  return lo;
}

export function OspaChart({ series, window: [lo, hi], t, cutoffM, onSeek }: OspaChartProps) {
  const box = useRef<SVGSVGElement>(null);
  const [width, setWidth] = useState(800);
  const [hover, setHover] = useState<number | null>(null);

  // Track the drawing width so lines stay 2px whatever the size.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => entry && setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const plotW = Math.max(width - PAD.left - PAD.right, 10);
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const x = (time: number) => PAD.left + ((time - lo) / Math.max(hi - lo, 1)) * plotW;
  const y = (m: number) => PAD.top + (1 - Math.min(m, cutoffM) / cutoffM) * plotH;

  const paths = useMemo(
    () =>
      series.map((s) =>
        s.times
          .map((time, i) => `${i ? "L" : "M"}${x(time).toFixed(1)},${y(s.ospa[i]!).toFixed(1)}`)
          .join(""),
      ),
    [series, width, lo, hi, cutoffM],
  );

  const ticksY = [0, 0.25, 0.5, 0.75, 1].map((f) => f * cutoffM);
  const ticksX = Array.from({ length: 7 }, (_, i) => lo + ((hi - lo) * i) / 6);
  const timeAt = (clientX: number) => {
    const rect = box.current!.getBoundingClientRect();
    return lo + ((clientX - rect.left - PAD.left) / plotW) * (hi - lo);
  };
  const shown = hover ?? t;

  return (
    <div className="chart">
      <div className="chart-head">
        <span className="strong">OSPA over time</span>
        <span className="muted small">metres, lower is better; capped at the {cutoffM} m cut-off</span>
        <span className="legend-inline">
          {series.map((s) => {
            const i = s.times.length ? nearest(s.times, shown) : -1;
            return (
              <span key={s.label} className="legend-item">
                <span className="swatch line" style={{ background: s.color }} />
                {s.label}
                <span className="value">{i >= 0 ? `${Math.round(s.ospa[i]!)} m` : "–"}</span>
              </span>
            );
          })}
          <span className="muted small">at {utc(shown)}</span>
        </span>
      </div>
      <svg
        ref={box}
        className="chart-svg"
        height={HEIGHT}
        role="img"
        aria-label={`OSPA over time for ${series.map((s) => s.label).join(" and ")}`}
        onMouseMove={(e) => setHover(Math.min(Math.max(timeAt(e.clientX), lo), hi))}
        onMouseLeave={() => setHover(null)}
        onClick={(e) => onSeek(Math.min(Math.max(timeAt(e.clientX), lo), hi))}
      >
        {ticksY.map((m) => (
          <g key={m}>
            <line className="grid" x1={PAD.left} x2={PAD.left + plotW} y1={y(m)} y2={y(m)} />
            <text className="tick" x={PAD.left - 6} y={y(m) + 4} textAnchor="end">
              {m}
            </text>
          </g>
        ))}
        {ticksX.map((time, i) => (
          <text
            key={time}
            className="tick"
            x={x(time)}
            y={HEIGHT - 6}
            textAnchor={i === 0 ? "start" : i === ticksX.length - 1 ? "end" : "middle"}
          >
            {utc(time).slice(0, 5)}
          </text>
        ))}
        {paths.map((d, i) => (
          <path key={series[i]!.label} d={d} fill="none" stroke={series[i]!.color} strokeWidth={2} strokeLinejoin="round" />
        ))}
        <line className="cursor" x1={x(t)} x2={x(t)} y1={PAD.top} y2={PAD.top + plotH} />
        {hover !== null && <line className="crosshair" x1={x(hover)} x2={x(hover)} y1={PAD.top} y2={PAD.top + plotH} />}
        {hover !== null &&
          series.map((s) => {
            if (!s.times.length) return null;
            const i = nearest(s.times, hover);
            return (
              <circle key={s.label} cx={x(s.times[i]!)} cy={y(s.ospa[i]!)} r={4} fill={s.color} stroke="var(--surface-1)" strokeWidth={2} />
            );
          })}
      </svg>
    </div>
  );
}
