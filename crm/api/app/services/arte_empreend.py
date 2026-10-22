"""
Compositor de arte de empreendimento (Fase 2 da Assistente).

Recebe a FOTO REAL do imóvel + os dados (nome/preço/bairro/zona/entrega) e compõe
um card branded Acme (foto + overlay). NÃO gera prédio por IA — usa a foto real.
Saída: PNG 1080×1350 (feed).
"""
from __future__ import annotations

import io
from PIL import Image, ImageDraw, ImageFont

# Brand Acme
LARANJA = (232, 114, 12)
GRAFITE = (21, 17, 13)
CREME = (243, 236, 221)
BRANCO = (255, 255, 255)

W, H = 1080, 1350
_FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_LOGO = "/opt/crm/projetos/dash-imobiliaria/public/acme-logo.jpg"


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    img = img.convert("RGB")
    r = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    x = (img.width - w) // 2
    y = (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def _wrap(draw, text, font, max_w):
    words, linha, out = text.split(), "", []
    for p in words:
        t = (linha + " " + p).strip()
        if draw.textlength(t, font=font) <= max_w:
            linha = t
        else:
            if linha:
                out.append(linha)
            linha = p
    if linha:
        out.append(linha)
    return out[:2]


def compor(foto_bytes: bytes, dados: dict) -> bytes:
    base = _cover(Image.open(io.BytesIO(foto_bytes)), W, H)

    # Gradiente escuro embaixo pra legibilidade
    grad = Image.new("L", (1, H), 0)
    for y in range(H):
        t = max(0.0, (y - H * 0.45) / (H * 0.55))
        grad.putpixel((0, y), int(235 * (t ** 1.4)))
    grad = grad.resize((W, H))
    overlay = Image.new("RGB", (W, H), GRAFITE)
    base = Image.composite(overlay, base, grad)

    d = ImageDraw.Draw(base)
    pad = 64
    y = H - pad

    # Preço (embaixo de tudo, laranja)
    preco = dados.get("preco_fmt") or ""
    if preco and preco != "sob consulta":
        f = _font(_FB, 62)
        y -= 62
        d.text((pad, y), f"A partir de {preco}", font=f, fill=LARANJA)
        y -= 14

    # Especificações: quartos · metros
    specs = []
    if dados.get("quartos"):
        specs.append(f"{dados['quartos']} quartos")
    if dados.get("metros"):
        specs.append(f"{dados['metros']}m²")
    if specs:
        f = _font(_FB, 38)
        y -= 44
        d.text((pad, y), " · ".join(specs), font=f, fill=BRANCO)
        y -= 6

    # Bairro · Zona
    loc = " · ".join([x for x in [dados.get("bairro"), dados.get("zona")] if x])
    if loc:
        f = _font(_FR, 40)
        y -= 44
        d.text((pad, y), loc, font=f, fill=CREME)
        y -= 10

    # Nome (grande, branco, até 2 linhas)
    nome = (dados.get("nome") or "Empreendimento").upper()
    f = _font(_FB, 76)
    linhas = _wrap(d, nome, f, W - 2 * pad)
    for ln in reversed(linhas):
        y -= 84
        d.text((pad, y), ln, font=f, fill=BRANCO)

    # Badge de entrega (acima do nome)
    ent = dados.get("entrega_fmt")
    if ent:
        f = _font(_FB, 32)
        tw = d.textlength(ent, font=f)
        y -= 60
        d.rounded_rectangle([pad, y, pad + tw + 40, y + 46], radius=23, fill=LARANJA)
        d.text((pad + 20, y + 6), ent, font=f, fill=BRANCO)

    # Logo Acme (topo esquerdo, com respiro)
    try:
        logo = Image.open(_LOGO).convert("RGB")
        lw = 180
        logo = logo.resize((lw, int(logo.height * lw / logo.width)), Image.LANCZOS)
        card = Image.new("RGB", (logo.width + 24, logo.height + 24), BRANCO)
        card.paste(logo, (12, 12))
        base.paste(card, (pad - 8, 40))
    except Exception:
        pass

    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()
