// Telefone pra discar no celular da corretora.
//
// Guardamos os números em E.164 (+5500000000000) porque é o formato que o
// WhatsApp e o Kommo usam. Mas na hora de LIGAR isso atrapalha: o iPhone
// mostra "Ligar +55 (15) 9..." e trata como chamada internacional — em alguns
// planos isso muda a tarifa ou pede confirmação de operadora.
//
// Pra ligação, o que a corretora quer é discar como ela disca sempre:
// DDD + número. Então tiramos o 55 quando o número é claramente brasileiro.

export function digitos(tel: string | null | undefined): string {
  return (tel ?? "").replace(/\D/g, "");
}

/** Número pronto pro discador: DDD + número, sem o 55 do Brasil. */
export function paraDiscar(tel: string | null | undefined): string {
  const d = digitos(tel);
  // 55 + DDD(2) + número(8 ou 9) = 12 ou 13 dígitos → é BR, tira o país.
  if (d.startsWith("55") && (d.length === 12 || d.length === 13)) {
    return d.slice(2);
  }
  return d;
}

/** href do link de ligação.
 *
 * `zeroDdd` vem do backend (`discagem` na resposta da fila) e diz como o
 * telefone DAQUELA corretora completa a chamada. Mostrar o número certo na tela
 * e abrir o discador com o formato errado é pior do que não mostrar nada: ela
 * toca, dá "número incorreto", e a culpa parece ser do cadastro do lead.
 */
export function linkTel(
  tel: string | null | undefined,
  zeroDdd = false,
): string {
  if (zeroDdd) {
    const chip = paraDiscarChipVirtual(tel);
    if (chip) return `tel:${chip}`;
  }
  return `tel:${paraDiscar(tel)}`;
}

/** 0 + DDD + número — o formato do CHIP VIRTUAL (Salvy).
 *
 * A Pessoa7 liga de um chip virtual e lá o E.164 não completa: a operadora
 * devolve "o número foi discado incorretamente". O que funciona é 0+DDD+número.
 *
 * NÃO dá pra fazer disso o padrão de todo mundo: em celular comum, discar
 * 0+DDD sem código de operadora falha do mesmo jeito. Quem liga de chip normal
 * (Pessoa10, Pessoa11, Pessoa12) continua com o link de sempre; este formato aparece ao lado,
 * como texto, pra quem precisa dele.
 *
 * "" quando o número não tem cara de celular BR — melhor não mostrar nada do
 * que mostrar um palpite que não disca.
 */
export function paraDiscarChipVirtual(tel: string | null | undefined): string {
  const d = paraDiscar(tel);
  return d.length === 11 && d[2] === "9" ? `0${d}` : "";
}

/** href do WhatsApp — aqui o 55 é obrigatório (padrão internacional).
 *
 * Mantido como nome histórico (outras telas importam este); a montagem vive em
 * `linkWhatsApp`, que valida o tamanho. Um só lugar decide o formato. */
export function linkWhats(tel: string | null | undefined): string {
  const d = digitos(tel);
  return linkWhatsApp(tel) || `https://wa.me/${d.length <= 11 ? `55${d}` : d}`;
}

/** +5500000000000 — como a corretora lê. */
export function formatarTel(tel: string | null | undefined): string {
  const n = paraDiscar(tel);
  if (n.length === 11) return `(${n.slice(0, 2)}) ${n.slice(2, 7)}-${n.slice(7)}`;
  if (n.length === 10) return `(${n.slice(0, 2)}) ${n.slice(2, 6)}-${n.slice(6)}`;
  return tel ?? "";
}

/** Link da conversa no WhatsApp — `wa.me` exige E.164 SEM o "+".
 *
 * ⚠️ Abre a CONVERSA, não a chamada: o WhatsApp não publica deep link que
 * inicie ligação de voz (não existe `whatsapp://call`). Da conversa aberta, o
 * ícone de telefone fica a um toque. É o mais perto que dá pra chegar sem
 * pedir pra ela procurar o contato na agenda.
 *
 * Fora do Brasil ou número torto devolve "" — link que abre conversa vazia é
 * pior que botão ausente.
 */
export function linkWhatsApp(tel: string | null | undefined): string {
  const d = digitos(tel);
  const e164 = d.startsWith("55") ? d : `55${d}`;
  // 55 + DDD(2) + número(8 ou 9)
  if (e164.length !== 12 && e164.length !== 13) return "";
  return `https://wa.me/${e164}`;
}
