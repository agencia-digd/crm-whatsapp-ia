"use client";

// 🏗️ Empreendimentos — catálogo VIVO da planilha CONTROLE EMPREENDIMENTOS.
// Só leitura: a planilha segue sendo editada pela equipe no Google Sheets;
// aqui o painel mostra o que o robô enxerga (mesma normalização que a Assistente
// usa pra casar lead com imóvel). Fonte: /api/empreendimentos → Mac (gviz).

import { useEffect, useMemo, useState } from "react";
import "./empreendimentos.css";

type Emp = {
  empreendimento: string;
  construtora: string;
  tipo: string;
  quartos: string;
  quartos_norm: number | null;
  preco_bruto: string;
  preco_num: number | null;
  preco_fmt: string;
  entrega_bucket: "pronto" | "lancamento" | "data" | null;
  entrega_fmt: string;
  zona: string;
  bairro: string;
  decorado: string;
  drive: string;
  tem_material: boolean;
};

type Payload = {
  configurado: boolean;
  atualizado_em: string | null;
  stale?: boolean;
  total: number;
  itens: Emp[];
  erro?: string;
};

const norm = (s: string) =>
  (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");

function fmtAtualizado(iso: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const hoje = new Date();
    const mesmoDia = d.toDateString() === hoje.toDateString();
    const hora = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
    return mesmoDia ? `hoje ${hora}` : `${d.toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" })} ${hora}`;
  } catch {
    return "";
  }
}

export default function EmpreendimentosPage() {
  const [dados, setDados] = useState<Payload | null>(null);
  const [carregando, setCarregando] = useState(true);
  const [busca, setBusca] = useState("");
  const [tipo, setTipo] = useState("");
  const [zona, setZona] = useState("");
  const [quartosMin, setQuartosMin] = useState(0);
  const [entrega, setEntrega] = useState("");
  const [ordem, setOrdem] = useState<"nome" | "preco-asc" | "preco-desc">("nome");

  useEffect(() => {
    fetch("/api/empreendimentos", { cache: "no-store" })
      .then((r) => r.json())
      .then(setDados)
      .catch(() => setDados(null))
      .finally(() => setCarregando(false));
  }, []);

  const itens = dados?.itens ?? [];

  const tipos = useMemo(
    () => Array.from(new Set(itens.map((e) => e.tipo).filter(Boolean))).sort(),
    [itens],
  );
  const zonas = useMemo(
    () => Array.from(new Set(itens.map((e) => e.zona).filter(Boolean))).sort(),
    [itens],
  );

  const filtrados = useMemo(() => {
    const q = norm(busca);
    const lista = itens.filter((e) => {
      if (q && !norm(`${e.empreendimento} ${e.construtora} ${e.bairro} ${e.zona}`).includes(q)) return false;
      if (tipo && e.tipo !== tipo) return false;
      if (zona && e.zona !== zona) return false;
      if (quartosMin && (e.quartos_norm == null || e.quartos_norm < quartosMin)) return false;
      if (entrega === "pronto" && e.entrega_bucket !== "pronto") return false;
      if (entrega === "lancamento" && e.entrega_bucket !== "lancamento") return false;
      if (entrega === "data" && e.entrega_bucket !== "data") return false;
      return true;
    });
    if (ordem === "nome") return lista.sort((a, b) => a.empreendimento.localeCompare(b.empreendimento, "pt-BR"));
    // preço: "sob consulta" (null) sempre no fim
    return lista.sort((a, b) => {
      if (a.preco_num == null && b.preco_num == null) return 0;
      if (a.preco_num == null) return 1;
      if (b.preco_num == null) return -1;
      return ordem === "preco-asc" ? a.preco_num - b.preco_num : b.preco_num - a.preco_num;
    });
  }, [itens, busca, tipo, zona, quartosMin, entrega, ordem]);

  const temFiltro = Boolean(busca || tipo || zona || quartosMin || entrega);

  return (
    <div className="emp">
      <div className="hoje-head">
        <div>
          <div className="hoje-kick">Catálogo</div>
          <h1 className="hoje-titulo">🏗️ Empreendimentos</h1>
          <p className="hoje-sub">
            A planilha CONTROLE EMPREENDIMENTOS, ao vivo. É isso que a Assistente usa pra casar lead com imóvel.
          </p>
        </div>
        <div className="emp-meta">
          {dados?.atualizado_em && <span className="emp-carimbo">🕐 planilha lida {fmtAtualizado(dados.atualizado_em)}</span>}
          {dados?.stale && (
            <span className="emp-stale" title="A fonte (Mac) está fora do ar — mostrando a última leitura boa.">
              ⚠️ dado pode estar desatualizado
            </span>
          )}
        </div>
      </div>

      <div className="emp-filtros">
        <input
          className="emp-busca"
          placeholder="Buscar por nome, construtora, bairro…"
          value={busca}
          onChange={(e) => setBusca(e.target.value)}
        />
        <select value={tipo} onChange={(e) => setTipo(e.target.value)}>
          <option value="">Tipo: todos</option>
          {tipos.map((t) => (
            <option key={t} value={t}>{t}</option>
          ))}
        </select>
        <select value={quartosMin} onChange={(e) => setQuartosMin(Number(e.target.value))}>
          <option value={0}>Quartos: todos</option>
          <option value={1}>1+</option>
          <option value={2}>2+</option>
          <option value={3}>3+</option>
          <option value={4}>4+</option>
        </select>
        <select value={zona} onChange={(e) => setZona(e.target.value)}>
          <option value="">Região: todas</option>
          {zonas.map((z) => (
            <option key={z} value={z}>{z}</option>
          ))}
        </select>
        <select value={entrega} onChange={(e) => setEntrega(e.target.value)}>
          <option value="">Entrega: todas</option>
          <option value="pronto">Pronto</option>
          <option value="lancamento">Lançamento</option>
          <option value="data">Com data</option>
        </select>
        <select value={ordem} onChange={(e) => setOrdem(e.target.value as any)}>
          <option value="nome">Ordem: A–Z</option>
          <option value="preco-asc">Menor preço</option>
          <option value="preco-desc">Maior preço</option>
        </select>
      </div>

      <div className="emp-contagem">
        {carregando
          ? "Carregando…"
          : temFiltro
            ? `${filtrados.length} de ${itens.length} empreendimentos`
            : `${itens.length} empreendimentos`}
      </div>

      {!carregando && dados && !dados.configurado && (
        <div className="hoje-vazio">
          Não consegui ler a planilha agora (a fonte no Mac parece fora do ar). Tenta de novo daqui a pouco.
        </div>
      )}

      {!carregando && dados?.configurado && filtrados.length === 0 && (
        <div className="hoje-vazio">Nenhum empreendimento com esses filtros.</div>
      )}

      {filtrados.length > 0 && (
        <div className="emp-tabelabox">
          <table className="emp-tabela">
            <thead>
              <tr>
                <th>Empreendimento</th>
                <th>Tipo</th>
                <th>Quartos</th>
                <th>A partir de</th>
                <th>Entrega</th>
                <th>Região / Bairro</th>
                <th>Decorado</th>
                <th>Material</th>
              </tr>
            </thead>
            <tbody>
              {filtrados.map((e, i) => (
                <tr key={`${e.empreendimento}-${i}`}>
                  <td>
                    <div className="emp-nome">{e.empreendimento}</div>
                    {e.construtora && <div className="emp-construtora">{e.construtora}</div>}
                  </td>
                  <td>{e.tipo && <span className="emp-tag">{e.tipo}</span>}</td>
                  <td className="emp-num">{e.quartos || "—"}</td>
                  <td className="emp-preco" title={e.preco_bruto || undefined}>{e.preco_fmt}</td>
                  <td>
                    {e.entrega_bucket === "pronto" && <span className="emp-tag verde">Pronto</span>}
                    {e.entrega_bucket === "lancamento" && <span className="emp-tag ambar">Lançamento</span>}
                    {e.entrega_bucket === "data" && <span className="emp-entrega">{e.entrega_fmt}</span>}
                    {!e.entrega_bucket && "—"}
                  </td>
                  <td>
                    <div>{e.zona || "—"}</div>
                    {e.bairro && <div className="emp-bairro">{e.bairro}</div>}
                  </td>
                  <td className="emp-decorado" title={e.decorado || undefined}>{e.decorado || "—"}</td>
                  <td>
                    {e.tem_material ? (
                      <a className="emp-drive" href={e.drive} target="_blank" rel="noreferrer">
                        📁 Drive
                      </a>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
