import { Markdown } from "@/lib/Markdown";
import { CsvView, DocxView, XlsxView } from "@/components/files/RichViewers";
import type { FileContent } from "@/lib/types";

const EXT_LANG: Record<string, string> = {
  ts: "typescript", tsx: "tsx", js: "javascript", jsx: "jsx",
  py: "python", json: "json", md: "markdown", css: "css", scss: "scss",
  html: "html", xml: "xml", sh: "bash", bash: "bash", zsh: "bash",
  yml: "yaml", yaml: "yaml", toml: "toml", ini: "ini", conf: "ini", cfg: "ini",
  sql: "sql", go: "go", rs: "rust", rb: "ruby", php: "php", java: "java",
  kt: "kotlin", swift: "swift", c: "c", h: "c", cpp: "cpp", hpp: "cpp",
  cs: "csharp", dockerfile: "dockerfile", env: "bash", txt: "",
};

// SVG НЕ здесь: его рендерим в sandbox-iframe (по содержимому), а не <img> с
// inline-URL — image/svg+xml исполняет скрипты при прямом открытии (XSS).
const IMAGE_EXTS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif",
]);

// Тяжёлые форматы (docx/xlsx) грузятся целиком в браузер — выше лимита
// показываем «Скачать», чтобы не подвесить вкладку.
const MAX_RICH_BYTES = 12 * 1024 * 1024;

function ext(rel: string): string {
  return rel.split(".").pop()?.toLowerCase() ?? "";
}

function extLang(rel: string): string {
  return EXT_LANG[ext(rel)] ?? "";
}

// Оборачиваем содержимое в fenced-блок длиннее любой внутренней серии бэктиков,
// чтобы текст с ``` не ломал разметку. Подсветка — существующий rehype-highlight.
// Длину самой длинной серии бэктиков считаем за ОДИН проход (раньше был
// while(includes) → O(n²) и подвешивал вкладку на больших файлах с бэктиками).
function fence(content: string, lang: string): string {
  const runs = content.match(/`+/g);
  const longest = runs ? runs.reduce((m, r) => Math.max(m, r.length), 0) : 0;
  const ticks = "`".repeat(Math.max(3, longest + 1));
  return `${ticks}${lang}\n${content}\n${ticks}`;
}

function DownloadFallback({
  label,
  onDownload,
}: {
  label: string;
  onDownload: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-3 p-8 text-center">
      <p className="text-sm text-[var(--fg-muted)]">{label}</p>
      <button
        onClick={onDownload}
        className="rounded-lg bg-[var(--accent)] px-4 py-2 text-sm font-medium text-[var(--bg-canvas)] transition-opacity hover:opacity-90"
      >
        Скачать
      </button>
    </div>
  );
}

export default function FileViewer({
  file,
  inlineUrl,
  downloadUrl,
  onDownload,
}: {
  file: FileContent;
  /** URL для inline-показа (картинка/PDF). */
  inlineUrl: string;
  /** URL для скачивания/чтения байтов (docx/xlsx). */
  downloadUrl: string;
  onDownload: () => void;
}) {
  const e = ext(file.rel);
  const tooBig = file.size_bytes > MAX_RICH_BYTES;

  // Картинки.
  if (IMAGE_EXTS.has(e)) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center overflow-auto bg-[var(--bg-canvas)] p-3">
        <img
          src={inlineUrl}
          alt={file.rel}
          className="max-h-full max-w-full object-contain"
        />
      </div>
    );
  }

  // PDF — встроенный просмотрщик браузера + страховка «открыть в новой вкладке»
  // (на случай, если встроенный просмотр PDF где-то заблокирован политиками).
  if (e === "pdf") {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center justify-end gap-3 border-b border-[var(--border-subtle)] px-3 py-1.5 text-xs">
          <a
            href={inlineUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[var(--accent)] hover:opacity-90"
          >
            Открыть в новой вкладке
          </a>
        </div>
        <iframe
          src={inlineUrl}
          title={file.rel}
          className="min-h-0 w-full flex-1 bg-white"
        />
      </div>
    );
  }

  // DOCX / XLSX — рендер по требованию (с лимитом размера).
  if (e === "docx") {
    return tooBig ? (
      <DownloadFallback label="Документ слишком большой для просмотра" onDownload={onDownload} />
    ) : (
      <DocxView url={downloadUrl} />
    );
  }
  if (e === "xlsx" || e === "xls" || e === "xlsm") {
    return tooBig ? (
      <DownloadFallback label="Таблица слишком большая для просмотра" onDownload={onDownload} />
    ) : (
      <XlsxView url={downloadUrl} />
    );
  }

  // Дальше — текстовые форматы: нужно загруженное содержимое.
  if (file.binary || file.too_large) {
    return (
      <DownloadFallback
        label={file.binary ? "Бинарный файл" : "Файл слишком большой для просмотра"}
        onDownload={onDownload}
      />
    );
  }

  // CSV / TSV → таблица.
  if (e === "csv" || e === "tsv") {
    return <CsvView text={file.content} delimiter={e === "tsv" ? "\t" : ","} />;
  }

  // HTML / SVG → безопасный sandbox-iframe (без скриптов и same-origin).
  if (e === "html" || e === "htm" || e === "svg") {
    return (
      <iframe
        sandbox=""
        title={file.rel}
        className="min-h-0 w-full flex-1 bg-white"
        srcDoc={file.content}
      />
    );
  }

  // Markdown — рендерим как разметку, а не «стену кода».
  if (e === "md" || e === "markdown" || e === "mdx") {
    return (
      <div className="file-view min-h-0 flex-1 overflow-auto p-4">
        <Markdown>{file.content}</Markdown>
      </div>
    );
  }

  // Остальной текст/код — подсветка в fenced-блоке.
  return (
    <div className="file-view min-h-0 flex-1 overflow-auto p-3">
      {/* detect: для файлов без узнаваемого расширения (Caddyfile, .conf и т.п.)
          highlight.js сам определит язык — подсветка вместо «стены решёток». */}
      <Markdown detect>{fence(file.content, extLang(file.rel))}</Markdown>
    </div>
  );
}
