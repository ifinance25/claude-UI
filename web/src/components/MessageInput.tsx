import { type ReactNode, useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "@/api/client";
import {
  CloseIcon,
  FileIcon,
  ImageIcon,
  PlusIcon,
  SendIcon,
  SkillsIcon,
  SlashIcon,
  StopIcon,
} from "@/components/icons";
import CommandsPopover from "@/components/CommandsPopover";
import InputModelButton from "@/components/InputModelButton";
import type { Attachment, SlashCommand } from "@/lib/types";

interface Props {
  /** UUID активной сессии — нужен для POST /api/uploads. */
  sessionUuid: string;
  onSubmit: (text: string, attachments: Attachment[]) => void;
  disabled?: boolean;
  placeholder?: string;
  /**
   * Внешний триггер для подстановки текста (AskUserQuestion → клик по
   * варианту). Меняется каждый клик — useEffect перехватит и применит
   * новое значение. Не использовать как контролируемое значение!
   */
  prefill?: { text: string; ts: number } | null;
  /** Путь проекта — для подгрузки команд/скиллов именно этого проекта. */
  projectPath?: string;
  /** Идёт ли генерация — переключает кнопку Send на Stop. */
  isGenerating?: boolean;
  /** Колбэк остановки генерации (клик по кнопке Stop). */
  onStop?: () => void;
  /** Необязательный слот в нижнем баре слева (напр. индикатор контекста). */
  leftSlot?: ReactNode;
}

const MAX_ROWS = 10;
// MIME-фильтр для file input — должен соответствовать тому, что
// принимает бэкенд (см. ALLOWED_MIME_PREFIXES / ALLOWED_TEXT_MIMES в
// routes_uploads.py).
const ACCEPT_MIME =
  "image/*,text/plain,text/markdown,application/json,.md,.txt,.py,.ts,.tsx,.js";

export default function MessageInput({
  sessionUuid,
  onSubmit,
  disabled,
  placeholder,
  prefill,
  projectPath,
  isGenerating,
  onStop,
  leftSlot,
}: Props) {
  const [text, setText] = useState("");
  const [slashes, setSlashes] = useState<SlashCommand[]>([]);
  const [focusedIdx, setFocusedIdx] = useState(0);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Меню команд/скиллов, открытое кнопкой (а не вводом "/").
  const [menu, setMenu] = useState<null | "cmd" | "skill">(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Актуальный sessionUuid — чтобы in-flight upload-цикл мог заметить
  // смену сессии и не дописать вложение в инпут другой сессии (CR3-11).
  const sessionUuidRef = useRef(sessionUuid);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await api.getSlashCommands(projectPath);
        if (!cancelled) setSlashes(list);
      } catch {
        // молча — бэк ещё не перезапущен, без меню тоже работает
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [projectPath]);

  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    const lineHeight = parseInt(window.getComputedStyle(el).lineHeight, 10) || 24;
    const maxHeight = lineHeight * MAX_ROWS;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [text]);

  // Подстановка текста извне (клик по варианту AskUserQuestion).
  // ts в prefill — счётчик, чтобы повторный клик по тому же варианту
  // тоже сработал (одинаковый text без ts не триггерит useEffect).
  useEffect(() => {
    if (!prefill || !prefill.text) return;
    // Добавляем к уже набранному тексту, а не затираем — чтобы не
    // потерять несохранённый черновик пользователя (CR3-17).
    setText((prev) => (prev.trim() ? `${prev}\n\n${prefill.text}` : prefill.text));
    taRef.current?.focus();
  }, [prefill]);

  // Сбрасываем attachments при смене сессии (sessionUuid меняется) и
  // обновляем ref для in-flight upload-цикла.
  useEffect(() => {
    sessionUuidRef.current = sessionUuid;
    setAttachments([]);
    setUploadError(null);
  }, [sessionUuid]);

  const showSlash =
    text.startsWith("/") && !text.includes("\n") && slashes.length > 0;
  const query = showSlash ? text.slice(1).toLowerCase() : "";
  const filtered = showSlash
    ? slashes.filter((s) => s.cmd.slice(1).toLowerCase().startsWith(query))
    : [];

  useEffect(() => {
    setFocusedIdx(0);
  }, [text]);

  const submit = (override?: string) => {
    const raw = override ?? text;
    const trimmed = raw.trim();
    if (!trimmed && attachments.length === 0) return;
    onSubmit(trimmed, attachments);
    setText("");
    setAttachments([]);
    setUploadError(null);
  };

  const insertSlash = (cmd: string) => {
    const needsArg = cmd === "/model" || cmd === "/verbose";
    if (needsArg) {
      setText(cmd + " ");
      taRef.current?.focus();
    } else {
      submit(cmd);
    }
  };

  const onPickFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const targetUuid = sessionUuid;
    setUploading(true);
    setUploadError(null);
    try {
      // Грузим последовательно, чтобы порядок attachments был
      // предсказуемым и чтобы не подвиснуть на ошибке на середине.
      for (const file of Array.from(files)) {
        const att = await api.uploadFile(targetUuid, file);
        // Сессию переключили во время заливки — прерываемся, чтобы не
        // добавить вложение в инпут уже другой сессии (CR3-11).
        if (sessionUuidRef.current !== targetUuid) return;
        setAttachments((arr) => [...arr, att]);
      }
    } catch (e) {
      if (sessionUuidRef.current === targetUuid) {
        setUploadError((e as Error).message);
      }
    } finally {
      setUploading(false);
      // Сбрасываем value, чтобы можно было выбрать тот же файл повторно.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const canSend =
    (text.trim().length > 0 || attachments.length > 0) && !disabled && !uploading;

  return (
    <div className="px-6 pb-8 pt-3">
      <div className="relative mx-auto w-full max-w-3xl">
        {/* Поповер команд/скиллов, открытый кнопкой. Прячем, когда юзер
            набирает "/" — тогда показывается типизированный поповер ниже,
            и два меню не накладываются. AnimatePresence даёт анимацию и
            на закрытие (exit), а не только на открытие. */}
        <AnimatePresence>
          {menu && !showSlash && (
            <CommandsPopover
              title={menu === "skill" ? "Скиллы" : "Команды"}
              items={
                menu === "skill"
                  ? slashes.filter((s) => s.kind === "skill")
                  : slashes
              }
              onClose={() => setMenu(null)}
              onPick={(cmd) => {
                setMenu(null);
                insertSlash(cmd);
              }}
            />
          )}
        </AnimatePresence>

        {/* Поповер slash-команд */}
        <AnimatePresence>
          {showSlash && filtered.length > 0 && (
            <motion.div
              initial={{ opacity: 0, y: 8, scale: 0.98 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ duration: 0.16, ease: "easeOut" }}
              className="absolute bottom-full left-0 right-0 mb-2 origin-bottom overflow-hidden rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-elevated)] shadow-lg"
            >
              <div className="px-4 py-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
                Команды
              </div>
              <div className="max-h-72 overflow-y-auto">
                {filtered.map((s, i) => (
                  <button
                    key={s.cmd}
                    onMouseEnter={() => setFocusedIdx(i)}
                    onClick={() => insertSlash(s.cmd)}
                    className={`flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm transition-colors ${
                      i === focusedIdx
                        ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                        : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                    }`}
                  >
                    <span className="font-mono text-[13px]">{s.cmd}</span>
                    <span className="flex-1 truncate text-[var(--fg-muted)]">
                      {s.label}
                    </span>
                    <span
                      className={`rounded-md px-1.5 py-0.5 text-[10px] uppercase tracking-wider ${
                        s.kind === "skill"
                          ? "bg-emerald-500/15 text-emerald-300"
                          : s.target === "bot"
                          ? "bg-violet-500/15 text-violet-300"
                          : "bg-sky-500/15 text-sky-300"
                      }`}
                    >
                      {s.kind === "skill"
                        ? "скилл"
                        : s.target === "bot"
                        ? "бот"
                        : "claude"}
                    </span>
                  </button>
                ))}
              </div>
            </motion.div>
          )}
        </AnimatePresence>

        <div className="flex flex-col gap-3 rounded-3xl bg-[var(--bg-input)] px-5 py-4 shadow-md ring-1 ring-[var(--border-subtle)] focus-within:ring-2 focus-within:ring-[var(--border-subtle)]">
          {/* Превью прикреплённых файлов */}
          {(attachments.length > 0 || uploading || uploadError) && (
            <div className="flex flex-wrap gap-2">
              {attachments.map((a, idx) => (
                <div
                  key={`${a.source_path}-${idx}`}
                  className="flex items-center gap-2 rounded-xl bg-[var(--bg-hover)] px-3 py-1.5 text-xs text-[var(--fg-primary)]"
                  title={a.source_path}
                >
                  {a.kind === "image" ? <ImageIcon size={16} /> : <FileIcon size={16} />}
                  <span className="max-w-[200px] truncate">{a.file_name}</span>
                  <button
                    type="button"
                    onClick={() =>
                      setAttachments((arr) => arr.filter((_, i) => i !== idx))
                    }
                    className="rounded-full p-0.5 text-[var(--fg-muted)] hover:bg-[var(--bg-canvas)] hover:text-[var(--fg-primary)]"
                    title="Убрать"
                  >
                    <CloseIcon size={12} />
                  </button>
                </div>
              ))}
              {uploading && (
                <div className="flex items-center gap-2 rounded-xl bg-[var(--bg-hover)] px-3 py-1.5 text-xs text-[var(--fg-muted)]">
                  загружаем…
                </div>
              )}
              {uploadError && (
                <div className="flex items-center gap-2 rounded-xl bg-red-900/30 px-3 py-1.5 text-xs text-red-300">
                  {uploadError}
                  <button
                    type="button"
                    onClick={() => setUploadError(null)}
                    className="rounded-full p-0.5 hover:text-red-100"
                  >
                    <CloseIcon size={12} />
                  </button>
                </div>
              )}
            </div>
          )}

          <textarea
            ref={taRef}
            rows={1}
            placeholder={placeholder ?? "Чем я могу помочь?"}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (showSlash && filtered.length > 0) {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  setFocusedIdx((i) => (i + 1) % filtered.length);
                  return;
                }
                if (e.key === "ArrowUp") {
                  e.preventDefault();
                  setFocusedIdx(
                    (i) => (i - 1 + filtered.length) % filtered.length
                  );
                  return;
                }
                if (e.key === "Tab") {
                  e.preventDefault();
                  setText(filtered[focusedIdx].cmd + " ");
                  return;
                }
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  insertSlash(filtered[focusedIdx].cmd);
                  return;
                }
                if (e.key === "Escape") {
                  e.preventDefault();
                  setText("");
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            disabled={disabled}
            className="w-full resize-none bg-transparent text-base leading-relaxed text-[var(--fg-primary)] placeholder:text-[var(--fg-muted)] focus:outline-none disabled:opacity-50"
            style={{ minHeight: "26px" }}
          />

          {/* Нижний бар */}
          <div className="flex items-center justify-between gap-3">
            <div className="flex items-center gap-2">
              {leftSlot}
              <InputModelButton />
              {/* Скрытый файл-инпут — клик идёт через PlusIcon кнопку */}
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept={ACCEPT_MIME}
                className="hidden"
                onChange={(e) => void onPickFiles(e.target.files)}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
                className="icon-btn rounded-full p-2 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)] disabled:opacity-50"
                title="Прикрепить файл — изображение или текст"
              >
                <PlusIcon size={20} />
              </button>
              <button
                type="button"
                onClick={() => setMenu((m) => (m === "cmd" ? null : "cmd"))}
                className="icon-btn rounded-full p-2 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                title="Команды"
              >
                <SlashIcon size={20} />
              </button>
              <button
                type="button"
                onClick={() => setMenu((m) => (m === "skill" ? null : "skill"))}
                className="icon-btn rounded-full p-2 text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
                title="Скиллы"
              >
                <SkillsIcon size={20} />
              </button>
            </div>
            {isGenerating ? (
              <button
                type="button"
                onClick={() => onStop?.()}
                className="icon-btn rounded-full bg-[var(--accent)] p-2.5 text-[var(--bg-canvas)] transition-colors hover:opacity-90"
                title="Остановить генерацию"
              >
                <StopIcon size={20} />
              </button>
            ) : (
              <button
                type="button"
                onClick={() => submit()}
                disabled={!canSend}
                className={`icon-btn rounded-full p-2.5 transition-colors ${
                  canSend
                    ? "bg-[var(--accent)] text-[var(--bg-canvas)] hover:opacity-90"
                    : "bg-[var(--bg-hover)] text-[var(--fg-muted)] cursor-not-allowed"
                }`}
                title="Отправить (Enter)"
              >
                <SendIcon size={20} />
              </button>
            )}
          </div>
        </div>
        <div className="mt-3 text-center text-xs text-[var(--fg-muted)]">
          Enter — отправить · Shift+Enter — перенос строки · «/» — команды
        </div>
      </div>
    </div>
  );
}
