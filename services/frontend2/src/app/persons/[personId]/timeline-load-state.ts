export type TimelineLoadState = "loading" | "complete" | "incomplete";

export function shouldShowEmptyTimeline(
  eventCount: number,
  loading: boolean,
  loadState: TimelineLoadState,
): boolean {
  return eventCount === 0 && !loading && loadState === "complete";
}

export function timelineLoadState(results: PromiseSettledResult<unknown>[]): TimelineLoadState {
  if (results.every((result) => result.status === "fulfilled")) return "complete";
  return "incomplete";
}

export function retainOnFailure<T>(
  previous: T[],
  result: PromiseSettledResult<{ data: T[] }>,
): T[] {
  return result.status === "fulfilled" ? result.value.data : previous;
}
