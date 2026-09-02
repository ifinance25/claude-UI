// Контракт разделителей thinking-блоков. ДОЛЖЕН совпадать с
// THINKING_OPEN_MARKER / THINKING_CLOSE_MARKER в src/claude/bridge.py.
// При изменении синхронизировать обе стороны + src/utils/formatter.py.
const OPEN = "\x01THINKING\x02";
const CLOSE = "\x02THINKING\x01";

// `OPEN ... CLOSE\n*` — целиком закрытый thinking-блок (с возможными
// переводами строк после, чтобы не оставлять пустых дыр в выводе).
const THINKING_BLOCK_RE = new RegExp(`${OPEN}[\\s\\S]*?${CLOSE}\\n*`, "g");
// `OPEN ... $` — незакрытый (хвостовой) блок: пока стрим не закончил thinking,
// показывать половину рассуждения бессмысленно.
const TRAILING_OPEN_THINKING_RE = new RegExp(`${OPEN}[\\s\\S]*$`);
// ЛЕГАСИ-ЗАЩИТА: осиротевший CLOSE без OPEN. До фикса bridge мышление рвалось
// интерливленными событиями → прилетал хвост «сырая_мысль + CLOSE + ответ» БЕЗ
// открывающего маркера, причём мышление всегда шло В НАЧАЛЕ сообщения. Поэтому
// срез «начало…первый осиротевший CLOSE» применяем ТОЛЬКО когда в исходнике
// НЕТ ни одного OPEN (чисто старый битый формат) — иначе в нормальном контенте
// (от исправленного bridge) можно случайно съесть видимый текст перед стрей-
// маркером. См. isLegacyOrphan ниже.
const ORPHAN_CLOSE_LEAD_RE = new RegExp(`^[\\s\\S]*?${CLOSE}\\n*`);
// Любые оставшиеся одиночные маркеры (страховка) и «голые» управляющие байты
// (\x01/\x02 — выводим из констант, чтобы не держать литеральные control-байты
// в исходнике) — чтобы в чат никогда не попало `□THINKING□` или невидимый мусор.
const LONE_MARKER_RE = new RegExp(`${OPEN}|${CLOSE}`, "g");
const CONTROL_BYTES_RE = new RegExp(`[${OPEN[0]}${OPEN[OPEN.length - 1]}]`, "g");

// Старый битый формат: есть CLOSE, но НЕТ ни одного OPEN. Только тогда срезаем
// «лид до осиротевшего CLOSE» как утёкшее мышление — без риска для нормального
// контента с валидными парами.
function isLegacyOrphan(raw: string): boolean {
  return !raw.includes(OPEN) && raw.includes(CLOSE);
}

export function stripThinking(raw: string): string {
  let out = raw.replace(THINKING_BLOCK_RE, "");
  if (isLegacyOrphan(raw)) {
    out = out.replace(ORPHAN_CLOSE_LEAD_RE, "");
  }
  return out
    .replace(TRAILING_OPEN_THINKING_RE, "")
    .replace(LONE_MARKER_RE, "")
    .replace(CONTROL_BYTES_RE, "")
    .replace(/\n{3,}/g, "\n\n")
    .replace(/^\s+|\s+$/g, "");
}

export function hasVisibleText(raw: string): boolean {
  return stripThinking(raw).trim().length > 0;
}

/**
 * Разбирает сырой контент на видимый текст и закрытые thinking-блоки.
 *
 * Нормальный случай (после фикса bridge): мышление приходит ОДНИМ атомарным
 * блоком `OPEN…CLOSE` — извлекаем его в «Размышления», в видимом остаётся
 * только ответ. Легаси/битый случай (осиротевший CLOSE без единого OPEN) —
 * текст до него считаем мышлением и тоже прячем в блок, чтобы не было утечки
 * сырых «мыслей» в старых сессиях.
 */
export function extractThinking(raw: string): {
  visible: string;
  thinking: string[];
} {
  const thinking: string[] = [];
  // 1) Нормальные закрытые пары.
  const closed = new RegExp(`${OPEN}([\\s\\S]*?)${CLOSE}`, "g");
  let m: RegExpExecArray | null;
  while ((m = closed.exec(raw)) !== null) {
    if (m[1].trim()) thinking.push(m[1].trim());
  }
  // 2) Легаси: текст перед осиротевшим CLOSE — ТОЛЬКО в чисто-битом формате
  //    (нет ни одного OPEN), иначе можно ошибочно забрать видимый текст.
  if (isLegacyOrphan(raw)) {
    const orphan = raw.match(new RegExp(`^([\\s\\S]*?)${CLOSE}`));
    if (orphan) {
      const lead = orphan[1].replace(CONTROL_BYTES_RE, "").trim();
      if (lead) thinking.push(lead);
    }
  }
  return { visible: stripThinking(raw), thinking };
}
