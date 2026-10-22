// O ESTADO DE CADA AUTOMAÇÃO — uma função por automação, usada pela página dela
// e pela lista. A lista dizer "agindo" e a página dizer "parada" seria a pior
// mentira possível, então as duas leem daqui.
//
// Só entra como TRAVA ("não sai") o que o motor de fato confere antes de mandar:
//   - o interruptor da automação;
//   - o modo simulação (DRY_RUN) do servidor;
//   - o relógio do motor (timer do systemd) desligado, pra quem roda nele;
//   - o limite por dia zerado (a cadência e a nutrição contam no mesmo limite);
//   - na boas-vindas, roleta sem ninguém com linha Cloud.
// Linha vermelha na Meta NÃO é trava aqui: a régua da cadência só segura a linha
// vermelha onde há rampa configurada, e a tela não afirma o que não sabe. Ela
// aparece como alerta na frase e em vermelho na resposta "por qual linha".
//
// O SERVIDOR DECIDE (06/10, junção com `resumo_da_lista`): o mapa traz `resumo[]`
// com o estado de cada automação (indisponível / desligada / parada / agindo /
// ligada) e o motivo. Ele sabe o que a tela não sabe (interruptor por corretora,
// mapa ilegível, fim de semana, roleta que o intake de fato usa). Quando a conta
// daqui e a dele divergem, vale a dele (`conciliar`). Uma exceção só: trava que
// o resumo do servidor não confere (modo simulação, relógio do motor parado,
// limite por dia zerado) segura de verdade, e aí a tela continua dizendo
// "parada" com o motivo dela mesmo que o servidor diga "ligada".

import type { PayloadConfig } from "./configOperacional";
import {
  frasesDasLinhas,
  horaCurta,
  linhaDaCorretora,
  linhaDoServidor,
  linhaPorSlug,
  lerChave,
  situacaoDe,
  type LinhaDaCorretora,
  type Situacao,
  type Tom,
} from "./painel";
import type { BlocoFollowUp, BlocoNutricao, Mapa, ResumoAutomacao } from "./types";

// ── o que o servidor decidiu ──
const TOM_DO_SERVIDOR: Record<ResumoAutomacao["estado"], Tom> = {
  indisponivel: "sem",
  desligada: "desligado",
  parada: "parado",
  agindo: "saindo",
  ligada: "ligado",
};

export function resumoDe(mapa: Mapa, id: ResumoAutomacao["id"]): ResumoAutomacao | null {
  return mapa.resumo?.find((r) => r.id === id) ?? null;
}

const maiuscula = (t: string) => (t ? t[0].toUpperCase() + t.slice(1) : t);

/**
 * O estado final: o da tela quando bate com o do servidor (a frase daqui é
 * mais rica), o do servidor quando diverge. `travaDaTela` = trava que o resumo
 * do servidor não confere (simulação, relógio, limite zero): essa segura.
 */
export function conciliar(
  sit: Situacao,
  item: ResumoAutomacao | null,
  o: { travaDaTela: boolean; masculino?: boolean; alerta?: string | null },
): Situacao & { servidor?: ResumoAutomacao["estado"] } {
  if (!item) return sit;
  const tom = TOM_DO_SERVIDOR[item.estado];
  if (!tom || tom === sit.tom) return { ...sit, servidor: item.estado };
  if (o.travaDaTela && sit.tom === "parado" && (tom === "ligado" || tom === "saindo")) {
    return { ...sit, servidor: item.estado };
  }
  const a = o.masculino ? "o" : "a";
  const extra = o.alerta ? ` Atenção: ${o.alerta}.` : "";
  const motivo = item.motivo ? `${maiuscula(item.motivo)}.` : "";
  const quando = horaCurta(item.ultima_acao_em);
  const porTom: Record<Tom, Situacao> = {
    sem: { tom, titulo: "Sem leitura", frase: `Não consegui ler se está ligad${a}. Enquanto isso o motor não envia.` },
    desligado: { tom, titulo: `Desligad${a}`, frase: `Nada sai sozinho.${motivo && motivo !== "Interruptor desligado." ? ` ${motivo}` : ""}` },
    parado: { tom, titulo: `Ligad${a}, mas parad${a}`, frase: `Não sai: ${item.motivo ?? "o servidor está segurando"}.${extra}` },
    saindo: { tom, titulo: "Agindo", frase: `Ligad${a} e mandando${quando ? `: último envio às ${quando}` : " hoje"}.${extra}` },
    ligado: { tom, titulo: `Ligad${a}`, frase: `Nada saiu hoje ainda.${extra}` },
  };
  return { ...porTom[tom], servidor: item.estado };
}

/** O bloco por corretora, só quando o servidor conseguiu montar. */
export function blocoFollowUp(mapa: Mapa): BlocoFollowUp | null {
  const b = mapa.follow_up;
  return b && !b.indisponivel ? b : null;
}
export function blocoNutricao(mapa: Mapa): BlocoNutricao | null {
  const b = mapa.nutricao.por_corretora;
  return b && !b.indisponivel ? b : null;
}

const plural = (n: number, um: string, varios: string) => `${n.toLocaleString("pt-BR")} ${n === 1 ? um : varios}`;

/** Travas do motor que valem pra toda automação que roda no relógio. */
function travasDoMotor(mapa: Mapa): string | null {
  if (mapa.operacao.dry_run) return "o servidor está em modo simulação, nada chega no WhatsApp";
  if (mapa.operacao.timer.enabled === false) return "o relógio do motor está desligado no servidor";
  return null;
}

/** Limite por dia de uma corretora, do jeito que o motor lê (próprio > padrão). */
export function capDaCorretora(cfg: PayloadConfig | null, mapa: Mapa, corretoraId: number): number {
  const proprio = (cfg?.config.cadencia_cap_por_corretora?.valor ?? {}) as Record<string, number>;
  const padrao = Number(cfg?.config.cadencia_cap_diario?.valor ?? mapa.operacao.cap_diario);
  return proprio[String(corretoraId)] ?? padrao;
}

export function linhasDoCadastro(mapa: Mapa): LinhaDaCorretora[] {
  const numeros = mapa.carrossel_corretor?.linha_disparadora?.numeros;
  return (mapa.boas_vindas.por_corretora ?? []).map((c) => linhaDaCorretora(c, numeros));
}

// ── boas-vindas ──
export function estadoBoasVindas(mapa: Mapa, cfg: PayloadConfig | null) {
  const bv = mapa.boas_vindas;
  const numeros = mapa.carrossel_corretor?.linha_disparadora?.numeros;
  const rol = bv.roleta ?? null;
  // A roleta que VALE é a que o intake usa (`valendo`, só corretora ativa), lida
  // pelo servidor. Sem ela (servidor antigo), a gravada na configuração.
  const roleta = (rol?.valendo ??
    (cfg?.config.roleta_corretoras?.valor as number[] | undefined) ??
    (bv.por_corretora ?? []).filter((c) => c.na_roleta).map((c) => c.corretora_id)) as number[];
  const naRoleta = roleta
    .map((id) => {
      const c = (bv.por_corretora ?? []).find((x) => x.corretora_id === id);
      const r = rol?.corretoras.find((x) => x.corretora_id === id);
      return c ?? (r ? { corretora_id: id, label: r.label, nome: r.nome } : null);
    })
    .filter((c): c is NonNullable<typeof c> => Boolean(c));
  const linhas = naRoleta.map((c) => {
    const r = rol?.corretoras.find((x) => x.corretora_id === c.corretora_id);
    if (r) return linhaDoServidor(r.label, r.linha);
    return "tem_linha_cloud" in c ? linhaDaCorretora(c, numeros) : linhaDoServidor(c.label, null);
  });
  const comLinha = linhas.filter((l) => l.temLinha);

  let trava: string | null = null;
  if (!bv.disponivel) trava = "o servidor não tem o módulo de boas-vindas";
  else if (mapa.operacao.dry_run) trava = "o servidor está em modo simulação, nada chega no WhatsApp";
  else if (roleta.length === 0) trava = "ninguém está na roleta";
  else if (bv.por_corretora && comLinha.length === 0) trava = "ninguém na roleta tem linha Cloud";

  const ligadoCfg = cfg ? lerChave(cfg.config.lead_intake_ativo) : undefined;
  const ligado = ligadoCfg === undefined ? bv.estado === "ligado" : ligadoCfg;
  const alerta = frasesDasLinhas(linhas);
  const sit = conciliar(
    situacaoDe({
      ligado,
      trava,
      saude: mapa.saude_envio?.["boas-vindas"],
      naFila: bv.aguardando ?? null,
      aoLigar: "o próximo lead que cair em Novo recebe a mensagem pela linha da dona",
      esperando: "Esperando lead novo cair em Novo.",
      alerta,
    }),
    resumoDe(mapa, "boas-vindas"),
    { travaDaTela: Boolean(trava) && mapa.operacao.dry_run, alerta },
  );
  return { sit, linhas, naRoleta, roleta, rol, ligado, trava };
}

// ── follow-up ──
export function estadoFollowUp(mapa: Mapa, cfg: PayloadConfig | null) {
  const bloco = blocoFollowUp(mapa);
  // Com o bloco por corretora, a linha de cada uma vem do servidor, e a
  // corretora com o follow-up desligado aparece riscada ("desligada").
  const linhas = bloco
    ? bloco.corretoras.map((c) => linhaDoServidor(c.label, c.linha, c.ligada === false))
    : linhasDoCadastro(mapa);
  const naRegua = mapa.operacao.matriculados.por_status.ativa ?? 0;
  const corretoras = mapa.boas_vindas.por_corretora ?? [];
  const caps = corretoras.filter((c) => c.tem_linha_cloud).map((c) => capDaCorretora(cfg, mapa, c.corretora_id));
  let trava = travasDoMotor(mapa);
  if (!trava && caps.length > 0 && caps.every((c) => c === 0)) trava = "o limite por dia é zero em todas as linhas";

  const ligadoCfg = cfg ? lerChave(cfg.config.cadencia_ativa) : undefined;
  const ligado = ligadoCfg === undefined ? mapa.operacao.cadencia_ativa : ligadoCfg;
  const alerta = frasesDasLinhas(linhas);
  const sitTela = situacaoDe({
    ligado,
    trava,
    saude: mapa.saude_envio?.["follow-up"],
    naFila: naRegua,
    masculino: true,
    aoLigar:
      naRegua > 0
        ? `${plural(naRegua, "lead na régua volta", "leads na régua voltam")} a receber no próximo horário do motor`
        : "começa a mandar pra quem a corretora arrastar pro Follow-up",
    esperando:
      naRegua > 0
        ? `${plural(naRegua, "lead na régua", "leads na régua")}; cada toque sai no dia dele.`
        : "Ninguém na régua: entra quem a corretora arrasta pro Follow-up.",
    alerta,
  });
  const sit = conciliar(sitTela, resumoDe(mapa, "follow-up"), { travaDaTela: Boolean(trava), masculino: true, alerta });
  return { sit, linhas, naRegua, ligado, trava, bloco };
}

// ── nutrição ──
//
// Desde 07/10 o marco sai pela linha da corretora DONA do lead quando ela tem
// linha e modelo aprovado; senão pela da Pessoa12 (`nutricao.linha` = a da Pessoa12, a de
// reserva). O servidor diz por corretora (`por_corretora.corretoras[].sai_por`)
// e se todas saem pela Pessoa12 (`toda_pela_corretor`). Servidor antigo: tudo pela Pessoa12, e o
// slug fixo aqui só vale pra ele.
export const SLUG_NUTRICAO = "corretor";

export function estadoNutricao(mapa: Mapa, cfg: PayloadConfig | null, naFila: number | null) {
  const numeros = mapa.carrossel_corretor?.linha_disparadora?.numeros;
  const bloco = blocoNutricao(mapa);
  const idDaLinha = mapa.nutricao.linha_corretora_id ?? null;
  const corretor =
    (mapa.boas_vindas.por_corretora ?? []).find((c) =>
      idDaLinha !== null ? c.corretora_id === idDaLinha : c.label.toLowerCase() === SLUG_NUTRICAO,
    ) ?? null;
  const n = mapa.nutricao.linha ?? null;
  const num = linhaPorSlug(n?.slug ?? SLUG_NUTRICAO, numeros);
  const nome = corretor?.label ?? "Pessoa12";
  const linha: LinhaDaCorretora =
    n && "registrada" in n
      ? linhaDoServidor(nome, n as Parameters<typeof linhaDoServidor>[1])
      : {
          nome,
          telefone: n?.telefone ?? num?.telefone ?? null,
          saude: n?.saude ?? num?.saude ?? { cor: "desconhecida" },
          temLinha: true,
        };
  const cap = corretor ? capDaCorretora(cfg, mapa, corretor.corretora_id) : null;
  let trava = travasDoMotor(mapa);
  if (!trava && cap === 0) trava = `o limite por dia da linha da ${linha.nome} é zero`;

  const ligadoCfg = cfg ? lerChave(cfg.config.nutricao_ativa) : undefined;
  const ligado = ligadoCfg === undefined ? mapa.nutricao.estado === "ligado" : ligadoCfg;
  const sitTela = situacaoDe({
    ligado,
    trava,
    saude: mapa.saude_envio?.nutricao,
    naFila,
    aoLigar:
      naFila && naFila > 0
        ? `os ${plural(naFila, "lead", "leads")} na nutrição voltam a receber no marco de cada um`
        : "o próximo marco de cada lead sai no dia dele",
    esperando:
      naFila === null
        ? "Esperando o próximo marco."
        : naFila > 0
          ? `${plural(naFila, "lead", "leads")} na nutrição; cada marco sai no dia dele.`
          : "Ninguém na nutrição: entra quem esgota o follow-up sem responder.",
    alerta: frasesDasLinhas([linha]),
  });
  const sit = conciliar(sitTela, resumoDe(mapa, "nutricao"), {
    travaDaTela: Boolean(trava),
    alerta: frasesDasLinhas([linha]),
  });
  // Por onde os marcos saem de fato: uma linha por saída distinta (07/10).
  const todaPelaGi = bloco ? (bloco.toda_pela_corretor ?? true) : true;
  const linhasDeSaida: LinhaDaCorretora[] = [];
  if (bloco && !todaPelaGi) {
    const vistos = new Set<string>();
    for (const c of bloco.corretoras) {
      if (!c.sai_por || vistos.has(c.sai_por)) continue;
      vistos.add(c.sai_por);
      const dona = bloco.corretoras.find((x) => x.linha_da_corretora === c.sai_por);
      linhasDeSaida.push(linhaDoServidor(dona?.label ?? c.sai_por, c.linha));
    }
  }
  return { sit, linha, cap, corretor, ligado, trava, bloco, todaPelaGi, linhasDeSaida: todaPelaGi ? [linha] : linhasDeSaida };
}

// ── nome de uma linha pelo slug (a lista recebe só o slug no `resumo`) ──
export function nomeDaLinha(mapa: Mapa, slug: string): string {
  const fu = blocoFollowUp(mapa)?.corretoras.find((c) => c.sai_por === slug);
  if (fu) return fu.label;
  const rol = mapa.boas_vindas.roleta?.corretoras.find((c) => c.linha?.slug === slug);
  if (rol) return rol.label;
  const env = mapa.boas_vindas.envios?.corretoras.find((c) => c.sai_por === slug);
  if (env) return env.label;
  return slug ? slug[0].toUpperCase() + slug.slice(1) : "linha";
}
