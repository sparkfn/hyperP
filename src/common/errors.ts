import type { FastifyReply, FastifyRequest } from "fastify";

export class AppError extends Error {
	constructor(
		public readonly statusCode: number,
		message: string,
		public readonly code?: string,
	) {
		super(message);
		this.name = "AppError";
	}
}

export class NotFoundError extends AppError {
	constructor(resource: string, id: string) {
		super(404, `${resource} not found: ${id}`, "NOT_FOUND");
	}
}

export class ConflictError extends AppError {
	constructor(message: string) {
		super(409, message, "CONFLICT");
	}
}

export class ForbiddenError extends AppError {
	constructor(message = "Insufficient permissions") {
		super(403, message, "FORBIDDEN");
	}
}

export function errorHandler(
	error: Error,
	_request: FastifyRequest,
	reply: FastifyReply,
) {
	if (error instanceof AppError) {
		return reply.status(error.statusCode).send({
			error: {
				code: error.code,
				message: error.message,
			},
			meta: { request_id: _request.id },
		});
	}

	// Fastify validation errors
	if ("validation" in error) {
		return reply.status(400).send({
			error: {
				code: "VALIDATION_ERROR",
				message: error.message,
			},
			meta: { request_id: _request.id },
		});
	}

	_request.log.error(error);
	return reply.status(500).send({
		error: {
			code: "INTERNAL_ERROR",
			message: "Internal server error",
		},
		meta: { request_id: _request.id },
	});
}
