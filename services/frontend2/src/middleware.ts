import { auth } from "@/auth";
import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

import { toBasePath, toRelativePath } from "@/lib/route-paths";

export default auth((req: NextRequest & { auth: unknown }) => {
  const relativePath = toRelativePath(req.nextUrl.pathname);
  if (relativePath === "/") {
    const url = req.nextUrl.clone();
    url.pathname = toBasePath("/persons");
    return NextResponse.redirect(url);
  }
  const res = NextResponse.next();
  res.headers.set("x-pathname", relativePath);
  return res;
});

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
