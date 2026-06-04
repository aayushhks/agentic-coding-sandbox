/** Format a 0..1 ratio as a whole-number percentage, e.g. 0.867 -> "87%". */
export function pct(ratio: number): string {
  return `${Math.round(ratio * 100)}%`;
}

/** Render an integer with an explicit sign, e.g. 120 -> "+120", -5 -> "-5". */
export function signed(value: number): string {
  return value > 0 ? `+${value}` : `${value}`;
}

/** Thousands-separated integer, e.g. 76645 -> "76,645". */
export function num(value: number): string {
  return value.toLocaleString("en-US");
}

/** A short, human date from an ISO timestamp; falls back to the raw string. */
export function shortDate(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}
