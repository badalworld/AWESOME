// Forwards the dashboard's /api/* calls to the self-hosted trading engine.
// Set ENGINE_URL in the Netlify UI (Project configuration > Environment variables)
// to the engine's address, e.g. "http://203.0.113.10:8080" (a bare IP or
// "IP:port" also works; port defaults to the engine's web.port of 8080).
// An "IPAddress" variable is accepted as a fallback when ENGINE_URL is unset.
import type { Config, Context } from "https://edge.netlify.com";

function engineBase(raw: string): URL | null {
  let value = raw.trim();
  if (!value) return null;
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  try {
    const url = new URL(value);
    if (!url.port && url.protocol === "http:" && !raw.includes("://")) url.port = "8080";
    return url;
  } catch {
    return null;
  }
}

export default async (req: Request, _context: Context) => {
  const base = engineBase(Netlify.env.get("ENGINE_URL") || Netlify.env.get("IPAddress") || "");
  if (!base) {
    return Response.json(
      { detail: "Engine address not configured: set the ENGINE_URL environment variable to your server's IP (e.g. http://203.0.113.10:8080)." },
      { status: 503 },
    );
  }

  const incoming = new URL(req.url);
  const target = new URL(incoming.pathname + incoming.search, base);

  const headers = new Headers(req.headers);
  headers.delete("host");
  headers.set("x-forwarded-host", incoming.host);
  headers.set("x-forwarded-proto", incoming.protocol.replace(":", ""));

  const hasBody = !["GET", "HEAD"].includes(req.method);
  try {
    const res = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      redirect: "manual",
    });
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers: res.headers });
  } catch {
    return Response.json(
      { detail: `Could not reach the engine at ${base.host}. Check that it is running and the port is open.` },
      { status: 502 },
    );
  }
};

export const config: Config = {
  path: "/api/*",
};
