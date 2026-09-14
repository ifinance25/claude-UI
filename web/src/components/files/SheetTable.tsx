type Cell = string | number | boolean | null | undefined;

/** Простая таблица для CSV/XLSX. Первая строка — заголовок. */
export default function SheetTable({ rows }: { rows: Cell[][] }) {
  if (!rows.length) {
    return (
      <div className="p-4 text-sm text-[var(--fg-muted)]">Пустая таблица.</div>
    );
  }
  const header = rows[0];
  const body = rows.slice(1);
  // Кол-во колонок = максимум по ВСЕМ строкам: иначе у «рваных» строк (данные
  // шире заголовка) хвостовые ячейки молча терялись бы.
  const cols = rows.reduce((m, r) => Math.max(m, r.length), 0);
  return (
    <div className="min-h-0 flex-1 overflow-auto p-2">
      <table className="border-collapse text-[13px]">
        <thead>
          <tr>
            {Array.from({ length: cols }, (_, i) => (
              <th
                key={i}
                className="sticky top-0 z-10 whitespace-nowrap border border-[var(--border-subtle)] bg-[var(--bg-sidebar)] px-2 py-1 text-left font-semibold text-[var(--fg-primary)]"
              >
                {String(header[i] ?? "")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((r, ri) => (
            <tr key={ri} className="odd:bg-[var(--bg-hover)]/30">
              {Array.from({ length: cols }, (_, ci) => (
                <td
                  key={ci}
                  className="border border-[var(--border-subtle)] px-2 py-1 align-top text-[var(--fg-secondary)]"
                >
                  {String(r[ci] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
