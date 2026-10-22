"""Camada 1 do banco de imóveis — captação da corretora (migration 0080).

Regra que manda em tudo aqui: **nada pode fazer o cadastro falhar.**

A tela é preenchida em 60 segundos, do celular, no meio da conversa com o dono.
Obrigatório de verdade são quatro campos (tipo, bairro, telefone do dono e
preço). Todo o resto entra depois — a lista mostra "faltam N campos", o
salvamento nunca bloqueia. Um 422 no meio da conversa custa o imóvel.

O que este módulo garante, e o motivo de cada trava:

  duplicata AVISA, não barra   dono com dois apês idênticos no mesmo prédio
                               existe. `parecidos()` devolve texto pronto ("a
                               Pessoa10 captou um parecido em 12/08") junto do 201.

  foto nunca é sobrescrita     `aplicar_logo()` cria LINHA NOVA com
                               `tratada_de_id` apontando pra original. Retirar
                               de circulação é `arquivada=true`, nunca DELETE.

  logo ausente → erro claro    sem `acme_logo_path` no disco, levanta
                               `LogoIndisponivel`. Não existe logo improvisada:
                               marca d'água errada em foto de cliente é pior
                               que nenhuma.

  foto de portal é impossível  `origem` só aceita 'dono'/'corretora' — CHECK no
                               banco (§3 do plano). Aqui só repete a validação
                               pra dar erro legível antes do IntegrityError.

  troca de dona é EVENTO       `corretora_id` define comissão
                               (`acme-comissoes-caixa-vs-crm`). Toda mudança
                               vira linha em `imovel_evento`, com autor e hora.

Escopo: corretora enxerga e edita só as captações DELA. A delegação de
atendimento (Pessoa12 cobrindo a linha da Pessoa7) NÃO vale aqui de propósito — ela é
sobre conversa, e isto é sobre propriedade da captação.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from app.config import settings

log = structlog.get_logger(__name__)

TIPOS = ("apartamento", "casa", "terreno", "comercial", "chacara", "outro")
SITUACOES = ("disponivel", "vendido", "desistiu", "exclusividade_outra", "nao_valida")
RELACOES = ("proprietario", "procurador", "familiar", "inventario", "outro")
FONTES = ("whatsapp", "indicacao", "placa", "porta", "portal", "pessoa", "outro")
# 🔴 'portal' NÃO existe aqui e não deve existir nunca (§3 do plano).
ORIGENS_FOTO = ("dono", "corretora")

MIMES_FOTO = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
FOTO_MAX_BYTES = 12 * 1024 * 1024


class LogoIndisponivel(RuntimeError):
    """`acme_logo_path` vazio ou arquivo ausente. Nunca improvisar uma logo."""


class PillowIndisponivel(RuntimeError):
    """Pillow não instalado no runtime — tratamento de imagem desligado."""


# ── Checklist de completude ───────────────────────────────────────────────────
#
# Os 4 primeiros são os obrigatórios de verdade (validados no schema). Os
# demais são o "faltam N campos" que a lista mostra — cobrança visual, nunca
# bloqueio. `fotos` é pseudo-campo: vem da contagem em imovel_foto.
CHECKLIST: tuple[tuple[str, str], ...] = (
    ("tipo", "tipo"),
    ("bairro", "bairro"),
    ("dono_telefone", "telefone do dono"),
    ("preco_pedido", "preço pedido"),
    ("dono_nome", "nome do dono"),
    ("quartos", "quartos"),
    ("vagas", "vagas"),
    ("area_m2", "área"),
    ("endereco", "endereço"),
    ("dono_relacao", "relação do dono"),
    ("aceita_financiamento", "aceita financiamento"),
    ("fotos", "fotos"),
    ("autorizacao_assinada", "autorização assinada"),
)
# Campos booleanos onde False é resposta válida ("perguntei e é não"), então
# só NULL conta como faltando. autorizacao_assinada é o oposto: false = falta.
_BOOL_TRISTATE = {"aceita_financiamento", "aceita_permuta"}


# ── Schemas ───────────────────────────────────────────────────────────────────


class ImovelIn(BaseModel):
    """Cadastro. Só 4 campos obrigatórios — o resto entra depois."""

    tipo: str
    bairro: str
    dono_telefone: str
    preco_pedido: Decimal | None = None

    cidade: str = "Cidade"
    quartos: int | None = Field(default=None, ge=0, le=50)
    vagas: int | None = Field(default=None, ge=0, le=50)
    suites: int | None = Field(default=None, ge=0, le=50)
    area_m2: Decimal | None = None
    endereco: str | None = None
    cep: str | None = None
    referencia: str | None = None
    aceita_financiamento: bool | None = None
    aceita_permuta: bool | None = None
    dono_nome: str | None = None
    dono_relacao: str | None = None
    exclusividade: bool = False
    autorizacao_assinada: bool = False
    fonte: str | None = None
    lead_id: str | None = None
    observacoes: str | None = None
    # Só admin: cadastrar NO LUGAR de uma corretora. Corretora comum ignora.
    corretora_id: int | None = None

    @field_validator("tipo")
    @classmethod
    def _tipo_valido(cls, v: str) -> str:
        v = (v or "").strip().lower()
        return v if v in TIPOS else "outro"

    @field_validator("dono_relacao")
    @classmethod
    def _relacao_valida(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().lower()
        return v if v in RELACOES else "outro"

    @field_validator("fonte")
    @classmethod
    def _fonte_valida(cls, v: str | None) -> str | None:
        if not v:
            return None
        v = v.strip().lower()
        return v if v in FONTES else "outro"

    # ⚠️ PydanticCustomError, não ValueError: o handler de validação do app
    # (AgentOS/Starlette) serializa o `ctx` do erro em JSON, e um ValueError
    # cru dentro do ctx explode com "Object of type ValueError is not JSON
    # serializable" — 500 no lugar do 422. Achado rodando a suíte (12/09).
    @field_validator("bairro", "cidade")
    @classmethod
    def _texto_obrigatorio(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise PydanticCustomError("campo_vazio", "campo obrigatório vazio")
        return v[:120]

    @field_validator("dono_telefone")
    @classmethod
    def _tel_nao_vazio(cls, v: str) -> str:
        v = (v or "").strip()
        if not re.sub(r"\D", "", v):
            raise PydanticCustomError("telefone_sem_digitos", "telefone sem nenhum dígito")
        return v[:40]


class ImovelPatch(BaseModel):
    """Edição — todo campo é opcional; só o que vier é gravado."""

    tipo: str | None = None
    bairro: str | None = None
    cidade: str | None = None
    quartos: int | None = None
    vagas: int | None = None
    suites: int | None = None
    area_m2: Decimal | None = None
    endereco: str | None = None
    cep: str | None = None
    referencia: str | None = None
    preco_pedido: Decimal | None = None
    aceita_financiamento: bool | None = None
    aceita_permuta: bool | None = None
    dono_nome: str | None = None
    dono_telefone: str | None = None
    dono_relacao: str | None = None
    situacao: str | None = None
    exclusividade: bool | None = None
    exclusividade_ate: str | None = None
    autorizacao_assinada: bool | None = None
    fonte: str | None = None
    observacoes: str | None = None
    # Transferência de captação — só admin, e vira evento assinado.
    corretora_id: int | None = None
    nota: str | None = None


class FotoOut(BaseModel):
    id: int
    ordem: int
    mime: str
    largura: int | None = None
    altura: int | None = None
    bytes: int | None = None
    origem: str
    tratada: bool
    tratada_de_id: int | None = None
    tratamento: str | None = None
    autorizacao_dono: bool
    criado_em: datetime | None = None
    # A foto NUNCA é servida por caminho público — só por esta rota autenticada.
    url: str


class EventoOut(BaseModel):
    id: int
    tipo: str
    de: dict[str, Any] | None = None
    para: dict[str, Any] | None = None
    por: str
    nota: str | None = None
    em: datetime | None = None


class ImovelOut(BaseModel):
    id: int
    corretora_id: int
    corretora_label: str | None = None
    captado_por: str
    captado_em: datetime | None = None
    tipo: str
    quartos: int | None = None
    vagas: int | None = None
    suites: int | None = None
    area_m2: float | None = None
    bairro: str
    cidade: str
    endereco: str | None = None
    cep: str | None = None
    referencia: str | None = None
    preco_pedido: float | None = None
    aceita_financiamento: bool | None = None
    aceita_permuta: bool | None = None
    dono_nome: str | None = None
    dono_telefone: str
    dono_relacao: str | None = None
    situacao: str
    exclusividade: bool
    exclusividade_ate: str | None = None
    autorizacao_assinada: bool
    fonte: str | None = None
    observacoes: str | None = None
    completude: int
    faltando: list[str] = []
    fotos_total: int = 0
    capa_url: str | None = None
    atualizado_em: datetime | None = None


class ParecidoOut(BaseModel):
    id: int
    aviso: str
    corretora_label: str | None = None
    captado_em: datetime | None = None
    mesmo_telefone: bool = False


class LeadInteressadoOut(BaseModel):
    telefone: str
    nome: str | None = None
    contexto: str
    criado_em: datetime | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────


def _sem_acento(s: str) -> str:
    n = unicodedata.normalize("NFD", (s or "").lower().strip())
    return "".join(c for c in n if unicodedata.category(c) != "Mn")


def _tel_normalizado(raw: str) -> str | None:
    """E.164 sem '+' quando dá; None quando o número não é reconhecível.

    Reusa `app.services.phone` e NUNCA levanta: telefone estranho não pode
    derrubar o cadastro. O campo cru (`dono_telefone`) fica como a corretora
    digitou — o normalizado é só pra casar duplicata.
    """
    try:
        from app.services.phone import normalize_phone

        return normalize_phone(raw)
    except Exception:  # noqa: BLE001
        digitos = re.sub(r"\D", "", raw or "")
        return digitos or None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError, InvalidOperation):
        return None


def _faltando(row: Any, fotos_total: int) -> list[str]:
    faltas: list[str] = []
    for campo, label in CHECKLIST:
        if campo == "fotos":
            if fotos_total <= 0:
                faltas.append(label)
            continue
        valor = getattr(row, campo, None) if not isinstance(row, dict) else row.get(campo)
        if campo in _BOOL_TRISTATE:
            if valor is None:
                faltas.append(label)
            continue
        if campo == "autorizacao_assinada":
            if not valor:
                faltas.append(label)
            continue
        if valor is None or (isinstance(valor, str) and not valor.strip()):
            faltas.append(label)
    return faltas


def _completude(row: Any, fotos_total: int) -> int:
    faltas = len(_faltando(row, fotos_total))
    total = len(CHECKLIST)
    return int(round(100 * (total - faltas) / total))


def _label_corretora(cid: int | None) -> str | None:
    if cid is None:
        return None
    try:
        from app.api.painel import CORRETORA_NOME_DB

        return CORRETORA_NOME_DB.get(cid) or f"C{cid}"
    except Exception:  # noqa: BLE001
        return f"C{cid}"


def _primeiro_nome(cid: int | None) -> str:
    label = _label_corretora(cid) or "uma corretora"
    return label.split()[0]


def _media_root() -> str:
    return settings.imoveis_media_dir


def _caminho_absoluto(relativo: str) -> str:
    """Resolve o caminho da foto DENTRO do diretório de mídia.

    O banco guarda caminho relativo de propósito (mover o disco não pode virar
    UPDATE em massa). Aqui a checagem de path traversal: um `arquivo` com
    '../' apontaria pra fora e serviria arquivo do servidor por uma rota que a
    corretora acessa.
    """
    root = os.path.realpath(_media_root())
    alvo = os.path.realpath(os.path.join(root, relativo))
    if alvo != root and not alvo.startswith(root + os.sep):
        raise ValueError(f"caminho_fora_do_media_dir: {relativo!r}")
    return alvo


def registrar_evento(
    s: Session,
    imovel_id: int,
    tipo: str,
    por: str,
    de: dict[str, Any] | None = None,
    para: dict[str, Any] | None = None,
    nota: str | None = None,
) -> None:
    """Append-only. Sem commit — quem chama controla a transação."""
    from app.db.models import ImovelEvento

    s.add(
        ImovelEvento(
            imovel_id=imovel_id, tipo=tipo, por=por[:120], de=de, para=para, nota=nota
        )
    )


def _fotos_total(s: Session, imovel_id: int) -> int:
    return int(
        s.execute(
            sql_text(
                "SELECT count(*) FROM imovel_foto "
                "WHERE imovel_id = :i AND arquivada = false AND tratada = false"
            ),
            {"i": imovel_id},
        ).scalar()
        or 0
    )


def recalcular_completude(s: Session, imovel: Any) -> int:
    """Recalcula e grava `completude`. Chamado após criar, editar e subir foto."""
    pct = _completude(imovel, _fotos_total(s, imovel.id))
    imovel.completude = pct
    return pct


# ── Duplicata: AVISA, nunca barra ─────────────────────────────────────────────


def parecidos(
    s: Session,
    *,
    tenant_id: int,
    cidade: str,
    bairro: str,
    tipo: str,
    quartos: int | None,
    area_m2: Decimal | None,
    tel_normalizado: str | None,
    ignorar_id: int | None = None,
) -> list[ParecidoOut]:
    """Imóveis que PARECEM o mesmo. Trava social, não erro HTTP.

    Dois caminhos, do mais forte pro mais fraco:
      1. mesmo telefone de dono → quase certeza (e vale mesmo em outro bairro);
      2. mesmo bloco (cidade+bairro+tipo+quartos) com área a menos de 10% de
         diferença — o discriminante é ÁREA, não preço (revisão §5b: preço é da
         oferta, metragem é do imóvel).

    Nunca levanta: se a consulta falhar, o cadastro segue sem aviso.
    """
    try:
        linhas = s.execute(
            sql_text(
                """
                SELECT id, corretora_id, captado_em, area_m2, quartos, bairro,
                       dono_telefone_normalizado
                FROM imoveis_captados
                WHERE tenant_id = :t
                  AND situacao <> 'nao_valida'
                  AND (CAST(:ign AS BIGINT) IS NULL OR id <> CAST(:ign AS BIGINT))
                  AND (
                        (CAST(:tel AS TEXT) IS NOT NULL
                         AND dono_telefone_normalizado = CAST(:tel AS TEXT))
                     OR (lower(cidade) = lower(CAST(:cid AS TEXT))
                         AND lower(bairro) = lower(CAST(:bai AS TEXT))
                         AND tipo = CAST(:tip AS TEXT)
                         AND (CAST(:q AS INT) IS NULL OR quartos IS NULL
                              OR quartos = CAST(:q AS INT)))
                  )
                ORDER BY captado_em DESC
                LIMIT 5
                """
            ),
            {
                "t": tenant_id,
                "ign": ignorar_id,
                "tel": tel_normalizado,
                "cid": cidade,
                "bai": bairro,
                "tip": tipo,
                "q": quartos,
            },
        ).all()
    except Exception as e:  # noqa: BLE001
        # Rollback obrigatório: no Postgres, uma query que falha aborta a
        # transação inteira — sem isto, o cadastro ESTAVA salvo mas a resposta
        # quebrava, que é exatamente o que a regra "nada faz o cadastro falhar"
        # proíbe. Achado rodando a suíte (12/09).
        s.rollback()
        log.warning("imoveis_parecidos_falhou", erro=str(e)[:160])
        return []

    achados: list[ParecidoOut] = []
    minha_area = _num(area_m2)
    for r in linhas:
        mesmo_tel = bool(tel_normalizado and r.dono_telefone_normalizado == tel_normalizado)
        if not mesmo_tel and minha_area is not None:
            outra = _num(r.area_m2)
            # Área conhecida nos dois lados e discrepante > 10% → não é o mesmo.
            if outra and abs(outra - minha_area) / max(outra, minha_area) > 0.10:
                continue
        quando = r.captado_em.strftime("%d/%m") if r.captado_em else "antes"
        quem = _primeiro_nome(r.corretora_id)
        motivo = "o mesmo telefone de dono" if mesmo_tel else "um parecido"
        achados.append(
            ParecidoOut(
                id=r.id,
                aviso=f"a {quem} captou {motivo} em {quando}",
                corretora_label=_label_corretora(r.corretora_id),
                captado_em=r.captado_em,
                mesmo_telefone=mesmo_tel,
            )
        )
    return achados


# ── CRUD ──────────────────────────────────────────────────────────────────────


def criar(
    s: Session,
    dados: ImovelIn,
    *,
    corretora_id: int,
    captado_por: str,
    tenant_id: int = 1,
) -> tuple[Any, list[ParecidoOut]]:
    """Cria a captação. Devolve (imovel, parecidos).

    Os parecidos são AVISO — o imóvel já está gravado quando eles voltam.
    """
    from app.db.models import ImovelCaptado

    tel_norm = _tel_normalizado(dados.dono_telefone)

    imovel = ImovelCaptado(
        tenant_id=tenant_id,
        corretora_id=corretora_id,
        captado_por=captado_por[:120],
        tipo=dados.tipo,
        quartos=dados.quartos,
        vagas=dados.vagas,
        suites=dados.suites,
        area_m2=dados.area_m2,
        bairro=dados.bairro,
        cidade=dados.cidade,
        endereco=(dados.endereco or None),
        cep=(dados.cep or None),
        referencia=(dados.referencia or None),
        preco_pedido=dados.preco_pedido,
        aceita_financiamento=dados.aceita_financiamento,
        aceita_permuta=dados.aceita_permuta,
        dono_nome=(dados.dono_nome or None),
        dono_telefone=dados.dono_telefone,
        dono_telefone_normalizado=tel_norm,
        dono_relacao=dados.dono_relacao,
        situacao="disponivel",
        exclusividade=dados.exclusividade,
        autorizacao_assinada=dados.autorizacao_assinada,
        fonte=dados.fonte,
        lead_id=(dados.lead_id or None),
        observacoes=(dados.observacoes or None),
        completude=0,
    )
    s.add(imovel)
    s.flush()

    imovel.completude = _completude(imovel, 0)
    registrar_evento(
        s,
        imovel.id,
        "criado",
        captado_por,
        para={"bairro": imovel.bairro, "tipo": imovel.tipo, "corretora_id": corretora_id},
    )
    s.commit()
    s.refresh(imovel)

    achados = parecidos(
        s,
        tenant_id=tenant_id,
        cidade=imovel.cidade,
        bairro=imovel.bairro,
        tipo=imovel.tipo,
        quartos=imovel.quartos,
        area_m2=imovel.area_m2,
        tel_normalizado=tel_norm,
        ignorar_id=imovel.id,
    )
    log.info(
        "imovel_captado_criado",
        imovel_id=imovel.id,
        corretora_id=corretora_id,
        por=captado_por,
        parecidos=len(achados),
    )
    return imovel, achados


# Campos que a corretora pode editar direto. `corretora_id` fica de fora: é
# transferência de captação (admin + evento assinado), tratada à parte.
_EDITAVEIS = (
    "tipo", "bairro", "cidade", "quartos", "vagas", "suites", "area_m2",
    "endereco", "cep", "referencia", "preco_pedido", "aceita_financiamento",
    "aceita_permuta", "dono_nome", "dono_telefone", "dono_relacao", "situacao",
    "exclusividade", "autorizacao_assinada", "fonte", "observacoes",
)
# Mudanças que geram evento PRÓPRIO (não entram no "editado" genérico) — são as
# que mexem em comissão, disponibilidade e negociação.
_EVENTO_PROPRIO = {"situacao": "situacao_mudou", "preco_pedido": "preco_mudou"}


def editar(
    s: Session,
    imovel: Any,
    patch: ImovelPatch,
    *,
    por: str,
    pode_transferir: bool,
) -> dict[str, Any]:
    """Aplica o patch e registra os eventos. Devolve o diff aplicado."""
    campos = patch.model_dump(exclude_unset=True)
    diff: dict[str, Any] = {}

    for campo in _EDITAVEIS:
        if campo not in campos:
            continue
        novo = campos[campo]
        if campo == "tipo":
            novo = (novo or "").strip().lower()
            if novo not in TIPOS:
                novo = "outro"
        if campo == "situacao":
            novo = (novo or "").strip().lower()
            if novo not in SITUACOES:
                continue  # valor inválido é ignorado, não derruba a edição
        if campo == "dono_relacao" and novo and novo not in RELACOES:
            novo = "outro"
        if campo == "fonte" and novo and novo not in FONTES:
            novo = "outro"
        if isinstance(novo, str):
            novo = novo.strip() or None
        antigo = getattr(imovel, campo)
        if str(antigo) == str(novo):
            continue
        setattr(imovel, campo, novo)
        diff[campo] = {"de": _jsonable(antigo), "para": _jsonable(novo)}
        if campo == "dono_telefone" and novo:
            imovel.dono_telefone_normalizado = _tel_normalizado(str(novo))

    if "exclusividade_ate" in campos:
        from datetime import date as _date

        bruto = campos["exclusividade_ate"]
        try:
            nova = _date.fromisoformat(bruto) if bruto else None
        except (TypeError, ValueError):
            nova = None
        if nova != imovel.exclusividade_ate:
            diff["exclusividade_ate"] = {
                "de": _jsonable(imovel.exclusividade_ate),
                "para": _jsonable(nova),
            }
            imovel.exclusividade_ate = nova

    # Transferência de captação: o dado que define comissão. Evento assinado,
    # nunca UPDATE silencioso (`acme-comissoes-caixa-vs-crm`).
    nova_dona = campos.get("corretora_id")
    if nova_dona and int(nova_dona) != int(imovel.corretora_id):
        if not pode_transferir:
            raise PermissionError("transferencia_requer_admin")
        de_cid = int(imovel.corretora_id)
        imovel.corretora_id = int(nova_dona)
        registrar_evento(
            s,
            imovel.id,
            "captacao_transferida",
            por,
            de={"corretora_id": de_cid, "label": _label_corretora(de_cid)},
            para={"corretora_id": int(nova_dona), "label": _label_corretora(int(nova_dona))},
            nota=patch.nota,
        )
        diff["corretora_id"] = {"de": de_cid, "para": int(nova_dona)}

    for campo, tipo_evento in _EVENTO_PROPRIO.items():
        if campo in diff:
            registrar_evento(
                s, imovel.id, tipo_evento, por,
                de={campo: diff[campo]["de"]}, para={campo: diff[campo]["para"]},
                nota=patch.nota,
            )

    genericos = {k: v for k, v in diff.items() if k not in _EVENTO_PROPRIO and k != "corretora_id"}
    if genericos:
        registrar_evento(
            s, imovel.id, "editado", por,
            de={k: v["de"] for k, v in genericos.items()},
            para={k: v["para"] for k, v in genericos.items()},
            nota=patch.nota,
        )

    if diff:
        imovel.atualizado_em = datetime.now(timezone.utc)
    recalcular_completude(s, imovel)
    s.commit()
    s.refresh(imovel)
    log.info("imovel_captado_editado", imovel_id=imovel.id, por=por, campos=list(diff))
    return diff


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


# ── Fotos ─────────────────────────────────────────────────────────────────────


def _abrir_pillow():  # type: ignore[no-untyped-def]
    try:
        from PIL import Image, ImageOps

        return Image, ImageOps
    except ImportError as e:  # pragma: no cover — runtime sem Pillow
        raise PillowIndisponivel(
            "Pillow não está instalado no runtime da copiloto-api — "
            "redimensionamento e logo desligados"
        ) from e


def _redimensionar(conteudo: bytes, lado_max: int) -> tuple[bytes, str, int, int]:
    """Rede de segurança do servidor. O front já reduz antes de subir (é o que
    faz funcionar em 4G na rua), mas quem bate direto na API não reduz nada."""
    Image, ImageOps = _abrir_pillow()
    with Image.open(io.BytesIO(conteudo)) as im:
        im = ImageOps.exif_transpose(im)  # foto de celular deitada
        im = im.convert("RGB")
        if max(im.size) > lado_max:
            im.thumbnail((lado_max, lado_max), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), "image/jpeg", im.width, im.height


def salvar_foto(
    s: Session,
    imovel: Any,
    conteudo: bytes,
    *,
    mime: str,
    origem: str = "dono",
    autorizacao_dono: bool = False,
    enviado_por: str,
) -> Any:
    """Grava a foto no disco e a linha em `imovel_foto`.

    Dedupe barato pelo sha256 (mesma foto mandada duas vezes no mesmo imóvel):
    o índice único do banco cobre a corrida; aqui a checagem evita o 500 e
    devolve a foto que já existe.
    """
    from app.db.models import ImovelFoto

    if origem not in ORIGENS_FOTO:
        # 🔴 'portal' cai aqui. O CHECK do banco também barra — esta é só a
        # mensagem legível antes do IntegrityError.
        raise ValueError(f"origem_invalida: {origem!r} (só 'dono' ou 'corretora')")
    if not conteudo:
        raise ValueError("arquivo_vazio")
    if len(conteudo) > FOTO_MAX_BYTES:
        raise ValueError("arquivo_muito_grande")

    dados, mime_final, larg, alt = _redimensionar(conteudo, settings.imoveis_foto_lado_max)
    digest = hashlib.sha256(dados).hexdigest()

    existente = s.execute(
        sql_text(
            "SELECT id FROM imovel_foto "
            "WHERE imovel_id = :i AND hash_sha256 = :h AND tratada = false LIMIT 1"
        ),
        {"i": imovel.id, "h": digest},
    ).scalar()
    if existente:
        log.info("imovel_foto_duplicada", imovel_id=imovel.id, foto_id=existente)
        return s.get(ImovelFoto, existente)

    relativo = f"{imovel.id}/{digest}.jpg"
    destino = _caminho_absoluto(relativo)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as f:
        f.write(dados)

    ordem = int(
        s.execute(
            sql_text("SELECT coalesce(max(ordem), -1) + 1 FROM imovel_foto WHERE imovel_id = :i"),
            {"i": imovel.id},
        ).scalar()
        or 0
    )
    foto = ImovelFoto(
        imovel_id=imovel.id,
        ordem=ordem,
        arquivo=relativo,
        mime=mime_final,
        bytes=len(dados),
        largura=larg,
        altura=alt,
        hash_sha256=digest,
        origem=origem,
        autorizacao_dono=autorizacao_dono,
        enviado_por=enviado_por[:120],
    )
    s.add(foto)
    s.flush()
    registrar_evento(
        s, imovel.id, "foto_adicionada", enviado_por,
        para={"foto_id": foto.id, "origem": origem, "bytes": len(dados)},
    )
    recalcular_completude(s, imovel)
    s.commit()
    s.refresh(foto)
    log.info("imovel_foto_salva", imovel_id=imovel.id, foto_id=foto.id, bytes=len(dados))
    return foto


def ler_foto(foto: Any) -> tuple[bytes, str]:
    """Bytes da foto pra rota autenticada. Nunca há caminho público."""
    caminho = _caminho_absoluto(foto.arquivo)
    if not os.path.exists(caminho):
        raise FileNotFoundError(foto.arquivo)
    with open(caminho, "rb") as f:
        return f.read(), (foto.mime or "image/jpeg")


def _logo_acme():  # type: ignore[no-untyped-def]
    """Abre a logo. Ausente → LogoIndisponivel. NUNCA gera logo improvisada."""
    Image, _ = _abrir_pillow()
    caminho = settings.acme_logo_path
    if not caminho:
        raise LogoIndisponivel("ACME_LOGO_PATH não configurado")
    if not os.path.isabs(caminho):
        # Relativo ao root do projeto (app/assets/acme-logo.png).
        raiz = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        caminho = os.path.join(raiz, caminho)
    if not os.path.exists(caminho):
        raise LogoIndisponivel(f"logo da Acme não encontrada em {caminho}")
    return Image.open(caminho).convert("RGBA")


def aplicar_logo(s: Session, imovel: Any, foto: Any, *, por: str) -> Any:
    """Gera a versão com a logo — ARQUIVO NOVO, linha NOVA.

    A original nunca é sobrescrita nem apagada: a nova linha nasce com
    `tratada=true` e `tratada_de_id` apontando pra ela (§3 do plano). Tratamento
    aqui é só marca d'água da PRÓPRIA Acme em foto que veio do dono — não tem
    nada a ver com remover marca d'água de portal, que a gente recusou.
    """
    from app.db.models import ImovelFoto

    if foto.tratada:
        raise ValueError("foto_ja_e_tratada")

    Image, _ = _abrir_pillow()
    logo = _logo_acme()  # levanta LogoIndisponivel ANTES de mexer na foto

    caminho = _caminho_absoluto(foto.arquivo)
    if not os.path.exists(caminho):
        raise FileNotFoundError(foto.arquivo)

    with Image.open(caminho) as base_raw:
        base = base_raw.convert("RGBA")
        # Logo em ~22% da largura, canto inferior direito, margem de 3,5%.
        alvo_l = max(64, int(base.width * 0.22))
        alvo_a = max(1, int(logo.height * alvo_l / logo.width))
        marca = logo.resize((alvo_l, alvo_a), Image.LANCZOS)
        margem = int(base.width * 0.035)
        pos = (base.width - alvo_l - margem, base.height - alvo_a - margem)
        camada = Image.new("RGBA", base.size, (0, 0, 0, 0))
        camada.paste(marca, pos, marca)
        composta = Image.alpha_composite(base, camada).convert("RGB")
        buf = io.BytesIO()
        composta.save(buf, format="JPEG", quality=88, optimize=True)
        dados = buf.getvalue()
        larg, alt = composta.size

    digest = hashlib.sha256(dados).hexdigest()
    relativo = f"{imovel.id}/logo-{digest}.jpg"
    destino = _caminho_absoluto(relativo)
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    with open(destino, "wb") as f:
        f.write(dados)

    nova = ImovelFoto(
        imovel_id=imovel.id,
        ordem=foto.ordem,
        arquivo=relativo,
        mime="image/jpeg",
        bytes=len(dados),
        largura=larg,
        altura=alt,
        hash_sha256=digest,
        origem=foto.origem,
        autorizacao_dono=foto.autorizacao_dono,
        tratada=True,
        tratada_de_id=foto.id,
        tratamento="logo_acme",
        enviado_por=por[:120],
    )
    s.add(nova)
    s.flush()
    registrar_evento(
        s, imovel.id, "foto_tratada", por,
        de={"foto_id": foto.id}, para={"foto_id": nova.id, "tratamento": "logo_acme"},
    )
    s.commit()
    s.refresh(nova)
    log.info("imovel_foto_logo", imovel_id=imovel.id, original=foto.id, tratada=nova.id)
    return nova


def arquivar_foto(s: Session, imovel: Any, foto: Any, *, por: str) -> None:
    """Retirar de circulação é isto — nada se apaga.

    A versão com logo sai junto: sem isso, excluir a foto da capa deixaria a
    cópia tratada dela no ar como se a corretora não tivesse apagado nada.
    """
    from app.db.models import ImovelFoto

    foto.arquivada = True
    derivadas = (
        s.query(ImovelFoto)
        .filter(ImovelFoto.tratada_de_id == foto.id, ImovelFoto.arquivada.is_(False))
        .all()
    )
    for d in derivadas:
        d.arquivada = True
    registrar_evento(
        s, imovel.id, "foto_arquivada", por,
        de={"foto_id": foto.id, "derivadas": [d.id for d in derivadas]},
    )
    recalcular_completude(s, imovel)
    s.commit()


def reordenar_fotos(s: Session, imovel: Any, ids: list[int], *, por: str) -> None:
    """Grava a ordem que a corretora arrastou na tela. A primeira vira a capa.

    `ids` tem que ser EXATAMENTE as fotos originais em circulação do imóvel —
    lista parcial ou com foto de outro imóvel é recusada, pra uma tela velha
    (aberta antes de alguém subir ou excluir foto) não embaralhar o resto.
    A versão com logo acompanha a ordem da original dela.
    """
    from app.db.models import ImovelFoto

    fotos = (
        s.query(ImovelFoto)
        .filter(ImovelFoto.imovel_id == imovel.id, ImovelFoto.arquivada.is_(False))
        .all()
    )
    originais = {f.id: f for f in fotos if not f.tratada}
    if len(ids) != len(set(ids)) or set(ids) != set(originais):
        raise ValueError("ordem_desatualizada")

    antes = [f.id for f in sorted(originais.values(), key=lambda f: (f.ordem, f.id))]
    if antes == ids:
        return
    posicao = {fid: i for i, fid in enumerate(ids)}
    for f in fotos:
        alvo = f.tratada_de_id if f.tratada else f.id
        if alvo in posicao:
            f.ordem = posicao[alvo]
    registrar_evento(s, imovel.id, "fotos_reordenadas", por, de={"ordem": antes}, para={"ordem": ids})
    s.commit()
    log.info("imovel_fotos_reordenadas", imovel_id=imovel.id, por=por, capa=ids[0] if ids else None)


def _capa(fotos: list[Any]) -> Any | None:
    """Capa = a 1ª original na ordem; se ela tem versão com logo, a versão."""
    originais = sorted((f for f in fotos if not f.tratada), key=lambda f: (f.ordem, f.id))
    if not originais:
        return None
    primeira = originais[0]
    tratada = next((f for f in fotos if f.tratada and f.tratada_de_id == primeira.id), None)
    return tratada or primeira


# ── Leitura ───────────────────────────────────────────────────────────────────


def _linha_para_out(row: Any, fotos_total: int, capa_id: int | None) -> ImovelOut:
    return ImovelOut(
        id=row.id,
        corretora_id=row.corretora_id,
        corretora_label=_label_corretora(row.corretora_id),
        captado_por=row.captado_por,
        captado_em=row.captado_em,
        tipo=row.tipo,
        quartos=row.quartos,
        vagas=row.vagas,
        suites=row.suites,
        area_m2=_num(row.area_m2),
        bairro=row.bairro,
        cidade=row.cidade,
        endereco=row.endereco,
        cep=row.cep,
        referencia=row.referencia,
        preco_pedido=_num(row.preco_pedido),
        aceita_financiamento=row.aceita_financiamento,
        aceita_permuta=row.aceita_permuta,
        dono_nome=row.dono_nome,
        dono_telefone=row.dono_telefone,
        dono_relacao=row.dono_relacao,
        situacao=row.situacao,
        exclusividade=row.exclusividade,
        exclusividade_ate=row.exclusividade_ate.isoformat() if row.exclusividade_ate else None,
        autorizacao_assinada=row.autorizacao_assinada,
        fonte=row.fonte,
        observacoes=row.observacoes,
        completude=row.completude,
        faltando=_faltando(row, fotos_total),
        fotos_total=fotos_total,
        capa_url=f"/painel/imoveis/fotos/{capa_id}" if capa_id else None,
        atualizado_em=row.atualizado_em,
    )


def listar(
    s: Session,
    *,
    tenant_id: int = 1,
    corretora_id: int | None,
    filtro_corretora: int | None = None,
    situacao: str | None = None,
    busca: str | None = None,
    limite: int = 200,
) -> list[ImovelOut]:
    """Carteira. `corretora_id=None` = admin (vê todas)."""
    from app.db.models import ImovelCaptado

    q = s.query(ImovelCaptado).filter(ImovelCaptado.tenant_id == tenant_id)
    if corretora_id is not None:
        q = q.filter(ImovelCaptado.corretora_id == corretora_id)
    elif filtro_corretora:
        q = q.filter(ImovelCaptado.corretora_id == filtro_corretora)
    if situacao and situacao in SITUACOES:
        q = q.filter(ImovelCaptado.situacao == situacao)
    if busca and busca.strip():
        termo = f"%{busca.strip()}%"
        q = q.filter(
            sql_text(
                "(bairro ILIKE :b OR coalesce(endereco,'') ILIKE :b "
                "OR coalesce(dono_nome,'') ILIKE :b OR dono_telefone ILIKE :b "
                "OR coalesce(referencia,'') ILIKE :b)"
            ).bindparams(b=termo)
        )
    linhas = q.order_by(ImovelCaptado.captado_em.desc()).limit(min(limite, 500)).all()
    if not linhas:
        return []

    ids = [r.id for r in linhas]
    contagem = {
        i: n
        for i, n in s.execute(
            sql_text(
                "SELECT imovel_id, count(*) FROM imovel_foto "
                "WHERE imovel_id = ANY(:ids) AND arquivada = false AND tratada = false "
                "GROUP BY imovel_id"
            ),
            {"ids": ids},
        ).all()
    }
    # Mesma regra de `_capa`: 1ª original na ordem; a versão com logo dela,
    # se existir, no lugar.
    capas = {
        i: fid
        for i, fid in s.execute(
            sql_text(
                "SELECT DISTINCT ON (o.imovel_id) o.imovel_id, coalesce(t.id, o.id) "
                "FROM imovel_foto o "
                "LEFT JOIN LATERAL (SELECT t.id FROM imovel_foto t "
                "  WHERE t.tratada_de_id = o.id AND t.arquivada = false "
                "  ORDER BY t.id DESC LIMIT 1) t ON true "
                "WHERE o.imovel_id = ANY(:ids) AND o.arquivada = false AND o.tratada = false "
                "ORDER BY o.imovel_id, o.ordem ASC, o.id ASC"
            ),
            {"ids": ids},
        ).all()
    }
    return [_linha_para_out(r, contagem.get(r.id, 0), capas.get(r.id)) for r in linhas]


def detalhe(s: Session, imovel: Any) -> dict[str, Any]:
    from app.db.models import ImovelEvento, ImovelFoto

    fotos = (
        s.query(ImovelFoto)
        .filter(ImovelFoto.imovel_id == imovel.id, ImovelFoto.arquivada.is_(False))
        .order_by(ImovelFoto.ordem.asc(), ImovelFoto.id.asc())
        .all()
    )
    eventos = (
        s.query(ImovelEvento)
        .filter(ImovelEvento.imovel_id == imovel.id)
        .order_by(ImovelEvento.em.desc())
        .limit(50)
        .all()
    )
    originais = [f for f in fotos if not f.tratada]
    capa = _capa(fotos)
    return {
        "imovel": _linha_para_out(imovel, len(originais), capa.id if capa else None),
        "fotos": [
            FotoOut(
                id=f.id, ordem=f.ordem, mime=f.mime, largura=f.largura, altura=f.altura,
                bytes=f.bytes, origem=f.origem, tratada=f.tratada,
                tratada_de_id=f.tratada_de_id, tratamento=f.tratamento,
                autorizacao_dono=f.autorizacao_dono, criado_em=f.criado_em,
                url=f"/painel/imoveis/fotos/{f.id}",
            )
            for f in fotos
        ],
        "eventos": [
            EventoOut(id=e.id, tipo=e.tipo, de=e.de, para=e.para, por=e.por, nota=e.nota, em=e.em)
            for e in eventos
        ],
    }


# ── Item 4 · "N leads da base pediram isso" ───────────────────────────────────
#
# Cruzamento BARATO com o que a Pessoa já guarda em `lead_handoff_notes`
# (origem 'pessoa'): o raio-X é TEXTO NARRATIVO ("Busca: apartamento 2 quartos no
# Jardim X. Faixa de valor: até 300 mil."), não campo estruturado.
#
# ⚠️ LIMITE ASSUMIDO, e ele importa: dá pra casar BAIRRO e TIPO por menção no
# texto — isso é confiável. FAIXA DE VALOR e QUARTOS vêm em português livre
# ("uns 300", "até trezentos mil", "2 ou 3 quartos") e parsear isso direito é
# projeto próprio, não linha de query. Então esta função NÃO promete faixa de
# preço: ela devolve quem pediu aquele bairro e aquele tipo, e a corretora lê o
# raio-X inteiro. Prometer match de preço com regex seria o erro do
# `followers_total` de novo — número que parece medido e não é.


def leads_interessados(
    s: Session, imovel: Any, *, limite: int = 8
) -> tuple[list[LeadInteressadoOut], str]:
    """Leads cujo raio-X menciona o bairro do imóvel (e o tipo, quando bate).

    Devolve (lista, explicação). Nunca levanta — é um bônus da tela, não pode
    derrubar o detalhe do imóvel.
    """
    bairro = (imovel.bairro or "").strip()
    if len(_sem_acento(bairro)) < 3:
        return [], "bairro curto demais pra cruzar"
    try:
        linhas = s.execute(
            sql_text(
                """
                SELECT DISTINCT ON (telefone) telefone, lead_nome, contexto, criado_em
                FROM lead_handoff_notes
                WHERE tenant_id = :t
                  AND status = 'ativo'
                  AND lower(contexto) LIKE :bai
                  AND (:tip = '' OR lower(contexto) LIKE :tip)
                ORDER BY telefone, criado_em DESC
                LIMIT :lim
                """
            ),
            {
                "t": imovel.tenant_id,
                "bai": f"%{bairro.lower()}%",
                "tip": f"%{imovel.tipo}%" if imovel.tipo in ("apartamento", "casa", "terreno") else "",
                "lim": limite,
            },
        ).all()
    except Exception as e:  # noqa: BLE001
        s.rollback()  # ver comentário em parecidos(): query morta aborta a tx
        log.warning("imovel_leads_interessados_falhou", imovel_id=imovel.id, erro=str(e)[:160])
        return [], "não consegui cruzar agora"

    achados = [
        LeadInteressadoOut(
            telefone=r.telefone, nome=r.lead_nome, contexto=r.contexto, criado_em=r.criado_em
        )
        for r in linhas
    ]
    explicacao = (
        f"leads cujo raio-X da Pessoa cita “{bairro}”"
        + (f" e {imovel.tipo}" if imovel.tipo in ("apartamento", "casa", "terreno") else "")
        + " — faixa de valor NÃO é cruzada (vem em texto livre); confira no raio-X."
    )
    return achados, explicacao
