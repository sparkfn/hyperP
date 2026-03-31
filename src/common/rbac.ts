import type { FastifyReply, FastifyRequest } from "fastify";
import type { Role } from "./auth.js";
import { ForbiddenError } from "./errors.js";

export function requireRole(...allowedRoles: Role[]) {
	return async (request: FastifyRequest, _reply: FastifyReply) => {
		await request.jwtVerify();
		const { role } = request.user;
		if (!allowedRoles.includes(role)) {
			throw new ForbiddenError(
				`Role '${role}' is not authorized. Required: ${allowedRoles.join(", ")}`,
			);
		}
	};
}
