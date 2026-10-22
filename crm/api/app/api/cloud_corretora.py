"""Coexistência Cloud API das corretoras — Fase 2 (Pessoa10 27/07, Pessoa11 depois).

A linha da Pessoa12 (2591) tem módulo próprio (`cloud_corretora_a.py`) porque carrega
regras dela: cadência, reengajamento, Assistente, rotação de template. Este módulo
é o caminho MAGRO das outras corretoras em coexistência: a mensagem que elas
mandam/recebem PELO CELULAR entra no thread do painel, e nada mais.

    Meta → api-disparos (roteia por phone_number_id) → /cloud/corretora/{slug}/…

O que ele faz e o que NÃO faz:
  • grava inbound do lead e echo do celular em `mensagens_whatsapp`, na
    instância e corretora certas (idempotente por wamid — reusa `gravar`);
  • NÃO responde nada, NÃO matricula em cadência, NÃO dispara template.
    Corretora em coexistência é atendimento humano; automação é decisão da Dona
    linha por linha.

Cadastrar uma corretora nova = 2 passos, nenhum deles em código:
  1. `CLOUD_LINHA_<SLUG>=<phone_number_id>` no .env (registry `cloud_lines`);
  2. `corretoras.canal='cloud'` no banco (faz o Inspector ler a conversa da
     tabela em vez de procurar instância do Evolution).

⚠️ Coexistência NÃO convive com Evolution no MESMO número: os dois conectados
levam ban (foi o que derrubou a linha da Pessoa12 2x em julho). Antes de ligar, a
instância Evolution daquela corretora tem que estar desconectada.
"""
from __future__ import annotations

import json as _json
import uuid
from functools import partial
from datetime import datetime, timezone
from typing import Any

import structlog
from fastapi import APIRouter, Header, HTTPException, Request

from app.api import cloud_corretora_a as corretor
from app.db.session import SessionLocal
from app.services import alerta_entrega
from app.services import atendimento_corretor
from app.services import carrossel_corretor as cb
from app.services import cloud_lines
from app.services import opt_out
from sqlalchemy import text as sql_text

log = structlog.get_logger()

router = APIRouter(prefix="/cloud/corretora", tags=["cloud"])

# slug da linha → (instance de gravação, corretora_id). A instância mantém o
# padrão histórico `acme_<nome>` pra thread da corretora não se partir em duas
# quando ela migra de Evolution pra Cloud (o painel lê por instance).
_DESTINO: dict[str, tuple[str, int]] = {
    "corretor": ("acme_corretora_c", 1),
    "corretor": ("acme_corretor", 2),
    # 0000 = linha DA CASA (remarketing em massa, dona Corretor). Ligada em
    # coexistência 30/07; era o número do `recuperador`, cujas instâncias
    # Evolution foram APAGADAS no mesmo dia (Evolution + Cloud = ban).
    "corretor": ("acme_corretor", 4),
    # Reforço de ligação/resgate (09/09). Elas atendem pelo próprio celular; a
    # Pessoa12 responde por dentro do painel, e a mensagem sai pelo número DELAS — o
    # lead vê uma linha só.
    "pessoa_d": ("acme_pessoa_d", 7),
    "corretor": ("acme_corretor", 8),
}


def _resolver(slug: str) -> tuple[str, int]:
    """Valida que o slug é de corretora, está cadastrado no registry (env) e tem
    destino conhecido. Fail-closed: linha desconhecida = 404, nunca grava solto."""
    destino = _DESTINO.get(slug)
    if destino is None:
        raise HTTPException(404, "linha_desconhecida")
    linha = cloud_lines.line_by_slug(slug)
    if linha is None:
        # env CLOUD_LINHA_<SLUG> ausente = linha não ligada ainda.
        raise HTTPException(404, "linha_nao_cadastrada")
    if linha.papel != "corretora":
        raise HTTPException(400, "linha_nao_e_de_corretora")
    return destino


def _ts(m: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(m["timestamp"]), tz=timezone.utc)
    except (KeyError, ValueError, TypeError, OSError):
        return None


def _texto_de(m: dict[str, Any]) -> tuple[str, str]:
    """(tipo, texto) de uma mensagem da Cloud API. Mídia entra como marcador —
    o download fica pro fluxo de mídia existente, que resolve por wamid."""
    tipo = m.get("type", "text")
    if tipo == "text":
        return tipo, (m.get("text") or {}).get("body", "")
    if tipo == "button":
        return tipo, (m.get("button") or {}).get("text", "[botão]")
    if tipo == "interactive":
        inter = m.get("interactive") or {}
        br = inter.get("button_reply") or inter.get("list_reply") or {}
        return tipo, br.get("title") or "[interativo]"
    return tipo, corretor.texto_de_midia(m, tipo)


# ─── Histórico de coexistência (field `history` da Meta) ─────────────────────
# Quando a corretora entra na coexistência e aceita "compartilhar histórico", a
# Meta reentrega ATÉ 6 MESES de conversa em 3 fases (metadata.phase: 0 =
# onboarding→dia 1, 1 = dia 1-90, 2 = dia 90-180), cada fase em vários chunks
# (`chunk_order`, `progress` 0→100). É a via OFICIAL de recuperar conversa antiga
# — a alternativa (religar Evolution/Baileys no mesmo número) já custou 2 bans na
# linha da Pessoa12.
#
# ⚠️ DIREÇÃO — a regra que inverte a conversa na tela se errar:
# no payload de history, `to` só vem quando a mensagem é ECHO DO NEGÓCIO (a
# corretora mandou). Mensagem do contato vem SEM `to`. Como cinto e suspensório
# também comparamos `from` com o `display_phone_number` da linha (mesma heurística
# do `_split_echoes_from_messages` do api-disparos), porque o `from` do echo é o
# número do próprio negócio.
#
# ⚠️ HISTÓRICO É INERTE: grava e nada mais. Ver `_MARCADOR_HISTORICO` e a nota de
# segurança em `corretora_history`.
_MARCADOR_HISTORICO = "historico_coexistencia"


def _thread_contato(thread: dict[str, Any]) -> str:
    """Telefone do contato do thread. `threads[].id` é o número do usuário
    WhatsApp — é ele que identifica a conversa, não o `from` da mensagem (que
    alterna entre contato e negócio)."""
    return corretor._so_digitos(str(thread.get("id") or ""))


def _direcao_historica(m: dict[str, Any], proprio: str) -> bool:
    """True se a mensagem histórica é DA CORRETORA (outbound).

    Dois sinais, na ordem de confiança:
      1. `to` presente → é echo do negócio (doc da Meta: "only included if SMB
         message echo");
      2. `from` == display_phone_number da linha → também é nossa.
    Sem nenhum dos dois, é mensagem do contato (inbound).
    """
    if m.get("to"):
        return True
    frm = corretor._so_digitos(str(m.get("from") or ""))
    return bool(proprio) and frm == proprio


def _linha_pra_insert(
    m: dict[str, Any], contato: str, from_corretora: bool
) -> dict[str, Any]:
    """Monta os binds de UMA mensagem histórica pro INSERT em lote.

    Mesmas colunas/semântica de `corretor.gravar` — a diferença é só que aqui o INSERT
    é agrupado (ver `_gravar_lote_historico`).
    """
    import json as _json

    tipo, texto = _texto_de(m)
    meta: dict[str, Any] = {_MARCADOR_HISTORICO: True}
    info = corretor.extrair_media_info(m)
    if info:
        # mídia de histórico quase nunca é baixável (media_id antigo): grava o
        # marcador e o id, sem tentar download — quem quiser tenta depois.
        meta.update({"cloud_media_id": info[0], "mime_type": info[1]})
    tel = corretor._so_digitos(contato)
    return {
        "id": str(uuid.uuid4()),
        "wamid": m.get("id") or "",
        # `instance`/`cid` são preenchidos pelo caller (valem pro lote inteiro)
        "instance": None,
        "cid": None,
        "jid": f"{tel}@s.whatsapp.net",
        "tel": tel,
        "nome": None,
        "fromc": from_corretora,
        "tipo": tipo,
        "texto": texto,
        "ts": _ts(m) or datetime.now(timezone.utc),
        "meta": _json.dumps(meta),
        "raw": _json.dumps(m),
    }


# Tamanho do lote de INSERT. Grande o bastante pra amortizar o round-trip,
# pequeno o bastante pra não montar um statement gigante em memória.
_LOTE = 500

# ON CONFLICT DO NOTHING na constraint `uq_mensagem_instance`
# (evolution_message_id, instance) — a MESMA usada pelo
# mensagens_evolution_service. É a rede de segurança final da idempotência: o
# SELECT em lote acima resolve o caso normal, mas duas entregas simultâneas da
# Meta podem passar pelo SELECT juntas. Sem o ON CONFLICT, a violação de unique
# abortaria o LOTE INTEIRO (500 mensagens boas perdidas por causa de 1 repetida).
_SQL_INSERT_HISTORICO = """
    INSERT INTO mensagens_whatsapp
        (id, evolution_message_id, instance, corretora_id,
         remote_jid, contato_telefone, contato_nome, from_corretora,
         tipo_mensagem, conteudo_texto, conteudo_metadata,
         timestamp_evolution, recebido_em, raw_payload)
    VALUES
        (:id, :wamid, :instance, :cid, :jid, :tel, :nome, :fromc,
         :tipo, :texto, CAST(:meta AS jsonb), :ts, now(),
         CAST(:raw AS jsonb))
    ON CONFLICT ON CONSTRAINT uq_mensagem_instance DO NOTHING
"""


def _gravar_lote_historico(
    mensagens: list[tuple[dict[str, Any], str, bool]],
    *,
    instance: str,
    corretora_id: int,
    slug: str,
) -> tuple[int, int]:
    """Grava as mensagens históricas EM LOTE. Retorna (gravadas, duplicadas).

    Por que não `corretor.gravar` num for: ele abre uma sessão E faz um SELECT de
    idempotência POR MENSAGEM (~250 msg/s medido). Um chunk de 6 meses de
    corretora ativa são milhares de mensagens, e a Meta REENTREGA o chunk se o
    webhook demorar. Aqui: uma sessão só, os wamids já existentes resolvidos em
    UMA query, e `executemany` em lotes de `_LOTE`.

    Idempotência (o sync reentrega o mesmo chunk, é o caso NORMAL):
      1. dedup DENTRO do payload (a Meta às vezes repete o wamid no mesmo chunk);
      2. SELECT em lote dos wamids que já estão no banco;
      3. cada lote de INSERT vai em savepoint próprio — se um lote falhar (ex:
         corrida com outra entrega), os outros seguem gravados.
    """
    if not mensagens:
        return 0, 0

    # 1. dedup intra-payload, preservando a ordem de chegada
    por_wamid: dict[str, tuple[dict[str, Any], str, bool]] = {}
    dups_payload = 0
    for item in mensagens:
        w = item[0].get("id") or ""
        if w in por_wamid:
            dups_payload += 1
            continue
        por_wamid[w] = item

    gravadas = 0
    duplicadas = dups_payload
    try:
        with SessionLocal() as s:
            # 2. quais wamids JÁ existem (uma query, não N)
            wamids = list(por_wamid)
            existentes: set[str] = set()
            for i in range(0, len(wamids), _LOTE):
                fatia = wamids[i:i + _LOTE]
                existentes.update(
                    s.execute(
                        sql_text(
                            "SELECT evolution_message_id FROM mensagens_whatsapp "
                            "WHERE evolution_message_id = ANY(:ws)"
                        ),
                        {"ws": fatia},
                    ).scalars()
                )
            duplicadas += len(existentes)

            novas = [
                _linha_pra_insert(m, contato, fromc)
                for w, (m, contato, fromc) in por_wamid.items()
                if w not in existentes
            ]
            for linha in novas:
                linha["instance"] = instance
                linha["cid"] = corretora_id

            # 3. INSERT em lotes, cada um em savepoint próprio
            for i in range(0, len(novas), _LOTE):
                fatia = novas[i:i + _LOTE]
                try:
                    with s.begin_nested():
                        res = s.execute(sql_text(_SQL_INSERT_HISTORICO), fatia)
                    # rowcount reflete o que REALMENTE entrou (ON CONFLICT DO
                    # NOTHING não conta), então `gravadas` não infla em corrida.
                    inseridas = res.rowcount if res.rowcount is not None else len(fatia)
                    gravadas += max(0, inseridas)
                    duplicadas += max(0, len(fatia) - max(0, inseridas))
                except Exception as e:  # noqa: BLE001 — um lote ruim não perde o resto
                    log.warning("cloud_corretora_history_lote_falha", slug=slug,
                                offset=i, tamanho=len(fatia), erro=str(e)[:200])
            s.commit()
    except Exception as e:  # noqa: BLE001 — histórico NUNCA derruba o webhook
        log.warning("cloud_corretora_history_falha", slug=slug, erro=str(e)[:200])

    log.info("cloud_corretora_history_lote", slug=slug, gravadas=gravadas,
             duplicadas=duplicadas, total=len(mensagens))
    return gravadas, duplicadas


@router.post("/{slug}/history")
async def corretora_history(
    slug: str,
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Histórico (até 6 meses) da coexistência. SÓ grava no thread.

    Responde IMEDIATAMENTE e grava em background: um chunk de corretora ativa tem
    milhares de mensagens e a Meta REENTREGA se o webhook demorar (o que
    duplicaria trabalho e derrubaria o sync). Nunca devolve 5xx — erro de
    gravação é logado e o ACK segue, senão a Meta reenfileira pra sempre.

    🔒 SEGURANÇA — histórico NÃO aciona automação:
    o único efeito deste endpoint é INSERT em `mensagens_whatsapp`
    (`_gravar_lote_historico`). Não passa por `tratar_resposta_cadencia_inbound`,
    `tratar_resposta_disparo` nem `tratar_intencao_indicacao` (os gatilhos vivem
    no /inbound, que este caminho NÃO chama), não matricula em cadência, não
    chama Assistente/IA e não envia nada. Além disso, a mensagem antiga entra com o
    `ts` ORIGINAL, então as varreduras por recência (transcrição e Inspector, os
    dois com cutoff) não a alcançam.

    ⚠️ Efeito colateral conhecido de gravar conversa velha: `cadencia.
    auto_followup_24h` procura leads "mudos" pela ÚLTIMA mensagem da thread. Uma
    mensagem histórica NUNCA é a última (é antiga por definição), exceto em lead
    que não tem nenhuma mensagem recente — e nesse caso ela só torna o lead
    ELEGÍVEL a follow-up, sem enviar nada por si só. Hoje isso está atrás de
    `CADENCIA_AUTO_FOLLOWUP_24H` (default False, ausente no .env de produção).
    Se a Dona for ligar essa flag, revisar antes: rodar o sync ANTES de ligar, ou
    excluir do scan quem tem `conteudo_metadata->>'historico_coexistencia'`.
    """
    corretor._auth_cloud_webhook(x_cloud_secret)
    instance, corretora_id = _resolver(slug)
    payload = await req.json()

    proprio = corretor._so_digitos(
        str((payload.get("metadata") or {}).get("display_phone_number") or "")
    )

    # Achata history[] → threads[] → messages[] numa lista só, já com contato e
    # direção resolvidos (parsing é rápido e síncrono; o DB é que vai pro fundo).
    mensagens: list[tuple[dict[str, Any], str, bool]] = []
    fases: list[Any] = []
    progresso: Any = None
    for bloco in payload.get("history") or []:
        if not isinstance(bloco, dict):
            continue
        bmeta = bloco.get("metadata") or {}
        fases.append(bmeta.get("phase"))
        if bmeta.get("progress") is not None:
            progresso = bmeta.get("progress")
        for thread in bloco.get("threads") or []:
            if not isinstance(thread, dict):
                continue
            contato = _thread_contato(thread)
            if not contato:
                continue
            for m in thread.get("messages") or []:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                mensagens.append((m, contato, _direcao_historica(m, proprio)))

    log.info("cloud_corretora_history_recebido", slug=slug,
             mensagens=len(mensagens), fases=fases, progresso=progresso)

    if mensagens:
        # threadpool: `gravar` é síncrono/bloqueante e o endpoint é async — rodar
        # inline travaria o event loop inteiro (gotcha conhecido do uvicorn).
        from fastapi.concurrency import run_in_threadpool

        await run_in_threadpool(
            _gravar_lote_historico, mensagens,
            instance=instance, corretora_id=corretora_id, slug=slug,
        )

    return {"ok": True, "recebidas": len(mensagens), "progresso": progresso}


@router.post("/{slug}/inbound")
async def corretora_inbound(
    slug: str,
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Mensagem que o LEAD mandou pra corretora. Só grava no thread.

    `X-Cloud-Velho: 1` (05/10/2026): o api-disparos marca a mensagem que a Meta
    reentregou com mais de 30 min de atraso. Ela é gravada e o card anda (a
    regra do carrossel já usa a hora da Meta), opt-out e parada de cadência
    valem — mas a Pessoa NÃO responde: atender hoje o que a pessoa escreveu há
    horas é a casa falando fora de hora."""
    corretor._auth_cloud_webhook(x_cloud_secret)
    instance, corretora_id = _resolver(slug)
    velho = corretor.mensagem_velha(req)
    payload = await req.json()
    contatos = {
        c.get("wa_id"): (c.get("profile") or {}).get("name")
        for c in (payload.get("contacts") or [])
    }
    gravadas = 0
    for m in payload.get("messages") or []:
        frm = m.get("from")
        if not frm:
            continue
        tipo, texto = _texto_de(m)
        meta = None
        info = corretor.extrair_media_info(m)
        if info:
            meta = {"cloud_media_id": info[0], "mime_type": info[1]}
        if corretor.gravar(
            wamid=m.get("id") or "", contato_telefone=frm,
            contato_nome=contatos.get(frm), from_corretora=False,
            texto=texto, tipo=tipo, ts=_ts(m), raw=m, metadata=meta,
            instance=instance, corretora_id=corretora_id,
        ):
            gravadas += 1
            # RESPOSTA À CADÊNCIA EM QUALQUER LINHA (02/10/2026, porte da DEX).
            # Até aqui só o inbound da Pessoa12 (`cloud_corretora_a.cloud_inbound`) parava
            # a régua: resposta na linha da Pessoa10/Pessoa11 ficava só segurada pelos 7
            # dias da trava de conversa humana e depois a cadência voltava a
            # disparar (em 02/10: Pessoa10 1 'respondida' x 30 'remarketing'; Pessoa11
            # 4 x 41; Pessoa12 159 x 681). Mesma função e mesmo lugar do inbound da
            # Pessoa12: dentro do `if gravar(...)` (reentrega da Meta não reprocessa),
            # ignora clique de botão e auto-reply, para as cadências gêmeas pelo
            # telefone. A linha da Corretor fica de fora: lá quem trata a resposta é o
            # carrossel + a Pessoa (bloco abaixo). Best-effort, nunca levanta.
            # `velho` vai junto (14/10): mensagem reentregue com +30 min para a
            # régua e move o card, mas não ganha áudio nem despedida.
            if slug != cb.SLUG_CORRETOR:
                from fastapi.concurrency import run_in_threadpool

                await run_in_threadpool(
                    corretor._tratar_resposta_cadencia_inbound, frm, texto, tipo,
                    velho=velho,
                )
        # Pedido de parar vale em QUALQUER linha de corretora, não só na da Corretor.
        # Até 23/08 só o carrossel da Corretor tratava opt-out aqui; nas linhas da
        # Pessoa10 e da Pessoa11 o clique em "Parar mensagens" era gravado pelo
        # api-disparos no tenant do HUNTER — ou seja, em lugar nenhum que a
        # Acme consulte, então o lead seguia elegível pra cadência. Achado
        # investigando o lead que recebeu a auto-resposta do Gustavo pela linha
        # da Pessoa10.
        if slug != cb.SLUG_CORRETOR and opt_out.is_opt_out(texto):
            from fastapi.concurrency import run_in_threadpool

            await run_in_threadpool(atendimento_corretor.marcar_opt_out, frm)
            log.info("cloud_corretora_opt_out", slug=slug, tel=frm[-4:])
        # PORTA DE SAÍDA DO REMARKETING (19/09): lead numa etapa do funil
        # remarketing que volta a falar sai da fila — a régua para e, com sinal
        # de interesse, o card volta pro funil principal. Aqui, nesta rota, é o
        # que faltava: os disparos indevidos de setembro (Pessoa16, Gabriel,
        # William) saíram e as respostas chegaram em `acme_corretor`, que não
        # passa pelo hook da cadência (esse só escuta a linha da Pessoa12).
        # NASCE DESARMADO (`saida_remarketing.ativa()`), NÃO manda mensagem
        # nenhuma e nunca levanta. Fora do `if gravar(...)` de propósito, igual
        # ao opt-out acima: mensagem duplicada ainda é resposta do lead.
        if slug != cb.SLUG_CORRETOR:
            from fastapi.concurrency import run_in_threadpool

            from app.services import saida_remarketing

            await run_in_threadpool(
                saida_remarketing.processar_inbound, frm, texto,
                tipo_mensagem=tipo,
            )
        # Carrossel da Corretor: quem responde sai da roda; quem pede pra parar
        # ganha opt-out. Fora do `if gravar(...)` de propósito — mensagem
        # duplicada (que `gravar` descarta) ainda é resposta do lead.
        if slug == cb.SLUG_CORRETOR:
            from fastapi.concurrency import run_in_threadpool

            # A classe da resposta decide a etapa do card (regra em código,
            # `remarketing_resposta`). O tipo e a hora da Meta vão junto: é
            # com eles que a regra separa clique de botão e resposta automática.
            saida = await run_in_threadpool(
                partial(cb.processar_inbound, frm, texto,
                        tipo=tipo, recebido_em=_ts(m))
            )
            # Quem pediu pra parar NÃO é atendido — atender depois de "PARAR" é
            # o caminho mais curto pra denúncia. Resposta automática do aparelho
            # do lead também não: não é gente, e a Pessoa respondendo robô ainda
            # punha o card na mão de uma corretora (o handoff lê "casa" numa
            # saudação de loja). Nos outros casos a Pessoa assume.
            #
            # Número errado (05/10): quem respondeu não é o lead — a Pessoa
            # não puxa conversa com um desconhecido.
            # QUANDO A PESSOA RESPONDE (05/10/2026, decisão da Dona: "todo
            # mundo que cair em Respondeu vai para a roleta"). O card acabou de
            # cair em Respondeu → no modo "todos" ele sai pra corretora da vez
            # na roleta do REMARKETING, com a tag RMKT e na etapa de entrada
            # escolhida na tela. Aqui, e não dentro do atendimento da Pessoa,
            # porque lá o áudio e a figurinha voltam antes (a Pessoa não
            # responde mídia) e o lead ficaria com o robô. No modo "interesse"
            # a função não faz nada: quem decide segue sendo o atendimento.
            # Recusa, parar, automática e número errado não chegam aqui.
            # Mensagem velha (reentrega de mais de 30 min) também não: é
            # gravada sem automação. Nunca levanta.
            if saida in ("respondeu", "interesse_lido") and not velho:
                from app.services import handoff_remarketing

                await run_in_threadpool(
                    partial(handoff_remarketing.processar_resposta, frm, texto,
                            contatos.get(frm))
                )
            if velho:
                log.warning("cloud_corretora_inbound_velho_sem_atendimento",
                            slug=slug, tel=frm[-4:], saida=saida)
            elif saida not in ("opt_out", "automatica", "numero_errado"):
                # A classe já foi decidida pela regra em código; o atendimento
                # recebe a decisão em vez de reclassificar por conta própria.
                # Sem isso, um "Não" (recusa só pra regra do remarketing)
                # chegava na Pessoa como conversa aberta.
                await run_in_threadpool(
                    partial(atendimento_corretor.atender, frm, texto, contatos.get(frm),
                            recusa_decidida=saida == "recusa",
                            interesse_decidido=saida == "interesse_lido")
                )
    log.info("cloud_corretora_inbound", slug=slug, gravadas=gravadas)
    return {"ok": True, "gravadas": gravadas}


@router.post("/{slug}/echo")
async def corretora_echo(
    slug: str,
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Echo: mensagem que a CORRETORA mandou pelo app do celular. Grava como
    outbound (from_corretora=True) — NUNCA dispara resposta automática."""
    corretor._auth_cloud_webhook(x_cloud_secret)
    instance, corretora_id = _resolver(slug)
    payload = await req.json()
    gravados = 0
    sem_destino = 0
    for echo in payload.get("echoes") or []:
        if not isinstance(echo, dict):
            continue
        destino = corretor._echo_destino(echo)
        if not destino:
            sem_destino += 1
            continue
        tipo, texto = corretor._echo_texto_tipo(echo)
        if corretor.gravar(
            wamid=echo.get("id") or "", contato_telefone=destino,
            contato_nome=None, from_corretora=True, texto=texto, tipo=tipo,
            ts=_ts(echo), raw=echo,
            instance=instance, corretora_id=corretora_id,
        ):
            gravados += 1
    log.info("cloud_corretora_echo", slug=slug, gravados=gravados,
             sem_destino=sem_destino)
    return {"ok": True, "gravados": gravados, "sem_destino": sem_destino}


@router.post("/{slug}/status")
async def corretora_status(
    slug: str,
    req: Request,
    x_cloud_secret: str | None = Header(default=None),
) -> dict[str, Any]:
    """Status de entrega (sent/delivered/read/failed). Só anota no metadata da
    mensagem já gravada — sem status, "enviei e não chegou" fica invisível.

    ⚠️ Grava na MESMA chave que a linha da Pessoa12 (`delivery_status`, via
    `build_status_patch`). Antes escrevia `status`, uma chave que NINGUÉM lê: o
    painel (`/painel`, anexo de delivery) e o alerta de linha com problema
    ("3+ envios failed no dia") filtram por `delivery_status`. Resultado: a Pessoa11
    e a Pessoa10 — as duas que mais mandam mensagem — ficavam invisíveis pro alerta,
    e "enviei e não chegou" continuava invisível justamente onde importava.
    Achado em 03/08, investigando suspeita de restrição na linha da Pessoa11.
    """
    corretor._auth_cloud_webhook(x_cloud_secret)
    _resolver(slug)
    payload = await req.json()
    from app.services import status_entrega

    vistos = 0
    # O que aconteceu com cada status, pra a resposta não dizer só "ok":
    # aplicado · orfao (não há mensagem com esse id; guardado pra reaplicar) ·
    # fora_de_ordem (o gravado é mais novo) · erro (não consegui gravar).
    contagem: dict[str, int] = {}
    for st in payload.get("statuses") or []:
        wamid = st.get("id")
        if not wamid:
            continue
        patch = corretor.build_status_patch(
            st.get("status"), st.get("timestamp"), st.get("errors")
        )
        try:
            with SessionLocal() as s:
                desfecho = status_entrega.aplicar(s, wamid, patch, slug)
                s.commit()
            contagem[desfecho] = contagem.get(desfecho, 0) + 1
            vistos += 1
            # Recusa por conta (pagamento/elegibilidade) não se resolve
            # tentando de novo — alguém precisa abrir o Business Manager. Fora
            # do try do UPDATE de propósito: o alerta não pode fazer o status
            # deixar de ser gravado, e ele já engole os próprios erros.
            if patch.get("delivery_status") == "failed":
                alerta_entrega.avisar_falha_de_conta(
                    linha=slug, erro=patch.get("delivery_error")
                )
        except Exception as e:  # noqa: BLE001 — status nunca derruba o webhook
            contagem["erro"] = contagem.get("erro", 0) + 1
            log.error("cloud_corretora_status_falha", slug=slug,
                      wamid=wamid, erro=str(e)[:160])
    return {"ok": True, "vistos": vistos, **contagem}
