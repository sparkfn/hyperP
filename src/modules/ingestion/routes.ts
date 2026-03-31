import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { requireRole } from "../../common/rbac.js";
import { dataResponse } from "../../common/schemas.js";
import { idempotencyGuard, cacheIdempotencyResponse } from "../../common/idempotency.js";
import { ingestBatch, ingestRecord } from "./service.js";

const identifierSchema = z.object({
	type: z.string().min(1),
	value: z.string().min(1),
	is_verified: z.boolean().optional(),
});

const ingestPayloadSchema = z.object({
	source_record_id: z.string().min(1),
	source_record_version: z.string().optional(),
	observed_at: z.string().optional(),
	identifiers: z.array(identifierSchema).min(1),
	attributes: z.record(z.string(), z.string()),
	raw_payload: z.record(z.string(), z.unknown()),
});

const batchIngestSchema = z.object({
	records: z.array(ingestPayloadSchema).min(1).max(500),
});

export async function ingestionRoutes(app: FastifyInstance) {
	// Ingest a single record
	app.post<{ Params: { source: string } }>(
		"/ingest/:source",
		{
			preHandler: [
				requireRole("ingest_service", "admin"),
				idempotencyGuard,
			],
		},
		async (request, reply) => {
			const body = ingestPayloadSchema.parse(request.body);
			const result = await ingestRecord(request.params.source, body);

			const status = result.status === "duplicate" ? 200 : 201;
			const response = dataResponse(result, request.id);
			cacheIdempotencyResponse(request, reply, response);
			return reply.status(status).send(response);
		},
	);

	// Batch ingest
	app.post<{ Params: { source: string } }>(
		"/ingest/:source/batch",
		{
			preHandler: [requireRole("ingest_service", "admin")],
		},
		async (request, reply) => {
			const body = batchIngestSchema.parse(request.body);
			const result = await ingestBatch(request.params.source, body.records);
			return reply.status(201).send(dataResponse(result, request.id));
		},
	);
}
