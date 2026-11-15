// Reading and immutably updating nested values by path.

export type Path = readonly (string | number)[];

export function getAt(value: unknown, path: Path): unknown {
  let current = value;
  for (const key of path) {
    if (current === null || typeof current !== "object") return undefined;
    current = (current as Record<string | number, unknown>)[key];
  }
  return current;
}

/** A copy of `value` with `path` set to `next`, creating objects on the way. */
export function setAt<T>(value: T, path: Path, next: unknown): T {
  if (path.length === 0) return next as T;
  const [key, ...rest] = path as [string | number, ...(string | number)[]];
  const container = (value ?? {}) as Record<string | number, unknown>;
  const copy = (Array.isArray(container) ? [...container] : { ...container }) as Record<string | number, unknown>;
  copy[key] = setAt(container[key], rest, next);
  return copy as T;
}
