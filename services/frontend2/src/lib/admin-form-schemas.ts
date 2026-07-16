import { z } from "zod";

import { OAUTH_CLIENT_SCOPES } from "./api-types-ops";

const oauthScopeSchema = z.string().refine(
  (value) => OAUTH_CLIENT_SCOPES.includes(value),
  "Unknown OAuth scope.",
);

export const oauthClientFormSchema = z.object({
  name: z.string().trim().min(1, "Name is required."),
  scopes: z.array(oauthScopeSchema).min(1, "Select at least one scope."),
  ttlMinutes: z.string().refine((value) => {
    const parsed = Number(value);
    return Number.isInteger(parsed) && parsed >= 5 && parsed <= 1440;
  }, "Token lifetime must be between 5 and 1440 minutes."),
});

export type OAuthClientFormValues = z.infer<typeof oauthClientFormSchema>;
