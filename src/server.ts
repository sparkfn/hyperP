import "dotenv/config";
import Fastify from "fastify";
import cors from "@fastify/cors";
import { loadConfig } from "./config.js";
import { errorHandler } from "./common/errors.js";
import { registerAuth } from "./common/auth.js";
import { prisma } from "./common/db.js";
import { sourceSystemRoutes } from "./modules/source-system/routes.js";
import { ingestionRoutes } from "./modules/ingestion/routes.js";
import { personRoutes } from "./modules/person/routes.js";

const config = loadConfig();

const app = Fastify({
	logger: {
		level: config.LOG_LEVEL,
		transport:
			config.NODE_ENV === "development"
				? { target: "pino-pretty", options: { colorize: true } }
				: undefined,
	},
	genReqId: () => crypto.randomUUID(),
});

await app.register(cors);
await registerAuth(app);
app.setErrorHandler(errorHandler);

// Health check
app.get("/v1/health", async () => {
	return { status: "ok" };
});

// Module routes
await app.register(sourceSystemRoutes, { prefix: "/v1" });
await app.register(ingestionRoutes, { prefix: "/v1" });
await app.register(personRoutes, { prefix: "/v1" });

async function start() {
	try {
		await app.listen({ port: config.PORT, host: "0.0.0.0" });
	} catch (err) {
		app.log.error(err);
		process.exit(1);
	}
}

async function shutdown() {
	await app.close();
	await prisma.$disconnect();
}

process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);

start();

export { app };
