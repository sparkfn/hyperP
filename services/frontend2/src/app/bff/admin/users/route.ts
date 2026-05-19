import type { NextResponse } from "next/server";

import type { UserResponse } from "@/lib/api-types-ops";
import { proxyToApi } from "@/lib/proxy";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  return proxyToApi<UserResponse[]>("/users");
}
