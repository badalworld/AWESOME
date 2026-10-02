// Forwards the dashboard's /api/* calls to the self-hosted trading engine when the
// static dashboard is served from Vercel (vercel.json rewrites /api/* here).
// Set ENGINE_URL in the Vercel project's Environment Variables to the engine's
// address, e.g. "http://203.0.113.10:8080" (a bare IP or "IP:port" also works;
// the port defaults to the engine's web.port of 8080). "IPAddress" is accepted
// as a fallback.
export const config = { runtime: "edge" };

const PATH_PARAM = "__engine_path";

function engineBase(raw) {
  let value = (raw || "").trim();
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

export default async function handler(req) {
  const base = engineBase(process.env.ENGINE_URL || process.env.IPAddress || "");
  if (!base) {
    return Response.json(
      { detail: "Engine address not configured: set the ENGINE_URL environment variable to your server's IP (e.g. http://203.0.113.10:8080)." },
      { status: 503 },
    );
  }

  const incoming = new URL(req.url);
  const enginePath = (incoming.searchParams.get(PATH_PARAM) || "").replace(/^\/+/, "");
  incoming.searchParams.delete(PATH_PARAM);
  const target = new URL(`/api/${enginePath}${incoming.search}`, base);

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
}
