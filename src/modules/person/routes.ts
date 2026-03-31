import type { FastifyInstance } from "fastify";
import { z } from "zod";
import { requireRole } from "../../common/rbac.js";
import { dataResponse } from "../../common/schemas.js";
import { getActivePerson, getPersonAudit, searchPersonsByIdentifier } from "./service.js";
import type { IdentifierType } from "../../generated/prisma/client.js";

const searchSchema = z.object({
	identifier_type: z.enum([
		"phone",
		"email",
		"government_id_hash",
		"external_customer_id",
		"membership_id",
		"crm_contact_id",
		"loyalty_id",
		"custom",
	]),
	value: z.string().min(1),
});

export async function personRoutes(app: FastifyInstance) {
	// Get person by ID (follows merge chain)
	app.get<{ Params: { id: string } }>(
		"/persons/:id",
		{
			preHandler: [
				requireRole("admin", "reviewer", "support_agent", "read_service", "service"),
			],
		},
		async (request) => {
			const person = await getActivePerson(request.params.id);
			return dataResponse(person, request.id);
		},
	);

	// Get person's source records
	app.get<{ Params: { id: string } }>(
		"/persons/:id/source-records",
		{
			preHandler: [
				requireRole("admin", "reviewer", "support_agent", "read_service", "service"),
			],
		},
		async (request) => {
			const person = await getActivePerson(request.params.id);
			return dataResponse(person.sourceRecords, request.id);
		},
	);

	// Get person audit trail
	app.get<{ Params: { id: string } }>(
		"/persons/:id/audit",
		{
			preHandler: [requireRole("admin", "reviewer", "service")],
		},
		async (request) => {
			const audit = await getPersonAudit(request.params.id);
			return dataResponse(audit, request.id);
		},
	);

	// Search persons by identifier
	app.get(
		"/persons/search",
		{
			preHandler: [
				requireRole("admin", "reviewer", "support_agent", "read_service", "service"),
			],
		},
		async (request) => {
			const query = searchSchema.parse(request.query);
			const persons = await searchPersonsByIdentifier(
				query.identifier_type as IdentifierType,
				query.value,
			);
			return dataResponse(persons, request.id);
		},
	);
}
