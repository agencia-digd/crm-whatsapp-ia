"use client";

// Editar o TEXTO de uma variante do teste A/B (pedido da Dona, 05/10/2026:
// "lá eu queria também poder mudar a copy").
//
// O texto mora no banco, com o do código como padrão. Quem valida é o
// servidor (`PUT /api/automacoes/remarketing/copy`): o que ele recusa nunca é
// gravado, e a recusa aparece aqui, ao lado do campo. O robô lê o texto do
// banco antes de cada mensagem.
//
// Três coisas que a tela diz porque mudam a decisão de quem edita:
//   - a prévia é a mensagem inteira, com o casco do template em volta;
//   - mudar o texto começa uma medição nova pra esta variante (o resultado do
//     texto antigo não soma no novo);
//   - a variante de marketing segue o texto da `direta` enquanto o espelho
//     estiver ligado — é o teste de categoria.

import { useEffect, useState } from "react";
import { CopiesDoCarrossel, MetricaVariante } from "../types";
import Previa, { canonizar } from "./previa";
import s from "./copy.module.css";

const dataHora = (iso: string) =>
  new Date(iso).toLocaleString("pt-BR", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });

type Resposta = { ok: boolean; erros?: Record<string, string>; erro?: string };

async function gravarCopy(corpo: Record<string, unknown>): Promise<Resposta> {
  try {
    const r = await fetch("/api/automacoes/remarketing/copy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corpo),
    });
    const out = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true };
    return {
      ok: false,
      erros: out?.erros ?? undefined,
      erro: String(out?.detail || out?.error || `HTTP ${r.status}`),
    };
  } catch (e) {
    return { ok: false, erro: String((e as Error).message || e) };
  }
}

export default function EditorDeCopy({
  v,
  copies,
  onSalvou,
}: {
  v: MetricaVariante;
  copies: CopiesDoCarrossel | undefined;
  onSalvou: () => void;
}) {
  const [aberto, setAberto] = useState(false);
  const [texto, setTexto] = useState(v.texto);
  const [erro, setErro] = useState<string | null>(null);
  const [salvando, setSalvando] = useState(false);

  // Recarregou do servidor: o rascunho volta pro que está gravado.
  useEffect(() => {
    setTexto(v.texto);
    setErro(null);
  }, [v.texto]);

  if (!copies || !v.ativa) return null; // servidor antigo, ou variante aposentada

  const max = copies.texto_max;
  const limpo = canonizar(texto);
  const mudou = limpo !== v.texto;
  const estourou = limpo.length > max;

  const enviar = async (corpo: Record<string, unknown>, fecha = true) => {
    setSalvando(true);
    setErro(null);
    const r = await gravarCopy(corpo);
    setSalvando(false);
    if (!r.ok) {
      setErro(r.erros?.[v.id] ?? r.erros?.espelho ?? r.erros?.textos ?? r.erro ?? "não consegui gravar");
      return;
    }
    if (fecha) setAberto(false);
    onSalvou();
  };

  const salvar = () => {
    if (
      v.enviados > 0 &&
      !window.confirm(
        `Trocar o texto da variante "${v.id}"?\n\n` +
          `Ela começa uma medição nova: os ${v.enviados} disparos do texto de agora ` +
          "ficam guardados à parte e não somam no texto novo.",
      )
    ) {
      return;
    }
    void enviar({ textos: { [v.id]: texto } });
  };

  const voltarAoPadrao = () => {
    if (!window.confirm(`Voltar a variante "${v.id}" pro texto original?\n\nO histórico das mudanças fica guardado.`)) return;
    void enviar({ textos: { [v.id]: null } });
  };

  // ── a variante que segue o texto de outra (o teste de categoria) ──
  if (v.espelhando) {
    return (
      <div className={s.editor} data-testid={`copy-${v.id}`}>
        <p className={s.dica}>
          Segue o texto de <strong>{v.espelha}</strong>: aqui se testa a categoria, não a copy.
        </p>
        <div className={s.acoes}>
          <button
            type="button"
            className={s.acaoLink}
            disabled={salvando}
            data-testid={`copy-separar-${v.id}`}
            onClick={() => {
              if (
                !window.confirm(
                  `Editar "${v.id}" separado de "${v.espelha}"?\n\n` +
                    "Com textos diferentes, o resultado deixa de dizer se a diferença veio da categoria ou do texto.",
                )
              ) {
                return;
              }
              void enviar({ espelho: false }, false);
            }}
          >
            editar separado
          </button>
        </div>
        {erro && <p className={s.erro}>{erro}</p>}
      </div>
    );
  }

  return (
    <div className={s.editor} data-testid={`copy-${v.id}`}>
      <p className={s.desde} data-testid={`copy-desde-${v.id}`}>
        {v.texto_desde
          ? `Este texto vale desde ${dataHora(v.texto_desde)}.`
          : "Texto original, nunca editado."}
        {v.textos_anteriores && v.textos_anteriores.enviados > 0
          ? ` Com o texto anterior: ${v.textos_anteriores.enviados} receberam, ${v.textos_anteriores.respostas} responderam.`
          : ""}
      </p>

      {!aberto ? (
        <div className={s.acoes}>
          <button
            type="button"
            className={s.acao}
            data-testid={`copy-editar-${v.id}`}
            onClick={() => setAberto(true)}
          >
            editar o texto
          </button>
          {v.texto_personalizado && (
            <button type="button" className={s.acaoLink} disabled={salvando} onClick={voltarAoPadrao}>
              voltar ao texto original
            </button>
          )}
          {v.espelha && (
            <button
              type="button"
              className={s.acaoLink}
              disabled={salvando}
              onClick={() => void enviar({ espelho: true }, false)}
            >
              voltar a seguir {v.espelha}
            </button>
          )}
        </div>
      ) : (
        <>
          <label className={s.previaRotulo} htmlFor={`copy-campo-${v.id}`}>
            texto da variante {v.id}
          </label>
          <textarea
            id={`copy-campo-${v.id}`}
            className={`${s.texto} ${erro || estourou ? s.textoComErro : ""}`}
            value={texto}
            disabled={salvando}
            aria-invalid={Boolean(erro) || estourou}
            aria-describedby={`copy-ajuda-${v.id}`}
            onChange={(e) => {
              setTexto(e.target.value);
              setErro(null);
            }}
          />
          <div className={s.linhaDoCampo} id={`copy-ajuda-${v.id}`}>
            <span>Editar o texto não reaprova o template nem muda a categoria.</span>
            <span className={`${s.contador} ${estourou ? s.contadorEstourou : ""}`}>
              {limpo.length} de {max}
            </span>
          </div>
          {erro && (
            <p className={s.erro} role="alert" data-testid={`copy-erro-${v.id}`}>
              {erro}
            </p>
          )}
          <Previa texto={texto} />
          <div className={s.acoes}>
            <button
              type="button"
              className={s.acaoPripessoa}
              disabled={salvando || !mudou}
              data-testid={`copy-salvar-${v.id}`}
              onClick={salvar}
            >
              {salvando ? "salvando…" : "salvar o texto"}
            </button>
            <button
              type="button"
              className={s.acao}
              disabled={salvando}
              onClick={() => {
                setTexto(v.texto);
                setErro(null);
                setAberto(false);
              }}
            >
              cancelar
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// ── histórico: quem mudou qual texto, de quê pra quê ──
export function HistoricoDeCopy({ copies }: { copies: CopiesDoCarrossel | undefined }) {
  if (!copies) return null;
  if (copies.historico_erro) {
    return <p className={s.erro}>Não consegui ler o histórico dos textos: {copies.historico_erro}</p>;
  }
  const itens = copies.historico ?? [];
  if (itens.length === 0) return null;
  return (
    <details className={s.historico} data-testid="copy-historico">
      <summary>
        Histórico dos textos ({itens.length} {itens.length === 1 ? "mudança" : "mudanças"})
      </summary>
      <ul className={s.historicoLista}>
        {itens.map((h, i) => (
          <li key={`${h.em}-${h.variante}-${i}`} className={s.historicoItem}>
            <span className={s.historicoQuem}>
              {h.em ? dataHora(h.em) : "sem data"} · {h.autor ?? "sem autor"}
              {h.variante ? ` · ${h.variante}` : ""}
            </span>
            {h.variante === null ? (
              <span className={s.historicoTexto}>
                {h.espelho
                  ? "a variante de marketing voltou a seguir o texto da direta"
                  : "a variante de marketing passou a ter texto próprio"}
              </span>
            ) : (
              <>
                <span className={s.historicoTexto}>antes: {h.antes}</span>
                <span className={s.historicoTexto}>
                  depois: {h.depois ?? "voltou ao texto original"}
                </span>
              </>
            )}
          </li>
        ))}
      </ul>
    </details>
  );
}
