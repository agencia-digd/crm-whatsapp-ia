"use client";

// PAINEL DO DISPARO — a parte da tela de remarketing que a Dona opera.
//
// Front novo de 05/10/2026 ("está confuso e horrível lá"). A tela responde,
// nesta ordem e sem rolar:
//   1. está ligado? sai mensagem agora? se não, por quê (uma frase);
//   2. de qual número sai e a saúde dele na Meta, em cor — sempre visível;
//   3. pra quem (etapa e quantos leads de verdade);
//   4. quando (dias e horário);
//   5. quanto (por dia, por semana, e quanto já saiu).
// Embaixo, os mesmos quatro assuntos viram o formulário de ajuste, com uma
// barra de salvar que só aparece quando há mudança — e que diz, sempre, que
// salvar não liga. Ligar é o interruptor do topo, com confirmação.
//
// O que a tela NÃO faz, de propósito (vale desde a versão anterior):
//   - Não valida sozinha. Horário torto, dia fora de 0–6 e limite negativo são
//     recusados pelo servidor (422 em português) — aqui só se mostra a resposta.
//   - Não deixa o "por dia" passar da rampa: o número é um PEDIDO, vale o menor
//     entre ele e o teto da rampa. A tela mostra o pedido e o que VALE hoje.
//   - Não escolhe número sozinha. Sem disparador não sai nada, e a tela diz.
//   - Não pinta a linha de verde sem leitura: sem resposta da Meta a bolinha é
//     cinza e diz "sem leitura"; leitura velha aparece com a hora dela.
//
// Campo vazio = sem regra própria (apaga a chave e volta ao comportamento de
// antes). Não existe "0 = sem limite": zero é zero, e para o disparo.
//
// Contrato (junção com o motor, 05/10/2026): o mapa manda os números com a
// saúde de cada um, o dia de hoje (aceito × entregue), a última volta do robô,
// o fechamento do conferente e as reservas sem disparo. Desta tela só aparece
// o número do DISPARO (pedido da Dona, 05/10): os das corretoras são de
// atendimento e não entram no seletor. A agenda é salva num
// pedido só (`remarketing/agenda`, tudo ou nada) e a troca de disparador
// também (`pausar_os_outros`). O que o servidor não leu chega marcado como
// indisponível, e a tela escreve "não consegui ler": nunca zero, nunca verde.

import { useEffect, useMemo, useState } from "react";
import type { Chave, useConfigOperacional } from "../configOperacional";
import Interruptor from "../Interruptor";
import type { Mapa, NumeroDoCliente, SaudeDoNumero } from "../types";
import { Barra, Bolinha, diaDoInstante, horaCurta, mil, saudeEmTexto } from "../painel";
import QuandoResponde from "./Responde";
import Recolhe from "./Recolhe";
import s from "./disparo.module.css";

type Config = ReturnType<typeof useConfigOperacional>;
type Carrossel = Mapa["carrossel_corretor"];

// Saúde por número: o servidor lê a de todos os que este sistema opera, com a
// linha pausada inclusive (é com ela pausada que se decide se pode voltar).
type Numero = NumeroDoCliente;

// Acima disto a linha nova (sem reputação) só sobe com GREEN na Meta.
const DEGRAU_BASE = 80;

const DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"];

const NOME_PAPEL: Record<string, string> = {
  disparo: "disparador",
  atendimento: "atendimento",
  pausado: "pausado",
};

function diasEmTexto(dias: number[]): string {
  if (dias.length === 0 || dias.length === 7) return "todo dia";
  const ord = [...dias].sort();
  if (ord.join() === "0,1,2,3,4") return "seg a sex";
  if (ord.join() === "0,1,2,3,4,5") return "seg a sáb";
  return ord.map((d) => DIAS[d]).join(", ");
}

const telefoneCurto = (t: string | null | undefined) =>
  t ? t.replace(/^\+55\s*15\s*/, "") : null;

// "2026-10-04" → "04/10". Dia de São Paulo, já vem pronto do servidor.
const diaCurto = (dia: string) => dia.slice(0, 10).split("-").reverse().slice(0, 2).join("/");

// ── o que impede a mensagem de sair AGORA, independente do interruptor ──
//
// A ordem é a ordem em que o tick desiste: número → etapa → agenda → linha
// vermelha / rampa → limites. Com o interruptor desligado a tela usa a mesma
// frase como "se ligar agora, não sai: …" — ligar no escuro é o erro que esta
// tela existe pra evitar.
export function travaAgora(car: Carrossel): string | null {
  const linha = car.linha_disparadora;
  if (linha?.indisponivel) return "não consegui ler o cadastro de números";
  if (linha && !linha.slug) return linha.motivo_texto ?? "nenhum número é disparador";
  const alvo = car.alvo?.config;
  if (car.alvo?.indisponivel) return "não consegui ler a etapa do disparo";
  if (alvo?.invalido) return "a etapa gravada pro disparo não serve";
  const ag = car.agenda?.config;
  if (car.agenda?.indisponivel) return "não consegui ler a agenda do disparo";
  if (ag?.ilegivel) return "a agenda gravada está ilegível";
  if (ag?.bloqueio_agora && ag.bloqueio_agora !== "cap_semanal_atingido") {
    return ag.bloqueio_agora_texto ?? "fora do dia ou do horário";
  }
  if (car.saude_linha?.cor === "vermelho") {
    return "a linha está vermelha na Meta, o limite de hoje é zero";
  }
  if (car.ritmo.por_dia <= 0) return car.ritmo.motivo_rampa ?? "o limite de hoje é zero";
  if (ag?.bloqueio_agora === "cap_semanal_atingido") return "o limite da semana já foi usado";
  const hoje = car.ritmo.disparos_hoje;
  if (hoje !== null && hoje >= car.ritmo.por_dia) return "o limite de hoje já foi usado";
  return null;
}

// ── o robô rodou? ──
//
// "Saindo" olhava o interruptor e as travas, nunca se o robô rodou: com o timer
// morto a tela dizia "Saindo" sem sair nada. O robô acorda de 3 em 3 minutos
// dentro do horário do timer; 15 minutos sem volta registrada, dentro do
// horário, é robô parado. Fora do horário ele não roda mesmo, e não é defeito.
const MINUTOS_PRA_ROBO_PARADO = 15;

const minutosDe = (hhmm: string) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

function roboParado(car: Carrossel, agora: Date = new Date()): string | null {
  // Servidor antigo (não manda a volta) ou leitura que falhou: não afirma nada.
  if (car.ultima_volta === undefined || car.ultima_volta_indisponivel) return null;
  const ag = car.agenda?.config;
  if (ag?.bloqueio_agora === "fora_dos_dias" || ag?.bloqueio_agora === "fora_da_janela") return null;
  const [inicio, fim] = (ag?.timer ?? "09:00-19:00").split("-").map(minutosDe);
  const sp = agora.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", timeZone: "America/Sao_Paulo" });
  const minutos = minutosDe(sp);
  // Nos primeiros minutos do dia a última volta é a de ontem: ainda não é parada.
  if (minutos < inicio + MINUTOS_PRA_ROBO_PARADO || minutos >= fim) return null;
  const em = car.ultima_volta?.em;
  if (!em) return "o robô ainda não rodou";
  if (agora.getTime() - new Date(em).getTime() <= MINUTOS_PRA_ROBO_PARADO * 60_000) return null;
  const dia = diaDoInstante(em);
  const hoje = diaDoInstante(agora.toISOString());
  return `o robô não roda desde ${horaCurta(em)}${dia !== hoje ? ` de ${dia}` : ""}`;
}

type Situacao = { tom: "saindo" | "parado" | "desligado" | "sem"; titulo: string; frase: string };

export function situacao(car: Carrossel, ligado: boolean | null): Situacao {
  const trava = travaAgora(car);
  if (ligado === null) {
    return {
      tom: "sem",
      titulo: "Sem leitura",
      frase: "Não consegui ler se o disparo está ligado. Enquanto isso o robô não envia.",
    };
  }
  if (!ligado) {
    return {
      tom: "desligado",
      titulo: "Desligado",
      frase: trava
        ? `Nada sai sozinho. Se ligar agora, ainda não sai: ${trava}.`
        : "Nada sai sozinho. Se ligar agora, começa a sair no próximo horário.",
    };
  }
  const robo = roboParado(car);
  if (trava) {
    return {
      tom: "parado",
      titulo: "Ligado, mas parado",
      frase: `Não sai: ${trava}.${robo ? ` E ${robo}.` : ""}`,
    };
  }
  if (robo) {
    return {
      tom: "parado",
      titulo: "Ligado, mas parado",
      frase: `Não sai: ${robo}.`,
    };
  }
  return { tom: "saindo", titulo: "Saindo", frase: "Ligado e mandando dentro do ritmo." };
}

// Saúde que a tela mostra pra um número: a do próprio número; se o servidor
// for antigo e não mandar, a do topo (`saude_linha`) quando é dele; senão "sem
// leitura". Nunca verde por falta de dado.
function saudeDe(n: Numero, car: Carrossel): SaudeDoNumero {
  if (n.saude) return n.saude;
  const sl = car.saude_linha;
  if (sl && (sl.phone_number_id === n.phone_number_id || sl.slug === n.slug)) {
    return { cor: sl.cor, qualidade: sl.qualidade, lido_em: sl.lido_em, velha: sl.velha, erro: sl.erro };
  }
  return { cor: "desconhecida" };
}

// ── o topo: estado, interruptor e as quatro respostas ──
function Estado({
  car,
  config,
}: {
  car: Carrossel;
  config: Config;
}) {
  const { dados, erro, ocupado, gravar, restaurar } = config;
  const estado = dados?.config.carrossel_corretor_ativo;
  // Chave ilegível vem com `valor: null`. Null não é desligado: é "não sei".
  const ilegivel = Boolean(estado?.indisponivel) || car.estado === "indisponivel";
  const ligado = estado && !ilegivel ? Boolean(estado.valor) : null;
  const sit = situacao(car, ligado);

  const linha = car.linha_disparadora;
  const numeros = (linha?.numeros ?? []) as Numero[];
  const disparador = numeros.find((n) => n.papel === "disparo") ?? null;
  // Sem disparador, a saúde do topo é a da linha que PODERIA disparar (a da
  // casa, pausada). O número grande continua dizendo "nenhum número": mostrar
  // o telefone dela ali seria dizer que sai por ela.
  const daCasa = disparador
    ? null
    : (numeros.find(
        (n) => car.saude_linha && (n.slug === car.saude_linha.slug || n.phone_number_id === car.saude_linha.phone_number_id),
      ) ?? null);
  const saude: SaudeDoNumero = disparador
    ? saudeDe(disparador, car)
    : daCasa
      ? saudeDe(daCasa, car)
      : car.saude_linha
        ? { cor: car.saude_linha.cor, qualidade: car.saude_linha.qualidade, lido_em: car.saude_linha.lido_em, velha: car.saude_linha.velha }
        : { cor: "desconhecida" };
  const numeroTexto = disparador?.telefone ?? null;
  const donoDaSaude = disparador
    ? disparador.nome
    : daCasa
      ? `${telefoneCurto(daCasa.telefone) ?? daCasa.nome ?? "linha da casa"}, ${NOME_PAPEL[daCasa.papel] ?? daCasa.papel}`
      : null;

  const alvo = car.alvo?.config ?? null;
  const etapa = alvo?.etapas.find((e) => e.id === alvo.etapa) ?? null;
  const ag = car.agenda?.config ?? null;
  const ritmo = car.ritmo;
  const semana = ag?.enviados_semana ?? null;

  // A frase do confirm leva o número, a etapa e o teto — é o que vai sair.
  // Se algo segura o envio agora (linha vermelha, teto, horário), o confirm diz:
  // "até 0 mensagens por dia" sozinho não explica por que nada vai sair.
  const travaDeAgora = travaAgora(car);
  const confirmarLigar = disparador
    ? `Ligar o remarketing?\n\nSai pelo ${disparador.telefone ?? disparador.phone_number_id}` +
      ` (${disparador.nome ?? "sem nome"}), até ${ritmo.por_dia} mensagens por dia` +
      (etapa ? `, para ${mil(etapa.leads_do_dono)} leads da etapa ${etapa.nome}.` : ".") +
      (travaDeAgora ? `\n\nAgora ainda não sai: ${travaDeAgora}.` : "")
    : "Ligar o remarketing?\n\nHoje nenhum número é disparador: fica ligado, mas não sai nada até marcar um.";

  return (
    <section className={`${s.estado} ${s[`tom_${sit.tom}`]}`} aria-label="Estado do disparo">
      <div className={s.estadoTopo}>
        <div className={s.estadoTexto}>
          <h2 className={s.estadoTitulo} data-testid="estado-titulo">
            <span className={s.estadoPonto} aria-hidden />
            {sit.titulo}
          </h2>
          <p className={s.estadoFrase} data-testid="estado-frase">
            {sit.frase}
          </p>
        </div>
        <div className={s.estadoChave}>
          {ligado === null ? (
            <span className={s.dica}>{ilegivel || erro ? "sem leitura" : "carregando…"}</span>
          ) : (
            <>
              <Interruptor
                grande
                ativa={ligado}
                ocupado={ocupado === "carrossel_corretor_ativo"}
                rotulo={ligado ? "Desligar o remarketing" : "Ligar o remarketing"}
                confirmarLigar={confirmarLigar}
                confirmarDesligar={`Desligar o remarketing${disparador?.telefone ? ` (${disparador.telefone})` : ""}? Nada mais sai sozinho.`}
                onMudar={(novo) => void gravar("carrossel_corretor_ativo", novo)}
              />
              <span className={s.chaveRotulo}>{ligado ? "ligado" : "desligado"}</span>
            </>
          )}
        </div>
      </div>

      <dl className={s.respostas}>
        <div className={`${s.resposta} ${saude.cor === "vermelho" ? s.respostaRuim : ""}`} data-testid="resposta-numero">
          <dt>de onde sai</dt>
          <dd className={s.respostaValor}>
            {numeroTexto ? telefoneCurto(numeroTexto) : "nenhum número"}
          </dd>
          <dd className={s.saudeLinha} data-testid="saude-do-topo" data-cor={saude.cor}>
            <Bolinha cor={saude.cor} />
            <span>
              {saudeEmTexto(saude)}
              {donoDaSaude ? ` · ${donoDaSaude}` : ""}
            </span>
          </dd>
        </div>

        <div className={s.resposta} data-testid="resposta-quem">
          <dt>pra quem</dt>
          <dd className={s.respostaValor}>{etapa ? mil(etapa.leads_do_dono) : "—"}</dd>
          <dd className={s.respostaSub}>
            {etapa
              ? `leads ${alvo?.dono_nome ? `da ${alvo.dono_nome} ` : ""}na ${etapa.nome}`
              : car.alvo?.indisponivel
                ? "não consegui ler a etapa"
                : "nenhuma etapa escolhida"}
          </dd>
        </div>

        <div className={s.resposta} data-testid="resposta-quando">
          <dt>quando</dt>
          <dd className={s.respostaValor}>{ag ? diasEmTexto(ag.dias_semana) : "—"}</dd>
          <dd className={s.respostaSub}>
            {ag?.janela_horario
              ? ag.janela_horario.replace("-", " às ")
              : `${(ag?.timer ?? "09:00-19:00").replace("-", " às ")}`}
          </dd>
        </div>

        <div className={s.resposta} data-testid="resposta-quanto">
          <dt>quanto</dt>
          <dd className={s.respostaValor}>
            {ritmo.disparos_hoje === null ? "—" : ritmo.disparos_hoje}
            <span className={s.de}>
              {ritmo.por_dia > 0 ? ` / ${ritmo.por_dia} hoje` : " hoje · limite zero"}
            </span>
          </dd>
          {ritmo.disparos_hoje !== null && ritmo.por_dia > 0 && (
            <dd>
              <Barra valor={ritmo.disparos_hoje} de={ritmo.por_dia} />
            </dd>
          )}
          <dd className={s.respostaSub}>
            {semana === null ? "semana sem leitura" : `${mil(semana)}`}
            {ag?.cap_semanal != null ? ` / ${mil(ag.cap_semanal)} na semana` : " na semana · sem teto"}
          </dd>
        </div>
      </dl>

      <FaixaHoje car={car} />

      {(erro || estado?.personalizado) && (
        <div className={s.estadoRodape}>
          {erro && <span className={s.erroTexto}>{erro}</span>}
          {estado?.personalizado && (
            <button
              type="button"
              className={s.link}
              disabled={ocupado === "carrossel_corretor_ativo"}
              onClick={() => {
                // "Padrão" é o arquivo de configuração do servidor — e ele pode
                // dizer LIGADO. Voltar ao padrão sem perguntar já ligaria o
                // remarketing da casa (05/10/2026).
                const padraoLigado = Boolean(estado?.padrao);
                const pergunta = padraoLigado
                  ? "O padrão do sistema é LIGADO. Voltar ao padrão LIGA o disparo do remarketing. Confirmar?"
                  : "O padrão do sistema é DESLIGADO. Voltar ao padrão desliga o disparo. Confirmar?";
                if (padraoLigado !== ligado && !confirm(pergunta)) return;
                void restaurar("carrossel_corretor_ativo");
              }}
            >
              voltar o interruptor ao padrão ({estado.padrao ? "ligado" : "desligado"})
            </button>
          )}
        </div>
      )}
    </section>
  );
}

// ── hoje, na Meta: aceito × entregue, lado a lado, e a última volta do robô ──
//
// "Hoje 23 de 80" conta o que a Meta ACEITOU. Aceito não é entregue: a faixa
// põe os dois juntos pra a diferença aparecer sem ninguém procurar. E a última
// volta, do registro durável, é o que sustenta "ligado e rodando": interruptor
// ligado com o robô parado era a tela dizendo "saudável" sem envio nenhum.
function FaixaHoje({ car }: { car: Carrossel }) {
  const hoje = car.ritmo.hoje;
  const volta = car.ultima_volta;
  // Backend antigo não manda nenhum dos dois: não desenha faixa vazia.
  if (hoje === undefined && volta === undefined) return null;

  let voltaTexto: string;
  if (car.ultima_volta_indisponivel) voltaTexto = "não consegui ler";
  else if (!volta) voltaTexto = "o robô ainda não rodou";
  else {
    const quando = `${horaCurta(volta.em) ?? "sem hora"} de ${diaDoInstante(volta.em) ?? "sem dia"}`;
    const enviados = volta.enviados ?? 0;
    if (enviados > 0) {
      // Mandou e parou antes de acabar o lote (desligaram, trocaram o número,
      // bateu o teto): o motivo vem junto, senão "mandou 1" parece lote normal.
      voltaTexto =
        `${quando}: mandou ${enviados}` +
        (volta.incertos ? `, ${volta.incertos} sem confirmação` : "") +
        (volta.motivo_texto ? ` e parou, ${volta.motivo_texto}` : "");
    } else if (volta.ligado === null) {
      voltaTexto = `${quando}: não mandou, não conseguiu ler o painel`;
    } else {
      voltaTexto = `${quando}: não mandou${volta.motivo_texto ? `, ${volta.motivo_texto}` : ""}`;
    }
  }

  return (
    <div className={s.faixaHoje} data-testid="faixa-hoje">
      <div className={s.faixaGrupo} data-testid="hoje-na-meta">
        <span className={s.faixaRotulo}>hoje, na Meta</span>
        {hoje ? (
          <span className={s.faixaNumeros}>
            <span>
              <strong>{mil(hoje.aceitos)}</strong> aceitos
            </span>
            <span>
              <strong>{mil(hoje.entregues)}</strong> entregues
            </span>
            <span>{mil(hoje.lidos)} lidos</span>
            <span className={hoje.falhas > 0 ? s.faixaRuim : ""}>{mil(hoje.falhas)} falharam</span>
            {hoje.sem_confirmacao_ainda > 0 && <span>{mil(hoje.sem_confirmacao_ainda)} sem confirmação</span>}
            {hoje.sem_rastro > 0 && <span>{mil(hoje.sem_rastro)} sem rastro</span>}
            {hoje.incertos > 0 && <span className={s.faixaRuim}>{mil(hoje.incertos)} não sei se saiu</span>}
          </span>
        ) : (
          <span className={s.faixaNumeros}>não consegui ler{car.ritmo.hoje_motivo ? ` (${car.ritmo.hoje_motivo})` : ""}</span>
        )}
      </div>
      <div className={s.faixaGrupo} data-testid="ultima-volta">
        <span className={s.faixaRotulo}>última volta do robô</span>
        <span className={s.faixaNumeros}>{voltaTexto}</span>
      </div>
    </div>
  );
}

// ── o fechamento do conferente, os alarmes e as reservas sem disparo ──
//
// O conferente fecha cada dia em código (previstos, aceitos, entregues, lidos,
// falhas) e levanta alarme quando a conta não bate. Aqui aparece o último dia
// FECHADO (o de hoje é parcial e já está na faixa de cima) e o que ele acusou.
// Sem nada a dizer, a seção não existe: aviso permanente vira papel de parede.
function Conferencia({ car }: { car: Carrossel }) {
  const conf = car.conferencia;
  const orfas = car.reservas_orfas;
  if (conf === undefined && orfas === undefined) return null; // backend antigo

  const dias = conf?.dias ?? null;
  const fechado = dias?.find((d) => !d.parcial) ?? null;
  // Alarme de dia parcial (hoje) também conta: "não sei se saiu" não espera a meia-noite.
  const comAlarme = (dias ?? []).filter((d) => d.alarmes.length > 0).slice(0, 2);
  const ultimaConferencia = dias?.[0]?.conferido_em ?? null;
  // O conferente roda de hora em hora. Se a última conferência é velha, quem
  // parou foi ELE, e "nenhum alarme" passaria a ser silêncio de vigia dormindo:
  // a tela diz que não sabe em vez de dizer que está tudo bem.
  const horasSemConferir = ultimaConferencia
    ? (Date.now() - new Date(ultimaConferencia).getTime()) / 3_600_000
    : null;
  const conferenteParado = horasSemConferir !== null && horasSemConferir > HORAS_PRA_CONFERENTE_PARADO;
  const semLeitura = conf?.indisponivel || car.reservas_orfas_indisponivel;

  const estados = orfas
    ? Object.entries(orfas.por_estado)
        .map(([estado, n]) => `${n} ${NOME_RESERVA[estado] ?? estado}`)
        .join(", ")
    : "";

  return (
    <section className={s.conferencia} aria-label="Conferência do disparo" data-testid="conferencia">
      <h2 className={s.ajustesTitulo}>Conferência</h2>
      <div className={s.confGrade}>
        <div className={s.bloco} data-testid="fechamento">
          <h3 className={s.blocoTitulo}>
            {fechado ? `Fechamento de ${diaCurto(fechado.dia)}` : "Fechamento do dia"}
          </h3>
          {conf?.indisponivel ? (
            <p className={s.aviso}>Não consegui ler o fechamento: {conf.motivo_indisponivel}.</p>
          ) : !fechado ? (
            <p className={s.dica}>
              {dias && dias.length > 0
                ? "Nenhum dia fechado ainda: só o de hoje, em andamento."
                : "O conferente ainda não fechou nenhum dia."}
            </p>
          ) : (
            <dl className={s.confNumeros}>
              <div>
                <dt>previstos</dt>
                <dd>{mil(fechado.previstos)}</dd>
              </div>
              <div>
                <dt>aceitos</dt>
                <dd>{mil(fechado.aceitos)}</dd>
              </div>
              <div>
                <dt>entregues</dt>
                <dd>{mil(fechado.entregues)}</dd>
              </div>
              <div>
                <dt>lidos</dt>
                <dd>{mil(fechado.lidos)}</dd>
              </div>
              <div>
                <dt>falhas</dt>
                <dd className={fechado.falhas > 0 ? s.faixaRuim : ""}>{mil(fechado.falhas)}</dd>
              </div>
            </dl>
          )}
          {ultimaConferencia && (
            <p className={s.dica}>
              conferido às {horaCurta(ultimaConferencia)} de {diaDoInstante(ultimaConferencia)}
            </p>
          )}
        </div>

        <div className={s.bloco} data-testid="alarmes">
          <h3 className={s.blocoTitulo}>Alarmes</h3>
          {conferenteParado && (
            <p className={s.aviso} data-testid="conferente-parado">
              <strong>O conferente não roda desde {horaCurta(ultimaConferencia)} de {diaDoInstante(ultimaConferencia)}.</strong>{" "}
              Sem ele não há alarme novo: o que aparece aqui é de antes.
            </p>
          )}
          {comAlarme.length === 0 ? (
            <p className={s.dica}>
              {conferenteParado
                ? "Nenhum alarme até a última conferência."
                : semLeitura
                ? "Sem leitura: não dá pra dizer que não há alarme."
                : dias && dias.length > 0
                  ? "Nenhum alarme nos dias conferidos."
                  : "Sem conferência, sem alarme: o conferente ainda não rodou."}
            </p>
          ) : (
            comAlarme.map((d) => (
              <p key={d.dia} className={s.aviso} data-testid="alarme-do-dia">
                <strong>
                  {diaCurto(d.dia)}
                  {d.parcial ? " (hoje)" : ""}:
                </strong>{" "}
                {d.alarmes.map((a) => `${a.texto} (${a.detalhe})`).join("; ")}.
              </p>
            ))
          )}
          {car.reservas_orfas_indisponivel ? (
            <p className={s.aviso}>Não consegui ler as reservas sem disparo.</p>
          ) : orfas && orfas.total > 0 ? (
            <p className={s.aviso} data-testid="reservas-orfas">
              <strong>
                {mil(orfas.total)} {orfas.total === 1 ? "lead está" : "leads estão"} em Disparado sem disparo
                registrado
              </strong>
              {orfas.mais_antiga ? `, desde ${diaDoInstante(orfas.mais_antiga)}` : ""}
              {estados ? ` (${estados})` : ""}. Não são reenviados sozinhos.
            </p>
          ) : null}
        </div>
      </div>
    </section>
  );
}

// O timer do conferente é de hora em hora; três horas sem rodar já é parada.
const HORAS_PRA_CONFERENTE_PARADO = 3;

const NOME_RESERVA: Record<string, string> = {
  aberta: "em aberto",
  incerto: "não sei se saiu",
  sem_registro: "saiu sem registro",
  sem_prova: "sem prova de envio",
};

// ── de onde sai: um seletor só ──
//
// Pedido da Dona (05/10): nada de lista com todos os telefones. Um campo que
// abre em lista, e na lista só entra número que o MOTOR aceita como disparador
// (`operavel`, decidido no servidor; hoje, a linha da casa) mais "nenhum". Os
// telefones das corretoras são de atendimento e não aparecem aqui. A saúde em
// cor fica ao lado do escolhido; sem escolhido, é a da linha que poderia
// disparar, com o papel dela escrito, porque é pausada que se decide se volta.
function Numeros({ car, onTrocou }: { car: Carrossel; onTrocou: () => void }) {
  const [ocupado, setOcupado] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const linha = car.linha_disparadora;
  if (!linha) return null; // backend antigo: não inventa seletor
  const numeros = linha.numeros as Numero[];
  const candidatos = numeros.filter((n) => n.operavel);
  const disparadores = numeros.filter((n) => n.papel === "disparo");
  const atual = disparadores.length === 1 ? disparadores[0] : null;
  // Marcado por fora da tela num número que o motor não opera: aparece (a tela
  // não esconde o que está gravado), mas não é opção.
  const atualDeFora = atual && !atual.operavel ? atual : null;
  const valor = atual?.phone_number_id ?? "";
  const mostrado = atual ?? (candidatos.length === 1 ? candidatos[0] : null);
  const saude = mostrado ? saudeDe(mostrado, car) : null;
  const tel = (n: Numero) => n.telefone ?? n.phone_number_id;

  async function escolher(id: string) {
    if (id === valor) return;
    const novo = candidatos.find((n) => n.phone_number_id === id) ?? null;
    let alvo: Numero;
    let corpo: Record<string, unknown>;
    let pergunta: string;
    if (novo) {
      // Marcar disparador autoriza disparo em massa por este número. Quem já
      // era disparador passa a pausado NO MESMO PEDIDO (o cadastro troca os
      // dois numa transação): não existe instante sem disparador nem com dois.
      const outros = disparadores.filter((x) => x.phone_number_id !== novo.phone_number_id);
      alvo = novo;
      corpo = { papel: "disparo", pausar_os_outros: true };
      pergunta =
        `Marcar ${tel(novo)} (${novo.nome ?? "sem nome"}) como DISPARADOR?\n\n` +
        "É por ele que o remarketing sai quando o interruptor estiver ligado." +
        (outros.length ? `\n${outros.map(tel).join(", ")} deixa de ser disparador e fica pausado.` : "");
    } else if (id === "" && atual) {
      alvo = atual;
      corpo = { papel: "pausado" };
      pergunta =
        `Pausar ${tel(atual)} (${atual.nome ?? "sem nome"})?\n\n` +
        "O remarketing fica sem número: nada sai, mesmo com o interruptor ligado.";
    } else {
      return;
    }
    if (!window.confirm(pergunta)) return;
    setOcupado(true);
    setErro(null);
    try {
      const r = await fetch(`/api/automacoes/remarketing/linhas/${alvo.phone_number_id}/papel`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(corpo),
      });
      const out = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
    } catch (e) {
      setErro(String((e as Error).message || e));
    } finally {
      setOcupado(false);
      onTrocou();
    }
  }

  return (
    <div className={s.bloco} data-testid="numeros-do-disparo">
      <h3 className={s.blocoTitulo}>
        <label htmlFor="rmk-numero">De onde sai</label>
      </h3>
      {linha.indisponivel ? (
        <p className={s.aviso}>
          Não consegui ler o cadastro de números. Enquanto isso o disparo fica segurado.
        </p>
      ) : (
        <>
          <select
            id="rmk-numero"
            className={s.campo}
            value={valor}
            disabled={ocupado}
            onChange={(e) => void escolher(e.target.value)}
          >
            <option value="">nenhum número (disparo pausado)</option>
            {candidatos.map((n) => (
              <option key={n.phone_number_id} value={n.phone_number_id}>
                {tel(n)}
                {n.nome ? ` · ${n.nome}` : ""}
              </option>
            ))}
            {atualDeFora && (
              <option value={atualDeFora.phone_number_id} disabled>
                {tel(atualDeFora)} · não é número de disparo
              </option>
            )}
          </select>
          {saude && mostrado && (
            <p className={s.saudeLinha} data-testid="saude-do-escolhido" data-cor={saude.cor} data-papel={mostrado.papel}>
              <Bolinha cor={saude.cor} />
              <span>
                {saudeEmTexto(saude)}
                {atual ? "" : ` · ${telefoneCurto(mostrado.telefone) ?? mostrado.nome ?? "linha da casa"}, ${NOME_PAPEL[mostrado.papel] ?? mostrado.papel}`}
              </span>
            </p>
          )}
          {candidatos.length === 0 && (
            <p className={s.aviso}>Nenhum número deste cliente serve pra disparo.</p>
          )}
          {atualDeFora && (
            <p className={s.erroTexto}>O sistema não consegue sair por este número. Escolha outro.</p>
          )}
        </>
      )}
      {linha.motivo === "linha_disparadora_ambigua" && (
        <p className={s.aviso}>Mais de um número está como disparador: o sistema segura até sobrar um.</p>
      )}
      {erro && <p className={s.erroTexto}>Não consegui trocar: {erro}</p>}
      <p className={s.dica}>Só um número dispara. Trocar aqui não liga o disparo.</p>
    </div>
  );
}

export default function PainelDisparo({
  car,
  config,
  onSalvou,
}: {
  car: Carrossel;
  config: Config;
  onSalvou: () => void;
}) {
  const { gravar, restaurar, ocupado, carregar } = config;
  const ag = car.agenda?.config ?? null;
  const alvo = car.alvo?.config ?? null;
  const ritmo = car.ritmo;
  const destino = car.parar?.config ?? null;
  const aviso = car.aviso_telegram;

  // ── rascunho do formulário (texto cru, do jeito que foi digitado) ──
  const [inicio, setInicio] = useState("");
  const [fim, setFim] = useState("");
  const [dias, setDias] = useState<number[]>([]);
  const [porDia, setPorDia] = useState("");
  const [porSemana, setPorSemana] = useState("");
  const [etapa, setEtapa] = useState("");
  const [parar, setParar] = useState("");
  const [erros, setErros] = useState<Partial<Record<Chave, string>>>({});
  const [salvando, setSalvando] = useState(false);
  const [salvo, setSalvo] = useState(false);

  // Rascunho nasce do que está gravado — e volta pra lá depois de salvar.
  // Depende dos VALORES, não do objeto: o mapa recarrega a cada ação e um
  // objeto novo com os mesmos números não pode apagar o que está sendo digitado.
  const assinatura = JSON.stringify([
    ag?.janela_horario ?? "",
    ag?.dias_semana ?? [],
    ag?.cap_diario ?? null,
    ag?.cap_semanal ?? null,
    alvo?.etapa ?? null,
    destino?.etapa ?? null,
  ]);
  const gravado = useMemo(() => {
    const [janela, diasGravados, dia, sem, etapaGravada, pararGravado] = JSON.parse(assinatura) as [
      string,
      number[],
      number | null,
      number | null,
      number | null,
      number | null,
    ];
    return { janela, dias: diasGravados, porDia: dia, porSemana: sem, etapa: etapaGravada, parar: pararGravado };
  }, [assinatura]);

  function voltarAoGravado() {
    const [i, f] = gravado.janela ? gravado.janela.split("-") : ["", ""];
    setInicio(i);
    setFim(f);
    setDias(gravado.dias);
    setPorDia(gravado.porDia === null ? "" : String(gravado.porDia));
    setPorSemana(gravado.porSemana === null ? "" : String(gravado.porSemana));
    setEtapa(gravado.etapa === null ? "" : String(gravado.etapa));
    setParar(gravado.parar === null ? "" : String(gravado.parar));
    setErros({});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(voltarAoGravado, [gravado]);

  const janelaRascunho = inicio || fim ? `${inicio}-${fim}` : "";
  const mudou = {
    remarketing_janela_horario: janelaRascunho !== gravado.janela,
    remarketing_dias_semana: [...dias].sort().join() !== [...gravado.dias].sort().join(),
    remarketing_cap_diario: porDia.trim() !== (gravado.porDia === null ? "" : String(gravado.porDia)),
    remarketing_cap_semanal:
      porSemana.trim() !== (gravado.porSemana === null ? "" : String(gravado.porSemana)),
    // Só conta como mudança se há etapa lida: sem leitura não se grava no escuro.
    remarketing_etapa: etapa !== "" && etapa !== (gravado.etapa === null ? "" : String(gravado.etapa)),
    // Mesmo cuidado da etapa: sem leitura do destino não se grava no escuro.
    remarketing_etapa_parar: parar !== "" && parar !== (gravado.parar === null ? "" : String(gravado.parar)),
  };
  const quantasMudancas = Object.values(mudou).filter(Boolean).length;

  // Texto que não é inteiro vai como texto mesmo: quem recusa é o backend.
  const numeroOuTexto = (t: string): number | string => {
    const limpo = t.trim();
    return /^-?\d+$/.test(limpo) ? Number(limpo) : limpo;
  };

  const capMaximo = ritmo.cap_maximo_aceito ?? 300;
  const pedidoDigitado = /^\d+$/.test(porDia.trim()) ? Number(porDia.trim()) : null;
  const tetoRampa = ritmo.cap_rampa ?? null;
  const teto = ritmo.teto ?? null;

  async function salvar() {
    // Teto acima de 80 é risco pra linha nova: pergunta com o número na frase.
    if (
      mudou.remarketing_cap_diario &&
      pedidoDigitado !== null &&
      pedidoDigitado > DEGRAU_BASE &&
      !window.confirm(
        `Gravar ${pedidoDigitado} disparos por dia?\n\n` +
          `Acima de ${DEGRAU_BASE} só vale com a linha verde na Meta, e saltar de uma vez é o ` +
          "padrão que a Meta lê como spam. O recomendado é subir em degraus (120, 170, 230, 300).",
      )
    ) {
      return;
    }
    // Trocar o destino de quem pede pra parar muda o que acontece com gente
    // de verdade na próxima resposta: pergunta com o nome da etapa na frase.
    const destinoNovo = destino?.opcoes.find((o) => String(o.id) === parar) ?? null;
    if (
      mudou.remarketing_etapa_parar &&
      !window.confirm(
        `Quem pedir pra parar passa a ir pra "${destinoNovo?.nome ?? parar}".\n\n` +
          "Vale a partir da próxima resposta. Quem já saiu da roda fica onde está.",
      )
    ) {
      return;
    }
    setSalvando(true);
    setSalvo(false);
    const novosErros: Partial<Record<Chave, string>> = {};

    // A AGENDA vai num pedido só, e o servidor grava tudo ou nada: o robô
    // nunca lê horário novo com teto antigo. `null` = voltar ao padrão.
    const agenda: Partial<Record<Chave, unknown>> = {};
    if (mudou.remarketing_janela_horario) agenda.remarketing_janela_horario = janelaRascunho === "" ? null : janelaRascunho;
    if (mudou.remarketing_dias_semana) agenda.remarketing_dias_semana = dias.length === 0 ? null : [...dias].sort();
    if (mudou.remarketing_cap_diario) agenda.remarketing_cap_diario = porDia.trim() === "" ? null : numeroOuTexto(porDia);
    if (mudou.remarketing_cap_semanal) agenda.remarketing_cap_semanal = porSemana.trim() === "" ? null : numeroOuTexto(porSemana);
    // Escolher a etapa padrão apaga a chave: volta a valer o comportamento
    // de sempre em vez de ficar "editado no painel" com o mesmo valor.
    if (mudou.remarketing_etapa) {
      agenda.remarketing_etapa = alvo !== null && etapa === String(alvo.etapa_padrao) ? null : numeroOuTexto(etapa);
    }
    const camposDaAgenda = Object.keys(agenda) as Chave[];
    if (camposDaAgenda.length > 0) {
      try {
        const r = await fetch("/api/automacoes/remarketing/agenda", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(agenda),
        });
        const out = await r.json().catch(() => ({}));
        if (!r.ok) {
          const porCampo = (out?.erros ?? null) as Partial<Record<Chave, string>> | null;
          if (porCampo && Object.keys(porCampo).length > 0) Object.assign(novosErros, porCampo);
          else {
            // Recusa sem campo (403, 502, servidor antigo): o erro é de todos
            // os campos que iam, porque NENHUM foi gravado.
            const msg = String(out?.detail || out?.error || `HTTP ${r.status}`);
            for (const c of camposDaAgenda) novosErros[c] = msg;
          }
        }
      } catch (e) {
        const msg = String((e as Error).message || e);
        for (const c of camposDaAgenda) novosErros[c] = msg;
      }
    }

    // O destino do "parar" não é agenda: chave própria, no caminho de sempre.
    if (mudou.remarketing_etapa_parar) {
      const voltaAoPadrao = Boolean(destinoNovo?.padrao);
      const r = voltaAoPadrao
        ? await restaurar("remarketing_etapa_parar", true)
        : await gravar("remarketing_etapa_parar", numeroOuTexto(parar), true);
      if (!r.ok) novosErros.remarketing_etapa_parar = r.erro ?? "não consegui gravar";
    }

    setErros(novosErros);
    setSalvando(false);
    if (Object.keys(novosErros).length === 0) setSalvo(true);
    void carregar(); // o estado das chaves (o hook não viu o pedido da agenda)
    onSalvou(); // recarrega o mapa: o topo muda junto
  }

  useEffect(() => {
    if (!salvo) return;
    const t = setTimeout(() => setSalvo(false), 4000);
    return () => clearTimeout(t);
  }, [salvo]);

  const etapaEscolhida = alvo?.etapas.find((e) => String(e.id) === etapa) ?? null;
  const timer = (ag?.timer ?? "09:00-19:00").replace("-", " às ");

  return (
    <>
      <Estado car={car} config={config} />

      <Conferencia car={car} />

      <section
        className={`${s.ajustes} ${quantasMudancas > 0 ? s.ajustesComMudanca : ""}`}
        aria-label="Disparo automático"
      >
        <h2 className={s.ajustesTitulo}>Ajustar</h2>

        {(ag?.ilegivel || car.agenda?.indisponivel || alvo?.invalido || car.alvo?.indisponivel) && (
          <p className={s.aviso}>
            {ag?.ilegivel
              ? `A agenda gravada está ilegível (${ag.ilegivel}). Regrave os campos para o disparo voltar.`
              : car.agenda?.indisponivel
                ? `Não consegui ler a agenda: ${car.agenda.motivo_indisponivel}.`
                : alvo?.invalido
                  ? `A etapa gravada não serve (${alvo.invalido}). Escolha outra abaixo.`
                  : `Não consegui ler a etapa: ${car.alvo?.motivo_indisponivel}.`}
          </p>
        )}

        <Recolhe titulo="Ajustar o disparo" dica="agenda, limite, etapa, aviso">
        <div className={s.grade}>
          <Numeros car={car} onTrocou={onSalvou} />

          <div className={s.bloco}>
            <h3 className={s.blocoTitulo}>
              <label htmlFor="rmk-etapa">Pra quem</label>
            </h3>
            <select
              id="rmk-etapa"
              className={s.campo}
              value={etapa}
              disabled={!alvo}
              onChange={(e) => setEtapa(e.target.value)}
            >
              {!alvo && <option value="">não consegui ler as etapas</option>}
              {alvo && alvo.etapa === null && <option value="">escolha uma etapa</option>}
              {alvo?.etapas.map((e) => (
                <option key={e.id} value={e.id} disabled={!e.permitida}>
                  {e.nome ?? `etapa ${e.id}`}
                  {e.funil ? ` (${e.funil})` : ""}: {mil(e.leads_do_dono)}
                  {e.padrao ? " · padrão" : ""}
                  {!e.permitida ? " · não pode" : ""}
                </option>
              ))}
            </select>
            {etapaEscolhida && (
              <p className={s.contagem} data-testid="leads-da-etapa">
                <strong>{mil(etapaEscolhida.leads_do_dono)}</strong> leads
                {alvo?.dono_nome ? ` da ${alvo.dono_nome}` : ""} nesta etapa
                {etapaEscolhida.leads > etapaEscolhida.leads_do_dono && (
                  <span className={s.dica}> · {mil(etapaEscolhida.leads - etapaEscolhida.leads_do_dono)} de corretora ficam de fora</span>
                )}
              </p>
            )}
            {etapaEscolhida && etapaEscolhida.leads_do_dono === 0 && (
              <p className={s.aviso} data-testid="aviso-etapa-vazia">
                Com esta etapa o disparo não manda nada.
              </p>
            )}
            {erros.remarketing_etapa && <p className={s.erroTexto}>{erros.remarketing_etapa}</p>}
            <p className={s.dica}>Quem recebe volta pra etapa depois de {ritmo.descanso_dias} dias.</p>
          </div>

          <div className={s.bloco}>
            <h3 className={s.blocoTitulo}>Quando</h3>
            <div className={s.dias} role="group" aria-label="dias da semana">
              {DIAS.map((nome, i) => {
                const on = dias.includes(i);
                return (
                  <button
                    key={nome}
                    type="button"
                    aria-pressed={on}
                    className={`${s.dia} ${on ? s.diaOn : ""}`}
                    onClick={() => setDias(on ? dias.filter((d) => d !== i) : [...dias, i])}
                  >
                    {nome}
                  </button>
                );
              })}
            </div>
            <div className={s.horario}>
              <label className={s.rotulo} htmlFor="rmk-inicio">
                das
              </label>
              <input
                id="rmk-inicio"
                type="time"
                className={s.campo}
                value={inicio}
                onChange={(e) => setInicio(e.target.value)}
                aria-label="começa às"
              />
              <label className={s.rotulo} htmlFor="rmk-fim">
                às
              </label>
              <input
                id="rmk-fim"
                type="time"
                className={s.campo}
                value={fim}
                onChange={(e) => setFim(e.target.value)}
                aria-label="termina às"
              />
            </div>
            {erros.remarketing_dias_semana && <p className={s.erroTexto}>{erros.remarketing_dias_semana}</p>}
            {erros.remarketing_janela_horario && (
              <p className={s.erroTexto}>{erros.remarketing_janela_horario}</p>
            )}
            <p className={s.dica}>Sem dia marcado = todo dia. Sem horário = {timer}.</p>
          </div>

          <div className={s.bloco}>
            <h3 className={s.blocoTitulo}>Quanto</h3>
            <div className={s.limites}>
              <label className={s.limite}>
                <span className={s.rotulo}>por dia</span>
                <input
                  id="rmk-dia"
                  type="number"
                  min={0}
                  max={capMaximo}
                  inputMode="numeric"
                  className={s.campo}
                  value={porDia}
                  placeholder={tetoRampa !== null ? String(tetoRampa) : String(DEGRAU_BASE)}
                  onChange={(e) => setPorDia(e.target.value)}
                />
              </label>
              <label className={s.limite}>
                <span className={s.rotulo}>por semana</span>
                <input
                  id="rmk-semana"
                  type="number"
                  min={0}
                  inputMode="numeric"
                  className={s.campo}
                  value={porSemana}
                  placeholder="sem teto"
                  onChange={(e) => setPorSemana(e.target.value)}
                />
              </label>
            </div>
            {teto && (teto.pedido !== null || teto.diverge) && (
              <p className={teto.diverge ? s.aviso : s.contagem} data-testid="teto-efetivo">
                vale hoje: <strong>{teto.efetivo ?? "sem dado"}</strong> por dia
                {teto.diverge && teto.explicacao ? ` (${teto.explicacao})` : ""}
              </p>
            )}
            {pedidoDigitado !== null && pedidoDigitado > DEGRAU_BASE && (
              <p className={s.aviso} data-testid="aviso-acima-de-80">
                Acima de {DEGRAU_BASE} só vale com a linha verde. Suba em degraus.
              </p>
            )}
            {erros.remarketing_cap_diario && <p className={s.erroTexto}>{erros.remarketing_cap_diario}</p>}
            {erros.remarketing_cap_semanal && <p className={s.erroTexto}>{erros.remarketing_cap_semanal}</p>}
            <p className={s.dica} data-testid="dica-por-dia">
              Vazio = a rampa decide ({DEGRAU_BASE}). Máximo {capMaximo}.
            </p>
          </div>

          {car.parar && (
            <div className={s.bloco} data-testid="bloco-parar">
              <h3 className={s.blocoTitulo}>
                <label htmlFor="rmk-parar">Quem pede pra parar</label>
              </h3>
              <select
                id="rmk-parar"
                className={s.campo}
                value={parar}
                disabled={!destino || destino.opcoes.length === 0}
                onChange={(e) => setParar(e.target.value)}
              >
                {!destino && <option value="">não consegui ler as etapas</option>}
                {destino && destino.etapa === null && <option value="">nenhuma etapa serve</option>}
                {destino?.opcoes.map((o) => (
                  <option key={o.id} value={o.id}>
                    vai pra {o.nome}
                    {o.padrao ? " · padrão" : ""}
                  </option>
                ))}
              </select>
              {destino?.invalido && (
                <p className={s.aviso} data-testid="parar-invalido">
                  O destino gravado não serve ({destino.invalido}). Está valendo Venda perdida.
                </p>
              )}
              {car.parar.indisponivel && (
                <p className={s.aviso}>Não consegui ler o destino: {car.parar.motivo_indisponivel}.</p>
              )}
              {erros.remarketing_etapa_parar && <p className={s.erroTexto}>{erros.remarketing_etapa_parar}</p>}
              <p className={s.dica}>Vale a partir da próxima resposta.</p>
            </div>
          )}

          {aviso && (
            <div className={s.bloco} data-testid="bloco-aviso">
              <h3 className={s.blocoTitulo}>Aviso diário no Telegram</h3>
              {aviso.ligado === null ? (
                <p className={s.aviso}>Não consegui ler se o aviso está ligado.</p>
              ) : (
                <Interruptor
                  ativa={aviso.ligado}
                  ocupado={ocupado === "remarketing_conferente_avisa_telegram"}
                  rotulo={aviso.ligado ? "Desligar o aviso diário no Telegram" : "Ligar o aviso diário no Telegram"}
                  confirmarLigar={
                    "Ligar o aviso diário no Telegram?\n\n" +
                    "O fechamento do dia passa a ser enviado uma vez por dia, só quando houve disparo ou alarme."
                  }
                  confirmarDesligar={
                    "Desligar o aviso diário no Telegram?\n\nOs alarmes continuam aparecendo só nesta tela."
                  }
                  onMudar={(novo) =>
                    void gravar("remarketing_conferente_avisa_telegram", novo).then(() => onSalvou())
                  }
                />
              )}
              <p className={s.dica}>Uma mensagem por dia, só se houve disparo ou alarme.</p>
            </div>
          )}
        </div>
        </Recolhe>

        <div
          className={`${s.salvarBarra} ${quantasMudancas > 0 ? s.salvarBarraAtiva : ""}`}
          data-testid="barra-salvar"
        >
          <span className={s.salvarTexto}>
            {salvando
              ? "salvando…"
              : quantasMudancas > 0
                ? `${quantasMudancas} ${quantasMudancas === 1 ? "mudança" : "mudanças"} sem salvar`
                : salvo
                  ? "gravado"
                  : "nada a salvar"}
            <span className={s.salvarNota}>salvar não liga o disparo</span>
          </span>
          {quantasMudancas > 0 && (
            <button type="button" className={s.link} disabled={salvando} onClick={voltarAoGravado}>
              descartar
            </button>
          )}
          <button
            type="button"
            className={s.btnPrimario}
            disabled={quantasMudancas === 0 || salvando || ocupado !== null}
            onClick={() => void salvar()}
          >
            {salvando ? "salvando…" : "salvar"}
          </button>
        </div>

        {/* O que acontece com quem responde: tem o seu próprio salvar (um
            pedido só, tudo ou nada) porque mexe em quem recebe lead, não no
            disparo. */}
        <Recolhe titulo="Quando a pessoa responde">
          <QuandoResponde car={car} onSalvou={onSalvou} />
        </Recolhe>
      </section>
    </>
  );
}
