import { HelpCircleIcon } from "@/components/icons";

/**
 * Красивый рендер AskUserQuestion-вызова Claude.
 *
 * Tool AskUserQuestion — это структурированный вопрос с вариантами
 * ответа. По умолчанию он бы показывался как обычный ToolUseEvent с
 * сырым JSON в content. Этот компонент перехватывает такие вызовы и
 * рендерит карточку с кнопками: клик по варианту вставляет его текст в
 * поле ввода (вместо тяжёлой инфраструктуры tool_result-callback'ов).
 */
interface Option {
  label: string;
  description: string;
}

interface Question {
  question: string;
  header: string;
  multiSelect: boolean;
  options: Option[];
}

interface Props {
  questions: Question[];
  /** Клик по варианту → строка вставляется в MessageInput. */
  onPick: (text: string) => void;
}

export function AskUserQuestionEvent({ questions, onPick }: Props) {
  if (!Array.isArray(questions) || questions.length === 0) return null;
  return (
    <div className="my-3 space-y-4 rounded-2xl border border-[var(--border-subtle)] bg-[var(--bg-sidebar)]/60 px-5 py-4">
      <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-[var(--fg-muted)]">
        <HelpCircleIcon size={14} className="shrink-0" />
        <span>Claude уточняет</span>
      </div>
      {questions.map((q, qi) => {
        // Defensive: вопрос из истории/сырых metadata может не иметь
        // ожидаемой формы. Нормализуем поля, чтобы кривой элемент не
        // ронял весь рендер Chat через .slice()/.map() по undefined
        // (CR3-L1).
        const questionText = String(q?.question ?? "");
        const header = q?.header ? String(q.header) : "";
        const options = Array.isArray(q?.options) ? q.options : [];
        return (
        // Стабильный key: индекс + первые символы вопроса. При обновлении
        // карточки (повторный AskUserQuestion с разными опциями) React
        // не переиспользует ноды и не теряет фокус/hover/анимации.
        <div key={`${qi}:${questionText.slice(0, 40)}`} className="space-y-3">
          {header && (
            <div className="text-xs font-medium uppercase tracking-wider text-[var(--fg-muted)]">
              {header}
            </div>
          )}
          <div className="text-base font-medium text-[var(--fg-primary)]">
            {questionText}
          </div>
          {options.length > 0 && (
            <div className="grid gap-2 sm:grid-cols-2">
              {options.map((opt, oi) => (
                <button
                  key={`${qi}:${oi}:${opt.label}`}
                  onClick={() => onPick(opt.label)}
                  className="group flex flex-col items-start gap-1 rounded-xl border border-[var(--border-subtle)] bg-[var(--bg-canvas)]/40 px-4 py-3 text-left transition-colors hover:border-[var(--fg-muted)] hover:bg-[var(--bg-hover)]"
                  title={opt.description || opt.label}
                >
                  <span className="text-sm font-semibold text-[var(--fg-primary)]">
                    {opt.label}
                  </span>
                  {opt.description && (
                    <span className="line-clamp-2 text-xs text-[var(--fg-muted)] group-hover:text-[var(--fg-secondary)]">
                      {opt.description}
                    </span>
                  )}
                </button>
              ))}
            </div>
          )}
        </div>
        );
      })}
      <div className="text-[11px] text-[var(--fg-muted)]">
        Тап по варианту вставит ответ в поле ввода — отправь как обычное сообщение.
      </div>
    </div>
  );
}
