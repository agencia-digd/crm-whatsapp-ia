"use client";

// 📣 REMARKETING DA BASE (carrossel da Corretor) — a página de UMA automação.
//
// É o fluxo com mais volume da operação e o que menos aparecia: o disparo
// diário do 0000-0000 (o telefone DA CASA), a Pessoa atendendo quem responde, a
// qualificação e o handoff pra Pessoa12. A tela mostrava só uma linha na lista.
//
// Irmã da página de Follow-up de propósito: mesmo canvas (`Canvas.tsx`), mesmos
// blocos (`blocos.tsx`), mesmo interruptor, mesma faixa de marco, mesma régua
// de leitura honesta. Duas páginas de automação com desenhos diferentes viram
// duas linguagens diferentes na primeira mudança.
//
// A parte que vale a viagem é o TESTE A/B: cada variante mostra o texto que
// sai, a categoria, o peso (editável) e quantos receberam/responderam. Duas
// travas seguram a edição de peso — e elas vivem no BACKEND, aqui só se
// antecipa o erro:
//   1. a soma dos pesos ativos tem que dar 100;
//   2. a fatia em MARKETING fica na cota da casa (hoje 20%) — é mix de
//      categoria, não performance.
// Um curl que passe por cima da tela bate na mesma validação.
//
// O TEXTO das variantes ativas se edita aqui desde 05/10/2026
// (`EditorDeCopy.tsx`): mora no banco, com o do código como padrão, e mudar o
// texto começa uma medição nova. `direta` e `direta_marketing` usam a MESMA
// copy de propósito (é teste de CATEGORIA): a de marketing segue o texto da
// direta até alguém mandar separar, e a tela avisa o que isso custa.
//
// Régua de leitura, igual à do Follow-up:
//   - `taxa: null` = "sem dado", nunca 0%;
//   - abaixo do `amostra_minima` do backend, o número vem marcado como ruído;
//   - o marco de régua diz desde quando a medição conta.
//
// Nada de id fixo: variantes, passos do atendimento, travas e limite do dia
// vêm todos do backend (`_mapa_carrossel_corretor`), que os deriva do código que
// roda de verdade.

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Espera, Node } from "../blocos";
import Canvas, { Edge, corrente } from "../Canvas";
import { useConfigOperacional } from "../configOperacional";
import PainelDisparo from "./Disparo";
import Recolhe from "./Recolhe";
import EditorDeCopy, { HistoricoDeCopy } from "./EditorDeCopy";
import {
  CopiesDoCarrossel,
  ForaDaTaxa,
  Mapa,
  MetricaVariante,
  MetricasRemarketing,
  RespostasPorTipo,
} from "../types";
import s from "./remarketing.module.css";

const PERIODOS = [7, 30, 90];
const LARGURA_NO = 240;

function pct(taxa: number | null): string {
  if (taxa === null) return "sem dado";
  // O backend já manda PERCENTUAL (0-100), igual ao resto do painel.
  return `${taxa.toFixed(1).replace(".", ",")}%`;
}

const dataCurta = (iso: string) =>
  new Date(iso).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" });

const numero = (n: number | null | undefined) =>
  n === null || n === undefined ? "sem dado" : String(n);

// ── entrega do último lote: o que CHEGOU, não o que saiu ──
//
// "Já saíram hoje" conta o aceite da Meta. Aceite não é entrega: a Fulano mandou
// 909 com 40% de falha e o relatório dizia que tinha saído tudo. Esta faixa
// mostra o que o webhook de status confirmou — e separa "em voo" (aceito, sem
// confirmação) de "entregue", porque somar os dois é o erro que escondeu a falha.
const ACAO_DA_CULPA: Record<string, string> = {
  lista: "a lista está ruim — disparar de novo piora a linha",
  conta: "é a conta — alguém precisa abrir o Business Manager",
  template: "é o template — pausado, ausente nessa linha ou com parâmetro errado",
  desconhecida: "motivo não classificado — olhar o código da Meta",
};

const diaBR = (iso: string) => iso.split("-").reverse().slice(0, 2).join("/");

function EntregaUltimoLote({
  entrega,
}: {
  entrega: Mapa["carrossel_corretor"]["entrega_ultimo_lote"];
}) {
  if (!entrega) return null; // backend antigo: não inventa faixa vazia
  if (entrega.indisponivel) {
    return (
      <div className={s.erro}>
        Não consegui ler a entrega do último lote: {entrega.motivo_indisponivel}
      </div>
    );
  }
  const p = entrega.placar;
  if (!p) return null; // carrossel nunca disparou — nada a mostrar
  const culpas = Object.entries(p.culpas).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0));
  const principal = p.motivos[0];
  return (
    <div className={s.ritmoBarra}>
      <div className={s.stat}>
        <div className={s.statLabel}>último lote · {diaBR(p.de)}</div>
        <div className={s.statValor}>{p.mandados}</div>
        <div className={s.statSub}>mandados (aceitos pela Meta)</div>
      </div>
      <div className={s.stat}>
        <div className={s.statLabel}>entregues</div>
        <div className={s.statValor}>{p.entregues}</div>
        <div className={s.statSub}>
          {p.pct_entregue === null
            ? "sem envio rastreável"
            : `${p.pct_entregue}% — chegou no aparelho`}
        </div>
      </div>
      <div className={s.stat}>
        <div className={s.statLabel}>em voo</div>
        <div className={s.statValor}>{p.em_voo}</div>
        <div className={s.statSub}>aceito, sem confirmação — não é entrega</div>
      </div>
      <div className={`${s.stat} ${p.falhas > 0 ? s.statAlerta : ""}`}>
        <div className={s.statLabel}>falharam</div>
        <div className={s.statValor}>
          {p.falhas}
          {p.pct_falha !== null ? ` (${p.pct_falha}%)` : ""}
        </div>
        <div className={s.statSub}>
          {p.falhas === 0
            ? "nenhuma falha confirmada"
            : culpas.map(([c, n]) => `${n}: ${ACAO_DA_CULPA[c] ?? c}`).join(" · ")}
          {principal
            ? ` — principal: ${principal.codigo ?? "sem código"} ${principal.titulo ?? ""} (${principal.n}x)`
            : ""}
        </div>
      </div>
      {p.sem_rastro > 0 && (
        <div className={s.stat}>
          <div className={s.statLabel}>sem rastro</div>
          <div className={s.statValor}>{p.sem_rastro}</div>
          <div className={s.statSub}>
            envio anterior a 04/10 — não dá pra saber se chegou
          </div>
        </div>
      )}
    </div>
  );
}

// ── respostas por tipo ──
//
// Seis tipos, na ordem em que a Dona pergunta: quem quis saber mais (escreveu
// ou clicou no botão), quem disse não, quem pediu pra parar, o WhatsApp que
// respondeu sozinho e quem respondeu sem dar sinal. Só os dois primeiros
// entram na taxa, e a tela separa os dois grupos em vez de somar tudo em
// "responderam". Servidor antigo (sem `respostas_por_tipo`) cai no que existe;
// o que ele não manda aparece como "sem dado", nunca como zero.
type Tipo = { chave: string; rotulo: string; valor: number | null; conta: boolean };

function tiposDe(
  porTipo: RespostasPorTipo | undefined,
  fora: ForaDaTaxa | undefined,
  respostas: number,
): Tipo[] {
  const separa = porTipo?.interesse !== undefined && porTipo?.botao !== undefined;
  const tipos: Tipo[] = [
    { chave: "interesse", rotulo: "interesse", valor: separa ? porTipo!.interesse! : null, conta: true },
    { chave: "botao", rotulo: "botão", valor: separa ? porTipo!.botao! : null, conta: true },
    { chave: "recusa", rotulo: "recusa", valor: porTipo?.disse_nao ?? fora?.recusas ?? null, conta: false },
    { chave: "parar", rotulo: "parar", valor: porTipo?.parar ?? fora?.pararam ?? null, conta: false },
    { chave: "automatica", rotulo: "automática", valor: porTipo?.automatica ?? fora?.automaticas ?? null, conta: false },
    { chave: "indefinida", rotulo: "indefinida", valor: porTipo?.sem_sinal ?? fora?.indefinidas ?? null, conta: false },
  ];
  // Servidor antigo: não separa interesse de botão, mas sabe a soma dos dois.
  if (!separa) tipos.splice(0, 2, { chave: "interesse", rotulo: "interesse ou botão", valor: respostas, conta: true });
  const errado = porTipo?.numero_errado ?? fora?.numeros_errados ?? 0;
  if (errado > 0) tipos.push({ chave: "numero_errado", rotulo: "número errado", valor: errado, conta: false });
  return tipos;
}

function RespostasPorTipoBloco({ met }: { met: MetricasRemarketing }) {
  const tipos = tiposDe(met.total.respostas_por_tipo, met.total.fora_da_taxa, met.total.respostas);
  const contam = tipos.filter((x) => x.conta).length;
  return (
    <div className={s.tipos} data-testid="respostas-por-tipo">
      <div className={s.tiposTitulo}>
        respostas por tipo
        <span className={s.tiposNota}>
          {contam === 2 ? "os dois primeiros entram na taxa" : "só o primeiro entra na taxa"}
        </span>
      </div>
      <dl className={s.tiposGrade}>
        {tipos.map((x) => (
          <div key={x.chave} className={`${s.tipo} ${x.conta ? s.tipoConta : ""}`} data-tipo={x.chave}>
            <dt>{x.rotulo}</dt>
            <dd>{x.valor === null ? "sem dado" : x.valor}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// ── cabeçalho: nome e o gatilho em português ──
//
// O interruptor saiu daqui no front novo (05/10/2026): mora no bloco de estado
// do painel do disparo, ao lado da frase "sai mensagem agora?". Um interruptor
// longe do motivo de não sair era o que deixava a tela confusa.
function Cabecalho({ gatilho, dryRun }: { gatilho: string; dryRun: boolean }) {
  return (
    <header className={s.cabecalho}>
      <h1 className={s.nome}>Remarketing da base</h1>
      <p className={s.gatilho}>{gatilho}</p>
      {dryRun && (
        <div className={s.desligadoAviso}>
          Modo simulação: o carrossel roda inteiro, mas nenhuma mensagem chega no
          WhatsApp de verdade.
        </div>
      )}
    </header>
  );
}

// ── faixa do MARCO: desde quando a medição conta ──
//
// Só leitura, de propósito. O marco é da RÉGUA inteira (a mesma linha vale pra
// cadência); o botão de recomeçar mora na página do Follow-up, que é onde ele
// nasceu. Ter dois botões pro mesmo registro faria alguém zerar a medição da
// cadência achando que estava mexendo só no remarketing.
function MarcoBarra({ met }: { met: MetricasRemarketing }) {
  return (
    <div className={s.marcoBarra}>
      <div className={s.marcoTexto}>
        {met.medindo_desde ? (
          <>
            <strong>medindo desde {dataCurta(met.medindo_desde)}</strong>
            {met.motivo_do_marco ? ` — ${met.motivo_do_marco}` : ""}
            {met.marco_por ? ` (${met.marco_por})` : ""}
          </>
        ) : (
          <>
            <strong>medindo todo o histórico</strong> — nenhuma medição foi
            recomeçada até agora.
          </>
        )}
      </div>
    </div>
  );
}

// ── um cartão de variante do teste A/B ──
function CartaoVariante({
  v,
  peso,
  amostraMinima,
  salvando,
  onPeso,
  copies,
  onSalvouTexto,
}: {
  v: MetricaVariante;
  peso: number;
  amostraMinima: number;
  salvando: boolean;
  onPeso: (novo: number) => void;
  copies: CopiesDoCarrossel | undefined;
  onSalvouTexto: () => void;
}) {
  const marketing = v.categoria === "marketing";
  const saiuEmOutra = v.por_categoria.filter(
    (x) => x.categoria.toLowerCase() !== v.categoria.toLowerCase(),
  );

  return (
    <div
      className={`${s.cartaoVariante} ${marketing ? s.cartaoMkt : ""} ${
        v.ativa ? "" : s.cartaoOff
      }`}
    >
      <div className={s.varTopo}>
        <span className={s.varId}>{v.id}</span>
        <span className={s.tagCat}>{v.categoria}</span>
        {!v.ativa && <span className={s.tagCat}>aposentada</span>}
        {v.peso_personalizado && <span className={s.tagCat}>peso editado</span>}
        {v.texto_personalizado && <span className={s.tagCat}>texto editado</span>}
      </div>

      <div className={s.varTexto} data-testid={`texto-${v.id}`}>{v.texto}</div>

      {/* Servidor antigo (sem edição de texto): segue o aviso de sempre. */}
      {!copies && v.texto_igual_a.length > 0 && (
        <div className={s.varAviso}>
          Mesmo texto de <strong>{v.texto_igual_a.join(", ")}</strong>: aqui se
          testa a categoria, não a copy.
        </div>
      )}
      <EditorDeCopy v={v} copies={copies} onSalvou={onSalvouTexto} />

      <div className={s.varNumeros}>
        {v.sem_dado ? (
          <span className={s.metricaSemDado}>sem dado</span>
        ) : (
          <>
            <span className={s.varTaxa}>{pct(v.taxa)}</span>
            <span className={s.metricaRotulo}>responderam</span>
            {v.amostra_pequena && (
              <span className={s.alerta} title={`menos de ${amostraMinima} envios`}>
                amostra pequena
              </span>
            )}
          </>
        )}
      </div>
      <div className={s.varDetalhe}>
        {v.sem_dado
          ? "nenhum disparo desta variante saiu no período."
          : `${v.enviados} receberam · ${v.respostas} responderam`}
        {!v.sem_dado && v.respostas_por_tipo && (
          <>
            {" "}
            ·{" "}
            {tiposDe(v.respostas_por_tipo, v.fora_da_taxa, v.respostas)
              .map((x) => `${x.rotulo} ${x.valor ?? "sem dado"}`)
              .join(", ")}
          </>
        )}
        {saiuEmOutra.length > 0 && (
          <>
            {" "}
            · saiu como{" "}
            {saiuEmOutra
              .map((x) => `${x.categoria} (${x.enviados})`)
              .join(", ")}{" "}
            quando a cota de marketing fechou
          </>
        )}
      </div>

      <div className={s.pesoLinha}>
        <span className={s.pesoRotulo}>fatia</span>
        {v.ativa ? (
          <>
            <input
              className={s.pesoInput}
              type="number"
              min={0}
              max={100}
              step={1}
              value={peso}
              disabled={salvando}
              onChange={(e) => onPeso(Number(e.target.value))}
              aria-label={`fatia da variante ${v.id} em %`}
            />
            <span className={s.pesoTravado}>
              % · padrão {v.peso_padrao}%
              {marketing ? " · fatia de categoria (cota da casa)" : ""}
            </span>
          </>
        ) : (
          <span className={s.pesoTravado}>
            0% — aposentada no código, não volta pela tela
          </span>
        )}
      </div>
    </div>
  );
}

// ── bloco do teste A/B: cartões + as duas travas + salvar ──
function TesteAB({
  met,
  cota,
  onSalvou,
}: {
  met: MetricasRemarketing;
  cota: number;
  onSalvou: () => void;
}) {
  const { gravar, restaurar, ocupado, erro: erroConfig } = useConfigOperacional();
  const [pesos, setPesos] = useState<Record<string, number>>({});
  const [erro, setErro] = useState<string | null>(null);

  // Os pesos vêm do backend; o estado local só existe entre abrir o campo e
  // salvar. Recarregou a métrica, volta a valer o que está gravado.
  useEffect(() => {
    const inicial: Record<string, number> = {};
    for (const v of met.variantes) inicial[v.id] = v.peso;
    setPesos(inicial);
    setErro(null);
  }, [met]);

  const ativas = met.variantes.filter((v) => v.ativa);
  const soma = ativas.reduce((acc, v) => acc + (pesos[v.id] ?? v.peso), 0);
  const somaMkt = ativas
    .filter((v) => v.categoria === "marketing")
    .reduce((acc, v) => acc + (pesos[v.id] ?? v.peso), 0);
  const mudou = met.variantes.some((v) => (pesos[v.id] ?? v.peso) !== v.peso);
  const personalizado = met.variantes.some((v) => v.peso_personalizado);

  // As MESMAS duas regras do backend, antecipadas. Quem manda é o servidor —
  // isto aqui só evita a viagem e explica antes do erro.
  const problemas: string[] = [];
  if (soma !== 100) problemas.push(`os pesos ativos somam ${soma}, não 100`);
  if (somaMkt !== cota)
    problemas.push(
      `a fatia em MARKETING está em ${somaMkt}% e tem que ficar em ${cota}%`,
    );

  const salvar = async () => {
    setErro(null);
    await gravar("carrossel_pesos", pesos);
    onSalvou();
  };

  const voltarAoPadrao = async () => {
    setErro(null);
    await restaurar("carrossel_pesos");
    onSalvou();
  };

  const salvando = ocupado === "carrossel_pesos";

  return (
    <div className={s.abBloco}>
      <h2 className={s.abTitulo}>Teste A/B das mensagens</h2>
      <p className={s.abDesc}>
        Cada lead fica sempre na mesma variante. Mudar a fatia no meio de uma
        rodada remexe os grupos.
      </p>
      {met.copies?.problema && (
        <div className={s.erro} data-testid="copy-problema">
          O disparo está parado por causa do texto gravado: {met.copies.problema}
        </div>
      )}

      <Recolhe titulo="Mensagens e fatias" dica="5 variantes">
        <div className={s.abGrid}>
          {met.variantes.map((v) => (
            <CartaoVariante
              key={v.id}
              v={v}
              peso={pesos[v.id] ?? v.peso}
              amostraMinima={met.amostra_minima}
              salvando={salvando}
              onPeso={(novo) => setPesos((p) => ({ ...p, [v.id]: novo }))}
              copies={met.copies}
              onSalvouTexto={onSalvou}
            />
          ))}
        </div>

        <div className={s.somaBarra}>
          <div className={s.somaTexto}>
            soma das ativas:{" "}
            <span className={soma === 100 ? s.somaOk : s.somaRuim}>{soma}%</span> ·
            em MARKETING:{" "}
            <span className={somaMkt === cota ? s.somaOk : s.somaRuim}>
              {somaMkt}%
            </span>{" "}
            (regra da casa: {cota}%)
            {problemas.length > 0 && (
              <>
                <br />
                {problemas.join(" · ")}
              </>
            )}
          </div>
          {personalizado && (
            <button
              type="button"
              className={s.btnRecomecar}
              onClick={() => void voltarAoPadrao()}
              disabled={salvando}
              title="Volta pros pesos escritos no código"
            >
              voltar ao padrão
            </button>
          )}
          <button
            type="button"
            className={s.btnSalvarPesos}
            onClick={() => void salvar()}
            disabled={salvando || !mudou || problemas.length > 0}
          >
            {salvando ? "salvando…" : "salvar as fatias"}
          </button>
        </div>
        {(erro || erroConfig) && <div className={s.erro}>{erro || erroConfig}</div>}
        <HistoricoDeCopy copies={met.copies} />
      </Recolhe>
    </div>
  );
}

// ── tabela comparativa (rola dentro do container, nunca na página) ──
function TabelaVariantes({ met }: { met: MetricasRemarketing }) {
  const linhas = [
    ...met.variantes.map((v) => ({
      ...v,
      chave: v.id,
      rotulo: `${v.id} · ${v.categoria}${v.ativa ? "" : " (aposentada)"}`,
      fatia: `${v.peso}%`,
    })),
    ...met.fora_do_teste.map((f) => ({
      ...f,
      chave: `fora-${f.variante}`,
      rotulo: `${f.variante} · fora do teste de hoje`,
      fatia: "—",
    })),
  ];

  return (
    <div className={s.tabelaWrap}>
      <table className={s.tabela}>
        <thead>
          <tr>
            <th>Variante</th>
            <th>Fatia</th>
            <th>Receberam</th>
            <th>Responderam</th>
            <th>Taxa</th>
          </tr>
        </thead>
        <tbody>
          {linhas.map((l) => (
            <tr key={l.chave}>
              <td>{l.rotulo}</td>
              <td>{l.fatia}</td>
              <td>
                {l.sem_dado ? <span className={s.celulaVazia}>sem dado</span> : l.enviados}
              </td>
              <td>{l.sem_dado ? <span className={s.celulaVazia}>—</span> : l.respostas}</td>
              <td className={s.taxaForte}>
                {l.sem_dado ? (
                  <span className={s.celulaVazia}>sem dado</span>
                ) : (
                  <>
                    {pct(l.taxa)}
                    {l.amostra_pequena && <span className={s.alerta}> amostra pequena</span>}
                  </>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ClienteRemarketing() {
  const [mapa, setMapa] = useState<Mapa | null>(null);
  const [met, setMet] = useState<MetricasRemarketing | null>(null);
  const [metErro, setMetErro] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [periodo, setPeriodo] = useState(30);
  const [recarga, setRecarga] = useState(0);
  const config = useConfigOperacional();

  const carregarMapa = useCallback(() => {
    fetch("/api/automacoes", { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMapa(body);
      })
      .catch((e) => setErro(String((e as Error).message || e)));
  }, []);

  useEffect(() => {
    carregarMapa();
  }, [carregarMapa]);

  useEffect(() => {
    setMet(null);
    setMetErro(null);
    fetch(`/api/automacoes/remarketing/metricas?dias=${periodo}`, { cache: "no-store" })
      .then(async (r) => {
        const body = await r.json();
        if (!r.ok) throw new Error(body?.error || body?.detail || `HTTP ${r.status}`);
        setMet(body);
      })
      .catch((e) => setMetErro(String((e as Error).message || e)));
  }, [periodo, recarga]);

  const car = mapa?.carrossel_corretor ?? null;
  const passos = useMemo(() => car?.quando_responde ?? [], [car]);

  // ids do canvas, na regra de HOJE do motor: fila → dispara → Disparado, e de
  // Disparado saem os quatro desfechos (interesse ou botão, recusa ou parar,
  // resposta automática, calado). Cada desfecho é uma coluna: o fio desce do
  // "Disparado" pra condição e da condição pro lugar onde o card vai parar.
  const edges: Edge[] = useMemo(() => {
    if (!car) return [];
    const es: Edge[] = corrente(["rmk-fila", "rmk-espera-ritmo", "rmk-disparo", "rmk-disparado"]);
    const desce = (from: string, to: string, tone: Edge["tone"]): Edge => ({
      from, to, kind: "branch", tone, dashed: true,
    });
    es.push(desce("rmk-disparado", "rmk-se-interesse", "warn"), desce("rmk-se-interesse", "rmk-respondeu", "warn"));
    es.push(desce("rmk-disparado", "rmk-se-nao", "base"), desce("rmk-se-nao", "rmk-saiu", "base"));
    es.push(desce("rmk-disparado", "rmk-se-auto", "muted"), desce("rmk-se-auto", "rmk-fica", "muted"));
    es.push(desce("rmk-disparado", "rmk-se-calado", "base"), desce("rmk-se-calado", "rmk-volta", "base"));
    return es;
  }, [car]);

  if (erro) {
    return (
      <div className="estado">
        <h2>Remarketing indisponível</h2>
        <p>Não consegui falar com o copiloto. Tenta de novo em instantes.</p>
        <div className="estado-meta">{erro}</div>
      </div>
    );
  }
  if (!mapa || !car) {
    return (
      <div className="estado">
        <h2>Carregando o remarketing…</h2>
      </div>
    );
  }

  const ritmo = car.ritmo;
  // Os nomes do desenho saem do que o motor mandou: a etapa de onde o disparo
  // puxa, o número que dispara e o destino de quem pede pra parar.
  const alvoCfg = car.alvo?.config ?? null;
  const etapaAlvo = alvoCfg?.etapas.find((e) => e.id === alvoCfg.etapa)?.nome ?? "Fila";
  const disparador = car.linha_disparadora?.numeros.find((n) => n.papel === "disparo") ?? null;
  const pararCfg = car.parar?.config ?? null;
  const destinoParar = pararCfg?.opcoes.find((o) => o.id === pararCfg.etapa)?.nome ?? "Venda perdida";
  const ativas = car.variantes.filter((v) => v.ativa);
  // O que acontece com quem responde, lido da MESMA configuração que o motor
  // usa pra transferir: quem vai, a tag, a etapa de entrada e a roleta.
  const rsp = car.responde?.config ?? null;
  const naRoleta = (rsp?.roleta?.corretoras ?? []).filter((x) => x.na_roleta).map((x) => x.nome);
  const todosVao = rsp?.quem_vai !== "interesse";

  return (
    <div>
      <div className={s.wrap}>
        <Link href="/automacoes" className={s.voltar}>
          ← todas as automações
        </Link>

        <Cabecalho gatilho={car.gatilho} dryRun={mapa.operacao.dry_run} />

        {/* ── O DISPARO: está ligado? quando? quanto já foi? a linha está boa? ──
            Substitui a antiga faixa de ritmo. Ela mostrava "cap configurado",
            que é o limite da CADÊNCIA da linha — nunca limitou o carrossel, e
            ao lado de "limite de hoje" parecia que sim. Quem manda no volume
            é a rampa, baixada (nunca subida) pelo campo "por dia" do painel. */}
        <PainelDisparo car={car} config={config} onSalvou={carregarMapa} />

        <h2 className={s.secaoTitulo} id="como-funciona-hoje">Como funciona hoje</h2>
        <p className={s.fluxoLegenda}>
          O caminho de um lead, lido do motor que dispara.
          <span className={s.fluxoArraste}> Arraste pro lado pra ver tudo.</span>
        </p>
      </div>

      {/* ── O DESENHO, sempre aberto (pedido da Dona, 05/10): "recebeu, vai pra
          tal lugar, conversa com tal coisa". É o MESMO canvas do mapa geral
          (Canvas.tsx). Fica fora do <details> porque o canvas mede os nós na
          montagem, e dentro de um <details> fechado mediria tudo zero. ── */}
      <div data-testid="desenho-do-fluxo">
      <Canvas edges={edges} zoomInicial={0.8} altura="auto">
        {(reg) => (
          <div className={s.flow}>
            <div className={s.row}>
              <Node reg={reg} id="rmk-fila" num={1} icon="⚡" titulo={`Etapa ${etapaAlvo}`} largura={LARGURA_NO}>
                <div>{car.gatilho}</div>
                <div className={s.notaRamo}>
                  Nunca recebe:
                  <ul className={s.listaNo}>
                    {car.travas.map((t) => (
                      <li key={t}>{t}</li>
                    ))}
                  </ul>
                </div>
              </Node>

              <Espera
                reg={reg}
                id="rmk-espera-ritmo"
                texto={`até ${ritmo.por_dia} por dia`}
              />

              <Node reg={reg} id="rmk-disparo" num={2} icon="💬" titulo="Dispara pela linha da Corretor" largura={LARGURA_NO}>
                <div>
                  {disparador
                    ? `Sai pelo ${disparador.telefone ?? disparador.phone_number_id}.`
                    : `${car.linha}. Hoje nenhum número está marcado pra disparar: não sai nada.`}
                </div>
                <div className={s.tagsNo}>
                  {ativas.map((v) => (
                    <span key={v.id} className={s.tagCat}>
                      {v.id} {v.peso}%
                    </span>
                  ))}
                </div>
                <div className={s.notaRamo}>O texto de cada variante está no teste A/B, abaixo.</div>
              </Node>

              <Node reg={reg} id="rmk-disparado" num={3} icon="📍" titulo="Etapa Disparado" largura={LARGURA_NO}>
                <div>O card fica aqui esperando a resposta. O que a pessoa fizer decide pra onde ele vai.</div>
              </Node>
            </div>

            <div className={s.faixaDesfechos}>O QUE A PESSOA FAZ DEPOIS</div>
            <div className={s.desfechos}>
              <div className={s.desfecho}>
                <Node
                  reg={reg}
                  id="rmk-se-interesse"
                  icon="🟢"
                  titulo={todosVao ? "Responde qualquer coisa" : "Clica no botão ou mostra interesse"}
                  cond
                  largura={LARGURA_NO}
                >
                  <div>
                    {todosVao
                      ? "Botão, interesse, “bom dia”, áudio. Na taxa de resposta só contam botão e interesse."
                      : "É o que conta na taxa de resposta."}
                  </div>
                </Node>
                <Node reg={reg} id="rmk-respondeu" num={4} icon="🤝" titulo="Etapa Respondeu, e daí a corretora" largura={LARGURA_NO}>
                  <div>Sai da roda na hora: ninguém mais dispara pra essa pessoa.</div>
                  {rsp && !rsp.problema && rsp.etapa && (
                    <div className={s.tagsNo} data-testid="desenho-responde">
                      <span className={s.tagCat}>tag {rsp.tag}</span>
                      <span className={s.tagCat}>etapa {rsp.etapa.valendo_nome}</span>
                      <span className={s.tagCat}>roleta: {naRoleta.join(", ")}</span>
                    </div>
                  )}
                  <ol className={s.listaNo}>
                    {passos.map((p) => (
                      <li key={p.passo}>
                        <strong>{p.quem}:</strong> {p.o_que}
                      </li>
                    ))}
                  </ol>
                  <div className={s.notaRamo}>Isso se troca em Ajustar, em “Quando a pessoa responde”.</div>
                </Node>
              </div>

              <div className={s.desfecho}>
                <Node reg={reg} id="rmk-se-nao" icon="✋" titulo="Recusa ou pede pra parar" cond largura={LARGURA_NO}>
                  <div>Disse que não quer, ou escreveu “parar”.</div>
                </Node>
                <Node reg={reg} id="rmk-saiu" num={5} icon="🚪" titulo="Sai da roda" largura={LARGURA_NO}>
                  <div>
                    Recusa vai pra <strong>Venda perdida</strong>. Quem pede pra parar vai pra{" "}
                    <strong>{destinoParar}</strong>
                    {destinoParar === "Venda perdida" ? " também" : ""}.
                  </div>
                  <div className={s.notaRamo}>O destino do “parar” se troca em Ajustar.</div>
                </Node>
              </div>

              <div className={s.desfecho}>
                <Node reg={reg} id="rmk-se-auto" icon="🤖" titulo="Resposta automática" cond largura={LARGURA_NO}>
                  <div>O WhatsApp da pessoa respondeu sozinho (“em breve retornaremos”).</div>
                </Node>
                <Node reg={reg} id="rmk-fica" num={6} icon="📍" titulo="Fica em Disparado" largura={LARGURA_NO}>
                  <div>Nada muda. Não conta como resposta e o card não sai do lugar.</div>
                </Node>
              </div>

              <div className={s.desfecho}>
                <Node reg={reg} id="rmk-se-calado" icon="🌙" titulo="Fica calado" cond largura={LARGURA_NO}>
                  <div>Não respondeu nada.</div>
                </Node>
                <Node reg={reg} id="rmk-volta" num={7} icon="🔄" titulo={`Volta pro fim da ${etapaAlvo}`} largura={LARGURA_NO}>
                  <div>
                    Descansa {ritmo.descanso_dias} dias em Disparado e volta pro fim da fila. Não recebe de novo no dia
                    seguinte.
                  </div>
                  <div className={s.tagsNo}>
                    <span className={s.tagFica}>a roda continua</span>
                  </div>
                </Node>
              </div>
            </div>
          </div>
        )}
      </Canvas>
      </div>

      <div className={s.wrap}>
        <h2 className={s.secaoTitulo}>Resultado</h2>
        <EntregaUltimoLote entrega={car.entrega_ultimo_lote} />

        <div className={s.periodo}>
          <span className={s.periodoRotulo}>período</span>
          {PERIODOS.map((d) => (
            <button
              key={d}
              type="button"
              className={`${s.pill} ${periodo === d ? s.pillOn : ""}`}
              onClick={() => setPeriodo(d)}
            >
              {d} dias
            </button>
          ))}
        </div>

        {metErro && <div className={s.erro}>Não consegui ler os números: {metErro}</div>}

        {met && (
          <>
            <MarcoBarra met={met} />

            <div className={s.resumo}>
              <div className={s.stat}>
                <div className={s.statLabel}>disparos no período</div>
                <div className={s.statValor}>{met.total.enviados}</div>
                <div className={s.statSub}>
                  {met.medindo_desde
                    ? `desde ${dataCurta(met.medindo_desde)}`
                    : `nos últimos ${met.dias} dias`}
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>responderam com interesse</div>
                <div className={s.statValor}>{met.total.respostas}</div>
                <div className={s.statSub}>
                  interesse ou clique no botão · um lead conta uma vez
                </div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>taxa de resposta</div>
                <div className={met.total.sem_dado ? s.semDado : s.statValor}>
                  {pct(met.total.taxa)}
                </div>
                <div className={s.statSub}>
                  {met.total.sem_dado
                    ? "nenhum disparo saiu no período"
                    : met.total.amostra_pequena
                      ? `amostra pequena (< ${met.amostra_minima} envios)`
                      : "interesse e botão ÷ disparos"}
                </div>
              </div>
            </div>

            {/* O "fora da taxa" que morava numa linha embaixo de "responderam"
                virou este bloco: os seis tipos, com os que contam separados. */}
            {(met.total.respostas_por_tipo || met.total.fora_da_taxa) && <RespostasPorTipoBloco met={met} />}

            {/* ── ESTOQUE DAS ETAPAS ──
                É o que está parado AGORA em cada etapa, não o que aconteceu no
                período. Fica em caixas separadas das de cima de propósito:
                somar "fila" com "disparos no período" seria somar foto com
                filme. */}
            <div className={s.resumo}>
              <div className={s.stat}>
                <div className={s.statLabel}>na fila</div>
                <div className={s.statValor}>{numero(met.estoque.fila)}</div>
                <div className={s.statSub}>esperando a vez agora</div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>disparado</div>
                <div className={s.statValor}>{numero(met.estoque.disparado)}</div>
                <div className={s.statSub}>recebeu e está descansando</div>
              </div>
              <div className={s.stat}>
                <div className={s.statLabel}>respondeu</div>
                <div className={s.statValor}>{numero(met.estoque.respondeu)}</div>
                <div className={s.statSub}>saiu da roda e foi pro atendimento</div>
              </div>
            </div>

            <TesteAB
              met={met}
              cota={met.cota_marketing_pct}
              onSalvou={() => {
                setRecarga((n) => n + 1);
                carregarMapa();
              }}
            />

            <TabelaVariantes met={met} />
          </>
        )}

        <details className={s.comoFunciona}>
          <summary>Mais sobre o funcionamento</summary>
          <div className={s.nota}>
            Quem sai da fila recebe, descansa {ritmo.descanso_dias} dias e{" "}
            <strong>volta pro fim da fila</strong>. Não é campanha com início e fim.
          </div>
          <div className={s.rodape}>
            {met?.definicao}
            <br />
            Fluxo lido do motor que roda de verdade — variantes, travas, limite do
            dia e passos do atendimento saem do código que dispara. A tela grava
            o interruptor, a agenda do disparo (horário, dias, limite por dia e
            por semana), o peso e o texto das variantes e o que acontece com
            quem responde — e tudo é validado no servidor, não aqui.
          </div>
        </details>
      </div>
    </div>
  );
}
