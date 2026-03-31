import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { requireRole } from "../../common/rbac.js";
import { dataResponse } from "../../common/schemas.js";
import {
	createSourceSystem,
	getSourceSystem,
	listFieldTrusts,
	listSourceSystems,
	setFieldTrust,
	updateSourceSystem,
} from "./service.js";

const createSchema = z.object({
	source_key: z.string().min(1),
	display_name: z.string().min(1),
	system_type: z.string().min(1),
});

const updateSchema = z.object({
	display_name: z.string().min(1).optional(),
	system_type: z.string().min(1).optional(),
	is_active: z.boolean().optional(),
});

const fieldTrustSchema = z.object({
	field_name: z.string().min(1),
	trust_tier: z.enum(["tier_1", "tier_2", "tier_3", "tier_4"]),
	notes: z.string().optional(),
});

export async function sourceSystemRoutes(app: FastifyInstance) {
	// List all source systems
	app.get(
		"/source-systems",
		{ preHandler: [requireRole("admin", "service", "read_service")] },
		async (request) => {
			const systems = await listSourceSystems();
			return dataResponse(systems, request.id);
		},
	);

	// Get single source system
	app.get<{ Params: { id: string } }>(
		"/source-systems/:id",
		{ preHandler: [requireRole("admin", "service", "read_service")] },
		async (request) => {
			const system = await getSourceSystem(request.params.id);
			return dataResponse(system, request.id);
		},
	);

	// Create source system
	app.post(
		"/source-systems",
		{ preHandler: [requireRole("admin")] },
		async (request, reply) => {
			const body = createSchema.parse(request.body);
			const system = await createSourceSystem({
				sourceKey: body.source_key,
				displayName: body.display_name,
				systemType: body.system_type,
			});
			return reply.status(201).send(dataResponse(system, request.id));
		},
	);

	// Update source system
	app.patch<{ Params: { id: string } }>(
		"/source-systems/:id",
		{ preHandler: [requireRole("admin")] },
		async (request) => {
			const body = updateSchema.parse(request.body);
			const system = await updateSourceSystem(request.params.id, {
				displayName: body.display_name,
				systemType: body.system_type,
				isActive: body.is_active,
			});
			return dataResponse(system, request.id);
		},
	);

	// List field trusts for a source system
	app.get<{ Params: { id: string } }>(
		"/source-systems/:id/field-trusts",
		{ preHandler: [requireRole("admin", "service", "read_service")] },
		async (request) => {
			const trusts = await listFieldTrusts(request.params.id);
			return dataResponse(trusts, request.id);
		},
	);

	// Set field trust for a source system
	app.put<{ Params: { id: string } }>(
		"/source-systems/:id/field-trusts",
		{ preHandler: [requireRole("admin")] },
		async (request) => {
			const body = fieldTrustSchema.parse(request.body);
			const trust = await setFieldTrust(request.params.id, {
				fieldName: body.field_name,
				trustTier: body.trust_tier,
				notes: body.notes,
			});
			return dataResponse(trust, request.id);
		},
	);
}
