import type { MapMouseEvent } from "maplibre-gl";
import { useEffect, useRef } from "react";
import { useMap } from "./MapView";

/** Calls `onClick` for clicks on the map itself; `cursor` styles the canvas meanwhile. */
export function MapClick({ onClick, cursor }: { onClick: (lat: number, lon: number) => void; cursor?: string }) {
  const map = useMap();
  const handler = useRef(onClick);
  handler.current = onClick;
  useEffect(() => {
    const click = (e: MapMouseEvent) => handler.current(e.lngLat.lat, e.lngLat.lng);
    map.on("click", click);
    const canvas = map.getCanvas();
    const before = canvas.style.cursor;
    if (cursor) canvas.style.cursor = cursor;
    return () => {
      map.off("click", click);
      canvas.style.cursor = before;
    };
  }, [map, cursor]);
  return null;
}
