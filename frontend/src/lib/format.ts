export function formatTime(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  // Round to ms before splitting so e.g. 119.9996 -> "2:00.000", not "1:60.000".
  const totalMs = Math.round(seconds * 1000);
  const m = Math.floor(totalMs / 60000);
  const s = ((totalMs - m * 60000) / 1000).toFixed(3);
  return `${m}:${s.padStart(6, "0")}`;
}
