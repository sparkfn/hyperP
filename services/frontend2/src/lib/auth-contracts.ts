import { z } from "zod";

const googleRefreshResponseSchema = z.object({
  id_token: z.string().min(1),
  access_token: z.string().min(1),
  expires_in: z.number().int().positive(),
});

const meResponseSchema = z.object({
  data: z.object({
    email: z.email(),
    google_sub: z.string().min(1),
    role: z.enum(["admin", "employee", "first_time"]),
    entity_key: z.string().nullable(),
    display_name: z.string().nullable(),
  }),
});

export type GoogleRefreshResponse = z.infer<typeof googleRefreshResponseSchema>;
export type MeResponse = z.infer<typeof meResponseSchema>;

export function parseGoogleRefreshResponse(value: unknown): GoogleRefreshResponse | null {
  const result = googleRefreshResponseSchema.safeParse(value);
  return result.success ? result.data : null;
}

export function parseMeResponse(value: unknown): MeResponse | null {
  const result = meResponseSchema.safeParse(value);
  return result.success ? result.data : null;
}
