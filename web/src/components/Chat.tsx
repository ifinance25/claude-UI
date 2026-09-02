import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { api } from "@/api/client";
import { openSessionWs, type WsHandle } from "@/api/ws";
import ChatEmptyState from "@/components/ChatEmptyState";
import { AskUserQuestionEvent } from "@/components/events/AskUserQuestionEvent";
import { ErrorEvent } from "@/components/events/ErrorEvent";
import { FileArtifactEvent } from "@/components/events/FileArtifactEvent";
import { LogEvent } from "@/components/events/LogEvent";
import { SubagentEvent } from "@/components/events/SubagentEvent";
import { TextEvent } from "@/components/events/TextEvent";
import { ToolUseEvent } from "@/components/events/ToolUseEvent";
import { UsageEvent } from "@/components/events/UsageEvent";
import { UserMessageEvent } from "@/components/events/UserMessageEvent";
import { BookIcon, FolderIcon } from "@/components/icons";
import DocsPanel from "@/components/DocsPanel";
import { ContinueInTelegram } from "@/components/ContinueInTelegram";
import { LiveActivityPanel } from "@/components/LiveActivityPanel";
import { activityFromEvent, type LiveActivity } from "@/lib/liveActivity";
import { useAuth } from "@/auth/AuthContext";
import MessageInput from "@/components/MessageInput";
import ThinkingIndicator from "@/components/ThinkingIndicator";
import { deriveTitle, sessionTitle } from "@/lib/sessionTitle";
import { usageBreakdown } from "@/lib/usage";
import { mergeTargetIndex } from "@/lib/bubbleMerge";
import { pendingRequestId } from "@/lib/pendingTurn";
import { useModelInfo } from "@/lib/useModelInfo";
import ContextGauge from "@/components/ContextGauge";
import { contextWindowFor } from "@/lib/modelContext";
import { CTX_RED_PCT } from "@/lib/contextGauge";
import type { Attachment, HistoryMessage, Session, WsMessage } from "@/lib/types";

interface Bubble {
  id: string;
  kind: string;
  content: string;
  metadata?: Record<string, unknown>;
  requestId?: string | null;
  source?: "telegram" | "web" | "webhook" | "cron";
}

interface Props {
  session: Session;
  onNewChat?: () => void;
}

let bubbleCounter = 0;
const nextId = (prefix: string) => `${prefix}-${++bubbleCounter}`;
// Rebuilds bubbles from persisted history.
//
// `usageShown` (optional) is the live dedup Set: usage rows whose
// request_id is already in it are skipped, and new ones are recorded —
// so a REST replay after reconnect can't re-add a usage plate that was
// already shown live (CR3-12).
//
// Consecutive `streaming_update` text rows for the same request_id are
// merged into one bubble: the backend may persist a single assistant turn
// as several rows (periodic flush), and history must reconstruct it as one
// continuous text bubble, like the live path does (CR3-6).
function historyToBubbles(
  history: HistoryMessage[],
  usageShown?: Set<string>,
): Bubble[] {
  const bubbles: Bubble[] = [];
  const pushUsage = (
    requestId: string | null,
    metadata: Record<string, unknown> | undefined,
  ) => {
    if (requestId && usageShown) {
      if (usageShown.has(requestId)) return;
      usageShown.add(requestId);
    }
    bubbles.push({
      id: nextId("usage"),
      kind: "usage",
      content: "",
      metadata,
      requestId,
    });
  };

  for (const m of history) {
    if (m.type === "user_message") {
      const src = (m.metadata as Record<string, unknown> | null)?.source as
        | Bubble["source"]
        | undefined;
      bubbles.push({
        id: nextId("u"),
        kind: "user_message",
        content: m.content,
        requestId: m.request_id,
        source: src,
      });
    } else if (m.type === "streaming_update") {
      if ((m.kind ?? "log") === "text") {
        const idx = mergeTargetIndex(bubbles, m.request_id);
        if (idx >= 0) {
          bubbles[idx] = {
            ...bubbles[idx],
            content: bubbles[idx].content + m.content,
          };
          continue;
        }
        bubbles.push({
          id: nextId("text"),
          kind: "text",
          content: m.content,
          requestId: m.request_id,
        });
        continue;
      }
      if (m.kind === "usage") {
        pushUsage(m.request_id, m.metadata ?? undefined);
        continue;
      }
      bubbles.push({
        id: nextId(m.kind ?? "evt"),
        kind: m.kind ?? "log",
        content: m.content,
        metadata: m.metadata ?? undefined,
        requestId: m.request_id,
      });
    } else if (m.type === "finished") {
      const meta = m.metadata ?? {};
      if (meta.error) {
        bubbles.push({
          id: nextId("err"),
          kind: "error",
          content: String(meta.error),
          requestId: m.request_id,
        });
      }
      if (meta.usage) {
        pushUsage(m.request_id, meta.usage as Record<string, unknown>);
      }
      // Время ответа сохранено в метадате finished — патчим им usage-бабл этого
      // хода (как делает живой путь на finished), иначе при перезагрузке истории
      // «время» в подвале сообщения пропадало.
      const elapsed = (meta as Record<string, unknown>).elapsed_ms;
      if (typeof elapsed === "number" && m.request_id) {
        for (let i = bubbles.length - 1; i >= 0; i--) {
          const b = bubbles[i];
          if (b.kind === "usage" && b.requestId === m.request_id) {
            b.metadata = { ...(b.metadata || {}), __elapsed_ms: elapsed };
            break;
          }
        }
      }
    }
  }
  return bubbles;
}

const NOTES_DEBOUNCE_MS = 500;

export default function Chat({ session, onNewChat }: Props) {
  const { user } = useAuth();
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [wsConnected, setWsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState(session.notes ?? "");
  // Заголовок для хедера/сайдбара. Обычно совпадает с notes, но при
  // авто-титуле (из первого сообщения) обновляется сразу, не дожидаясь
  // 4-секундного reload сайдбара. Держим отдельно, чтобы текст хедера
  // не зависел от того, открыт ли drawer заметок.
  const [displayNotes, setDisplayNotes] = useState(session.notes ?? "");
  const [docsOpen, setDocsOpen] = useState(false);
  // Уровень детализации из настроек. Кнопки-тоггла в шапке нет (шапку чистим),
  // но уровень по-прежнему решает, показывать ли размышления внутри ответа.
  const [verbose, setVerbose] = useState<number>(1);
  const { info } = useModelInfo(true);
  const [ctxTokens, setCtxTokens] = useState(0);
  const [ctxHintTrigger, setCtxHintTrigger] = useState(0);
  // Chat ремоунтится на смену сессии (key=session_uuid в ChatPage), поэтому
  // ctxTokens/ctxHintTrigger/ctxHighRef сбрасываются автоматически.
  const ctxHighRef = useRef(false);

  // Считает заполнение окна из usage-меты последнего хода и, при первом
  // пересечении красного порога за сессию, инкрементит триггер авто-подсказки.
  const trackContext = (meta: Record<string, unknown> | null | undefined) => {
    const b = usageBreakdown(meta);
    const tokens = b.input + b.cacheRead + b.cacheCreation;
    if (tokens <= 0) return;
    setCtxTokens(tokens);
    // info?.current здесь читается из замыкания WS-обработчика (захвачено на
    // маунте сессии). Сейчас безвредно: все окна = 1M (modelContext.ts,
    // решение 2026-07-02), так что порог считается одинаково независимо от
    // модели. Если появится модель с другим окном — прокинуть окно через ref.
    const win = contextWindowFor(info?.current);
    const pct = win > 0 ? Math.min(100, Math.round((tokens / win) * 100)) : 0;
    if (pct >= CTX_RED_PCT) {
      if (!ctxHighRef.current) {
        ctxHighRef.current = true;
        setCtxHintTrigger((n) => n + 1);
      }
    } else {
      ctxHighRef.current = false;
    }
  };
  // Активный request_id Claude — выставляется на agent_started, гасится
  // первым tool_use/text/finished. Используется и для индикатора
  // "Claude думает…", и для дедупликации usage (см. addLiveEvent).
  const [thinkingRequestId, setThinkingRequestId] = useState<string | null>(null);
  // Живой таймер генерации: тикает, пока есть активный thinkingRequestId.
  // Сбрасывается в 0 на finished/stopped (thinkingRequestId → null).
  const [genElapsedMs, setGenElapsedMs] = useState(0);
  const genStartRef = useRef<number | null>(null);
  // Живой поток «мыслей» (thinking-дельты активного запроса) — копится в буфер,
  // НЕ становится баблом ленты; показывается в раскрываемой LiveActivityPanel.
  const [liveThinking, setLiveThinking] = useState("");
  // Текущая активность Claude (фаза + контекст) для заголовка индикатора —
  // выводится из последнего стрим-события (activityFromEvent).
  const [activity, setActivity] = useState<LiveActivity>({ phase: "thinking", context: "" });
  // Открыта ли панель мыслей. Липко в localStorage, по умолчанию закрыта.
  const [panelOpen, setPanelOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem("vels.thinkingPanelOpen") === "1";
    } catch {
      return false;
    }
  });
  const togglePanel = () => {
    setPanelOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem("vels.thinkingPanelOpen", next ? "1" : "0");
      } catch {
        // приватный режим — не критично
      }
      return next;
    });
  };
  // request_id'ы, для которых мы уже отрисовали usage bubble — чтобы
  // не дублировать его, если бэк прислал одинаковый и в
  // `streaming_update kind=usage`, и в `finished`.
  const usageShownRef = useRef<Set<string>>(new Set());
  // request_id'ы ходов, которые УЖЕ полностью показаны живьём (пришёл finished).
  // На реконнекте/gap REST-добор отдаёт всё с last_event_id, а живые события
  // event_id клиенту не несут (текст ещё и батчится в БД) → lastEventIdRef не
  // двигался и добор пере-тягивал уже показанные ходы = дубль ленты. Пропускаем
  // в доборе сообщения завершённых ходов (частый случай: блип между ходами).
  const seenFinishedRef = useRef<Set<string>>(new Set());
  // Текст-подсказка для MessageInput (клик по варианту AskUserQuestion).
  // ts инкрементируется при каждом клике, чтобы повторный выбор того же
  // варианта тоже триггерил useEffect внутри MessageInput.
  const [prefill, setPrefill] = useState<{ text: string; ts: number } | null>(null);
  const wsRef = useRef<WsHandle | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const lastEventIdRef = useRef(0);
  const hasOpenedRef = useRef(false);
  const notesTimerRef = useRef<number | null>(null);
  const notesSavedRef = useRef(session.notes ?? "");
  // Watchdog для ThinkingIndicator: если за 90 с не пришло НИ ОДНОГО
  // стрим-события и нет finished — сбрасываем индикатор, чтобы он не
  // крутился вечно при потерянном finished. Пере-армится на каждом
  // streaming_update (см. addLiveEvent), поэтому живой поток мыслей/действий
  // (даже длинный, без видимого текста) его не рвёт.
  const thinkingTimerRef = useRef<number | null>(null);
  const THINKING_WATCHDOG_MS = 90_000;

  // (Пере)запускает watchdog индикатора. Армится и на agent_started, и
  // сразу в sendMessage на 'pending' — иначе при потерянном agent_started
  // (дроп ws-кадра, сокет не OPEN, краш бэка) индикатор «Claude думает…»
  // висел бы вечно (CR3-8).
  const armThinkingWatchdog = () => {
    if (thinkingTimerRef.current !== null) {
      window.clearTimeout(thinkingTimerRef.current);
    }
    thinkingTimerRef.current = window.setTimeout(() => {
      setThinkingRequestId(null);
      thinkingTimerRef.current = null;
    }, THINKING_WATCHDOG_MS);
  };

  // Тикающий таймер генерации. Старт фиксируем по первому ненулевому
  // thinkingRequestId, обновляем раз в 500мс, сбрасываем на null.
  useEffect(() => {
    if (thinkingRequestId === null) {
      genStartRef.current = null;
      setGenElapsedMs(0);
      return;
    }
    if (genStartRef.current === null) genStartRef.current = Date.now();
    setGenElapsedMs(Date.now() - genStartRef.current);
    const id = window.setInterval(() => {
      if (genStartRef.current !== null) setGenElapsedMs(Date.now() - genStartRef.current);
    }, 500);
    return () => window.clearInterval(id);
  }, [thinkingRequestId]);

  // Чистим живое состояние мыслей/активности, когда генерация завершилась
  // (thinkingRequestId → null): панель не должна держать «прошлые» мысли.
  useEffect(() => {
    if (thinkingRequestId === null) {
      setLiveThinking("");
      setActivity({ phase: "thinking", context: "" });
    }
  }, [thinkingRequestId]);

  useEffect(() => {
    let cancelled = false;
    setBubbles([]);
    setError(null);
    setThinkingRequestId(null);
    if (thinkingTimerRef.current !== null) {
      window.clearTimeout(thinkingTimerRef.current);
      thinkingTimerRef.current = null;
    }
    usageShownRef.current = new Set();
    seenFinishedRef.current = new Set();
    lastEventIdRef.current = 0;
    hasOpenedRef.current = false;

    (async () => {
      try {
        const history = await api.getMessages(session.session_uuid, 0);
        if (cancelled) return;
        setBubbles(historyToBubbles(history, usageShownRef.current));
        if (history.length) {
          lastEventIdRef.current = history[history.length - 1].event_id;
        }
        // Ход мог остаться незавершённым: пользователь ушёл в другой диалог,
        // пока Claude отвечал. Генерация при этом продолжается, но пока идут
        // размышления, в историю писать нечего — и чат выглядел пустым,
        // будто его сбросили. Возвращаем индикатор «Claude думает…» и
        // сторожевой таймер (иначе он висел бы вечно, если ход всё-таки умер).
        const pending = pendingRequestId(history);
        if (pending) {
          setThinkingRequestId(pending);
          armThinkingWatchdog();
        }
      } catch (e) {
        if (!cancelled) setError(`Не удалось загрузить историю: ${(e as Error).message}`);
      }
    })();

    const handle = openSessionWs(
      session.session_uuid,
      (msg) => addLiveEvent(msg),
      {
        onOpen: async () => {
          setWsConnected(true);
          if (!hasOpenedRef.current) {
            hasOpenedRef.current = true;
            return;
          }
          // Реконнект: если индикатор завис на 'pending' (исходная отправка не
          // дошла, agent_started не пришёл) — снимаем его, чтобы не висел
          // вечно «Клод думает…» (находка аудита #2).
          setThinkingRequestId((cur) => (cur === "pending" ? null : cur));
          try {
            const missed = await api.getMessages(
              session.session_uuid,
              lastEventIdRef.current
            );
            if (cancelled || missed.length === 0) return;
            // Пропускаем сообщения ходов, уже показанных живьём целиком
            // (анти-дубль). lastEventIdRef двигаем по ПОЛНОМУ missed, чтобы
            // повторно их не запрашивать.
            const fresh = missed.filter(
              (m) => !(m.request_id && seenFinishedRef.current.has(m.request_id)),
            );
            if (fresh.length) {
              setBubbles((current) => [
                ...current,
                ...historyToBubbles(fresh, usageShownRef.current),
              ]);
            }
            lastEventIdRef.current = missed[missed.length - 1].event_id;
          } catch (e) {
            if (!cancelled) {
              setError(`Не удалось восстановить состояние после переподключения: ${(e as Error).message}`);
            }
          }
        },
        onClose: () => setWsConnected(false),
      }
    );
    wsRef.current = handle;

    return () => {
      cancelled = true;
      handle.close();
      wsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session.session_uuid]);

  // Уровень детализации живёт на сервере (настройки → «Логи») и общий с
  // Telegram-командой /verbose. Читаем один раз на монтировании.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.getSettings();
        if (!cancelled) setVerbose(s.verbose_level);
      } catch {
        // молча — остаётся дефолт 1
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !stickToBottomRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [bubbles]);

  // Держим низ при ЛЮБОМ росте высоты контента, а не только при приходе чанка
  // ([bubbles]). Typewriter растит высоту бабла между чанками (и после
  // finished, пока допечатывает) — без этого индикатор «Клод думает» уезжал
  // вниз на экране. ResizeObserver ловит рост и подкручивает скролл, если
  // пользователь у низа (stickToBottom). Callback-ref переподключает наблюдатель
  // при смене контейнера (пустой ↔ с баблами).
  const observerRef = useRef<ResizeObserver | null>(null);
  const setBubblesContentRef = useCallback((node: HTMLDivElement | null) => {
    observerRef.current?.disconnect();
    observerRef.current = null;
    if (node && typeof ResizeObserver !== "undefined") {
      const ro = new ResizeObserver(() => {
        const el = scrollRef.current;
        if (el && stickToBottomRef.current) el.scrollTop = el.scrollHeight;
      });
      ro.observe(node);
      observerRef.current = ro;
    }
  }, []);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stickToBottomRef.current = nearBottom;
  };

  const addLiveEvent = (msg: WsMessage) => {
    if (msg.type === "user_message") {
      setBubbles((b) => {
        if (b.some((x) => x.kind === "user_message" && x.requestId === msg.request_id)) {
          return b;
        }
        return [
          ...b,
          {
            id: nextId("u"),
            kind: "user_message",
            content: msg.content,
            requestId: msg.request_id,
            source: msg.source,
          },
        ];
      });
      return;
    }
    if (msg.type === "agent_started") {
      // Запоминаем activeRequest — индикатор «Claude думает…» висит,
      // пока не прилетит первый видимый текст или finished.
      setThinkingRequestId(msg.request_id);
      // Сбрасываем предыдущий watchdog и запускаем новый: страховка от
      // ответов, которые состоят только из thinking-блоков и не имеют
      // явного finished (потерянный ws-пакет, краш бэка).
      armThinkingWatchdog();
      return;
    }
    if (msg.type === "streaming_update") {
      // Пере-армим watchdog на КАЖДОМ стрим-событии: длинный ход из одних
      // только мыслей/инструментов (без видимого текста и finished) иначе
      // был бы убит таймером на 90с прямо посреди работы Claude.
      armThinkingWatchdog();
      // Обновляем «заголовок» активности из события (глагол + контекст).
      const act = activityFromEvent(msg.kind, msg.content, msg.metadata || {});
      if (act) setActivity(act);
      // Мысли копим в живой буфер (панель), НЕ в ленту баблов.
      if (msg.kind === "thinking") {
        setLiveThinking((prev) => prev + msg.content);
        return;
      }
      // Не сбрасываем thinkingRequestId на text — индикатор гаснет
      // автоматически в render-цикле, когда в bubbles появляется
      // text-bubble с ВИДИМЫМ (после strip-thinking) контентом для
      // того же request_id. Это убирает паузу между «индикатор
      // пропал» и «появился bubble».
      if (msg.kind === "text") {
        setBubbles((b) => {
          const idx = mergeTargetIndex(b, msg.request_id);
          if (idx >= 0) {
            const merged = { ...b[idx], content: b[idx].content + msg.content };
            return [...b.slice(0, idx), merged, ...b.slice(idx + 1)];
          }
          return [
            ...b,
            {
              id: nextId("text"),
              kind: "text",
              content: msg.content,
              requestId: msg.request_id,
            },
          ];
        });
        return;
      }
      if (msg.kind === "usage") {
        // usage прилетает и из streaming_update, и из finished.
        // Показываем один раз — по request_id (см. usageShownRef).
        if (msg.request_id && usageShownRef.current.has(msg.request_id)) return;
        if (msg.request_id) usageShownRef.current.add(msg.request_id);
        setBubbles((b) => [
          ...b,
          {
            id: nextId("usage"),
            kind: "usage",
            content: "",
            metadata: msg.metadata,
            requestId: msg.request_id,
          },
        ]);
        trackContext(msg.metadata);
        return;
      }
      setBubbles((b) => [
        ...b,
        {
          id: nextId(msg.kind),
          kind: msg.kind,
          content: msg.content,
          metadata: msg.metadata,
          requestId: msg.request_id,
        },
      ]);
      return;
    }
    if (msg.type === "finished") {
      setThinkingRequestId(null);
      // Ход полностью показан живьём — пометим, чтобы REST-добор на
      // реконнекте/gap не пере-добавил его (анти-дубль ленты).
      if (msg.request_id) seenFinishedRef.current.add(msg.request_id);
      if (typeof msg.elapsed_ms === "number") {
        // Прокидываем время ответа в подвал сообщения: usage-бабл этого хода
        // обычно уже создан (из streaming_update kind=usage) — патчим его
        // метадату ключом __elapsed_ms, который читает UsageEvent.
        const ms = msg.elapsed_ms;
        setBubbles((b) =>
          b.map((x) =>
            x.kind === "usage" && x.requestId === msg.request_id
              ? { ...x, metadata: { ...(x.metadata || {}), __elapsed_ms: ms } }
              : x,
          ),
        );
      }
      if (thinkingTimerRef.current !== null) {
        window.clearTimeout(thinkingTimerRef.current);
        thinkingTimerRef.current = null;
      }
      if (msg.error) {
        setBubbles((b) => [
          ...b,
          { id: nextId("err"), kind: "error", content: msg.error! },
        ]);
      }
      if (msg.usage) {
        // Дедуп с usage из streaming_update, чтобы не было двух плашек.
        if (msg.request_id && usageShownRef.current.has(msg.request_id)) return;
        if (msg.request_id) usageShownRef.current.add(msg.request_id);
        const usageMeta: Record<string, unknown> = { ...msg.usage! };
        if (typeof msg.elapsed_ms === "number") usageMeta.__elapsed_ms = msg.elapsed_ms;
        setBubbles((b) => [
          ...b,
          { id: nextId("usage"), kind: "usage", content: "", metadata: usageMeta },
        ]);
        trackContext(usageMeta);
      }
      return;
    }
    if (msg.type === "gap") {
      // Сервер отбросил часть событий стрима (очередь клиента
      // переполнилась) и прислал маркер gap. Сокет остаётся открытым,
      // поэтому onOpen-recovery не сработает сам — догружаем пропущенное
      // через REST ?since= прямо здесь (CR3-4).
      void (async () => {
        try {
          const missed = await api.getMessages(
            session.session_uuid,
            lastEventIdRef.current,
          );
          if (missed.length === 0) return;
          // Анти-дубль: не пере-добавляем уже показанные целиком ходы.
          const fresh = missed.filter(
            (m) => !(m.request_id && seenFinishedRef.current.has(m.request_id)),
          );
          if (fresh.length) {
            setBubbles((current) => [
              ...current,
              ...historyToBubbles(fresh, usageShownRef.current),
            ]);
          }
          lastEventIdRef.current = missed[missed.length - 1].event_id;
        } catch (e) {
          setError(
            `Не удалось восстановить пропущенные события: ${(e as Error).message}`,
          );
        }
      })();
      return;
    }
    if (msg.type === "error") {
      setBubbles((b) => [
        ...b,
        { id: nextId("err"), kind: "error", content: msg.error },
      ]);
    }
  };

  useEffect(() => {
    setNotes(session.notes ?? "");
    setDisplayNotes(session.notes ?? "");
    notesSavedRef.current = session.notes ?? "";
  }, [session.session_uuid, session.notes]);

  useEffect(() => {
    if (notesTimerRef.current !== null) {
      window.clearTimeout(notesTimerRef.current);
    }
    if (notes === notesSavedRef.current) return;
    notesTimerRef.current = window.setTimeout(async () => {
      try {
        await api.patchSessionNotes(session.session_uuid, notes);
        notesSavedRef.current = notes;
        setDisplayNotes(notes);
      } catch (e) {
        setError(`Не удалось сохранить заголовок чата: ${(e as Error).message}`);
      }
    }, NOTES_DEBOUNCE_MS);
    return () => {
      if (notesTimerRef.current !== null) {
        window.clearTimeout(notesTimerRef.current);
      }
    };
  }, [notes, session.session_uuid]);

  const sendMessage = (text: string, attachments: Attachment[] = []) => {
    stickToBottomRef.current = true;
    // Авто-заголовок чата из первого сообщения, чтобы в истории сайдбара
    // было понятно, о чём шёл разговор. Делаем только когда заголовка
    // ещё нет (ни в проп-session, ни в локально сохранённых notes),
    // текст непустой и это не slash-команда. Существующий заголовок не
    // перезаписываем.
    const trimmed = text.trim();
    if (
      trimmed &&
      !trimmed.startsWith("/") &&
      !session.notes?.trim() &&
      !notesSavedRef.current.trim()
    ) {
      const title = deriveTitle(trimmed);
      if (title) {
        // Оптимистично обновляем локальный state (хедер + drawer), затем
        // персистим. На ошибке молча откатываемся к таймстемпу — заголовок
        // не критичен, лишний тост не нужен.
        notesSavedRef.current = title;
        setNotes(title);
        setDisplayNotes(title);
        void api.patchSessionNotes(session.session_uuid, title).catch(() => {
          notesSavedRef.current = "";
        });
      }
    }
    // Включаем индикатор "Claude думает…" сразу при отправке, чтобы
    // не было паузы тишины до agent_started от бэка. Он гаснет на
    // первом text/tool_use/finished.
    setThinkingRequestId("pending");
    // Сбрасываем живое состояние прошлого хода — новый ход начинается с
    // чистой панели мыслей и фазы «думает».
    setLiveThinking("");
    setActivity({ phase: "thinking", context: "" });
    // Армим watchdog сразу — если agent_started не дойдёт, индикатор не
    // зависнет навсегда на 'pending' (CR3-8).
    armThinkingWatchdog();
    wsRef.current?.send({
      type: "user_message",
      text,
      attachments: attachments.map((a) => ({
        source_path: a.source_path,
        file_name: a.file_name,
        mime_type: a.mime_type,
        kind: a.kind,
      })),
    });
  };

  // Остановить текущую генерацию: бэк отменяет отслеживаемую задачу и
  // присылает finished (request_id игнорируется бэком — он использует свой
  // собственный отслеживаемый id, см. backend stop_generation).
  const stopGeneration = () => {
    wsRef.current?.send({ type: "stop_generation", request_id: thinkingRequestId });
  };

  const isEmpty = bubbles.length === 0;

  // Живой текст-бабл — последний text-бабл активного request'а, пока идёт
  // генерация. Только он печатается «машинкой» (typewriter); история и
  // завершённые баблы рендерятся целиком. Считаем один раз до .map.
  const liveTextId = useMemo(
    () =>
      thinkingRequestId !== null
        ? [...bubbles]
            .reverse()
            .find((x) => x.kind === "text" && x.requestId === thinkingRequestId)
            ?.id
        : undefined,
    [thinkingRequestId, bubbles],
  );

  // Индикатор виден ВСЮ генерацию — пока есть активный request. Фаза
  // («думает»/«работает инструментом»/«печатает») берётся из activity, которую
  // обновляет activityFromEvent на каждом стрим-событии (босс: индикатор и
  // таймер не должны пропадать, когда Claude начинает отвечать).
  const isGenerating = thinkingRequestId !== null;
  const showThinking = isGenerating;

  // На новом (пустом) диалоге поле ввода поднимается к центру под
  // приветствие — как в ChatGPT; с первым же сообщением (showThinking
  // или появились bubbles) опускается вниз. Единый инстанс MessageInput
  // переиспользуется в обеих раскладках.
  const centeredComposer = isEmpty && !showThinking;
  const ctxWindow = contextWindowFor(info?.current);
  const ctxPct = ctxWindow > 0 ? Math.min(100, Math.round((ctxTokens / ctxWindow) * 100)) : 0;
  const messageInput = (
    <MessageInput
      sessionUuid={session.session_uuid}
      onSubmit={sendMessage}
      disabled={!wsConnected}
      prefill={prefill}
      projectPath={session.project_path}
      isGenerating={isGenerating}
      onStop={stopGeneration}
      leftSlot={
        <ContextGauge
          pct={ctxPct}
          contextTokens={ctxTokens}
          window={ctxWindow}
          onNewChat={() => onNewChat?.()}
          onCompact={() => sendMessage("/compact")}
          autoOpenTrigger={ctxHintTrigger}
        />
      }
    />
  );

  // Единый блок «живого статуса»: (опц.) раскрытая панель мыслей + индикатор
  // активности с тогглом. Обёртка pointer-events-auto, т.к. наружный sticky
  // контейнер гасит указатель (pointer-events-none).
  const THINKING_PANEL_ID = "live-thinking-panel";
  const liveStatus = (
    <div className="pointer-events-auto mx-auto max-w-3xl px-6">
      {panelOpen && <LiveActivityPanel id={THINKING_PANEL_ID} thinking={liveThinking} />}
      <ThinkingIndicator
        activity={activity}
        elapsedMs={genElapsedMs}
        open={panelOpen}
        onToggle={togglePanel}
        panelId={THINKING_PANEL_ID}
      />
    </div>
  );

  // Рендер одного бабла по его kind. Ключ (key) теперь живёт на
  // оборачивающем <div className="animate-fadeInUp"> в .map, поэтому
  // здесь key-проп не нужен — иначе React ругался бы на дубль.
  const renderBubble = (b: Bubble) => {
    switch (b.kind) {
      case "user_message":
        return <UserMessageEvent content={b.content} source={b.source} />;
      case "text":
        return (
          <TextEvent
            content={b.content}
            // Блок размышлений виден со 2-го уровня «Логов» (свёрнут,
            // разворачивается кликом). Тоггла в шапке нет — уровень задаётся
            // в настройках и общий с Telegram-командой /verbose.
            reasoningOn={verbose >= 2}
            live={b.id === liveTextId}
          />
        );
      case "tool_use": {
        const meta = b.metadata || {};
        const toolName = String(meta.name || "");
        // Skill — служебный вызов плагина (например,
        // superpowers:brainstorming). Сам skill продолжает
        // работать, но в чате его не показываем — это
        // внутренняя кухня Claude, юзеру неинтересно.
        if (toolName === "Skill") return null;
        const isAsk =
          toolName === "AskUserQuestion" || toolName === "ask_user_question";
        const questions = isAsk
          ? (meta.questions as unknown[] | undefined)
          : undefined;
        if (isAsk && Array.isArray(questions) && questions.length > 0) {
          return (
            <AskUserQuestionEvent
              questions={questions as never}
              onPick={(text) => setPrefill({ text, ts: Date.now() })}
            />
          );
        }
        const filePath = meta.file_path as string | undefined;
        if (
          (toolName === "Write" ||
            toolName === "Edit" ||
            toolName === "MultiEdit") &&
          filePath
        ) {
          return (
            <div>
              <ToolUseEvent name={b.content} metadata={meta} />
              <FileArtifactEvent
                sessionUuid={session.session_uuid}
                filePath={filePath}
                action={
                  toolName === "Edit" || toolName === "MultiEdit"
                    ? "Edit"
                    : "Write"
                }
              />
            </div>
          );
        }
        // Bash, создавший файлы/папки (cp, tee, > …) — карточки со скачиванием
        // по каждому выходу, чтобы результат «сделай копию» был сразу под рукой.
        const bashOutputs = meta.bash_outputs as string[] | undefined;
        if (toolName === "Bash" && Array.isArray(bashOutputs) && bashOutputs.length) {
          return (
            <div>
              <ToolUseEvent name={b.content} metadata={meta} />
              {bashOutputs.map((p) => (
                <FileArtifactEvent
                  key={p}
                  sessionUuid={session.session_uuid}
                  filePath={p}
                  action="Write"
                />
              ))}
            </div>
          );
        }
        return <ToolUseEvent name={b.content} metadata={meta} />;
      }
      case "log":
      case "init":
        return <LogEvent content={b.content} />;
      case "subagent_start":
      case "subagent_log":
      case "subagent_finish":
        return <SubagentEvent kind={b.kind} content={b.content} />;
      case "usage":
        return <UsageEvent usage={b.metadata || {}} modelId={info?.current} />;
      case "error":
        return <ErrorEvent error={b.content} />;
      default:
        return null;
    }
  };

  return (
    <div className="flex min-w-0 flex-1">
      <main className="flex min-w-0 flex-1 flex-col bg-[var(--bg-canvas)]">
      {/* Хедер на всю ширину main-колонки (не max-w-4xl) — так подписи кнопок
          помещаются. Ряд действий переносится на 2-ю строку на узком окне
          (flex-wrap), НИКОГДА не обрезается. */}
      <header className="border-b border-[var(--border-subtle)]">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2.5">
          {/* Бредкрамб проекта → чат. Имя проекта НЕ сжимается (shrink-0):
              обрезается только заголовок чата. Разделитель «/» вложен в
              обрезаемую часть, чтобы не болтался пустой слэш при усечении. */}
          <div className="flex min-w-0 flex-1 items-center gap-2 text-[var(--fg-secondary)]">
            <FolderIcon size={18} />
            <span className="shrink-0 text-base font-semibold text-[var(--fg-primary)]">
              {session.project_name || "Сессия"}
            </span>
            <span className="truncate text-sm text-[var(--fg-secondary)]">
              <span className="text-[var(--fg-muted)]">/ </span>
              {sessionTitle({ notes: displayNotes, created_at: session.created_at })}
            </span>
            <span
              className={`ml-1 block h-2 w-2 shrink-0 self-center rounded-full ${
                wsConnected ? "bg-emerald-500" : "bg-amber-500"
              }`}
              title={wsConnected ? "Подключено" : "Переподключение…"}
            />
          </div>
          {/* Правый кластер действий с подписями. flex-wrap: на узком окне
              переносится на новую строку целиком (бредкрамб остаётся сверху),
              ничего не обрезается. Активная кнопка/тогл подсвечены токеном
              выбора --bg-hover. */}
          <div className="flex flex-wrap items-center justify-end gap-1.5">
            <button
              onClick={() => setDocsOpen((v) => !v)}
              className={`icon-btn flex h-9 shrink-0 items-center gap-2 rounded-xl px-2.5 text-sm transition-colors ${
                docsOpen
                  ? "bg-[var(--bg-hover)] text-[var(--fg-primary)]"
                  : "text-[var(--fg-secondary)] hover:bg-[var(--bg-hover)] hover:text-[var(--fg-primary)]"
              }`}
              title="Документация проекта"
              aria-label="Документация"
              aria-pressed={docsOpen}
            >
              <BookIcon size={18} />
              <span>Документация</span>
            </button>
            <ContinueInTelegram
              botUsername={user?.telegram_bot_username}
              sessionUuid={session.session_uuid}
              canContinue={user?.can_continue_in_telegram ?? false}
            />
          </div>
        </div>
      </header>

      {/* Слот панели хедера: документация проекта, разворачивается кнопкой. */}
      <AnimatePresence mode="wait" initial={false}>
        {docsOpen && (
          <motion.div
            key="docs"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.24, ease: "easeOut" }}
            className="overflow-hidden"
          >
            <DocsPanel onClose={() => setDocsOpen(false)} />
          </motion.div>
        )}
      </AnimatePresence>

      {error && (
        <div className="mx-auto mt-3 w-full max-w-4xl animate-slideUpIn px-6">
          <div className="rounded-xl bg-red-900/30 px-4 py-2.5 text-sm text-red-300">
            {error}
          </div>
        </div>
      )}

      {/* Bubbles */}
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className={
          centeredComposer
            ? "flex flex-1 flex-col items-center justify-end overflow-y-auto px-6"
            : "flex-1 overflow-y-auto"
        }
      >
        {isEmpty ? (
          showThinking ? (
            <div className="py-8">{liveStatus}</div>
          ) : (
            <ChatEmptyState
              projectName={session.project_name || "Без проекта"}
              projectMissing={!session.project_name}
              modelLabel={info?.known.find((m) => m.id === info?.current)?.label}
              modelId={info?.current}
            />
          )
        ) : (
          <>
            <div
              ref={setBubblesContentRef}
              className="mx-auto max-w-3xl space-y-2 px-6 py-8"
            >
              {bubbles.map((b) => {
                const el = renderBubble(b);
                // Skill / неизвестные kind дают null — раньше они не
                // оставляли DOM вовсе, так что и оборачивающий div не
                // рендерим (иначе пустой зазор в space-y-2).
                if (el === null) return null;
                return (
                  <div key={b.id} className="animate-fadeInUp">
                    {el}
                  </div>
                );
              })}
            </div>
            {/* Индикатор «думает/печатает» ПРИКЛЕЕН к низу области сообщений
                (sticky bottom): всегда на виду, даже когда пользователь
                прокрутил вверх, и не уезжает с растущим ответом. Лёгкий
                градиент-подложка, чтобы текст под ним не «просвечивал». */}
            {showThinking && (
              <div className="pointer-events-none sticky bottom-0 z-10 bg-gradient-to-t from-[var(--bg-canvas)] via-[var(--bg-canvas)] to-transparent pb-2 pt-3">
                {liveStatus}
              </div>
            )}
          </>
        )}
      </div>

      {/* MessageInput держим в ОДНОЙ позиции дерева (никогда не переезжает),
          иначе React ремоунтит его и теряет набранный черновик. Центрирование
          на пустом чате — нижним flex-спейсером, а не перемещением инпута. */}
      {messageInput}
      {centeredComposer && <div className="flex-1" aria-hidden="true" />}
      </main>
    </div>
  );
}
