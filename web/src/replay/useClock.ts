// The replay clock: a time within a window that plays at a chosen speed.

import { useCallback, useEffect, useRef, useState } from "react";

export interface Clock {
  t: number;
  playing: boolean;
  speed: number;
  setT: (t: number) => void;
  setPlaying: (playing: boolean) => void;
  setSpeed: (speed: number) => void;
}

const FRAME_MS = 66; // about 15 redraws a second is smooth enough for a map

export function useClock(window: [number, number] | null): Clock {
  const [t, setTState] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(10);
  const current = useRef(0);
  const [lo, hi] = window ?? [0, 0];

  // A new window starts at its beginning, paused.
  useEffect(() => {
    current.current = lo;
    setTState(lo);
    setPlaying(false);
  }, [lo, hi]);

  const setT = useCallback(
    (next: number) => {
      current.current = Math.min(Math.max(next, lo), hi);
      setTState(current.current);
    },
    [lo, hi],
  );

  useEffect(() => {
    if (!playing) return;
    let last = performance.now();
    let shown = last;
    let frame = 0;
    const tick = (now: number) => {
      current.current = Math.min(current.current + ((now - last) / 1000) * speed, hi);
      last = now;
      if (now - shown >= FRAME_MS || current.current >= hi) {
        shown = now;
        setTState(current.current);
      }
      if (current.current >= hi) setPlaying(false);
      else frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, speed, hi]);

  return { t, playing, speed, setT, setPlaying, setSpeed };
}
