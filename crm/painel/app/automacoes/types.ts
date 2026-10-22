// Tipos do payload GET /painel/automacoes (copiloto-api) — compartilhados
// entre a vista Cards (page.tsx) e a vista Fluxo (FluxoView.tsx).

export type Toque = {
  indice: number;
  dia: string;
  offset_dias?: number;
  tipo: "mensagem" | "tarefa_ligacao";
  canal: string;
  categoria: string;
  titulo?: string;
  com_botao?: boolean;
  cta?: string | null;    // verbo do {{3}} quando o toque tem botão
  copy?: string;          // corpo {{2}} EFETIVO
  copy_padrao?: string;
  personalizada?: boolean;
  preview?: string | null; // texto INTEIRO que o lead vê no WhatsApp
  template?: string;
  acao?: string;          // pra tarefa_ligacao
};

// Item de GET /api/automacoes/copies — copy editável de um toque de mensagem.
// toque -1 = mensagem de remarketing (30 em 30 dias).
export type CopyItem = {
  toque: number;
  dia: string;
  tipo: string;
  categoria: string;
  com_botao: boolean;
  default: string;
  override: string | null;
  efetiva: string;
  personalizada: boolean;
  atualizado_em: string | null;
  atualizado_por: string | null;
};

export const TOQUE_REMARKETING = -1;

// Boas-vindas: automação SEPARADA da cadência (lei Dona 10/07). Lead cai em
// "Novo" → 1 mensagem (template + botão Saiba mais) → FIM (aguarda corretora).
// Payload de _mapa_boas_vindas (copiloto lead_intake).
export type BoasVindas = {
  descricao: string;
  gatilho: string;
  acao: string;
  copy: string | null; // corpo {{2}}
  cta?: string | null;
  com_botao?: boolean;
  botoes?: string[];
  template?: string;
  preview?: string | null; // texto INTEIRO que o lead recebe
  copies_por_corretora?: Record<string, string>;
  previews_por_corretora?: Record<string, string>;
  /** Teste A/B da primeira mensagem — texto com {nome} cru (é o que se edita). */
  variantes?: VarianteBoasVindas[];
  /** id da variante de CONTROLE (a que já rodava). */
  controle?: string | null;
  /** Uma entrada por corretora ATIVA do cadastro, com a copy já renderizada. */
  por_corretora?: CorretoraBoasVindas[];
  /**
   * Os caminhos que o motor executa de verdade — o desenho mostrava só o
   * primeiro. `no_ab: false` = envio que NÃO participa do teste A/B.
   */
  caminhos?: {
    id: string;
    titulo: string;
    detalhe: string;
    copy: string | null;
    no_ab: boolean;
  }[];
  /** Copy de CONTINUIDADE: quem foi adotado por LIGAÇÃO, fora do A/B. */
  continuidade?: {
    id: string;
    texto: string;
    preview: string | null;
    quando: string;
    no_ab: boolean;
    por_que: string;
  } | null;
  estado: "ligado" | "desligado";
  flag: string; // "LEAD_INTAKE_ATIVO"
  /** Chave de config_operacional que o interruptor grava (motor lê a MESMA). */
  chave_config?: string;
  detalhe: string;
  fim: string;
  disponivel: boolean;
};

export type VarianteBoasVindas = {
  id: string;
  rotulo: string;
  peso: number;
  ativa: boolean;
  /** "na rodada" · "aguardando a próxima rodada" · "aposentada". */
  estado: string;
  /** true = fora da fila mas AINDA NÃO COMPETIU (≠ aposentada, que perdeu). */
  aguardando: boolean;
  controle: boolean;
  hipotese: string;
  /** corpo com {nome} — o placeholder é o nome da corretora da linha. */
  texto: string;
  padrao: string;
  personalizada: boolean;
};

export type CorretoraBoasVindas = {
  corretora_id: number;
  nome: string;
  label: string;
  /** nome que assina a copy naquela linha (Pessoa12 = "Pessoa"). */
  nome_na_copy: string;
  /** false = sem linha Cloud → a automação NÃO roda pra ela. */
  tem_linha_cloud: boolean;
  na_roleta: boolean;
  posicao_roleta: number | null;
  textos: Record<string, string>;
  previews: Record<string, string>;
  /** A linha Cloud dela com a saúde (PENDENCIAS-BACKEND, item 5). Ausente =
   *  servidor ainda não manda; a tela junta pelo slug do número. */
  linha?: { slug: string | null; telefone: string | null; saude: SaudeDoNumero | null } | null;
};

// GET /api/automacoes/boas-vindas/metricas?dias=N
// `taxa: null` = SEM DADO (ninguém recebeu) — diferente de 0%. `participacao`
// null = a linha não recebeu lead nenhum no período (não é "0% da roleta").
export type BlocoBoasVindas = {
  enviadas: number;
  responderam: number;
  taxa: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
};

export type MetricasBoasVindas = {
  gerado_em: string;
  dias: number;
  desde: string;
  ate: string;
  medindo_desde: string | null;
  motivo_do_marco: string | null;
  marco_por: string | null;
  amostra_minima: number;
  total: BlocoBoasVindas & { simuladas: number } & EntregaDaJanela;
  por_corretora: (BlocoBoasVindas & {
    corretora_id: number | null;
    nome: string;
    na_roleta: boolean;
    posicao_roleta: number | null;
    participacao: number | null;
  })[];
  variantes: (BlocoBoasVindas & {
    id: string;
    rotulo: string;
    peso: number;
    ativa: boolean;
    estado: string;
    aguardando: boolean;
    controle: boolean;
  })[];
  roleta: {
    corretoras: number;
    participacao_ideal: number | null;
    sem_dado: boolean;
  };
  definicao: string;
};

export type Gatilho = {
  id: string;
  quando: string;
  entao: string;
  estado: "ligado" | "desligado";
  flag: string | null;
  detalhe: string;
  /** true = tira o lead da régua · false = não tira · null = não é saída. */
  interrompe?: boolean | null;
};

export type Marco = { etapa: string; tag: string | null; copy: string | null };

// ── Remarketing (carrossel da Corretor) ───────────────────────────────────────────

/**
 * Placar de entrega (`remarketing_placar.placar` no copiloto). Os quatro baldes
 * somam `mandados`. `em_voo` (aceito/enviado, sem confirmação) NÃO é entrega.
 */
export type PlacarEntrega = {
  de: string;
  ate: string;
  mandados: number;
  entregues: number;
  em_voo: number;
  falhas: number;
  /** Envio anterior a 04/10/2026, sem id da mensagem: não dá pra saber. */
  sem_rastro: number;
  /** Sobre os envios COM rastro. null = nenhum envio rastreável no lote. */
  pct_falha: number | null;
  pct_entregue: number | null;
  culpas: Partial<Record<"lista" | "conta" | "template" | "desconhecida", number>>;
  motivos: { codigo: string | null; titulo: string | null; n: number; culpa: string }[];
};
// Variante do teste A/B de copy. `texto_igual_a` lista as OUTRAS variantes que
// dividem o MESMO texto: hoje `direta` e `direta_marketing` são o teste de
// CATEGORIA (mesma copy, categorias diferentes). Por isso `texto_editavel` é
// false — editar um lado só transforpessoa o teste de categoria em teste de copy.
export type VarianteCarrossel = {
  id: string;
  peso: number;
  peso_padrao: number;
  categoria: string; // "utility" | "marketing" (a PRETENDIDA)
  texto: string;
  ativa: boolean;
  texto_igual_a: string[];
  texto_editavel: boolean;
  // Edição do texto pela tela (05/10/2026). Opcionais: servidor antigo não manda.
  /** Texto escrito no código (o "voltar ao padrão"). */
  texto_padrao?: string;
  texto_personalizado?: boolean;
  /** Desde quando ESTE texto vale. null = é o do código e ninguém mexeu. */
  texto_desde?: string | null;
  /** Id da variante cujo texto esta acompanha (o teste de categoria). */
  espelha?: string | null;
  espelhando?: boolean;
  /** A mensagem inteira como chega no celular (casco do template + texto). */
  previa?: string;
};

/** Bloco da edição de copy: espelho, teto, problema no gravado e histórico. */
export type CopiesDoCarrossel = {
  espelho: boolean;
  texto_max: number;
  /** O mesmo motivo que segura o disparo. null = serve. */
  problema: string | null;
  indisponivel: string | null;
  historico:
    | {
        variante: string | null;
        espelho: boolean | null;
        antes: string | null;
        depois: string | null;
        depois_e_o_padrao?: boolean;
        autor: string | null;
        em: string | null;
      }[]
    | null;
  historico_erro: string | null;
};

/** "Quando a pessoa responde": o que o handoff lê, do jeito que a tela desenha. */
export type QuandoResponde = {
  /** Motivo pelo qual NINGUÉM está sendo transferido agora. null = está valendo. */
  problema: string | null;
  motivo: string | null;
  detalhe?: string;
  tag: string;
  quem_vai?: "todos" | "interesse" | null;
  quem_vai_padrao?: string;
  etapa?: {
    valendo: number | null;
    valendo_nome: string | null;
    gravada: number | null;
    padrao: number | null;
    opcoes: { id: number; nome: string; permitida: boolean; padrao: boolean }[];
  };
  roleta?: {
    ids: number[];
    validas: number[];
    da_vez: number | null;
    da_vez_nome: string | null;
    periodo_dias: number;
    corretoras: {
      id: number;
      nome: string;
      nome_publico: string | null;
      na_roleta: boolean;
      recebeu: number;
    }[];
  };
  roleta_do_meta?: { ids: number[]; nomes: string[] };
  aviso?: {
    ligado: boolean;
    com_resumo: boolean;
    texto: string | null;
    padrao: string;
    personalizado: boolean;
    problema: string | null;
    previa: string;
    previa_de: string | null;
    max: number;
    placeholder: string;
  };
  em_respondeu?: number;
  // Não transferir quem já conversa pelo celular da linha (05/10). null = valor
  // torto no banco; ausente = servidor antigo.
  respeita_humano?: boolean | null;
  segurados_humano?: number;
  // IA do lado (05/10): resumo no card e opinião no que a regra não entendeu.
  // Ausente = servidor antigo (a tela não mostra o bloco).
  ia?: {
    resumo_ligado: boolean | null; // null = valor torto no banco (vale desligado)
    opina_ligada: boolean | null;
    corte: number | null;
    corte_padrao: number;
    corte_problema: string | null;
    prazo_opiniao_s: number;
    falha_resumo: string;
    falha_opiniao: string;
    para_que_serve_opiniao: string;
    ultimos_30_dias: {
      resumos_ok: number;
      resumos_falhou: number;
      opinioes: number;
      pesou: number;
      recusas_ia: number;
    } | null;
  };
};

// O que NÃO entra na taxa de resposta do remarketing (05/10/2026): só conta
// interesse e clique no botão. Opcional porque backend antigo não manda.
export type ForaDaTaxa = {
  recusas: number;
  pararam: number;
  automaticas: number;
  indefinidas: number;
  numeros_errados?: number;
};

// Respostas por tipo (junção com o motor, 05/10/2026). `interesse` + `botao`
// fecham com `interessado`, que é o que entra na taxa; o resto fica fora dela.
// Opcional em tudo: backend antigo não manda, e aí a tela não inventa zero.
export type RespostasPorTipo = {
  interessado: number;
  interesse?: number;
  botao?: number;
  parar: number;
  outro: number;
  disse_nao: number;
  sem_sinal: number;
  numero_errado: number;
  automatica: number;
};

// GET /api/automacoes/remarketing/metricas?dias=N
// `taxa: null` = SEM DADO (ninguém recebeu) — diferente de 0%.
export type MetricaVariante = VarianteCarrossel & {
  fora_da_taxa?: ForaDaTaxa;
  respostas_por_tipo?: RespostasPorTipo;
  peso_personalizado: boolean;
  enviados: number;
  respostas: number;
  taxa: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
  /** Categoria que SAIU de verdade (marketing cai em UTILITY sem cota/template). */
  por_categoria: { categoria: string; enviados: number; respostas: number }[];
  /** O que a variante mediu com OUTRO texto no período (fora da taxa de hoje). */
  textos_anteriores?: {
    enviados: number;
    respostas: number;
    taxa: number | null;
  } | null;
};

export type MetricasRemarketing = {
  gerado_em: string;
  dias: number;
  desde: string;
  ate: string;
  medindo_desde: string | null;
  motivo_do_marco: string | null;
  marco_por: string | null;
  amostra_minima: number;
  cota_marketing_pct: number;
  variantes: MetricaVariante[];
  copies?: CopiesDoCarrossel;
  /** Disparo que não é de nenhuma variante de hoje (ex: antes do teste A/B). */
  fora_do_teste: {
    variante: string;
    enviados: number;
    respostas: number;
    taxa: number | null;
    sem_dado: boolean;
    amostra_pequena: boolean;
  }[];
  estoque: {
    fila: number | null;
    disparado: number | null;
    respondeu: number | null;
  };
  total: {
    enviados: number;
    respostas: number;
    taxa: number | null;
    sem_dado: boolean;
    amostra_pequena: boolean;
    fora_da_taxa?: ForaDaTaxa;
    respostas_por_tipo?: RespostasPorTipo;
  };
  definicao: string;
};

// Saúde de envio por automação: quando cada uma mandou a última mensagem que
// SAIU DE VERDADE. "ligado" e "funcionando" são coisas diferentes — a nutrição
// aparecia ligada havia semanas sem nunca ter enviado nada real.
// `dias: null` + `nunca_enviou: true` = nasceu morta (não é "faz 0 dias").
export type SaudeEnvio = {
  disponivel: boolean;
  ultimo_envio_real: string | null;
  dias: number | null;
  nunca_enviou: boolean | null;
  alerta: boolean;
  /** Limiar do alerta, POR automação — cada uma tem ritmo próprio. */
  limiar_dias: number;
  fonte: string;
};

export type AgendaRemarketing = {
  /** Horário em que o timer do servidor acorda o disparo, "09:00-19:00". */
  timer: string;
  /** "09:00-18:00" ou null = sem janela própria. */
  janela_horario: string | null;
  /** 0=seg … 6=dom. Vazio = todo dia. */
  dias_semana: number[];
  /** null = quem manda é a rampa. */
  cap_diario: number | null;
  /** null = sem limite na semana. */
  cap_semanal: number | null;
  /** null = não consegui contar (nunca 0). */
  enviados_semana: number | null;
  enviados_semana_indisponivel: boolean;
  saldo_semana: number | null;
  /** Por que NÃO dispara agora (dia, hora, teto semanal) — null se pode. */
  bloqueio_agora: string | null;
  bloqueio_agora_texto: string | null;
  /** Valor gravado por fora da tela que o motor não entende: ele segura. */
  ilegivel: string | null;
};

export type SaudeLinha = {
  slug: string;
  phone_number_id: string | null;
  numero: string | null;
  nome: string | null;
  qualidade: string | null;
  status: string | null;
  cor: "verde" | "amarelo" | "vermelho" | "desconhecida";
  /** Hora em que a Meta respondeu ESTES números (ISO UTC). null = nunca leu. */
  lido_em: string | null;
  /** true = a Meta não respondeu agora; isto é a última leitura boa. */
  velha: boolean;
  erro: string | null;
  /** De quem é esta saúde: do disparador ou, sem disparador, da linha da casa
   *  (pausada). Sem isto a tela pintaria a cor sem dizer de qual número. */
  de_qual?: "disparador" | "linha_da_casa_sem_marca_de_disparo";
};

/** Saúde de UM número do cadastro (todos têm, não só o disparador).
 *  `cor: "desconhecida"` = sem leitura; nunca vira verde por falta de dado. */
export type SaudeDoNumero = {
  cor: "verde" | "amarelo" | "vermelho" | "desconhecida";
  qualidade?: string | null;
  status?: string | null;
  lido_em?: string | null;
  velha?: boolean;
  erro?: string | null;
};

/** Limite por dia: o que foi pedido na tela, o que vale hoje e por quê. */
export type TetoDisparo = {
  pedido: number | null;
  rampa: number | null;
  /** null = não consegui ler a rampa — nunca um número inventado. */
  efetivo: number | null;
  /** true = o número pedido NÃO é o que sai. A tela tem que dizer. */
  diverge: boolean;
  explicacao: string | null;
  /** Quanto cabe no horário configurado (3 a cada 3 min). null = não calculado. */
  cabe_no_horario?: number | null;
};

export type PapelDaLinha = "disparo" | "atendimento" | "pausado";

/** Um número do cadastro e pra que ele serve. */
export type NumeroDoCliente = {
  phone_number_id: string;
  telefone: string | null;
  nome: string | null;
  /** Texto livre de propósito: valor escrito por fora da tela aparece como está. */
  papel: string;
  slug: string | null;
  /** O remarketing consegue sair por este número? */
  operavel: boolean;
  /** null = número que este sistema não opera (não há como ler a saúde). */
  saude?: SaudeDoNumero | null;
};

/** O dia de hoje, separando o que a Meta ACEITOU do que CHEGOU. */
export type RitmoHoje = {
  /** O que gastou o teto: aceitos + reservas abertas. null = não consegui contar. */
  conta_no_teto: number | null;
  aceitos: number;
  entregues: number;
  lidos: number;
  falhas: number;
  sem_confirmacao_ainda: number;
  sem_rastro: number;
  reservas_abertas: number;
  incertos: number;
  explicacao: string;
};

/** A última volta do robô, do registro durável. */
export type UltimaVolta = {
  em: string | null;
  /** null = o robô não conseguiu ler o painel naquela volta. */
  ligado: boolean | null;
  motivo_parada: string | null;
  motivo_texto: string | null;
  cap_do_dia: number | null;
  enviados: number | null;
  falhas: number | null;
  incertos: number | null;
};

/** Leads em Disparado sem disparo registrado. */
export type ReservasOrfas = {
  total: number;
  por_estado: Record<string, number>;
  mais_antiga: string | null;
  itens: {
    lead_id: number;
    nome: string | null;
    desde: string | null;
    estado: string;
    motivo: string | null;
    linha: string | null;
  }[];
  o_que_e: string;
};

export type AlarmeDoConferente = { codigo: string; texto: string; detalhe: string };

/** O fechamento de um dia, gravado pelo conferente. */
export type ConferenciaDoDia = {
  dia: string;
  /** true = o dia ainda não acabou quando foi conferido. */
  parcial: boolean;
  previstos: number;
  aceitos: number;
  entregues: number;
  lidos: number;
  falhas: number;
  em_voo: number;
  sem_rastro: number;
  reservas_abertas: number;
  incertos: number;
  respostas?: Record<string, number>;
  alarmes: AlarmeDoConferente[];
  conferido_em: string | null;
  aviso_enviado_em: string | null;
};

/** Pra onde vai quem pede pra parar: as etapas que servem e a que vale. */
export type DestinoParar = {
  /** A etapa que VALE agora (se a gravada não serve, é a Venda perdida). */
  etapa: number | null;
  gravada: unknown;
  padrao: boolean;
  invalido: string | null;
  opcoes: { id: number; nome: string; padrao: boolean }[];
};

/** Por qual número o remarketing sai. `slug: null` = não sai por nenhum. */
export type LinhaDisparadora = {
  slug: string | null;
  phone_number_id: string | null;
  motivo: string | null;
  motivo_texto: string | null;
  detalhe: string | null;
  numeros: NumeroDoCliente[];
  /** true = não consegui LER o cadastro (≠ "não há número"). */
  indisponivel: boolean;
};

export type EtapaDoFunil = {
  id: number;
  nome: string | null;
  funil: string | null;
  tipo: string | null;
  leads: number;
  /** Quantos o disparo PEGARIA: só os do dono configurado. */
  leads_do_dono: number;
  permitida: boolean;
  motivo_bloqueio: string | null;
  padrao: boolean;
};

/** De qual etapa e de quem são os leads do disparo. */
export type AlvoRemarketing = {
  etapa: number | null;
  dono: number | null;
  dono_nome: string | null;
  padrao: boolean;
  /** Valor gravado que o motor não aceita: ele segura o disparo. */
  invalido: string | null;
  motivo: string | null;
  etapas: EtapaDoFunil[];
  etapa_padrao: number;
  dono_padrao: number;
};

// ── Contrato do servidor das automações por corretora (06/10/2026) ──
// `AUTOMACOES-BACKEND.md`, seção 7. Tudo opcional: a tela continua desenhando
// "sem leitura" quando um campo não vem, nunca um zero inventado.

/** Uma linha (número Cloud) como cabe numa célula: `cor` já resolvida. */
export type LinhaCurta = {
  slug: string;
  telefone: string | null;
  /** false = o copiloto não tem o phone_number_id: nada sai por ela. */
  registrada: boolean;
  cor: "verde" | "amarelo" | "vermelho" | "desconhecida";
  saude: SaudeDoNumero | null;
  /** true = a última notícia da linha é recusa por COBRANÇA da Meta. */
  cobranca_bloqueada: boolean | null;
};

/** O que saiu numa janela (hoje = dia de São Paulo; semana = últimos 7 dias). */
export type JanelaEnvio = {
  saiu: number;
  entregue: number;
  lido: number;
  falhou: number;
  /** Saiu sem wamid gravado: não dá pra saber se entregou. */
  sem_rastro: number;
};

/** Uma linha da lista, decidida no servidor (`resumo_da_lista`). */
export type ResumoAutomacao = {
  id: "boas-vindas" | "follow-up" | "remarketing" | "nutricao";
  nome: string;
  estado: "indisponivel" | "desligada" | "parada" | "agindo" | "ligada";
  motivo: string | null;
  linhas: LinhaCurta[];
  numero_do_dia: { rotulo: string; valor: number | null };
  ultima_acao_em: string | null;
};

/** Interruptor por corretora: `mapa: null` + indisponivel = não li (≠ todas ligadas). */
export type InterruptorPorCorretora = {
  mapa: Record<string, boolean> | null;
  indisponivel: boolean;
  ilegivel: boolean;
  motivo: string | null;
  personalizado: boolean | null;
};

export type CorretoraRoleta = {
  corretora_id: number;
  nome: string;
  label: string;
  na_roleta: boolean;
  posicao: number | null;
  e_a_vez: boolean;
  recebeu_7d: number | null;
  recebeu_30d: number | null;
  /** false = recebe o lead, mas a boas-vindas não sai (sem linha registrada). */
  boas_vindas_sai: boolean;
  linha: LinhaCurta | null;
};

export type MudancaDeConfig = { chave: string; acao: string; antes: unknown; depois: unknown; autor: string | null; em: string | null };

/** GET /painel/automacoes/boas-vindas/roleta (e `boas_vindas.roleta` no mapa). */
export type RoletaMeta = {
  gravada: number[];
  valendo: number[];
  vez: number | null;
  vazia: boolean;
  aviso: string | null;
  ignoradas: number[];
  corretoras: CorretoraRoleta[];
  recebeu_indisponivel?: boolean;
  historico: MudancaDeConfig[] | null;
  historico_indisponivel?: boolean;
  regra_rmkt: string;
};

export type CorretoraFollowUp = {
  corretora_id: number;
  nome: string;
  label: string;
  /** A chave dela (null = não li). Fora do mapa = ligada. */
  ligada: boolean | null;
  /** O que vale de fato: o geral desligado vence. */
  ligada_efetiva: boolean | null;
  linha: LinhaCurta | null;
  sai_por: string | null;
  na_regua: number | null;
  devidas_agora: number | null;
  hoje: JanelaEnvio | null;
  semana: JanelaEnvio | null;
};

export type BlocoFollowUp = {
  indisponivel?: false;
  geral: { ligado: boolean | null; indisponivel: boolean; chave: string };
  chave: string;
  interruptor: InterruptorPorCorretora;
  corretoras: CorretoraFollowUp[];
  total_hoje: JanelaEnvio | null;
  total_semana: JanelaEnvio | null;
  envios_indisponivel: boolean;
  regua_indisponivel: boolean;
  definicao: string;
};

export type CorretoraNutricao = Omit<CorretoraFollowUp, "devidas_agora"> & {
  devidos_agora: number | null;
  linha_da_corretora: string | null;
  telefone_fora_do_padrao: number | null;
  /** Por que o marco do lead dela sai por `sai_por` (07/10). Servidor antigo não manda. */
  motivo_linha?: "dona_com_linha" | "dona_sem_linha" | "template_nao_aprovado_na_waba" | "chave_desligada" | string;
  motivo_linha_texto?: string;
};

export type BlocoNutricao = Omit<BlocoFollowUp, "corretoras"> & {
  /** "corretor" quando toda corretora sai pela Pessoa12; "dona" quando cada uma sai pela sua (07/10). */
  sai_por: string;
  /** Servidor antigo não manda: aí vale "toda pela Pessoa12". */
  toda_pela_corretor?: boolean;
  pela_linha_da_dona?: { chave: string; ligada: boolean; problema: string | null };
  linha: LinhaCurta | null;
  por_qual_linha: string;
  corretoras: CorretoraNutricao[];
  /** null = não consegui contar. */
  telefone_invalido: { marcados: number; ainda_na_regua: number } | null;
};

/** Bloco que o servidor não conseguiu montar: a tela diz "não consegui ler". */
export type BlocoIndisponivel = { indisponivel: true; motivo: string };

/** GET /painel/automacoes/linhas */
export type LinhaDoCliente = {
  slug: string;
  instance: string;
  registrada: boolean;
  phone_number_id: string | null;
  telefone: string | null;
  nome: string | null;
  papel: PapelDaLinha | string | null;
  corretora_id: number | null;
  corretora: string | null;
  saude: SaudeDoNumero | null;
  cobranca_bloqueada: boolean | null;
};
export type LinhasDoCliente = {
  gerado_em: string;
  linhas: LinhaDoCliente[];
  lido_em: string;
  erros: Record<string, string>;
};

/** Entregue/lido/falha na mesma janela dos endpoints de métricas (pendência 8). */
export type EntregaDaJanela = {
  entregues?: number | null;
  lidos?: number | null;
  falhas?: number | null;
  sem_rastro?: number | null;
  entrega_indisponivel?: boolean;
};

export type Mapa = {
  gerado_em: string;
  /** Uma linha por automação, decidida no servidor. null = não consegui montar. */
  resumo?: ResumoAutomacao[] | null;
  /** Follow-up por corretora (interruptor, linha, régua, envios). */
  follow_up?: BlocoFollowUp | BlocoIndisponivel;
  /** chave = mesmo id usado na lista ("follow-up", "nutricao"…). */
  saude_envio?: Record<string, SaudeEnvio>;
  boas_vindas: BoasVindas & {
    por_corretora_indisponivel?: boolean;
    /** Leads em Novo que ainda não receberam (PENDENCIAS-BACKEND, item 7). */
    aguardando?: number | null;
    /** A roleta do Meta como o intake usa no próximo lead. */
    roleta?: RoletaMeta | null;
    roleta_indisponivel?: boolean;
    /** Saiu/entregue/falhou por corretora e quem está em Novo sem a mensagem. */
    envios?: {
      corretoras: { corretora_id: number; label: string; sai_por: string | null; linha: LinhaCurta | null; hoje: JanelaEnvio | null; semana: JanelaEnvio | null }[];
      em_novo: {
        total: number;
        sem_boas_vindas: number;
        sem_dona: number;
        por_corretora: Record<string, { total: number; sem_boas_vindas: number }>;
      } | null;
      em_novo_indisponivel: boolean;
      total_hoje: JanelaEnvio | null;
      total_semana: JanelaEnvio | null;
      envios_indisponivel: boolean;
    } | null;
    envios_indisponivel?: boolean;
  };
  operacao: {
    dry_run: boolean;
    cadencia_ativa: boolean;
    cap_diario: number;
    min_intervalo_s: number;
    cooldown_remarketing_dias: number;
    timer: {
      unit: string;
      on_calendar: string | null;
      enabled: boolean | null;
      janela: string;
    };
    matriculados: { por_status: Record<string, number>; total: number };
  };
  cadencia: {
    descricao: string;
    toques: Toque[];
    remarketing: {
      regra: string;
      intervalo_dias: number;
      copy: string;
      copy_padrao?: string;
      personalizada?: boolean;
      com_botao?: boolean;
      preview?: string | null;
      template?: string;
    };
  };
  carrossel_corretor: {
    titulo: string;
    linha: string;
    gatilho: string;
    ritmo: {
      por_dia: number;
      fonte_do_limite: string;
      /** Teto da rampa hoje, já considerando o limite pedido na tela. */
      cap_rampa?: number | null;
      motivo_rampa: string | null;
      /** Pedido × efetivo × por quê. Opcional: backend antigo não manda. */
      teto?: TetoDisparo;
      /** Maior número que o campo "por dia" aceita. */
      cap_maximo_aceito?: number;
      cap_configurado: number;
      /** null = não consegui ler (ver `disparos_hoje_indisponivel`) — nunca 0. */
      disparos_hoje: number | null;
      disparos_hoje_indisponivel?: boolean;
      disparos_hoje_motivo?: string | null;
      /** Aceito × entregue de hoje. null = não consegui ler (nunca zero). */
      hoje?: RitmoHoje | null;
      hoje_indisponivel?: boolean;
      hoje_motivo?: string | null;
      descanso_dias: number;
      detalhe: string;
    };
    variantes: VarianteCarrossel[];
    cota_marketing_pct: number;
    /**
     * ZERO ≠ AUSÊNCIA: `indisponivel` marca falha de leitura. Sem isso, banco
     * fora aparecia pra Dona como "não tem ninguém na fila".
     */
    estoque: {
      fila: number | null;
      disparado: number | null;
      respondeu: number | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    /**
     * O que a Meta ENTREGOU no último dia de disparo. Opcional: backend antigo
     * não manda. `placar: null` sem `indisponivel` = o carrossel nunca disparou.
     */
    entrega_ultimo_lote?: {
      placar: PlacarEntrega | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    /**
     * Quando e quanto o disparo roda, como gravado no painel, mais o gasto da
     * semana. Opcional: backend antigo não manda. `config: null` = não leu.
     */
    agenda?: {
      config: AgendaRemarketing | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    /** Qualidade da linha na Meta, com a hora da leitura. null = sem disparador. */
    saude_linha?: SaudeLinha | null;
    /** Por qual número sai + os números do cliente e o papel de cada um. */
    linha_disparadora?: LinhaDisparadora;
    /** De qual etapa e de quem. `config: null` = não consegui ler. */
    alvo?: {
      config: AlvoRemarketing | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    /** null sem `_indisponivel` = o robô nunca rodou (não é "rodou e parou"). */
    ultima_volta?: UltimaVolta | null;
    ultima_volta_indisponivel?: boolean;
    reservas_orfas?: ReservasOrfas | null;
    reservas_orfas_indisponivel?: boolean;
    /** `dias: null` = não consegui ler; `[]` = o conferente nunca fechou um dia. */
    conferencia?: {
      dias: ConferenciaDoDia[] | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    parar?: {
      config: DestinoParar | null;
      indisponivel?: boolean;
      motivo_indisponivel?: string | null;
    };
    /** `ligado: null` = não consegui ler (≠ desligado). */
    aviso_telegram?: { ligado: boolean | null; indisponivel: boolean };
    quando_responde: { passo: number; quem: string; onde: string; o_que: string }[];
    copies?: CopiesDoCarrossel;
    /** `config: null` = não consegui ler. */
    responde?: {
      config: QuandoResponde | null;
      indisponivel: boolean;
      motivo_indisponivel: string | null;
    };
    travas: string[];
    /** `indisponivel` = o servidor não leu o painel; o robô também não envia. */
    estado: "ligado" | "desligado" | "indisponivel";
    /** Chave de config que liga/desliga ESTA automação (não a da cadência). */
    flag?: string;
  };
  nutricao: {
    descricao: string;
    regra_entrada: string;
    intervalo_dias: number;
    etapa_final: string;
    marcos: Marco[];
    copies_status: string;
    /** Estado do kill-switch PRÓPRIO da nutrição (`nutricao_ativa`, 05/08).
     *  Antes a lista mostrava o estado da cadência aqui — vira mentira assim
     *  que as duas divergirem. */
    estado: "ligado" | "desligado";
    flag: string;
    /** Por qual linha os marcos saem (PENDENCIAS-BACKEND, item 6). Ausente =
     *  servidor ainda não manda; hoje o motor manda pela da Pessoa12. */
    linha?: { slug: string | null; telefone: string | null; saude: SaudeDoNumero | null } | null;
    /** A corretora dona dessa linha (a Pessoa12), pro limite por dia. */
    linha_corretora_id?: number | null;
    /** Nutrição por corretora (dona do lead): interruptor, régua, envios, telefones. */
    por_corretora?: BlocoNutricao | BlocoIndisponivel;
  };
  gatilhos: Gatilho[];
  todo: string;
  error?: string;
};

// GET /api/automacoes/metricas?dias=N — engajamento por toque.
// `taxa: null` = SEM DADO (ninguém recebeu), que é diferente de 0% (recebeu e
// ninguém respondeu). Toque de ligação vem com tem_taxa=false.
export type MetricaToque = {
  indice: number;
  dia: string;
  offset_dias: number;
  tipo: "mensagem" | "tarefa_ligacao";
  canal: string;
  categoria: string;
  enviados: number | null;
  responderam: number | null;
  opt_outs: number | null;
  tarefas?: number;
  taxa: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
  tem_taxa: boolean;
};

export type Metricas = {
  gerado_em: string;
  dias: number;
  desde: string;
  ate: string;
  /** Marco de régua: data a partir da qual a métrica conta. null = mede tudo. */
  medindo_desde: string | null;
  motivo_do_marco: string | null;
  marco_por: string | null;
  amostra_minima: number;
  toques: MetricaToque[];
  total: {
    enviados: number;
    responderam: number;
    opt_outs: number;
    tarefas: number;
    taxa: number | null;
    sem_dado: boolean;
    amostra_pequena: boolean;
  } & EntregaDaJanela;
  // Visita: o que a resposta VIROU. `marcadas` e `compareceram` são contagens
  // SEPARADAS — somar as duas faz a tela dizer que a automação entrega o dobro.
  // `vencidas_sem_confirmacao` é pendência de registro, não estatística.
  visitas?: {
    disponivel: boolean;
    marcadas: number | null;
    compareceram: number | null;
    vencidas_sem_confirmacao: number | null;
    taxa_comparecimento: number | null;
    sem_dado: boolean;
    amostra_pequena?: boolean;
  };
  definicao: string;
};

// GET /api/automacoes/nutricao/metricas?dias=N — engajamento por MARCO da
// nutrição + fila atual por etapa.
//
// `taxa: null` = SEM DADO (ninguém recebeu) — diferente de 0%. A etapa de
// ENTRADA (Base) vem com tem_taxa=false: o lead cai lá ao esgotar o follow-up e
// ainda não recebeu mensagem nenhuma. `em_fila: null` = a contagem não está
// disponível (funil ainda não existe no banco), NÃO "está vazia".
export type MetricaMarco = {
  etapa: string;
  ordem: number;
  tag: string | null;
  copy: string | null;
  tem_taxa: boolean;
  final: boolean;
  em_fila: number | null;
  /** dos que estão na etapa, quantos NÃO têm próxima data (congelados —
   *  `proximo_toque_em` NULL fora do marco final = nunca mais recebem nada). */
  sem_agenda: number | null;
  enviados: number | null;
  responderam: number | null;
  opt_outs: number | null;
  taxa: number | null;
  sem_dado: boolean;
  amostra_pequena: boolean;
};

export type MetricasNutricao = {
  gerado_em: string;
  dias: number;
  desde: string;
  ate: string;
  medindo_desde: string | null;
  motivo_do_marco: string | null;
  marco_por: string | null;
  amostra_minima: number;
  intervalo_dias: number;
  etapa_entrada: string;
  etapa_final: string;
  marcos: MetricaMarco[];
  fila: {
    disponivel: boolean;
    total: number | null;
    por_etapa: Record<string, number>;
    sem_agenda_por_etapa: Record<string, number> | null;
    sem_agenda_total: number | null;
  };
  /** false hoje: as copies dos marcos vivem em código, sem tabela de override. */
  copies_editaveis: boolean;
  total: {
    enviados: number;
    responderam: number;
    opt_outs: number;
    taxa: number | null;
    sem_dado: boolean;
    amostra_pequena: boolean;
  } & EntregaDaJanela;
  definicao: string;
};

export const STATUS_LABEL: Record<string, string> = {
  ativa: "na cadência",
  respondida: "responderam",
  remarketing: "em nutrição",
  opt_out: "opt-out",
  concluida: "concluídas",
  // Telefone fora de 55 + 10/11 dígitos: a nutrição marca e tira da régua (06/10).
  telefone_invalido: "telefone inválido",
};
