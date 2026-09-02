import { useEffect, useRef, useState } from "react";
import { prefersReducedMotion } from "@/lib/reducedMotion";

// Плавная «печатная машинка». Главное — РОВНОСТЬ: текст не «появляется резко»
// (то 1 символ, то 15 за кадр), а печатается с плавно меняющейся скоростью.
// Скорость целимся подобрать так, чтобы догнать бэклог примерно за LAG_SEC, но
// держим её в [MIN_CPS, MAX_CPS] и ПЛАВНО (low-pass, EASE_MS) подтягиваем к
// целевой — поэтому при приходе очередного чанка скорость не прыгает скачком, а
// мягко разгоняется/замедляется. Так печать выглядит как настоящая машинка и
// при этом не отстаёт сильно от генерации.
const MIN_CPS = 160; // нижняя скорость (символов/сек) — ровная спокойная печать
const MAX_CPS = 420; // верхняя — чтобы большой бэклог не «выплёвывался» блоком
const LAG_SEC = 0.45; // целевое время «догона» бэклога (плавно)
const EASE_MS = 140; // постоянная сглаживания скорости (мягкий разгон/торможение)

/**
 * Плавно «печатает» full.
 *
 * enabled=true → анимируем; начавшись, анимация ДОПЕЧАТЫВАЕТ до конца, даже если
 * enabled станет false (стрим завершился раньше, чем печать догнала текст).
 * enabled=false С САМОГО НАЧАЛА (история/готовые баблы) или reduced-motion →
 * отдаём full сразу, без анимации.
 */
export function useTypewriter(full: string, enabled: boolean): string {
  const reduce = prefersReducedMotion();
  const animate = enabled && !reduce;
  const [shown, setShown] = useState(animate ? 0 : full.length);
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const rafRef = useRef<number | null>(null);
  const lastTsRef = useRef<number | null>(null);
  // Текущая скорость печати (символов/сек). Живёт между кадрами И между сменами
  // full, чтобы скорость менялась ПЛАВНО, а не сбрасывалась на каждый чанк.
  const speedRef = useRef(MIN_CPS);
  const startedRef = useRef(animate);
  if (animate) startedRef.current = true;

  useEffect(() => {
    if (!startedRef.current) {
      setShown(full.length);
      return;
    }
    if (shownRef.current > full.length) setShown(full.length); // бабл укоротился
    const tick = (ts: number) => {
      if (lastTsRef.current === null) lastTsRef.current = ts;
      // Клампим dt: после фоновой вкладки/паузы один кадр мог бы дать огромный
      // скачок — печать бы «дёрнулась». Ограничиваем ~4 кадрами.
      const dt = Math.min(64, ts - lastTsRef.current);
      lastTsRef.current = ts;
      const cur = shownRef.current;
      const target = full.length;
      if (cur >= target) {
        rafRef.current = null;
        lastTsRef.current = null;
        return;
      }
      const backlog = target - cur;
      const targetSpeed = Math.min(MAX_CPS, Math.max(MIN_CPS, backlog / LAG_SEC));
      // Low-pass: плавно тянем текущую скорость к целевой (без скачков).
      const k = Math.min(1, dt / EASE_MS);
      speedRef.current += (targetSpeed - speedRef.current) * k;
      const add = Math.max(1, Math.round((speedRef.current * dt) / 1000));
      setShown(Math.min(target, cur + add));
      rafRef.current = requestAnimationFrame(tick);
    };
    if (rafRef.current === null) rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      lastTsRef.current = null;
    };
    // НАМЕРЕННО без `enabled`: завершение стрима не должно обрывать уже идущую
    // анимацию — она допечатает сама.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [full]);

  return startedRef.current ? full.slice(0, shown) : full;
}
