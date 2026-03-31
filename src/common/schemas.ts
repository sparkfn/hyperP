import { z } from "zod";

// Standard pagination query params
export const paginationSchema = z.object({
	limit: z.coerce.number().int().min(1).max(100).default(20),
	offset: z.coerce.number().int().min(0).default(0),
});

export type PaginationParams = z.infer<typeof paginationSchema>;

// Standard response envelope
export function dataResponse<T>(data: T, requestId: string) {
	return {
		data,
		meta: { request_id: requestId },
	};
}

// Paginated response envelope
export function paginatedResponse<T>(
	data: T[],
	total: number,
	params: PaginationParams,
	requestId: string,
) {
	return {
		data,
		meta: { request_id: requestId },
		pagination: {
			total,
			limit: params.limit,
			offset: params.offset,
			has_more: params.offset + params.limit < total,
		},
	};
}
