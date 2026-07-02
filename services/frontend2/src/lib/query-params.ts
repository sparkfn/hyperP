/**
 * Pure query-parameter helpers shared by the BFF proxy ({@link "./proxy"}) and
 * the server API client ({@link "./api-server"}).
 *
 * Kept free of `server-only` / `next-auth` imports so they are unit-testable in
 * isolation (see `__tests__/query-params.test.ts`).
 *
 * Repeated query keys (e.g. `?entity_key=a&entity_key=b`) MUST round-trip as a
 * list, never be collapsed to the last value — otherwise multi-value filters
 * such as the persons-list entity/source "AND" mode silently lose all but the
 * last selected key.
 */

export type QueryValue = string | number | boolean | null | undefined;

/** A query map whose values may be a scalar or a list of scalars. */
export type QueryParams = Record<string, QueryValue | QueryValue[]>;

/**
 * Convert a `URLSearchParams` to a plain object, preserving repeated keys as
 * arrays.
 *
 * This replaces `Object.fromEntries(searchParams.entries())`, which silently
 * keeps only the LAST value for a repeated key — collapsing
 * `entity_key=fundbox&entity_key=onediver` to `{ entity_key: "onediver" }` and
 * so starving multi-value filters down to a single key.
 */
export function searchParamsToQuery(searchParams: URLSearchParams): QueryParams {
  const out: QueryParams = {};
  for (const [key, value] of searchParams.entries()) {
    const existing = out[key];
    if (existing === undefined) {
      out[key] = value;
    } else if (Array.isArray(existing)) {
      existing.push(value);
    } else {
      out[key] = [existing, value];
    }
  }
  return out;
}

/**
 * Append query values onto a `URL`, serializing arrays as repeated params
 * (`key=a&key=b`) and skipping `null`/`undefined`. Scalars use `set` (single
 * value); array elements use `append` (repeated) so the upstream API receives
 * every value.
 */
export function appendQueryParams(url: URL, query: QueryParams | undefined): void {
  if (!query) return;
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined) continue;
    if (Array.isArray(value)) {
      if (value.length === 0) continue;
      for (const item of value) {
        if (item === null || item === undefined) continue;
        url.searchParams.append(key, String(item));
      }
    } else {
      url.searchParams.set(key, String(value));
    }
  }
}
