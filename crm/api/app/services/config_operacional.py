"""Configuração de operação editável pelo painel, em vez do arquivo `.env`.

Por que existe: ligar/desligar a cadência, mudar o limite diário de uma
corretora ou tirar alguém da roleta eram edições de `.env` + restart do serviço
— ou seja, passavam por mim, no terminal. Num produto que o cliente opera
sozinho isso não fecha: é a diferença entre "painel bonito" e SaaS.

Como funciona: cada chave é uma linha em `config_operacional` (JSONB). Quem
pergunta usa as funções daqui, nunca o `settings.*` direto.

**Fallback é a regra, não a exceção.** Chave ausente → vale o `.env` de sempre.
Isso é o que torna a migração segura: enquanto ninguém tocar no painel, o
comportamento é byte a byte o de antes. E se o banco cair no meio de um tick, a
cadência não vira "desligada por acidente" — ela segue com o valor do arquivo.

Cache de 30s: o tick roda a cada 3 min, então desligar pelo painel tem efeito
no próximo ciclo. Curto o suficiente pra parecer imediato, longo o suficiente
pra não bater no banco a cada lead.

## A exceção: o remarketing falha FECHADO (05/10/2026)

O fallback acima vale pros motores que só perdem se pararem (cadência,
boas-vindas). O disparo em massa é o contrário: "não consegui ler o painel"
virar "vale o padrão" é como uma pausa gravada pela Dona deixa de valer por uma
falha de conexão. Pras chaves de `CHAVES_ESTRITAS`:

- leitura que FALHA não é chave AUSENTE: `get` levanta `ConfigIndisponivel` e
  o tick não envia (`motivo_parada="config_indisponivel"`);
- o motor relê o banco na hora (`recarregar_estrito`), sem cache, no começo da
  volta e antes de CADA mensagem;
- o interruptor não tem mais padrão de `.env`: sem linha no banco é DESLIGADO.

Toda gravação e todo "voltar ao padrão" deixa uma linha em
`config_operacional_historico` (quem, quando, de, para) — migration 0090.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import structlog
from sqlalchemy import text as sql_text

from app.config import settings
from app.db.session import SessionLocal

log = structlog.get_logger()

_TTL_SEC = 30
_cache: dict[str, Any] = {}
_cache_em: float = 0.0
# Por que a ÚLTIMA carga falhou (None = leu). É o que separa "a chave não está
# no banco" de "não consegui ler o banco".
_cache_erro: str | None = None
_lock = threading.Lock()


class ConfigIndisponivel(RuntimeError):
    """Não deu pra ler `config_operacional`. Pra chave estrita isto NÃO vira
    padrão: quem lê segura o envio."""


# Chaves do disparo em massa. Leitura que falha levanta em vez de cair no
# padrão — ver "A exceção" no cabeçalho.
CHAVES_ESTRITAS: frozenset[str] = frozenset({
    "carrossel_corretor_ativo",
    "carrossel_pesos",
    "carrossel_auto_resposta_segundos",
    "remarketing_janela_horario",
    "remarketing_dias_semana",
    "remarketing_cap_diario",
    "remarketing_cap_semanal",
    "remarketing_etapa",
    "remarketing_dono",
    "remarketing_etapa_parar",
    "remarketing_conferente_avisa_telegram",
    # Texto das variantes (05/10): copy ilegível segura o disparo.
    "carrossel_copies",
    "carrossel_marketing_espelha_direta",
    # "Quando a pessoa responde" (05/10): quem vai pra corretora, pra qual
    # etapa, por qual roleta e com qual aviso. Leitura que falha NÃO transfere
    # (o card fica em Respondeu, visível) — melhor que transferir pelo padrão
    # pra uma roleta que a Dona acabou de mudar.
    "handoff_remarketing_quem_vai",
    "handoff_remarketing_etapa",
    "handoff_remarketing_roleta",
    "handoff_remarketing_avisa_lead",
    "handoff_remarketing_aviso_texto",
    "handoff_remarketing_aviso_resumo",
    "handoff_remarketing_respeita_humano",
    # Interruptor POR CORRETORA do follow-up e da nutrição (06/10): o motor
    # relê antes de cada envio (`automacao_por_corretora.pode_enviar`) e
    # leitura que falha segura a mensagem.
    "cadencia_por_corretora",
    "nutricao_por_corretora",
})

# "Voltar ao padrão" nestas chaves GRAVA o padrão em vez de apagar a linha: a
# linha é a decisão de alguém (em produção, o desligado de 21/09 "pedido da
# Dona"), e apagá-la deixava o interruptor sem dono e sem data.
CHAVES_QUE_NAO_SE_APAGAM: frozenset[str] = frozenset({"carrossel_corretor_ativo"})

# Chaves conhecidas → de onde vem o default quando a linha não existe.
# Manter esse mapa é o contrato: chave fora daqui o painel recusa gravar, pra
# ninguém criar "cadencia_ativo" (sem o 'a') e passar uma tarde caçando o motivo
# de o botão não fazer nada.
DEFAULTS: dict[str, Any] = {
    "cadencia_ativa": lambda: bool(settings.cadencia_ativa),
    # Dia útil (02/10/2026): cadência, nutrição e mensagem planejada seguram o
    # envio no sábado e no domingo e soltam na segunda. Nasce LIGADO porque só
    # SEGURA mensagem — não cria envio novo e não perde nenhum (tudo que está
    # devido continua devido). Ver app/services/dia_util.py.
    "envio_dias_uteis": lambda: bool(settings.envio_dias_uteis),
    # Mensagem que sai sozinha quando a corretora marca "atendeu e quer
    # continuar" na ligação. Nasce DESLIGADA de propósito: é envio automático
    # pra lead real, e quem arma isso é a Dona, não um deploy.
    # Meta de ligação POR corretora: {"7": 100}. Chave = corretora_id em texto
    # (JSON não tem chave numérica). Quem não está aqui usa `ligacao_meta_diaria`.
    "ligacao_meta_por_corretora": lambda: {},
    "pos_ligacao_ativo": lambda: False,
    # PONTE TEMPORÁRIA (09/09): deixa a pós-ligação usar template MARKETING
    # enquanto a versão UTILITY não sai da análise. Marketing custa ~10x e conta
    # como promoção pra Meta — desligar isto assim que a UTILITY aprovar.
    "pos_ligacao_permite_marketing": lambda: False,
    # Kill-switch PRÓPRIO da nutrição (marcos 30/60/90/120/150 dias).
    #
    # Até 05/08 a nutrição lia `cadencia_ativa` direto: desligar o follow-up
    # parava os marcos junto, sem ninguém perceber. São riscos diferentes — o
    # follow-up fala com lead quente, a nutrição com base fria de 30 a 150 dias.
    #
    # O default HERDA o valor efetivo de `cadencia_ativa` (não o `.env`): sem
    # linha no banco nada muda de estado no dia da separação, inclusive se a
    # cadência já estiver desligada pelo painel. Depois que alguém grava a
    # chave, as duas passam a ser independentes de verdade.
    "nutricao_ativa": lambda: bool(get("cadencia_ativa")),
    "cadencia_cap_diario": lambda: int(settings.cadencia_cap_diario),
    # Interruptor POR CORRETORA (06/10/2026), mapa corretora → true/false.
    # Fora do mapa = ligada; o geral desligado vence. Ver
    # app/services/automacao_por_corretora.py. Nascem vazios: publicar não
    # desliga ninguém.
    "cadencia_por_corretora": lambda: {},
    "nutricao_por_corretora": lambda: {},
    # Nutrição pela linha da DONA do lead (07/10/2026, decisão da Dona): o marco
    # sai pela linha Cloud da corretora dona quando ela tem linha registrada e
    # modelo sem botão aprovado como UTILITY na WABA dela; senão, pela da Pessoa12.
    # Nasce LIGADA porque é a decisão dela. Desligada = tudo pela Pessoa12, como antes.
    # O motor relê do banco antes de cada marco; valor torto = Pessoa12.
    "nutricao_pela_linha_da_dona": lambda: True,
    "cadencia_cap_por_corretora": lambda: dict(settings.cadencia_cap_por_corretora),
    "roleta_corretoras": lambda: list(settings.roleta_corretoras),
    # Boas-vindas (automação separada da cadência): kill-switch e copy.
    # `lead_intake_ativo` era SÓ env (LEAD_INTAKE_ATIVO) — o interruptor da
    # página de boas-vindas seria decorativo sem esta chave.
    "lead_intake_ativo": lambda: bool(settings.lead_intake_ativo),
    # Override do corpo da boas-vindas POR VARIANTE do A/B:
    # {"cadastro": "Sou a {nome}, da Acme..."}. Vazio = texto do código
    # (`boas_vindas_copies.VARIANTES`).
    # A chave é a VARIANTE, não a corretora: o que muda de uma linha pra outra é
    # só o `{nome}`, e guardar uma cópia por corretora faria o teste A/B virar
    # seis copies soltas na primeira edição.
    "boas_vindas_copies": lambda: {},
    # Pesos do A/B da boas-vindas: {"cadastro": 50, "triagem": 50}.
    # Vazio = os pesos do código.
    "boas_vindas_pesos": lambda: {},
    # Remarketing (carrossel da Corretor). O interruptor da automação era só o `.env`
    # (`CARROSSEL_CORRETOR_ATIVO`) — ligar/desligar o disparo em massa do 0000
    # passava por terminal. Desde 05/10/2026 a fonte é SÓ o banco: sem linha é
    # DESLIGADO. O `.env` (`CARROSSEL_CORRETOR_ATIVO`) não liga mais nada — disparo
    # em massa não pode voltar a ligar porque alguém apagou uma linha.
    "carrossel_corretor_ativo": lambda: False,
    # Resposta automática do WhatsApp do lead (05/10/2026): quem "responde" em
    # menos de N segundos depois do nosso envio é o aparelho, não a pessoa —
    # fica em Disparado e fora da taxa de resposta. 5 é a decisão da Dona; o
    # dado (395 respostas reais) mostra as 16 abaixo de 5 s todas automáticas e
    # a pessoa mais rápida em 10,5 s. Zero desliga a regra do relógio (a do
    # conteúdo continua). Ver app/services/remarketing_resposta.py.
    "carrossel_auto_resposta_segundos": lambda: 5,
    # Fatia de cada variante do teste A/B do carrossel: {"direta": 40, ...}.
    # Vazio = os pesos do código. Validado em `carrossel_copies.validar_pesos`
    # (soma 100 + cota de MARKETING) antes de gravar.
    "carrossel_pesos": lambda: {},
    # Texto de cada variante ATIVA, editado na tela: {"direta": "..."}. Vazio =
    # o texto do código. Validado em `carrossel_copies.validar_copies`; o
    # disparo confere de novo antes de cada mensagem e segura se não servir.
    "carrossel_copies": lambda: {},
    # A variante de MARKETING usa o mesmo texto da `direta` (é o teste de
    # categoria). Ligado por padrão; desligar deixa editar as duas separadas.
    "carrossel_marketing_espelha_direta": lambda: True,
    # ── Meta de ligação por corretora (05/08) ────────────────────────────────
    # A Dona quer 100 ligações por semana. Dividido em 5 dias daria 20/dia — ela
    # prefere 25 pra sobrar folga e a semana poder fechar antes da sexta.
    # Fica em config (e não cravado no código) porque é número de OPERAÇÃO: muda
    # com o tamanho do time e com a época do ano, e mudar não pode exigir deploy.
    "ligacao_meta_diaria": lambda: 25,
    "ligacao_meta_semanal": lambda: 100,
    # ── Anti-ghosting (05/08) ────────────────────────────────────────────────
    # Reabre conversa que parou no meio, com texto livre dentro da janela de
    # 24h. Nasce DESLIGADO e sem chave no `.env` de propósito: é a primeira
    # automação que fala com lead sem template, e ligar tem que ser um ato
    # deliberado da Dona no painel — nunca efeito colateral de um deploy.
    "anti_ghosting_ativo": lambda: False,
    # No-show (05/08): cobra quem marcou visita e não apareceu, no dia seguinte.
    # Dispara só em falta MARCADA — visita vencida sem registro não é palpite da
    # automação, é cobrança da corretora no painel.
    "no_show_ativo": lambda: False,
    # ── Régua do proprietário (16/09) ────────────────────────────────────────
    # "Seu imóvel ainda está à venda?" a cada 40 dias, pela linha da casa.
    # NASCE FALSE: sai pelo 0000-0000, que é o telefone DA CASA, e pergunta
    # sobre o imóvel de gente real. Ligar é decisão da Dona, e depende do
    # template estar aprovado como UTILITY na WABA da Corretor.
    "regua_proprietario_ativa": lambda: False,
    # ── Handoff automático do remarketing (05/09) ────────────────────────────
    # Lead da linha da Corretor que mostra interesse real vira card da corretora
    # humana na hora (app/services/handoff_remarketing.py). NÃO manda WhatsApp
    # — é só ownership + etapa + evento — por isso nasce LIGADO (default do
    # .env). O destino fica aqui e não no código porque a roleta da Acme
    # mudou 4x em agosto: trocar quem recebe não pode exigir deploy.
    "handoff_remarketing_ativo": lambda: bool(settings.handoff_remarketing_ativo),
    "handoff_remarketing_corretora": lambda: int(settings.handoff_remarketing_corretora),
    # ROLETA DO REMARKETING (18/09). Quem sai da Corretor é dividido entre Pessoa10(1),
    # Pessoa11(2) e Pessoa12(3) em round-robin. SEPARADA da `roleta_corretoras`: aquela é
    # do lead novo do Meta, onde a Pessoa12 continua FORA (regra da Dona 26/07 —
    # divisão de papel, não performance). Lista vazia = destino único de
    # `handoff_remarketing_corretora`.
    "handoff_remarketing_roleta": lambda: list(settings.handoff_remarketing_roleta),
    # Cap diário do aviso, POR LINHA (18/09). As linhas das corretoras passam
    # semanas sem disparar template; 30 de uma vez é pico, e pico é o que a Meta
    # lê como qualidade. 10/dia/linha enche a fila em 3 dias sem assustar.
    "aviso_remarketing_cap_linha": lambda: 10,
    # AVISO PELA LINHA DA CORRETORA (15/09). Medido em setembro: 70 leads
    # responderam ao remarketing, 24 foram pra Pessoa12 e só 11 receberam mensagem —
    # os outros 13 vieram por esta rota, que era silenciosa de propósito (o
    # aviso era o card na coluna "Novo"). Card só funciona pra quem olha o
    # painel o tempo todo; mensagem chega. Decisão da Dona: TODO transferido
    # recebe.
    # Nasce DESARMADO, como toda automação que fala com lead real: quem arma é
    # a Dona no painel, nunca um deploy.
    "handoff_remarketing_avisa_lead": lambda: False,
    # ── Quando a pessoa responde (05/10) ─────────────────────────────────────
    # QUEM vai pra corretora: "todos" = todo mundo que cair em Respondeu
    # (decisão da Dona, 05/10: inclui "bom dia", áudio, figurinha); "interesse"
    # = só botão ou interesse lido (o comportamento até 05/10). Recusa, parar e
    # resposta automática nunca vão, em nenhum dos dois.
    "handoff_remarketing_quem_vai": lambda: "todos",
    # Etapa de entrada no funil de vendas. None = "Novo" do funil principal.
    "handoff_remarketing_etapa": lambda: None,
    # O {{2}} do aviso que o lead recebe pela linha da corretora. None = o
    # texto da Dona (`quando_responde.AVISO_PADRAO`). Aceita `{corretora}`.
    "handoff_remarketing_aviso_texto": lambda: None,
    # Lembrar no aviso o que a pessoa disse ("você me disse que…"). Saiu do
    # padrão em 05/10; fica como opção desligada.
    "handoff_remarketing_aviso_resumo": lambda: False,
    # Não transferir quem já está em conversa pelo celular da linha da Corretor
    # (adendo da orquestradora, 05/10/2026). LIGADO por padrão: tem gente
    # atendendo o 0000-0000 ao vivo e a roleta não pode tirar a conversa da mão
    # dela. O card cai em Respondeu, não é transferido nem avisado, e o motivo
    # fica no card. Desligado = "todo mundo" sem exceção.
    "handoff_remarketing_respeita_humano": lambda: True,
    # ── IA do lado (05/10/2026) ──────────────────────────────────────────────
    # Resumo por IA no card que vai pra corretora e opinião da IA no que a
    # regra chamou de `indefinida`. NÃO são estritas de propósito: leitura que
    # falha vale "desligado", que é o comportamento de antes — a IA nunca fica
    # no caminho do card. Ver app/services/ia_do_lado.py.
    "ia_resumo_card": lambda: True,
    "ia_opina_indefinida": lambda: True,
    "ia_opina_corte": lambda: _ia_corte_padrao(),
    # ── Agenda do remarketing (05/10) ────────────────────────────────────────
    # Quando e quanto o carrossel da Corretor dispara, pela tela. As quatro nascem
    # VAZIAS e vazio = comportamento de antes (sem janela própria, todo dia,
    # volume da rampa, sem teto semanal): publicar não muda um disparo sequer.
    # Formato, validação e o porquê de cada trava: `remarketing_agenda.py`.
    # `envio_dias_uteis` continua sendo dos OUTROS motores (cadência, nutrição,
    # mensagem planejada) — o carrossel nunca a leu e segue sem ler.
    "remarketing_janela_horario": lambda: None,
    "remarketing_dias_semana": lambda: [],
    "remarketing_cap_diario": lambda: None,
    "remarketing_cap_semanal": lambda: None,
    # ── Alvo do remarketing (05/10) ──────────────────────────────────────────
    # De qual etapa e de qual dono saem os leads do carrossel. Eram constantes
    # no código (`STAGE_FILA = 23`, `CORRETORA_CORRETOR = 4`); o padrão é o mesmo
    # valor, então sem linha no banco nada muda. Validação (etapa do cliente,
    # nunca Disparado/Respondeu/Não perturbe) em `remarketing_alvo.py`.
    "remarketing_etapa": lambda: 23,
    "remarketing_dono": lambda: 4,
    # Pra qual etapa vai quem pede pra PARAR (05/10/2026). None = "Venda
    # perdida" do funil principal, decisão da Dona. Fica como configuração
    # porque a casa também tem a etapa "Não perturbe" e a escolha entre as duas
    # segue aberta com ela. Validação em `remarketing_alvo.validar_etapa_parar`.
    "remarketing_etapa_parar": lambda: None,
    # Aviso diário do conferente no Telegram da Dona. NASCE DESLIGADO: é envio
    # externo, quem liga é ela. Ver app/services/remarketing_conferente.py.
    "remarketing_conferente_avisa_telegram": lambda: False,
}


def _ia_corte_padrao() -> float:
    from app.services.ia_do_lado import CORTE_PADRAO

    return CORTE_PADRAO


def _carregar() -> dict[str, Any]:
    global _cache, _cache_em, _cache_erro
    agora = time.time()
    if _cache_em and (agora - _cache_em) < _TTL_SEC:
        return _cache
    with _lock:
        if _cache_em and (time.time() - _cache_em) < _TTL_SEC:
            return _cache
        try:
            with SessionLocal() as s:
                linhas = s.execute(
                    sql_text("SELECT chave, valor FROM config_operacional")
                ).all()
            _cache = {str(r[0]): r[1] for r in linhas}
            _cache_em = time.time()
            _cache_erro = None
        except Exception as e:
            # Banco fora, tabela ainda não migrada, o que for: as chaves comuns
            # seguem no .env; as ESTRITAS levantam em `get` (o erro fica aqui).
            log.warning("config_operacional_indisponivel", erro=str(e)[:140])
            _cache = {}
            _cache_em = time.time()
            _cache_erro = str(e)[:200] or type(e).__name__
        return _cache


def invalidar_cache() -> None:
    """Chamado logo após gravar, pra a tela refletir na hora."""
    global _cache_em
    _cache_em = 0.0


def recarregar_estrito() -> None:
    """Lê o banco AGORA, sem cache, e levanta `ConfigIndisponivel` se falhar.

    É o que o disparo em massa chama antes de decidir qualquer coisa: o que a
    Dona gravou há 2 segundos vale nesta volta, e banco fora não vira padrão.
    """
    invalidar_cache()
    _carregar()
    if _cache_erro:
        raise ConfigIndisponivel(_cache_erro)


def leitura_falhou() -> str | None:
    """Erro da última carga do banco (None = leu). Pra tela dizer "não
    consegui ler" em vez de mostrar padrão com cara de leitura boa."""
    _carregar()
    return _cache_erro


def get(chave: str) -> Any:
    """Valor efetivo da chave: banco se existir, senão o default.

    Chave de `CHAVES_ESTRITAS` levanta `ConfigIndisponivel` quando a leitura
    do banco falhou — ausência confirmada usa o padrão; falha de leitura não.
    """
    if chave not in DEFAULTS:
        raise KeyError(f"chave desconhecida: {chave}")
    valor = _carregar().get(chave)
    if _cache_erro and chave in CHAVES_ESTRITAS:
        raise ConfigIndisponivel(f"{chave}: {_cache_erro}")
    if valor is None:
        return DEFAULTS[chave]()
    return valor


def _historico(s: Any, chave: str, acao: str, antes: Any, depois: Any,
               autor: str) -> None:
    """Uma linha por mudança, na MESMA transação da mudança.

    Em SAVEPOINT: se o histórico falhar (tabela ainda não migrada), a gravação
    da chave continua valendo — desligar o disparo não pode depender da tabela
    de auditoria — e o erro vai pro log com nome próprio.
    """
    import json as _json

    try:
        with s.begin_nested():
            s.execute(
                sql_text(
                    """INSERT INTO config_operacional_historico
                           (chave, acao, valor_antes, valor_depois, autor)
                       VALUES (:k, :a, CAST(:de AS jsonb), CAST(:para AS jsonb), :autor)"""
                ),
                {"k": chave, "a": acao, "de": _json.dumps(antes),
                 "para": _json.dumps(depois), "autor": autor},
            )
    except Exception as e:  # noqa: BLE001
        log.error("config_operacional_historico_falhou", chave=chave, acao=acao,
                  autor=autor, erro=str(e)[:140])


def _valor_gravado(s: Any, chave: str) -> Any:
    return s.execute(
        sql_text("SELECT valor FROM config_operacional WHERE chave = :k"),
        {"k": chave},
    ).scalar()


def definir(chave: str, valor: Any, autor: str) -> None:
    """Grava a chave. `autor` fica registrado — configuração que muda sozinha,
    sem dono, é a primeira coisa que ninguém consegue explicar depois."""
    if chave not in DEFAULTS:
        raise KeyError(f"chave desconhecida: {chave}")
    with SessionLocal() as s:
        _gravar(s, chave, valor, autor, acao="definir")
        s.commit()
    invalidar_cache()
    log.info("config_operacional_alterada", chave=chave, autor=autor)


def _gravar(s: Any, chave: str, valor: Any, autor: str, *, acao: str) -> None:
    """UPSERT + histórico, sem commit (quem chama fecha a transação)."""
    import json as _json

    antes = _valor_gravado(s, chave)
    s.execute(
        sql_text(
            """INSERT INTO config_operacional (chave, valor, atualizado_em,
                                               atualizado_por)
               VALUES (:k, CAST(:v AS jsonb), NOW(), :a)
               ON CONFLICT (chave) DO UPDATE
                  SET valor = EXCLUDED.valor,
                      atualizado_em = NOW(),
                      atualizado_por = EXCLUDED.atualizado_por"""
        ),
        {"k": chave, "v": _json.dumps(valor), "a": autor},
    )
    _historico(s, chave, acao, antes, valor, autor)


def definir_varias(valores: dict[str, Any], autor: str) -> None:
    """Grava várias chaves numa transação só: ou entram todas, ou nenhuma.

    É o "salvar" da agenda do remarketing. Campo a campo, o tick podia ler o
    horário novo com os tetos antigos no meio do salvamento, e uma falha no
    terceiro campo deixava a agenda pela metade sem ninguém saber qual metade.
    `None` apaga a linha (a chave volta ao padrão), também no mesmo commit.
    """
    for chave in valores:
        if chave not in DEFAULTS:
            raise KeyError(f"chave desconhecida: {chave}")
    with SessionLocal() as s:
        for chave, valor in valores.items():
            if valor is None and chave not in CHAVES_QUE_NAO_SE_APAGAM:
                _apagar(s, chave, autor)
            else:
                _gravar(s, chave, valor, autor, acao="definir")
        s.commit()
    invalidar_cache()
    log.info("config_operacional_alteradas", chaves=sorted(valores), autor=autor)


class ValorIlegivel(ValueError):
    """O valor gravado não é do formato da chave — não dá pra mexer num item."""


def definir_item(chave: str, item: str, valor: Any, autor: str) -> dict[str, Any]:
    """Grava UM item de uma chave-mapa (ex.: a corretora 2 em
    `cadencia_por_corretora`) sem pisar nos outros.

    Lê e escreve na mesma transação, sob trava do Postgres por chave: dois
    cliques em corretoras diferentes, ao mesmo tempo, não se apagam. O
    histórico guarda o mapa inteiro, antes e depois. Valor gravado que não é
    mapa levanta `ValorIlegivel` (quem conserta é "voltar ao padrão").
    """
    if chave not in DEFAULTS:
        raise KeyError(f"chave desconhecida: {chave}")
    with SessionLocal() as s:
        s.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": chave})
        atual = _valor_gravado(s, chave)
        if atual is None:
            atual = DEFAULTS[chave]()
        if not isinstance(atual, dict):
            raise ValorIlegivel(f"{chave}: o valor gravado não é um mapa")
        novo = {**atual, str(item): valor}
        _gravar(s, chave, novo, autor, acao="definir")
        s.commit()
    invalidar_cache()
    log.info("config_operacional_item_alterado", chave=chave, item=str(item), autor=autor)
    return novo


def _apagar(s: Any, chave: str, autor: str) -> None:
    antes = _valor_gravado(s, chave)
    s.execute(sql_text("DELETE FROM config_operacional WHERE chave = :k"),
              {"k": chave})
    if antes is not None:
        _historico(s, chave, "restaurar", antes, None, autor)


def restaurar_padrao(chave: str, autor: str) -> None:
    """A chave volta ao padrão — e fica escrito quem voltou, quando, e o que
    valia antes (`config_operacional_historico`).

    Nas chaves de `CHAVES_QUE_NAO_SE_APAGAM` o padrão é GRAVADO na linha em vez
    de a linha ser apagada: o interruptor do disparo continua com dono e data.
    """
    if chave not in DEFAULTS:
        raise KeyError(f"chave desconhecida: {chave}")
    with SessionLocal() as s:
        if chave in CHAVES_QUE_NAO_SE_APAGAM:
            _gravar(s, chave, DEFAULTS[chave](), autor, acao="restaurar")
        else:
            _apagar(s, chave, autor)
        s.commit()
    invalidar_cache()
    log.info("config_operacional_restaurada", chave=chave, autor=autor)


def historico(chaves: list[str] | None = None, limite: int = 50) -> list[dict[str, Any]]:
    """Últimas mudanças, da mais nova pra mais velha. Levanta se o banco
    falhar — quem mostra decide como dizer "não consegui ler"."""
    with SessionLocal() as s:
        linhas = s.execute(
            sql_text(
                """SELECT chave, acao, valor_antes, valor_depois, autor, em
                     FROM config_operacional_historico
                    WHERE (CAST(:todas AS boolean) OR chave = ANY(:chaves))
                    ORDER BY em DESC, id DESC
                    LIMIT :lim"""
            ),
            {"todas": not chaves, "chaves": list(chaves or []), "lim": int(limite)},
        ).all()
    return [
        {"chave": r[0], "acao": r[1], "antes": r[2], "depois": r[3],
         "autor": r[4], "em": r[5].isoformat() if r[5] else None}
        for r in linhas
    ]


# ── Atalhos usados pelo motor ────────────────────────────────────────────────


def cadencia_ativa() -> bool:
    return bool(get("cadencia_ativa"))


def envio_dias_uteis() -> bool:
    """Trava de dia útil dos motores automáticos (ver dia_util.py)."""
    return bool(get("envio_dias_uteis"))


def nutricao_ativa() -> bool:
    """Kill-switch da nutrição. Sem linha no banco, herda `cadencia_ativa`."""
    return bool(get("nutricao_ativa"))


def cap_diario() -> int:
    return int(get("cadencia_cap_diario"))


def cap_por_corretora() -> dict[str, int]:
    bruto = get("cadencia_cap_por_corretora") or {}
    return {str(k): int(v) for k, v in bruto.items()}


def cap_da_corretora(corretora_id: int | None) -> int:
    """Cap efetivo de uma linha: o dela se houver, senão o global."""
    if corretora_id is None:
        return cap_diario()
    return int(cap_por_corretora().get(str(corretora_id), cap_diario()))


def roleta_corretoras() -> list[int]:
    return [int(x) for x in (get("roleta_corretoras") or [])]


def lead_intake_ativo() -> bool:
    """Kill-switch da boas-vindas (painel > LEAD_INTAKE_ATIVO do .env)."""
    return bool(get("lead_intake_ativo"))


def boas_vindas_copies() -> dict[str, str]:
    """Overrides do corpo da boas-vindas por VARIANTE do A/B (id → copy)."""
    bruto = get("boas_vindas_copies") or {}
    return {str(k): str(v) for k, v in bruto.items() if str(v).strip()}


def carrossel_corretor_ativo() -> bool:
    """Interruptor do remarketing em massa. Só o banco liga; sem linha é
    desligado; leitura que falha levanta `ConfigIndisponivel`. Só `true`
    (booleano) liga — "sim", 1 ou qualquer outra coisa gravada por fora é
    desligado."""
    return get("carrossel_corretor_ativo") is True


def carrossel_auto_resposta_segundos() -> float:
    """Corte do relógio da resposta automática. Valor torto no banco não pode
    virar "tudo é robô": fora de 0–60 vale o padrão."""
    try:
        v = float(get("carrossel_auto_resposta_segundos"))
    except (TypeError, ValueError):
        return 5.0
    return v if 0 <= v <= 60 else 5.0


def no_show_ativo() -> bool:
    """Kill-switch do no-show. Sem linha no banco: DESLIGADO."""
    return bool(get("no_show_ativo"))


def regua_proprietario_ativa() -> bool:
    """Kill-switch da régua do proprietário. Sem linha no banco: DESLIGADA."""
    return bool(get("regua_proprietario_ativa"))


def anti_ghosting_ativo() -> bool:
    """Kill-switch do anti-ghosting. Sem linha no banco: DESLIGADO."""
    return bool(get("anti_ghosting_ativo"))


def handoff_remarketing_ativo() -> bool:
    """Kill-switch do handoff automático do remarketing (painel > .env)."""
    return bool(get("handoff_remarketing_ativo"))


def handoff_remarketing_avisa_lead() -> bool:
    """Manda (ou não) o template de apresentação pela linha da corretora que
    recebeu o lead. Sem linha no banco: DESARMADO."""
    return bool(get("handoff_remarketing_avisa_lead"))


def handoff_remarketing_roleta() -> list[int]:
    """Corretoras que dividem o lead que sai da Corretor, na ordem da rotação.

    Vazia = sem roleta; quem chama cai no destino único. Ids inválidos NÃO são
    filtrados aqui (isto é config, não banco) — quem usa valida existência e
    `ativo`, como faz `lead_intake.corretoras_roleta_validas`.
    """
    return [int(x) for x in (get("handoff_remarketing_roleta") or [])]


def aviso_remarketing_cap_linha() -> int:
    """Quantos avisos cada linha pode mandar por dia."""
    return int(get("aviso_remarketing_cap_linha") or 0)


def handoff_remarketing_corretora() -> int | None:
    """corretora_id que recebe o lead do remarketing com interesse real.
    None quando o valor gravado não é um id (valida existência/ativo quem usa)."""
    bruto = get("handoff_remarketing_corretora")
    try:
        return int(bruto)
    except (TypeError, ValueError):
        return None


def ligacao_meta_diaria(corretora_id: int | None = None) -> int:
    """Quantas ligações a corretora tem pra fazer no dia (fila do tamanho certo:
    lista infinita desanima).

    Com `corretora_id`, respeita a meta INDIVIDUAL quando existir. Nasceu em
    09/09: a Pessoa7 fez 47 ligações num dia — a operação inteira fazia 3 por
    SEMANA — e liberar bloco extra na mão toda tarde não é rotina, é remendo.
    Quem só liga (resgate) tem meta diferente de quem também atende e vende.
    """
    padrao = max(1, int(get("ligacao_meta_diaria") or 25))
    if corretora_id is None:
        return padrao
    individual = (get("ligacao_meta_por_corretora") or {}).get(str(int(corretora_id)))
    return max(1, int(individual)) if individual else padrao


def ligacao_meta_semanal() -> int:
    """Meta da semana por corretora — é o que a barra de progresso persegue."""
    return max(1, int(get("ligacao_meta_semanal") or 100))


def estado_atual() -> dict[str, Any]:
    """Tudo o que a tela mostra, com a marca de quem veio do painel e quem
    ainda está no arquivo — sem isso não dá pra saber se o botão foi mexido."""
    do_banco = _carregar()
    out: dict[str, Any] = {}
    for chave in DEFAULTS:
        try:
            out[chave] = {
                "valor": get(chave),
                "personalizado": chave in do_banco,
                "padrao": DEFAULTS[chave](),
            }
        except ConfigIndisponivel as e:
            # Não consegui ler ≠ está no padrão. A tela mostra o erro.
            out[chave] = {"valor": None, "personalizado": None, "padrao": None,
                          "indisponivel": True, "motivo": str(e)[:160]}
    return out
