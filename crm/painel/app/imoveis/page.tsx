"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import GaleriaFotos from "./GaleriaFotos";

// 🏠 Captação — a tela que faltava pro banco de imóveis (Camada 1).
//
// O backend está pronto desde 12/09 (imoveis_captados.py, 9 endpoints, 18
// testes) e tinha ZERO registros, porque não havia onde cadastrar. A frase que
// originou o módulo é da Dona, em 12/09: "as minhas corretoras estão reclamando
// que todo mundo manda 'ah, eu tenho esse imóvel pra vender', e elas não têm
// onde cadastrar".
//
// A regra que manda no desenho inteiro: **nada pode fazer o cadastro falhar.**
// Quatro campos obrigatórios (tipo, bairro, telefone do dono, preço), o resto
// entra depois. A corretora preenche do celular, em 60 segundos, no meio da
// conversa com o dono — um 422 ali custa o imóvel.
//
// Por isso:
//   • o formulário abre já aberto, sem clique extra;
//   • o resto dos campos vive atrás de "mais detalhes", fechado por padrão;
//   • duplicata AVISA depois de salvar, nunca barra antes;
//   • foto entra DEPOIS do registro existir, então salvar nunca espera upload.

type Imovel = {
  id: number;
  corretora_id: number;
  corretora_label: string | null;
  captado_por: string;
  captado_em: string | null;
  tipo: string;
  quartos: number | null;
  vagas: number | null;
  suites: number | null;
  area_m2: number | null;
  bairro: string;
  cidade: string;
  endereco: string | null;
  preco_pedido: number | null;
  aceita_financiamento: boolean | null;
  aceita_permuta: boolean | null;
  dono_nome: string | null;
  dono_telefone: string;
  dono_relacao: string | null;
  situacao: string;
  exclusividade: boolean;
  autorizacao_assinada: boolean;
  fonte: string | null;
  observacoes: string | null;
  completude: number;
  faltando: string[];
  fotos_total: number;
  capa_url: string | null;
};

type Corretora = { id: number; label: string };

type Aviso = { id?: number; texto?: string; [k: string]: unknown };

const TIPOS = ["apartamento", "casa", "terreno", "comercial", "chacara", "outro"];
const RELACOES = ["proprietario", "procurador", "familiar", "inventario", "outro"];
const FONTES = ["whatsapp", "indicacao", "placa", "porta", "pessoa", "outro"];

const ROTULO_SITUACAO: Record<string, string> = {
  disponivel: "Disponível",
  vendido: "Vendido",
  desistiu: "Desistiu",
  exclusividade_outra: "Com outra imobiliária",
  nao_valida: "Não validado",
};

function moeda(v: number | null): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("pt-BR", {
    style: "currency",
    currency: "BRL",
    maximumFractionDigits: 0,
  });
}

/** Aceita o que a corretora digitar: "450", "450mil", "450.000", "R$ 450 mil". */
function parsePreco(bruto: string): number | null {
  const t = bruto.trim().toLowerCase();
  if (!t) return null;
  const temMil = /\bmil\b|\bk\b/.test(t);
  const num = t.replace(/[^0-9,.]/g, "").replace(/\./g, "").replace(",", ".");
  if (!num) return null;
  let v = Number(num);
  if (!Number.isFinite(v)) return null;
  if (temMil) v *= 1000;
  // "450" digitado sozinho quase sempre é 450 mil, não R$ 450. O limite é
  // generoso de propósito: terreno de R$ 80 mil existe, imóvel de R$ 900 não.
  else if (v > 0 && v < 10000) v *= 1000;
  return Math.round(v);
}

const VAZIO = {
  tipo: "casa",
  bairro: "",
  dono_telefone: "",
  preco: "",
  cidade: "Cidade",
  quartos: "",
  vagas: "",
  suites: "",
  area_m2: "",
  endereco: "",
  dono_nome: "",
  dono_relacao: "proprietario",
  fonte: "whatsapp",
  aceita_financiamento: false,
  aceita_permuta: false,
  exclusividade: false,
  autorizacao_assinada: false,
  observacoes: "",
  corretora_id: "",
};

export default function ImoveisPage() {
  const [itens, setItens] = useState<Imovel[]>([]);
  const [isAdmin, setIsAdmin] = useState(false);
  const [corretoras, setCorretoras] = useState<Corretora[]>([]);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);

  const [form, setForm] = useState({ ...VAZIO });
  const [detalhes, setDetalhes] = useState(false);
  const [salvando, setSalvando] = useState(false);
  const [recem, setRecem] = useState<{ id: number; avisos: Aviso[] } | null>(null);

  const [busca, setBusca] = useState("");
  const [situacao, setSituacao] = useState("");
  const fotoInput = useRef<HTMLInputElement>(null);
  const [subindoFoto, setSubindoFoto] = useState(false);
  const [editando, setEditando] = useState<Imovel | null>(null);

  const carregar = useCallback(async () => {
    try {
      const qs = new URLSearchParams();
      if (busca.trim()) qs.set("q", busca.trim());
      if (situacao) qs.set("situacao", situacao);
      const r = await fetch(`/api/imoveis?${qs}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error ?? `http_${r.status}`);
      setItens(d.imoveis ?? []);
      setIsAdmin(Boolean(d.is_admin));
      setCorretoras(d.corretoras ?? []);
      setErro(null);
    } catch (e) {
      setErro(e instanceof Error ? e.message : "erro_desconhecido");
    } finally {
      setCarregando(false);
    }
  }, [busca, situacao]);

  useEffect(() => {
    const t = setTimeout(carregar, busca ? 300 : 0);
    return () => clearTimeout(t);
  }, [carregar, busca]);

  const precoPreview = useMemo(() => parsePreco(form.preco), [form.preco]);

  // Só o que o backend exige. Tudo o mais é opcional de propósito.
  const podeSalvar =
    form.tipo &&
    form.bairro.trim() &&
    form.dono_telefone.trim() &&
    (!isAdmin || form.corretora_id);

  async function salvar(e: React.FormEvent) {
    e.preventDefault();
    if (!podeSalvar || salvando) return;
    setSalvando(true);
    setRecem(null);
    try {
      const body: Record<string, unknown> = {
        tipo: form.tipo,
        bairro: form.bairro.trim(),
        dono_telefone: form.dono_telefone.trim(),
        preco_pedido: precoPreview,
        cidade: form.cidade.trim() || "Cidade",
        endereco: form.endereco.trim() || null,
        dono_nome: form.dono_nome.trim() || null,
        dono_relacao: form.dono_relacao || null,
        fonte: form.fonte || null,
        aceita_financiamento: form.aceita_financiamento,
        aceita_permuta: form.aceita_permuta,
        exclusividade: form.exclusividade,
        autorizacao_assinada: form.autorizacao_assinada,
        observacoes: form.observacoes.trim() || null,
      };
      for (const k of ["quartos", "vagas", "suites"] as const) {
        const v = form[k].trim();
        if (v) body[k] = Number(v);
      }
      if (form.area_m2.trim()) body.area_m2 = Number(form.area_m2.replace(",", "."));
      if (isAdmin && form.corretora_id) body.corretora_id = Number(form.corretora_id);

      const r = await fetch("/api/imoveis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail ?? d.error ?? `http_${r.status}`);

      setRecem({ id: d.id, avisos: d.avisos ?? [] });
      setForm({ ...VAZIO, corretora_id: form.corretora_id });
      setDetalhes(false);
      await carregar();
    } catch (e) {
      setErro(e instanceof Error ? e.message : "nao_consegui_salvar");
    } finally {
      setSalvando(false);
    }
  }

  async function enviarFotos(files: FileList | null) {
    if (!files?.length || !recem) return;
    setSubindoFoto(true);
    try {
      for (const f of Array.from(files)) {
        const fd = new FormData();
        fd.append("arquivo", f);
        await fetch(`/api/imoveis/${recem.id}/fotos`, { method: "POST", body: fd });
      }
      await carregar();
    } catch {
      setErro("upload_falhou");
    } finally {
      setSubindoFoto(false);
      if (fotoInput.current) fotoInput.current.value = "";
    }
  }

  const set = (k: keyof typeof VAZIO) => (v: string | boolean) =>
    setForm((f) => ({ ...f, [k]: v }));

  return (
    <>
      <header className="page-head">
        <h1>🏠 Captação</h1>
        <span className="page-meta">
          {itens.length} {itens.length === 1 ? "imóvel" : "imóveis"} na carteira
        </span>
      </header>

      {erro && (
        <div className="cap-erro" role="alert">
          {erro === "copiloto_indisponivel"
            ? "O copiloto não respondeu. Tenta de novo em alguns segundos."
            : `Deu problema: ${erro}`}
        </div>
      )}

      <form className="cap-form" onSubmit={salvar}>
        <p className="cap-form-titulo">Novo imóvel</p>
        <p className="cap-form-sub">
          Só precisa de tipo, bairro e telefone do dono. O resto você completa depois —
          salvar nunca trava.
        </p>

        <div className="cap-grid">
          <label className="cap-campo">
            <span>Tipo</span>
            <select value={form.tipo} onChange={(e) => set("tipo")(e.target.value)}>
              {TIPOS.map((t) => (
                <option key={t} value={t}>
                  {t[0].toUpperCase() + t.slice(1)}
                </option>
              ))}
            </select>
          </label>

          <label className="cap-campo">
            <span>Bairro *</span>
            <input
              value={form.bairro}
              onChange={(e) => set("bairro")(e.target.value)}
              placeholder="Bairro A"
              autoComplete="off"
            />
          </label>

          <label className="cap-campo">
            <span>Telefone do dono *</span>
            <input
              value={form.dono_telefone}
              onChange={(e) => set("dono_telefone")(e.target.value)}
              placeholder="+5500000000000"
              inputMode="tel"
              autoComplete="off"
            />
          </label>

          <label className="cap-campo">
            <span>Quanto ele pede</span>
            <input
              value={form.preco}
              onChange={(e) => set("preco")(e.target.value)}
              placeholder="450 mil"
              inputMode="decimal"
              autoComplete="off"
            />
            {precoPreview !== null && (
              <em className="cap-preview">{moeda(precoPreview)}</em>
            )}
          </label>

          {isAdmin && (
            <label className="cap-campo">
              <span>Captado por *</span>
              <select
                value={form.corretora_id}
                onChange={(e) => set("corretora_id")(e.target.value)}
              >
                <option value="">escolha…</option>
                {corretoras.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <button
          type="button"
          className="cap-toggle"
          onClick={() => setDetalhes((v) => !v)}
          aria-expanded={detalhes}
        >
          {detalhes ? "− menos detalhes" : "+ mais detalhes"}
        </button>

        {detalhes && (
          <div className="cap-grid cap-extra">
            <label className="cap-campo">
              <span>Nome do dono</span>
              <input value={form.dono_nome} onChange={(e) => set("dono_nome")(e.target.value)} />
            </label>
            <label className="cap-campo">
              <span>Ele é</span>
              <select
                value={form.dono_relacao}
                onChange={(e) => set("dono_relacao")(e.target.value)}
              >
                {RELACOES.map((r) => (
                  <option key={r} value={r}>
                    {r[0].toUpperCase() + r.slice(1)}
                  </option>
                ))}
              </select>
            </label>
            <label className="cap-campo">
              <span>Endereço</span>
              <input value={form.endereco} onChange={(e) => set("endereco")(e.target.value)} />
            </label>
            <label className="cap-campo">
              <span>Cidade</span>
              <input value={form.cidade} onChange={(e) => set("cidade")(e.target.value)} />
            </label>
            <label className="cap-campo cap-mini">
              <span>Quartos</span>
              <input
                value={form.quartos}
                onChange={(e) => set("quartos")(e.target.value)}
                inputMode="numeric"
              />
            </label>
            <label className="cap-campo cap-mini">
              <span>Suítes</span>
              <input
                value={form.suites}
                onChange={(e) => set("suites")(e.target.value)}
                inputMode="numeric"
              />
            </label>
            <label className="cap-campo cap-mini">
              <span>Vagas</span>
              <input
                value={form.vagas}
                onChange={(e) => set("vagas")(e.target.value)}
                inputMode="numeric"
              />
            </label>
            <label className="cap-campo cap-mini">
              <span>Área m²</span>
              <input
                value={form.area_m2}
                onChange={(e) => set("area_m2")(e.target.value)}
                inputMode="decimal"
              />
            </label>
            <label className="cap-campo">
              <span>Como chegou</span>
              <select value={form.fonte} onChange={(e) => set("fonte")(e.target.value)}>
                {FONTES.map((f) => (
                  <option key={f} value={f}>
                    {f[0].toUpperCase() + f.slice(1)}
                  </option>
                ))}
              </select>
            </label>
            <label className="cap-campo cap-larga">
              <span>Observações</span>
              <textarea
                rows={2}
                value={form.observacoes}
                onChange={(e) => set("observacoes")(e.target.value)}
                placeholder="Chave com a vizinha, visitas após as 14h…"
              />
            </label>
            <div className="cap-checks">
              {([
                ["aceita_financiamento", "Aceita financiamento"],
                ["aceita_permuta", "Aceita permuta"],
                ["exclusividade", "Exclusividade"],
                ["autorizacao_assinada", "Autorização assinada"],
              ] as const).map(([k, rot]) => (
                <label key={k} className="cap-check">
                  <input
                    type="checkbox"
                    checked={form[k] as boolean}
                    onChange={(e) => set(k)(e.target.checked)}
                  />
                  {rot}
                </label>
              ))}
            </div>
          </div>
        )}

        <div className="cap-acoes">
          <button type="submit" className="cap-salvar" disabled={!podeSalvar || salvando}>
            {salvando ? "salvando…" : "Salvar imóvel"}
          </button>
          {!podeSalvar && (
            <span className="cap-dica">
              falta {!form.bairro.trim() ? "o bairro" : ""}
              {!form.bairro.trim() && !form.dono_telefone.trim() ? " e " : ""}
              {!form.dono_telefone.trim() ? "o telefone do dono" : ""}
              {isAdmin && !form.corretora_id ? " · escolha quem captou" : ""}
            </span>
          )}
        </div>
      </form>

      {recem && (
        <div className="cap-ok">
          <strong>Imóvel #{recem.id} salvo.</strong>
          {recem.avisos.length > 0 && (
            <ul className="cap-avisos">
              {recem.avisos.map((a, i) => (
                <li key={i}>
                  ⚠️ {String(a.texto ?? "parecido com uma captação que já existe")}
                </li>
              ))}
            </ul>
          )}
          <label className="cap-fotos">
            <input
              ref={fotoInput}
              type="file"
              accept="image/*"
              multiple
              onChange={(e) => enviarFotos(e.target.files)}
              hidden
            />
            <span
              className="cap-fotos-botao"
              onClick={() => fotoInput.current?.click()}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && fotoInput.current?.click()}
            >
              {subindoFoto ? "enviando…" : "📷 Adicionar fotos agora"}
            </span>
          </label>
        </div>
      )}

      <div className="cap-filtros">
        <input
          className="cap-busca"
          value={busca}
          onChange={(e) => setBusca(e.target.value)}
          placeholder="buscar por bairro, endereço, dono ou telefone"
        />
        <select value={situacao} onChange={(e) => setSituacao(e.target.value)}>
          <option value="">Todas as situações</option>
          {Object.entries(ROTULO_SITUACAO).map(([v, r]) => (
            <option key={v} value={v}>
              {r}
            </option>
          ))}
        </select>
      </div>

      {carregando ? (
        <p className="cap-vazio">carregando…</p>
      ) : itens.length === 0 ? (
        <p className="cap-vazio">
          Nenhum imóvel ainda. O primeiro que alguém te oferecer no WhatsApp, cadastra aqui.
        </p>
      ) : (
        <div className="cap-lista">
          {itens.map((i) => (
            <article key={i.id} className="cap-card">
              {i.capa_url ? (
                // capa_url vem do copiloto como "/painel/imoveis/fotos/{id}";
                // aqui só o id interessa, o proxy monta o resto com o escopo.
                <img
                  className="cap-capa"
                  src={`/api/imoveis/fotos/${i.capa_url.split("/").pop()}`}
                  alt=""
                  loading="lazy"
                />
              ) : (
                <div className="cap-capa cap-sem-foto">sem foto</div>
              )}
              <div className="cap-card-corpo">
                <div className="cap-card-topo">
                  <h3>
                    {i.tipo[0].toUpperCase() + i.tipo.slice(1)} · {i.bairro}
                  </h3>
                  <strong className="cap-preco">{moeda(i.preco_pedido)}</strong>
                </div>
                <p className="cap-card-meta">
                  {[
                    i.quartos ? `${i.quartos} quartos` : null,
                    i.suites ? `${i.suites} suíte${i.suites > 1 ? "s" : ""}` : null,
                    i.vagas ? `${i.vagas} vaga${i.vagas > 1 ? "s" : ""}` : null,
                    i.area_m2 ? `${i.area_m2} m²` : null,
                  ]
                    .filter(Boolean)
                    .join(" · ") || "sem detalhes ainda"}
                </p>
                <p className="cap-card-dono">
                  {i.dono_nome || "dono"} ·{" "}
                  <a
                    href={`https://wa.me/${i.dono_telefone.replace(/\D/g, "")}`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    {i.dono_telefone}
                  </a>
                </p>
                <div className="cap-card-tags">
                  <span className={`cap-sit cap-sit-${i.situacao}`}>
                    {ROTULO_SITUACAO[i.situacao] ?? i.situacao}
                  </span>
                  {i.exclusividade && <span className="cap-tag">exclusividade</span>}
                  {i.aceita_permuta && <span className="cap-tag">permuta</span>}
                  {i.fotos_total > 0 && <span className="cap-tag">📷 {i.fotos_total}</span>}
                  {isAdmin && i.corretora_label && (
                    <span className="cap-tag">{i.corretora_label}</span>
                  )}
                </div>
                {i.faltando.length > 0 && (
                  <p className="cap-faltando" title={i.faltando.join(", ")}>
                    faltam {i.faltando.length} campos · {i.completude}% completo
                  </p>
                )}
                <button type="button" className="cap-editar" onClick={() => setEditando(i)}>
                  ✏️ Editar imóvel
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      {editando && (
        <EditarImovel
          imovel={editando}
          isAdmin={isAdmin}
          corretoras={corretoras}
          onFechar={() => setEditando(null)}
          onSalvo={async () => {
            setEditando(null);
            await carregar();
          }}
        />
      )}
    </>
  );
}

// ── Edição ───────────────────────────────────────────────────────────────────
//
// Manda SÓ o que mudou: o copiloto grava um evento por alteração (preço e
// situação têm evento próprio), e reenviar o formulário inteiro sujaria o
// histórico com "preço mudou" de 450000.00 pra 450000.

function paraTexto(v: number | null): string {
  return v === null || v === undefined ? "" : String(v);
}

function formDe(i: Imovel) {
  return {
    tipo: i.tipo,
    bairro: i.bairro ?? "",
    dono_telefone: i.dono_telefone ?? "",
    preco: i.preco_pedido ? Math.round(i.preco_pedido).toLocaleString("pt-BR") : "",
    cidade: i.cidade ?? "",
    quartos: paraTexto(i.quartos),
    vagas: paraTexto(i.vagas),
    suites: paraTexto(i.suites),
    area_m2: paraTexto(i.area_m2),
    endereco: i.endereco ?? "",
    dono_nome: i.dono_nome ?? "",
    dono_relacao: i.dono_relacao ?? "",
    fonte: i.fonte ?? "",
    situacao: i.situacao,
    aceita_financiamento: Boolean(i.aceita_financiamento),
    aceita_permuta: Boolean(i.aceita_permuta),
    exclusividade: Boolean(i.exclusividade),
    autorizacao_assinada: Boolean(i.autorizacao_assinada),
    observacoes: i.observacoes ?? "",
    corretora_id: String(i.corretora_id),
  };
}

type FormEdicao = ReturnType<typeof formDe>;

function numeroOuNull(t: string): number | null {
  const v = t.trim().replace(",", ".");
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function EditarImovel({
  imovel,
  isAdmin,
  corretoras,
  onFechar,
  onSalvo,
}: {
  imovel: Imovel;
  isAdmin: boolean;
  corretoras: Corretora[];
  onFechar: () => void;
  onSalvo: () => Promise<void>;
}) {
  const [f, setF] = useState<FormEdicao>(() => formDe(imovel));
  const [salvando, setSalvando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  // Foto adicionada, excluída ou reordenada já está gravada no momento do
  // gesto; fechar a janela só precisa recarregar a lista pra capa atualizar.
  const fotosMudaram = useRef(false);
  const fechar = useCallback(() => {
    if (fotosMudaram.current) onSalvo();
    else onFechar();
  }, [onFechar, onSalvo]);

  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && fechar();
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  }, [fechar]);

  const set = (k: keyof FormEdicao) => (v: string | boolean) =>
    setF((x) => ({ ...x, [k]: v }));

  const precoPreview = useMemo(() => parsePreco(f.preco), [f.preco]);

  const mudancas = useMemo(() => {
    const m: Record<string, unknown> = {};
    const texto = (k: keyof Imovel & keyof FormEdicao) => {
      const novo = String(f[k]).trim();
      if (novo !== String(imovel[k] ?? "").trim()) m[k] = novo || null;
    };
    for (const k of ["tipo", "bairro", "dono_telefone", "cidade", "endereco", "dono_nome",
      "dono_relacao", "fonte", "situacao", "observacoes"] as const) texto(k);
    for (const k of ["quartos", "vagas", "suites", "area_m2"] as const) {
      const novo = numeroOuNull(f[k]);
      if (novo !== (imovel[k] ?? null)) m[k] = novo;
    }
    if (precoPreview !== (imovel.preco_pedido === null ? null : Math.round(imovel.preco_pedido))) {
      m.preco_pedido = precoPreview;
    }
    for (const k of ["aceita_financiamento", "aceita_permuta", "exclusividade",
      "autorizacao_assinada"] as const) {
      if (f[k] !== Boolean(imovel[k])) m[k] = f[k];
    }
    if (isAdmin && Number(f.corretora_id) !== imovel.corretora_id) {
      m.corretora_id = Number(f.corretora_id);
    }
    return m;
  }, [f, imovel, isAdmin, precoPreview]);

  const falta = !f.bairro.trim() ? "o bairro" : !f.dono_telefone.trim() ? "o telefone do dono" : null;
  const nMudancas = Object.keys(mudancas).length;

  async function salvar(e: React.FormEvent) {
    e.preventDefault();
    if (salvando || falta) return;
    if (nMudancas === 0) {
      fechar();
      return;
    }
    setSalvando(true);
    setErro(null);
    try {
      const r = await fetch(`/api/imoveis/${imovel.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(mudancas),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        throw new Error(
          d.detail === "imovel_de_outra_corretora"
            ? "esse imóvel é de outra corretora"
            : d.detail === "transferencia_requer_admin"
              ? "só a administração troca quem captou"
              : typeof d.detail === "string"
                ? d.detail
                : d.error ?? `http_${r.status}`,
        );
      }
      await onSalvo();
    } catch (e) {
      setErro(e instanceof Error ? e.message : "nao_consegui_salvar");
      setSalvando(false);
    }
  }

  return (
    <div className="cap-modal-fundo" onClick={fechar}>
      <form
        className="cap-form cap-modal"
        onSubmit={salvar}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="cap-editar-titulo"
      >
        <div className="cap-modal-topo">
          <p className="cap-form-titulo" id="cap-editar-titulo">
            Editar imóvel #{imovel.id}
          </p>
          <button type="button" className="cap-fechar" onClick={fechar} aria-label="Fechar">
            ×
          </button>
        </div>

        {erro && <div className="cap-erro" role="alert">Deu problema: {erro}</div>}

        <GaleriaFotos imovelId={imovel.id} onMudou={() => { fotosMudaram.current = true; }} />

        <div className="cap-grid">
          <label className="cap-campo">
            <span>Tipo</span>
            <select value={f.tipo} onChange={(e) => set("tipo")(e.target.value)}>
              {TIPOS.map((t) => (
                <option key={t} value={t}>{t[0].toUpperCase() + t.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="cap-campo">
            <span>Situação</span>
            <select value={f.situacao} onChange={(e) => set("situacao")(e.target.value)}>
              {Object.entries(ROTULO_SITUACAO).map(([v, r]) => (
                <option key={v} value={v}>{r}</option>
              ))}
            </select>
          </label>
          <label className="cap-campo">
            <span>Bairro *</span>
            <input value={f.bairro} onChange={(e) => set("bairro")(e.target.value)} autoComplete="off" />
          </label>
          <label className="cap-campo">
            <span>Telefone do dono *</span>
            <input
              value={f.dono_telefone}
              onChange={(e) => set("dono_telefone")(e.target.value)}
              inputMode="tel"
              autoComplete="off"
            />
          </label>
          <label className="cap-campo">
            <span>Quanto ele pede</span>
            <input
              value={f.preco}
              onChange={(e) => set("preco")(e.target.value)}
              placeholder="450 mil"
              inputMode="decimal"
              autoComplete="off"
            />
            {precoPreview !== null && <em className="cap-preview">{moeda(precoPreview)}</em>}
          </label>
          {isAdmin && (
            <label className="cap-campo">
              <span>Captado por</span>
              <select value={f.corretora_id} onChange={(e) => set("corretora_id")(e.target.value)}>
                {!corretoras.some((c) => String(c.id) === f.corretora_id) && (
                  <option value={f.corretora_id}>{imovel.corretora_label ?? `C${imovel.corretora_id}`}</option>
                )}
                {corretoras.map((c) => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </select>
            </label>
          )}
        </div>

        <div className="cap-grid cap-extra">
          <label className="cap-campo">
            <span>Nome do dono</span>
            <input value={f.dono_nome} onChange={(e) => set("dono_nome")(e.target.value)} />
          </label>
          <label className="cap-campo">
            <span>Ele é</span>
            <select value={f.dono_relacao} onChange={(e) => set("dono_relacao")(e.target.value)}>
              {!f.dono_relacao && <option value="">—</option>}
              {RELACOES.map((r) => (
                <option key={r} value={r}>{r[0].toUpperCase() + r.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="cap-campo">
            <span>Endereço</span>
            <input value={f.endereco} onChange={(e) => set("endereco")(e.target.value)} />
          </label>
          <label className="cap-campo">
            <span>Cidade</span>
            <input value={f.cidade} onChange={(e) => set("cidade")(e.target.value)} />
          </label>
          {([
            ["quartos", "Quartos", "numeric"],
            ["suites", "Suítes", "numeric"],
            ["vagas", "Vagas", "numeric"],
            ["area_m2", "Área m²", "decimal"],
          ] as const).map(([k, rot, modo]) => (
            <label key={k} className="cap-campo cap-mini">
              <span>{rot}</span>
              <input value={f[k]} onChange={(e) => set(k)(e.target.value)} inputMode={modo} />
            </label>
          ))}
          <label className="cap-campo">
            <span>Como chegou</span>
            <select value={f.fonte} onChange={(e) => set("fonte")(e.target.value)}>
              {!f.fonte && <option value="">—</option>}
              {(FONTES.includes(f.fonte) || !f.fonte ? FONTES : [...FONTES, f.fonte]).map((x) => (
                <option key={x} value={x}>{x[0].toUpperCase() + x.slice(1)}</option>
              ))}
            </select>
          </label>
          <label className="cap-campo cap-larga">
            <span>Observações</span>
            <textarea
              rows={3}
              value={f.observacoes}
              onChange={(e) => set("observacoes")(e.target.value)}
            />
          </label>
          <div className="cap-checks">
            {([
              ["aceita_financiamento", "Aceita financiamento"],
              ["aceita_permuta", "Aceita permuta"],
              ["exclusividade", "Exclusividade"],
              ["autorizacao_assinada", "Autorização assinada"],
            ] as const).map(([k, rot]) => (
              <label key={k} className="cap-check">
                <input type="checkbox" checked={f[k]} onChange={(e) => set(k)(e.target.checked)} />
                {rot}
              </label>
            ))}
          </div>
        </div>

        <div className="cap-acoes">
          <button type="submit" className="cap-salvar" disabled={salvando || Boolean(falta)}>
            {salvando ? "salvando…" : "Salvar alterações"}
          </button>
          <button type="button" className="cap-cancelar" onClick={fechar}>
            {nMudancas ? "Cancelar" : "Fechar"}
          </button>
          <span className="cap-dica">
            {falta
              ? `falta ${falta}`
              : nMudancas
                ? `${nMudancas} ${nMudancas === 1 ? "alteração" : "alterações"}`
                : "fotos já ficam salvas na hora"}
          </span>
        </div>
      </form>
    </div>
  );
}
