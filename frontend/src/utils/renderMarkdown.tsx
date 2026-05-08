/**
 * renderMarkdown.tsx — N5
 * ========================================================
 * Tiny markdown renderer for tutor messages. Anthropic's models
 * regularly produce **bold**, *italic*, `code`, and bullet lists in
 * tutor replies, but the chat UI was rendering them as raw text
 * (e.g., "**B Cell Differentiation and Activation**" showing the
 * asterisks visibly).
 *
 * Why an inline parser instead of `react-markdown`:
 *   - Zero new dependencies (avoids npm install + lockfile churn)
 *   - We control exactly what's rendered (no link/image/HTML risk)
 *   - The LLM-produced markdown surface is small: bold, italic,
 *     inline code, lists, paragraph breaks. No tables, no headers.
 *
 * Supported subset:
 *   **bold** | __bold__       → <strong>
 *   *italic* | _italic_       → <em>
 *   `code`                    → <code>
 *   - item / * item / 1. item → <ul>/<ol> with <li> children
 *   blank line                → paragraph break
 *
 * Explicitly NOT supported:
 *   Links, images, HTML, headers, blockquotes, tables.
 *   These are intentional omissions — chat replies don't need them.
 *
 * Safety: all text content is rendered via React (auto-escapes HTML).
 * No dangerouslySetInnerHTML anywhere.
 */

import type { ReactNode } from "react";

// ─── Inline parser ─────────────────────────────────────────────
// Order matters: process `code` first (its delimiter is unique),
// then bold (** / __), then italic (* / _). Bold-before-italic
// avoids "**foo**" being parsed as italic-italic-foo-italic-italic.

type Inline =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string }
  | { kind: "bold"; value: string }
  | { kind: "italic"; value: string };

function parseInline(text: string): Inline[] {
  // Single-pass tokenizer with a few regex patterns. We use a
  // longest-match-first approach by scanning the string.
  const tokens: Inline[] = [];
  let i = 0;
  while (i < text.length) {
    // `code` — runs to next backtick
    if (text[i] === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i) {
        tokens.push({ kind: "code", value: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }
    // **bold** or __bold__ (longest match first)
    if (text.startsWith("**", i)) {
      const end = text.indexOf("**", i + 2);
      if (end > i) {
        tokens.push({ kind: "bold", value: text.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    if (text.startsWith("__", i)) {
      const end = text.indexOf("__", i + 2);
      if (end > i) {
        tokens.push({ kind: "bold", value: text.slice(i + 2, end) });
        i = end + 2;
        continue;
      }
    }
    // *italic* or _italic_ — but skip if surrounded by word chars
    // (e.g., "B_cell" shouldn't italicize). Cheap heuristic: italic
    // delimiter must be preceded by start-of-string / whitespace /
    // punctuation, AND followed by a non-whitespace char.
    if (text[i] === "*" || text[i] === "_") {
      const ch = text[i];
      const prev = i === 0 ? " " : text[i - 1];
      const next = text[i + 1] ?? "";
      const isOpen = /[\s(\[{>"']/.test(prev) && next !== " " && next !== "";
      if (isOpen) {
        // Find matching close: same char, preceded by non-space, not doubled.
        let end = i + 1;
        while (end < text.length) {
          end = text.indexOf(ch, end);
          if (end < 0) break;
          if (text[end + 1] === ch) {
            end += 2;
            continue; // skip the **/__ form
          }
          if (text[end - 1] !== " ") break;
          end += 1;
        }
        if (end > i + 1) {
          tokens.push({ kind: "italic", value: text.slice(i + 1, end) });
          i = end + 1;
          continue;
        }
      }
    }
    // Plain text — accumulate until next special char
    let plain = "";
    while (
      i < text.length &&
      text[i] !== "`" &&
      !text.startsWith("**", i) &&
      !text.startsWith("__", i) &&
      !(text[i] === "*" || text[i] === "_")
    ) {
      plain += text[i];
      i += 1;
    }
    if (plain) {
      tokens.push({ kind: "text", value: plain });
    } else {
      // Couldn't match any inline rule and didn't advance — push the
      // single char as text so we don't infinite-loop.
      tokens.push({ kind: "text", value: text[i] ?? "" });
      i += 1;
    }
  }
  return tokens;
}

function renderInline(tokens: Inline[], keyPrefix: string): ReactNode[] {
  return tokens.map((t, idx) => {
    const k = `${keyPrefix}-${idx}`;
    switch (t.kind) {
      case "bold":
        return <strong key={k}>{t.value}</strong>;
      case "italic":
        return <em key={k}>{t.value}</em>;
      case "code":
        return (
          <code
            key={k}
            className="rounded bg-muted/30 px-1 py-0.5 text-[0.92em] font-mono"
          >
            {t.value}
          </code>
        );
      case "text":
      default:
        return <span key={k}>{t.value}</span>;
    }
  });
}

// ─── Block parser (lists + paragraphs) ─────────────────────────

type Block =
  | { kind: "para"; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] };

function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let buf: Block | null = null;

  const flush = () => {
    if (buf) blocks.push(buf);
    buf = null;
  };

  for (const raw of lines) {
    const line = raw;
    const ulMatch = /^\s*[-*]\s+(.*)$/.exec(line);
    const olMatch = /^\s*\d+\.\s+(.*)$/.exec(line);
    if (line.trim() === "") {
      flush();
      continue;
    }
    if (ulMatch) {
      if (!buf || buf.kind !== "ul") {
        flush();
        buf = { kind: "ul", items: [] };
      }
      buf.items.push(ulMatch[1]);
      continue;
    }
    if (olMatch) {
      if (!buf || buf.kind !== "ol") {
        flush();
        buf = { kind: "ol", items: [] };
      }
      buf.items.push(olMatch[1]);
      continue;
    }
    // Plain paragraph line
    if (!buf || buf.kind !== "para") {
      flush();
      buf = { kind: "para", lines: [] };
    }
    buf.lines.push(line);
  }
  flush();
  return blocks;
}

// ─── Public API ────────────────────────────────────────────────

/**
 * Render a markdown-flavored string into React nodes.
 *
 * Use in JSX:
 *   <div>{renderMarkdown(message.content)}</div>
 *
 * For LLM-generated tutor replies. Idempotent on plain text (input
 * with no markdown returns visually-identical output).
 */
export function renderMarkdown(text: string): ReactNode {
  if (!text) return null;
  const blocks = parseBlocks(text);
  return blocks.map((block, bIdx) => {
    if (block.kind === "ul") {
      return (
        <ul key={`ul-${bIdx}`} className="list-disc pl-5 space-y-0.5 my-1">
          {block.items.map((item, iIdx) => (
            <li key={iIdx}>
              {renderInline(parseInline(item), `ul-${bIdx}-${iIdx}`)}
            </li>
          ))}
        </ul>
      );
    }
    if (block.kind === "ol") {
      return (
        <ol key={`ol-${bIdx}`} className="list-decimal pl-5 space-y-0.5 my-1">
          {block.items.map((item, iIdx) => (
            <li key={iIdx}>
              {renderInline(parseInline(item), `ol-${bIdx}-${iIdx}`)}
            </li>
          ))}
        </ol>
      );
    }
    // Paragraph: join lines with line breaks, parse inline.
    const joined = block.lines.join("\n");
    const inline = parseInline(joined);
    return (
      <p key={`p-${bIdx}`} className="whitespace-pre-wrap">
        {renderInline(inline, `p-${bIdx}`)}
      </p>
    );
  });
}
