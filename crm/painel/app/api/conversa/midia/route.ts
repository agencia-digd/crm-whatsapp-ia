import { NextRequest, NextResponse } from "next/server";
import { cfg, resolveViewer } from "@/lib/config";

export const dynamic = "force-dynamic";

// Stream da mídia (áudio, foto, vídeo, documento) de uma mensagem, pela linha da corretora dona.
// Privacidade no copiloto (403 pra lead de outra corretora).
export async function GET(req: NextRequest) {
  const leadId = req.nextUrl.searchParams.get("lead_id");
  const msgId = req.nextUrl.searchParams.get("msg_id");
  if (!leadId || !msgId) {
    return NextResponse.json({ error: "params_obrigatorios" }, { status: 400 });
  }
  const viewer = resolveViewer(req.headers.get("x-auth-user"));
  try {
    const r = await fetch(
      `${cfg.copilotoUrl}/painel/conversa/midia?lead_id=${encodeURIComponent(leadId)}&msg_id=${encodeURIComponent(msgId)}`,
      { headers: { "X-Painel-User": viewer.user }, cache: "no-store" },
    );
    if (!r.ok) {
      return NextResponse.json({ error: "midia_indisponivel" }, { status: r.status });
    }
    const buf = await r.arrayBuffer();
    const tipo = r.headers.get("content-type") ?? "application/octet-stream";
    const base = {
      "Content-Type": tipo,
      "Cache-Control": "private, max-age=86400",
      "Accept-Ranges": "bytes",
    };
    // Safari (iPhone) pede o áudio em pedaços (Range) e, sem resposta 206,
    // não descobre a duração e o player fica em "0:00 / 0:00". O arquivo já
    // está inteiro aqui; só devolvemos o pedaço pedido.
    const faixa = parseRange(req.headers.get("range"), buf.byteLength);
    if (faixa === "invalida") {
      return new NextResponse(null, {
        status: 416,
        headers: { ...base, "Content-Range": `bytes */${buf.byteLength}` },
      });
    }
    if (faixa) {
      const [ini, fim] = faixa;
      return new NextResponse(buf.slice(ini, fim + 1), {
        status: 206,
        headers: {
          ...base,
          "Content-Range": `bytes ${ini}-${fim}/${buf.byteLength}`,
          "Content-Length": String(fim - ini + 1),
        },
      });
    }
    return new NextResponse(buf, {
      status: 200,
      headers: { ...base, "Content-Length": String(buf.byteLength) },
    });
  } catch {
    return NextResponse.json({ error: "copiloto_indisponivel" }, { status: 502 });
  }
}

// "bytes=0-1023" | "bytes=500-" | "bytes=-500" → [início, fim] inclusivo.
// Sem cabeçalho (ou formato que não entendemos) → null = arquivo inteiro.
function parseRange(h: string | null, total: number): [number, number] | "invalida" | null {
  if (!h || total <= 0) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(h.trim());
  if (!m || (m[1] === "" && m[2] === "")) return null;
  let ini: number;
  let fim: number;
  if (m[1] === "") {
    ini = Math.max(0, total - Number(m[2]));
    fim = total - 1;
  } else {
    ini = Number(m[1]);
    fim = m[2] === "" ? total - 1 : Math.min(Number(m[2]), total - 1);
  }
  if (ini >= total || ini > fim) return "invalida";
  return [ini, fim];
}
