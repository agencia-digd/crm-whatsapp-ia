"use client";

// Estado dos controles da operação (liga/desliga, limites, roleta) — o mesmo
// GET/PUT/DELETE de /api/automacoes/config usado pelo bloco "Controle da
// operação" e pelo interruptor no cabeçalho de cada automação.
//
// Vive num hook porque as duas telas mexem NA MESMA chave: duas cópias da
// lógica de gravação viram dois comportamentos diferentes na primeira mudança —
// e do outro lado disso sai mensagem no WhatsApp de gente real.

import { useCallback, useEffect, useState } from "react";

export type Chave =
  | "cadencia_ativa"
  // Kill-switch PRÓPRIO da nutrição (05/08). Era a mesma chave da cadência —
  // desligar o follow-up parava os marcos junto, sem ninguém perceber.
  | "nutricao_ativa"
  | "cadencia_cap_diario"
  | "cadencia_cap_por_corretora"
  | "roleta_corretoras"
  // Remarketing (carrossel da Corretor): kill-switch da automação e os pesos do
  // teste A/B de copy (id da variante → fatia em %).
  | "carrossel_corretor_ativo"
  | "carrossel_pesos"
  // Agenda do remarketing (05/10): quando e quanto o carrossel dispara. O
  // tick lê as MESMAS chaves; vazio (chave apagada) = comportamento de antes.
  | "remarketing_janela_horario"
  | "remarketing_dias_semana"
  | "remarketing_cap_diario"
  | "remarketing_cap_semanal"
  // Alvo do remarketing (05/10): etapa de origem e dono dos leads do disparo.
  | "remarketing_etapa"
  | "remarketing_dono"
  // Junção com o motor (05/10): destino de quem pede pra parar e o aviso
  // diário do conferente no Telegram.
  | "remarketing_etapa_parar"
  | "remarketing_conferente_avisa_telegram"
  // Texto das variantes e "quando a pessoa responde" (05/10): quem vai pra
  // corretora, etapa de entrada, roleta do remarketing e o aviso ao lead.
  | "carrossel_copies"
  | "carrossel_marketing_espelha_direta"
  | "handoff_remarketing_quem_vai"
  | "handoff_remarketing_etapa"
  | "handoff_remarketing_roleta"
  | "handoff_remarketing_avisa_lead"
  | "handoff_remarketing_aviso_texto"
  | "handoff_remarketing_aviso_resumo"
  | "handoff_remarketing_respeita_humano"
  // IA do lado (05/10): resumo no card e opinião no que a regra não entendeu.
  | "ia_resumo_card"
  | "ia_opina_indefinida"
  | "ia_opina_corte"
  // Boas-vindas: kill-switch (era só a env LEAD_INTAKE_ATIVO — o interruptor da
  // página seria decorativo sem esta chave), copies e pesos do teste A/B da
  // primeira mensagem. O motor lê as MESMAS chaves.
  | "lead_intake_ativo"
  | "boas_vindas_copies"
  | "boas_vindas_pesos"
  // Interruptor POR CORRETORA (06/10): mapa id → true/false, fora = ligada. O
  // clique grava pela rota própria; a chave aqui é pro "voltar ao padrão".
  | "cadencia_por_corretora"
  | "nutricao_por_corretora"
  // Nutrição pela linha da DONA do lead (07/10): ligada = cada marco sai pela
  // linha dela quando serve; desligada = toda pela Pessoa12. O motor relê antes de
  // cada marco.
  | "nutricao_pela_linha_da_dona";

/** `indisponivel` = o servidor não conseguiu ler esta chave no banco. Nesse
 *  caso `valor` vem null, e null NÃO é "desligado": quem desenha confere. */
export type Estado = {
  valor: unknown;
  personalizado: boolean;
  padrao: unknown;
  indisponivel?: boolean;
  motivo?: string;
};
export type Corretora = { id: number; nome: string; label: string };
/** Resultado de uma gravação. Quem tem campo próprio de erro (a agenda do
 *  remarketing) usa isto pra mostrar a recusa AO LADO do campo que errou. */
export type Resultado = { ok: boolean; erro?: string };
export type PayloadConfig = { config: Record<Chave, Estado>; corretoras: Corretora[] };

export function useConfigOperacional() {
  const [dados, setDados] = useState<PayloadConfig | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [ocupado, setOcupado] = useState<Chave | null>(null);

  const carregar = useCallback(async () => {
    try {
      const r = await fetch("/api/automacoes/config", { cache: "no-store" });
      const out = await r.json();
      if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
      setDados(out);
      setErro(null);
    } catch (e) {
      setErro(String((e as Error).message || e));
    }
  }, []);

  useEffect(() => {
    void carregar();
  }, [carregar]);

  const gravar = useCallback(
    // `erroLocal`: quem chama mostra o erro no próprio campo — não repete na
    // faixa geral do cabeçalho.
    async (chave: Chave, valor: unknown, erroLocal = false): Promise<Resultado> => {
      setOcupado(chave);
      setErro(null);
      try {
        const r = await fetch(`/api/automacoes/config/${chave}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ valor }),
        });
        const out = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
        await carregar();
        return { ok: true };
      } catch (e) {
        const msg = String((e as Error).message || e);
        if (!erroLocal) setErro(msg);
        return { ok: false, erro: msg };
      } finally {
        setOcupado(null);
      }
    },
    [carregar],
  );

  const restaurar = useCallback(
    async (chave: Chave, erroLocal = false): Promise<Resultado> => {
      setOcupado(chave);
      setErro(null);
      try {
        const r = await fetch(`/api/automacoes/config/${chave}`, { method: "DELETE" });
        if (!r.ok) {
          const out = await r.json().catch(() => ({}));
          throw new Error(out?.detail || out?.error || `HTTP ${r.status}`);
        }
        await carregar();
        return { ok: true };
      } catch (e) {
        const msg = String((e as Error).message || e);
        if (!erroLocal) setErro(msg);
        return { ok: false, erro: msg };
      } finally {
        setOcupado(null);
      }
    },
    [carregar],
  );

  return { dados, erro, ocupado, carregar, gravar, restaurar };
}
