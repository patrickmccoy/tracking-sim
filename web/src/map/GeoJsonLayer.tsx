// One GeoJSON source and the layers drawn from it.

import type {
  FilterSpecification,
  GeoJSONSource,
  LayerSpecification,
  Map as MlMap,
  MapLayerMouseEvent,
} from "maplibre-gl";
import { useEffect, useRef } from "react";
import { useMap } from "./MapView";

/** A font the base style's glyph server has: the one its own labels use. */
function styleFont(map: MlMap): string[] | undefined {
  for (const layer of map.getStyle().layers) {
    const font = layer.type === "symbol" ? layer.layout?.["text-font"] : undefined;
    if (Array.isArray(font) && font.every((f) => typeof f === "string")) return font as string[];
  }
  return undefined;
}

type LayerDef = Omit<LayerSpecification & { source?: unknown }, "source">;

interface GeoJsonLayerProps {
  id: string;
  data: GeoJSON.FeatureCollection;
  /** Layer specs without `source`; their ids should start with the source id. */
  layers: LayerDef[];
  /** Draw below this existing layer (default: on top). */
  before?: string;
  visible?: boolean;
  /** A filter applied to every layer (in addition to none of their own). */
  filter?: FilterSpecification;
  onClick?: (feature: GeoJSON.Feature, event: MapLayerMouseEvent) => void;
}

export function GeoJsonLayer({ id, data, layers, before, visible = true, filter, onClick }: GeoJsonLayerProps) {
  const map = useMap();
  const click = useRef(onClick);
  click.current = onClick;

  // Source and layers live as long as the component.
  useEffect(() => {
    map.addSource(id, { type: "geojson", data });
    const font = styleFont(map);
    for (const layer of layers) {
      const spec = { ...layer, source: id } as LayerSpecification;
      if (spec.type === "symbol" && font && !spec.layout?.["text-font"]) {
        spec.layout = { ...spec.layout, "text-font": font };
      }
      map.addLayer(spec, before && map.getLayer(before) ? before : undefined);
    }
    const ids = layers.map((l) => l.id);
    const handler = (e: MapLayerMouseEvent) => {
      const feature = e.features?.[0];
      if (feature && click.current) click.current(feature, e);
    };
    const enter = () => (map.getCanvas().style.cursor = click.current ? "pointer" : "");
    const leave = () => (map.getCanvas().style.cursor = "");
    for (const layer of ids) {
      map.on("click", layer, handler);
      map.on("mouseenter", layer, enter);
      map.on("mouseleave", layer, leave);
    }
    return () => {
      for (const layer of ids) {
        map.off("click", layer, handler);
        map.off("mouseenter", layer, enter);
        map.off("mouseleave", layer, leave);
        if (map.getLayer(layer)) map.removeLayer(layer);
      }
      if (map.getSource(id)) map.removeSource(id);
    };
    // Layer specs are fixed per id; data and visibility update below.
  }, [map, id]);

  useEffect(() => {
    (map.getSource(id) as GeoJSONSource | undefined)?.setData(data);
  }, [map, id, data]);

  useEffect(() => {
    for (const layer of layers) {
      if (map.getLayer(layer.id)) map.setLayoutProperty(layer.id, "visibility", visible ? "visible" : "none");
    }
  }, [map, visible]);

  const filterKey = JSON.stringify(filter ?? null);
  useEffect(() => {
    for (const layer of layers) {
      if (map.getLayer(layer.id)) map.setFilter(layer.id, filter ?? null);
    }
  }, [map, filterKey]);

  return null;
}
