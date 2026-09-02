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

export function releaseDetailGeneration(
  owners: Map<string, DetailRequestToken>,
  claims: ReadonlyMap<string, DetailRequestToken>,
): void {
  for (const [detailId, token] of claims) releaseDetailRequest(owners, detailId, token);
}

export function isAbortError(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}
