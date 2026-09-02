import { describe, it, expect } from "vitest";
import {
  stripThinking,
  hasVisibleText,
  extractThinking,
} from "@/lib/stripThinking";

const OPEN = "\x01THINKING\x02";
const CLOSE = "\x02THINKING\x01";

describe("stripThinking / extractThinking", () => {
  it("извлекает нормальную закрытую пару, в видимом — только ответ", () => {
    const raw = `${OPEN}рассуждаю про задачу${CLOSE}\n\nВот ответ.`;
    const { visible, thinking } = extractThinking(raw);
    expect(visible).toBe("Вот ответ.");
    expect(thinking).toEqual(["рассуждаю про задачу"]);
    expect(visible).not.toContain("\x01");
    expect(visible).not.toContain("\x02");
  });

  it("ЛЕГАСИ: осиротевший CLOSE без OPEN — мысль прячется, маркер не течёт", () => {
    // Так выглядела сломанная история до фикса bridge.
    const raw = `The user greeted me. I respond naturally.${CLOSE}\n\nПривет!`;
    const { visible, thinking } = extractThinking(raw);
    expect(visible).toBe("Привет!");
    expect(visible).not.toContain("greeted me");
    expect(visible).not.toContain("THINKING");
    expect(visible).not.toContain("\x01");
    expect(visible).not.toContain("\x02");
    // Утёкшее мышление подбирается в свёрнутый блок, а не теряется молча.
    expect(thinking).toEqual(["The user greeted me. I respond naturally."]);
  });

  it("незакрытый хвостовой OPEN (середина стрима) не показывает половину мысли", () => {
    const raw = `${OPEN}думаю, пока не закончил`;
    expect(stripThinking(raw)).toBe("");
    expect(hasVisibleText(raw)).toBe(false);
  });

  it("любые одиночные маркеры/управляющие байты не попадают в видимый текст", () => {
    const raw = `ответ\x01ещё${OPEN}x`;
    const out = stripThinking(raw);
    expect(out).not.toContain("\x01");
    expect(out).not.toContain("\x02");
    expect(out).not.toContain("THINKING");
  });

  it("обычный текст без маркеров не трогается", () => {
    expect(stripThinking("Просто текст.")).toBe("Просто текст.");
    expect(extractThinking("Просто текст.")).toEqual({
      visible: "Просто текст.",
      thinking: [],
    });
  });

  it("несколько закрытых блоков — все попадают в thinking, видимое склеено", () => {
    const raw = `${OPEN}мысль1${CLOSE}ответ A${OPEN}мысль2${CLOSE}ответ B`;
    const { visible, thinking } = extractThinking(raw);
    expect(thinking).toEqual(["мысль1", "мысль2"]);
    expect(visible).toContain("ответ A");
    expect(visible).toContain("ответ B");
    expect(visible).not.toContain("\x01");
  });

  it("РЕГРЕССИЯ (#1 аудит): видимый текст ПЕРЕД стрей-CLOSE не съедается, когда есть валидный OPEN", () => {
    // Контент с валидной парой И последующим осиротевшим CLOSE. Раньше
    // ORPHAN_CLOSE_LEAD_RE срезал «Привет … Мир» до стрей-CLOSE — текст терялся.
    const raw = `Привет ${OPEN}мысль${CLOSE} Мир ${CLOSE}Хвост`;
    const visible = stripThinking(raw);
    expect(visible).toContain("Привет");
    expect(visible).toContain("Мир");
    expect(visible).toContain("Хвост");
    expect(visible).not.toContain("\x01");
    expect(visible).not.toContain("\x02");
    expect(visible).not.toContain("THINKING");
  });
});
