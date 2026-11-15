import type { Clock } from "./useClock";

const SPEEDS = [1, 5, 10, 30, 60, 120];

export const utc = (t: number) => new Date(t * 1000).toISOString().slice(11, 19);

interface TimeBarProps {
  clock: Clock;
  window: [number, number];
  /** Seconds per step for the step buttons: one scan. */
  stepS: number;
}

export function TimeBar({ clock, window: [lo, hi], stepS }: TimeBarProps) {
  const { t, playing, speed, setT, setPlaying, setSpeed } = clock;
  return (
    <div className="timebar">
      <button
        className="primary play"
        onClick={() => {
          if (!playing && t >= hi) setT(lo);
          setPlaying(!playing);
        }}
        aria-label={playing ? "Pause" : "Play"}
      >
        {playing ? "❚❚" : "▶"}
      </button>
      <button onClick={() => setT(t - stepS)} title={`Back ${stepS} s (one scan)`} aria-label="Step back">
        ‹
      </button>
      <button onClick={() => setT(t + stepS)} title={`Forward ${stepS} s (one scan)`} aria-label="Step forward">
        ›
      </button>
      <span className="clock">{utc(t)} UTC</span>
      <input
        className="slider"
        type="range"
        min={lo}
        max={hi}
        step={0.5}
        value={t}
        onChange={(e) => setT(Number(e.target.value))}
        aria-label="Replay time"
      />
      <span className="muted small elapsed">
        {Math.floor((t - lo) / 60)}:{String(Math.floor((t - lo) % 60)).padStart(2, "0")} / {Math.round((hi - lo) / 60)} min
      </span>
      <label className="field inline">
        <span>Speed</span>
        <select value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
          {SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
