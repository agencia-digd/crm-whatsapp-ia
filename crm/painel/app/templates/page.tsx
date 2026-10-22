// Templates de WhatsApp por linha: o que a Meta diz AGORA (aprovado, em análise,
// reprovado, pausado), a categoria real e o motivo quando reprova. Só admin.
//
// Os dados vêm do snapshot que o timer `acme-templates-sync` grava a cada hora
// (projetos/acme/templates-sync). Esta página não fala com a Graph nem com o
// banco: só lê o arquivo, então não leva token nem senha pro processo do Next.
import { readFile } from "node:fs/promises";
import { redirect } from "next/navigation";
import { headers } from "next/headers";
import { resolveViewer } from "@/lib/config";
import "./templates.css";

export const dynamic = "force-dynamic";

const ARQUIVO = process.env.TEMPLATES_SNAPSHOT || "/var/lib/acme-painel/templates-meta.json";

type Tpl = { nome: string; status: string; categoria: string; idioma: string | null; motivo: string | null };
type Waba = { waba: number; nome: string; erro: string | null; templates: Tpl[] };
type Snap = { lido_em: string; wabas: Waba[] };

const STATUS: Record<string, { rotulo: string; cls: string }> = {
  aprovado: { rotulo: "Aprovado", cls: "ok" },
  analise: { rotulo: "Em análise", cls: "an" },
  reprovado: { rotulo: "Reprovado", cls: "rp" },
  pausado: { rotulo: "Pausado", cls: "ps" },
  desabilitado: { rotulo: "Desabilitado", cls: "rp" },
  apagado: { rotulo: "Apagado", cls: "ps" },
};
const ORDEM = ["aprovado", "analise", "reprovado", "pausado", "desabilitado", "apagado"];

async function ler(): Promise<Snap | null> {
  try {
    return JSON.parse(await readFile(ARQUIVO, "utf8")) as Snap;
  } catch {
    return null;
  }
}

function contar(ts: Tpl[]) {
  const c: Record<string, number> = {};
  for (const t of ts) c[t.status] = (c[t.status] ?? 0) + 1;
  return c;
}

function quando(iso: string) {
  const min = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  const rel = min < 60 ? `há ${min} min` : `há ${Math.floor(min / 60)} h`;
  return { rel, velho: min > 180 };
}

function Selos({ c }: { c: Record<string, number> }) {
  return (
    <span className="tp-selos">
      {ORDEM.filter((s) => c[s]).map((s) => (
        <span key={s} className={`tp-selo tp-${STATUS[s].cls}`}>{c[s]} {STATUS[s].rotulo.toLowerCase()}</span>
      ))}
    </span>
  );
}

export default async function Page() {
  const viewer = resolveViewer(headers().get("x-auth-user"));
  if (!viewer.isAdmin) redirect("/crm");

  const snap = await ler();
  if (!snap) {
    return (
      <main className="tp">
        <h1 className="tp-h1">Templates</h1>
        <p className="tp-vazio">Ainda não há leitura da Meta. O timer <code>acme-templates-sync</code> grava o arquivo a cada hora.</p>
      </main>
    );
  }

  const todos = snap.wabas.flatMap((w) => w.templates);
  const total = contar(todos);
  const marketing = todos.filter((t) => t.categoria === "MARKETING").length;
  const idade = quando(snap.lido_em);

  return (
    <main className="tp">
      <header className="tp-head">
        <h1 className="tp-h1">Templates</h1>
        <p className={`tp-sub${idade.velho ? " tp-velho" : ""}`}>
          Status real na Meta, lido {idade.rel}{idade.velho ? " (leitura atrasada: confira o timer)" : ""}.
        </p>
      </header>

      <section className="tp-topo" aria-label="Totais">
        <div className="tp-card"><b>{todos.length}</b><span>templates</span></div>
        {ORDEM.filter((s) => total[s]).map((s) => (
          <div key={s} className={`tp-card tp-${STATUS[s].cls}`}><b>{total[s]}</b><span>{STATUS[s].rotulo.toLowerCase()}</span></div>
        ))}
        <div className="tp-card"><b>{marketing}</b><span>categoria marketing</span></div>
      </section>

      {snap.wabas.map((w) => {
        const c = contar(w.templates);
        const lista = [...w.templates].sort(
          (a, b) => ORDEM.indexOf(a.status) - ORDEM.indexOf(b.status) || a.nome.localeCompare(b.nome),
        );
        return (
          <details key={w.waba} className="tp-waba" open={!w.erro && w.templates.length <= 20}>
            <summary>
              <span className="tp-wn">{w.nome}</span>
              {w.erro ? <span className="tp-selo tp-rp">sem leitura</span> : <Selos c={c} />}
              {!w.erro && <span className="tp-n">{w.templates.length}</span>}
            </summary>
            {w.erro ? (
              <p className="tp-erro">A Meta não respondeu para esta linha: {w.erro}</p>
            ) : (
              <div className="tp-tabela" role="table">
                {lista.map((t) => (
                  <div key={t.nome} className="tp-linha" role="row">
                    <span className="tp-nome" role="cell">{t.nome}</span>
                    <span className={`tp-cat${t.categoria === "MARKETING" ? " tp-mkt" : ""}`} role="cell">{t.categoria.toLowerCase()}</span>
                    <span className={`tp-selo tp-${(STATUS[t.status] ?? STATUS.pausado).cls}`} role="cell">
                      {(STATUS[t.status] ?? { rotulo: t.status }).rotulo}
                    </span>
                    {t.motivo && <span className="tp-motivo" role="cell">{t.motivo}</span>}
                  </div>
                ))}
              </div>
            )}
          </details>
        );
      })}
    </main>
  );
}
