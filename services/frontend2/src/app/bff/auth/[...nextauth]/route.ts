import { NextRequest } from "next/server";

import { handlers } from "@/auth";
import { BASE_PATH } from "@/lib/route-paths";

type RouteHandler = (req: NextRequest) => Promise<Response>;

// Only the fields we copy. `duplex` is required by the Fetch spec when building a
// request with a body stream but is not yet in the TS RequestInit type; declaring
// our own avoids the global RequestInit's nullable `signal` clashing with
// NextRequest's constructor type.
interface CloneInit {
  method: string;
  headers: Headers;
  body?: ReadableStream<Uint8Array> | null;
  duplex?: "half";
}

// Next.js strips the framework basePath (/app/v2) before route handlers run, but
// next-auth's basePath includes it (so OAuth signin/callback URLs carry the prefix
// and the two app versions don't collide at /bff/auth). Re-add the prefix to the
// request URL here so @auth/core can parse the action from the otherwise-stripped
// path. Method, headers, and body are copied explicitly so POST sign-in requests
// survive the reconstruction. See the basePath note in src/auth.ts.
function withBasePath(handler: RouteHandler): RouteHandler {
  return (req: NextRequest): Promise<Response> => {
    const url = new URL(req.url);
    if (url.pathname.startsWith(`${BASE_PATH}/`)) {
      return handler(req);
    }
    url.pathname = `${BASE_PATH}${url.pathname}`;
    const init: CloneInit = { method: req.method, headers: req.headers };
    if (req.method !== "GET" && req.method !== "HEAD") {
      init.body = req.body;
      init.duplex = "half";
    }
    return handler(new NextRequest(url, init));
  };
}

export const GET = withBasePath(handlers.GET);
export const POST = withBasePath(handlers.POST);
