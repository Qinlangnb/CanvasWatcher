import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";

// No raw HTML, executable protocols, or automatic requests to model-supplied images.
export function ChatMarkdown({ content }: { content: string }) {
  return <div className="chat-markdown">
    <Markdown
      skipHtml
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[[rehypeKatex, { trust: false, strict: "ignore", maxExpand: 200, maxSize: 20 }]]}
      components={{
        img: ({ alt }) => <span>[Image omitted{alt ? `: ${alt}` : ""}]</span>,
        a: ({ href, children }) => href
          ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
          : <span>{children}</span>,
        table: ({ children }) => <div className="chat-table-scroll" tabIndex={0} role="region" aria-label="Scrollable table"><table>{children}</table></div>,
      }}
    >{content}</Markdown>
  </div>;
}
