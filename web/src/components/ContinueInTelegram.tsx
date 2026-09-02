import { ReplyIcon } from "@/components/icons";

export function ContinueInTelegram({
  botUsername,
  sessionUuid,
  canContinue = true,
}: {
  botUsername?: string;
  sessionUuid: string;
  // Скрываем у локальных (логин/пароль) юзеров: для них deeplink-owner-check
  // не совпадёт и кнопка вела бы в тупик (флаг из /api/me).
  canContinue?: boolean;
}) {
  if (!botUsername || !canContinue) return null;
  const href = `https://t.me/${botUsername}?start=continue_${encodeURIComponent(
    sessionUuid,
  )}`;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="icon-btn flex h-9 shrink-0 items-center gap-2 rounded-xl px-2.5 text-sm text-[var(--fg-secondary)] transition-colors hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
      title="Продолжить эту сессию в Telegram"
      aria-label="Продолжить в Telegram"
    >
      <ReplyIcon size={18} />
      <span>В Telegram</span>
    </a>
  );
}
