"""Эвристический разбор Bash-команд на создаваемые файлы/папки.

Зачем: артефакты Claude в вебе строятся из tool_use-сообщений с ``file_path``
(его проставляют только Write/Edit). Файлы, созданные через Bash (`cp`, `tee`,
`cat > f`, `mkdir` …), в список не попадали — поэтому после «сделай копию»
артефакта не было. Здесь — консервативный парсер: извлекает ПУТИ-ЦЕЛИ из
распространённых команд, чтобы бридж пометил их как артефакты.

Это эвристика: сложные пайплайны/подстановки могут быть пропущены. Лучше
пропустить, чем поймать мусор (ложные пути ломали бы панель артефактов).
"""
from __future__ import annotations

import re
import shlex

# Команды, у которых последний позиционный аргумент — это ЦЕЛЬ (dest).
_DEST_LAST = {"cp", "mv", "install", "rsync", "ln"}
# Команды, у которых ВСЕ позиционные аргументы — создаваемые цели.
_DEST_ALL = {"touch", "mkdir"}
# Пути, которые точно не артефакты.
_IGNORE_PREFIXES = ("/dev/", "/proc/", "/sys/", "/dev", "-")
_IGNORE_EXACT = {"/dev/null", "/dev/stdout", "/dev/stderr", ".", "..", "/"}


def _is_pathish(tok: str) -> bool:
    if not tok or tok in _IGNORE_EXACT:
        return False
    if tok.startswith(_IGNORE_PREFIXES):
        return False
    # Подстановки/переменные/глоб — пропускаем (не детерминированный путь).
    if any(ch in tok for ch in ("$", "*", "?", "`", "\n")):
        return False
    return True


_BRACE_LIMIT = 100  # макс. имён на токен; больше — отсекаем (не плодим мусор)
_MAX_BRACE_GROUPS = 32  # макс. групп {..} в токене; больше — отсекаем (DoS-защита)


def _expand_braces(token: str, limit: int = _BRACE_LIMIT) -> list[str] | None:
    """Раскрывает bash brace-expansion в конкретные имена.

    Поддержка: числовой диапазон ``{1..N}`` (+zero-pad, +шаг ``{1..N..S}``) и
    список ``{a,b,c}``. Несколько групп раскрываются декартово.

    Возвращает список имён; ``None`` — если скобки есть, но паттерн не
    распознан ИЛИ имён > ``limit`` (сигнал вызывающему: отсечь токен, не
    записывать фантом вроде ``file{1..10}.txt``). Токен без скобок → ``[token]``.
    """
    if "{" not in token and "}" not in token:
        return [token]
    # незакрытые/непарные скобки → отсечь
    if token.count("{") != token.count("}"):
        return None
    # Слишком много групп → рекурсивный (глубина == число групп → RecursionError)
    # и декартов взрыв. Отсекаем ДО раскрытия (лучше пропустить, чем DoS).
    if token.count("{") > _MAX_BRACE_GROUPS:
        return None

    m = re.search(r"\{([^{}]*)\}", token)
    if m is None:
        return None  # есть скобки, но не нашли простую группу (вложенность и т.п.)
    inner = m.group(1)
    pre, post = token[: m.start()], token[m.end():]

    items: list[str] | None = None
    rng = re.fullmatch(r"(-?\d+)\.\.(-?\d+)(?:\.\.(-?\d+))?", inner)
    if rng:
        a, b = int(rng.group(1)), int(rng.group(2))
        step = abs(int(rng.group(3))) if rng.group(3) else 1
        if step == 0:
            return None
        # Размер диапазона считаем ДО материализации — иначе {1..2000000000}
        # (короткий токен) построил бы миллиарды строк (OOM/зависание event
        # loop) прежде, чем сработает cap ниже.
        if (abs(b - a) // step) + 1 > limit:
            return None
        width = 0
        for g in (rng.group(1), rng.group(2)):
            s = g.lstrip("-")
            if len(s) > 1 and s[0] == "0":
                width = max(width, len(s))
        rng_iter = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        items = [str(n).zfill(width) if width else str(n) for n in rng_iter]
    elif "," in inner:
        items = inner.split(",")  # список {a,b,c}
    else:
        # {single} без запятой и без диапазона — не brace-expansion; bash
        # оставил бы литерал. Консервативно отсекаем (лучше пропустить, чем мусор).
        return None

    results: list[str] = []
    for it in items:
        tail = _expand_braces(pre + it + post, limit)
        if tail is None:
            return None
        results.extend(tail)
        if len(results) > limit:
            return None
    return results


def _split_segments(command: str) -> list[str]:
    """Грубое деление на отдельные команды по ; && || | и переводам строк."""
    return [s for s in re.split(r"\|\||&&|[;\n|]", command) if s.strip()]


def _tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, comments=True)
    except ValueError:
        # незакрытая кавычка и т.п. — мягкий фолбэк по пробелам
        return segment.split()


def _redirect_targets(tokens: list[str]) -> list[str]:
    """Цели перенаправлений: `> f`, `>> f`, `1> f`, прилипшее `>f`.

    НЕ считаем целью fd-дупликацию `2>&1` / `>&2` (после стрелки идёт `&N`) —
    иначе на каждой команде со `2>&1` появлялся фантомный артефакт `&1`.
    """
    out: list[str] = []
    n = len(tokens)
    i = 0
    while i < n:
        t = tokens[i]
        if re.fullmatch(r"\d*>>?", t):  # стрелка отдельным токеном → цель следом
            nxt = tokens[i + 1] if i + 1 < n else ""
            if nxt and not nxt.startswith("&") and _is_pathish(nxt):
                out.append(nxt)
            i += 2
            continue
        m = re.match(r"^\d*(?:>>|>)(.+)$", t)  # прилипшее: >file, >>file, 2>file
        if m:
            cand = m.group(1)
            # cand не должен начинаться с '>' (>>>) или '&' (fd-дупликация).
            if not cand.startswith((">", "&")) and _is_pathish(cand):
                out.append(cand)
        i += 1
    return out


def extract_bash_outputs(command: str) -> list[str]:
    """Список путей-целей, создаваемых командой. Уникальные, в порядке встречи."""
    if not command or not isinstance(command, str):
        return []
    # Heredoc (`cat <<EOF > f` … тело … `EOF`): тело идёт отдельными строками и
    # _split_segments приняло бы их за команды → фантомные артефакты из текста
    # документа. Парсим только первую строку (где и стоит реальное `> f`).
    if "<<" in command:
        command = command.split("\n", 1)[0]

    seen: set[str] = set()
    out: list[str] = []

    def add(p: str) -> None:
        expanded = _expand_braces(p.strip())
        if expanded is None:
            return  # нераспознанные/слишком большие скобки — не артефакт
        for name in expanded:
            name = name.rstrip("/") or name
            if name and name not in seen and _is_pathish(name):
                seen.add(name)
                out.append(name)

    for segment in _split_segments(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        # Перенаправления вывода — в любой команде сегмента.
        for tgt in _redirect_targets(tokens):
            add(tgt)

        cmd = tokens[0].rsplit("/", 1)[-1]  # имя бинаря без пути (/bin/cp → cp)
        # cp/mv/install с -t DIR / --target-directory=DIR: цель — это DIR, а
        # последний позиционный — ИСТОЧНИК. Ловим явно, иначе записали бы файл-
        # источник как артефакт.
        target_dir: str | None = None
        if cmd in _DEST_LAST:
            for j, t in enumerate(tokens[1:], start=1):
                if t.startswith("--target-directory="):
                    target_dir = t.split("=", 1)[1]
                elif t in ("-t", "--target-directory") and j + 1 < len(tokens):
                    target_dir = tokens[j + 1]
        # позиционные аргументы (без опций и без перенаправлений)
        positional: list[str] = []
        skip_next = False
        after_ddash = False
        for t in tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if not after_ddash and t == "--":
                after_ddash = True  # дальше всё — позиционные
                continue
            if not after_ddash:
                if re.fullmatch(r"\d*>>?", t) or re.match(r"^\d*>>?.+$", t):
                    # перенаправление уже учли выше; пропускаем его (и аргумент)
                    if re.fullmatch(r"\d*>>?", t):
                        skip_next = True
                    continue
                if t in ("-t", "--target-directory"):
                    skip_next = True  # его аргумент — не позиционный
                    continue
                if t.startswith("-"):
                    continue
            positional.append(t)

        if target_dir is not None:
            add(target_dir)
        elif cmd == "tee":
            for p in positional:
                add(p)
        elif cmd in _DEST_ALL:
            for p in positional:
                add(p)
        elif cmd in _DEST_LAST and len(positional) >= 2:
            add(positional[-1])

    return out
