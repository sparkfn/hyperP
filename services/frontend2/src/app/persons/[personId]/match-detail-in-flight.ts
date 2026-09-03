export type DetailRequestToken = symbol;

export function claimDetailRequest(
  owners: Map<string, DetailRequestToken>,
  detailId: string,
): DetailRequestToken | null {
  if (owners.has(detailId)) return null;
  const token = Symbol(detailId);
  owners.set(detailId, token);
  return token;
}

export interface AbortableDetailRequest {
  token: DetailRequestToken;
  controller: AbortController;
}

export function claimAbortableDetailRequest(
  owners: Map<string, DetailRequestToken>,
  controllers: Map<string, AbortController>,
  detailId: string,
  controller: AbortController,
): AbortableDetailRequest | null {
  const token = claimDetailRequest(owners, detailId);
  if (token === null) return null;
  controllers.set(detailId, controller);
  return { token, controller };
}

export function needsDetail<T>(
  cachedDetails: Readonly<Record<string, T>>,
  owners: ReadonlyMap<string, DetailRequestToken>,
  detailId: string,
): boolean {
  return cachedDetails[detailId] === undefined && !owners.has(detailId);
}

export function ownsDetailRequest(
  owners: ReadonlyMap<string, DetailRequestToken>,
  detailId: string,
  token: DetailRequestToken,
): boolean {
  return owners.get(detailId) === token;
}

export function releaseDetailRequest(
  owners: Map<string, DetailRequestToken>,
  detailId: string,
  token: DetailRequestToken,
): void {
  if (ownsDetailRequest(owners, detailId, token)) owners.delete(detailId);
}

export function releaseAbortableDetailRequest(
  owners: Map<string, DetailRequestToken>,
  controllers: Map<string, AbortController>,
  detailId: string,
  request: AbortableDetailRequest,
): void {
  if (!ownsDetailRequest(owners, detailId, request.token)) return;
  releaseDetailRequest(owners, detailId, request.token);
  if (controllers.get(detailId) === request.controller) controllers.delete(detailId);
}

export function shouldShowDetailLoading<T>(
  detail: T | undefined,
  error: string | undefined,
  loading: boolean | undefined,
): boolean {
  return detail === undefined && (loading === true || error === undefined);
}

export function releaseDetailGeneration(
  owners: Map<string, DetailRequestToken>,
  claims: ReadonlyMap<string, DetailRequestToken>,
): void {
  for (const [detailId, token] of claims) releaseDetailRequest(owners, detailId, token);
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
