import type { Capture } from "../api";

/**
 * A scenario's name as its traffic changes, so runs on a capture don't pass
 * for runs on the scenario's own traffic. Only a name the user hasn't
 * changed (the file's, or one made here) is replaced.
 */
export function nameFor(current: string, original: string, picked: Capture | null | undefined): string {
  if (picked === undefined) return current;
  const ours = current === original || current.startsWith(`${original}-on-`);
  if (!ours) return current;
  return picked ? `${original}-on-${picked.id.replace(/-[0-9a-f]{10}$/, "")}` : original;
}
