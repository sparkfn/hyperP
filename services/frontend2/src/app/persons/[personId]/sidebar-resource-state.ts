export type SidebarResourceState = "loading" | "ready" | "unavailable";

export function sidebarResourceState<T>(result: PromiseSettledResult<T>): SidebarResourceState {
  return result.status === "fulfilled" ? "ready" : "unavailable";
}
