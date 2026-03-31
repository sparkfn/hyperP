import type { FastifyInstance, FastifyRequest } from "fastify";
import fastifyJwt from "@fastify/jwt";
import { loadConfig } from "../config.js";

export type Role =
	| "ingest_service"
	| "read_service"
	| "service"
	| "support_agent"
	| "reviewer"
	| "admin";

declare module "@fastify/jwt" {
	interface FastifyJWT {
		payload: { sub: string; role: Role };
		user: { sub: string; role: Role };
	}
}

export async function registerAuth(app: FastifyInstance) {
	const config = loadConfig();

	await app.register(fastifyJwt, {
		secret: config.JWT_SECRET,
	});

	app.decorate("authenticate", async (request: FastifyRequest) => {
		await request.jwtVerify();
	});
}
