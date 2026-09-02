export type TimelineLoadState = "loading" | "complete" | "incomplete";

export function timelineLoadState(results: PromiseSettledResult<unknown>[]): TimelineLoadState {
  if (results.every((result) => result.status === "fulfilled")) return "complete";
  return "incomplete";
}
