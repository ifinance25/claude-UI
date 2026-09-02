import { type ComponentProps } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeSanitize from "rehype-sanitize";

type RehypePlugins = ComponentProps<typeof ReactMarkdown>["rehypePlugins"];

/**
 * Единый markdown-рендер для ответов Claude и просмотра документации.
 * remark-gfm: таблицы, списки задач, ~~strike~~, автоссылки.
 * rehype-sanitize: вырезает опасный HTML (XSS), запускается ПОСЛЕ
 * highlight, чтобы пропустить только безопасные className подсветки.
 * Базовый вес — нормальный; жирным только реальный **bold** (см. typography).
 */
export function Markdown({
  children,
  detect = false,
}: {
  children: string;
  // detect: включить авто-определение языка highlight.js для блоков кода без
  // явного языка (нужно файловому просмотру, где расширение не всегда известно).
  detect?: boolean;
}) {
  const rehypePlugins: RehypePlugins = detect
    ? [[rehypeHighlight, { detect: true }], rehypeSanitize]
    : [rehypeHighlight, rehypeSanitize];
  return (
    <div className="markdown-body text-base leading-[1.7] text-[var(--fg-primary)]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={rehypePlugins}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
