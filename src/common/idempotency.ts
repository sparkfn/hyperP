import type { FastifyReply, FastifyRequest } from "fastify";

const idempotencyStore = new Map<string, { status: number; body: unknown }>();

export async function idempotencyGuard(
	request: FastifyRequest,
	reply: FastifyReply,
) {
	const key = request.headers["idempotency-key"] as string | undefined;
	if (!key) return;

	const cached = idempotencyStore.get(key);
	if (cached) {
		return reply.status(cached.status).send(cached.body);
	}
}

export function cacheIdempotencyResponse(
	request: FastifyRequest,
	reply: FastifyReply,
	body: unknown,
) {
	const key = request.headers["idempotency-key"] as string | undefined;
	if (key) {
		idempotencyStore.set(key, { status: reply.statusCode, body });
	}
}
