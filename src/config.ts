import { z } from "zod";

const envSchema = z.object({
	DATABASE_URL: z.string(),
	REDIS_URL: z.string().default("redis://localhost:6379"),
	JWT_SECRET: z.string(),
	PORT: z.coerce.number().default(3000),
	LOG_LEVEL: z.enum(["fatal", "error", "warn", "info", "debug", "trace"]).default("info"),
	NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
});

export type Env = z.infer<typeof envSchema>;

export function loadConfig(): Env {
	const result = envSchema.safeParse(process.env);
	if (!result.success) {
		console.error("Invalid environment variables:", result.error.format());
		process.exit(1);
	}
	return result.data;
}
