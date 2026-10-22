import { NextRequest } from "next/server";

/** Origem pública por trás do nginx (nunca localhost:3009). */
export function publicOrigin(req: NextRequest): string {
  const host =
    req.headers.get("x-forwarded-host") ||
    req.headers.get("host") ||
    "";
  const proto = req.headers.get("x-forwarded-proto") || "https";

  // next start atrás de proxy sem trustHost: nextUrl.origin vira
  // http://localhost:3009 e o redirect quebra no browser da Dona.
  if (
    !host ||
    host.includes("localhost") ||
    host.startsWith("127.") ||
    host.startsWith("0.0.0.0")
  ) {
    return process.env.PUBLIC_ORIGIN || "https://app.example.com";
  }
  return `${proto.split(",")[0].trim()}://${host.split(",")[0].trim()}`;
}
