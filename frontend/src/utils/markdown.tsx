import type { ReactNode } from 'react';

// ═══════════════════════════════════════════════════════════════════════
// Inline markdown renderer (shared)
//
// Handles the shapes used across the app's docs — the Help → Reference
// viewer and the Reports page's pipeline-documentation viewer:
//   # / ## / ### / #### headers
//   Bullet lists (- ) and numbered lists (1. )
//   Tables (GFM pipe tables with separator row)
//   **bold**, *italic*, `code`
//   ```fenced code blocks```
//   > blockquotes
//   Links [text](url)
//   Horizontal rules (---)
//
// Not a full CommonMark implementation — just enough for our docs.
// Extracted from DocsReference so both viewers share one renderer.
// ═══════════════════════════════════════════════════════════════════════

export function renderMarkdown(src: string): ReactNode {
  if (!src) return null;

  // Split by fenced code blocks first — easier than regex with escapes.
  const blocks: Array<{ kind: 'text' | 'code'; body: string; lang?: string }> = [];
  const lines = src.split('\n');
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^```(\w+)?/.exec(line);
    if (fence) {
      // Start of code block — find matching close.
      const lang = fence[1] || '';
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // skip the closing ```
      blocks.push({ kind: 'code', body: codeLines.join('\n'), lang });
    } else {
      const textLines: string[] = [];
      while (
        i < lines.length &&
        !/^```(\w+)?/.test(lines[i])
      ) {
        textLines.push(lines[i]);
        i++;
      }
      blocks.push({ kind: 'text', body: textLines.join('\n') });
    }
  }

  return blocks.map((b, idx) =>
    b.kind === 'code' ? (
      <pre
        key={idx}
        className="my-4 overflow-x-auto rounded-lg bg-slate-900 p-4 text-xs leading-relaxed text-slate-100"
      >
        <code>{b.body}</code>
      </pre>
    ) : (
      <TextBlock key={idx} src={b.body} />
    ),
  );
}

// Renders a text block (non-code). Walks the lines and emits
// paragraphs, headers, lists, tables, blockquotes, horizontal rules.
function TextBlock({ src }: { src: string }): ReactNode {
  const lines = src.split('\n');
  const out: ReactNode[] = [];
  let i = 0;
  let key = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Blank line → skip
    if (!line.trim()) {
      i++;
      continue;
    }

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      out.push(<hr key={key++} className="my-6 border-slate-200" />);
      i++;
      continue;
    }

    // Headers
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      const level = h[1].length;
      const text = h[2].trim();
      out.push(renderHeader(level, text, key++));
      i++;
      continue;
    }

    // Table — a line starting with `|` followed by a separator row of `|---|`
    if (line.trim().startsWith('|') && i + 1 < lines.length &&
        /^\s*\|[\s\-\|:]+\|\s*$/.test(lines[i + 1])) {
      const tableLines: string[] = [line];
      i++; // header separator
      const sep = lines[i];
      tableLines.push(sep);
      i++;
      while (i < lines.length && lines[i].trim().startsWith('|')) {
        tableLines.push(lines[i]);
        i++;
      }
      out.push(renderTable(tableLines, key++));
      continue;
    }

    // Blockquote
    if (line.startsWith('>')) {
      const quoteLines: string[] = [];
      while (i < lines.length && lines[i].startsWith('>')) {
        quoteLines.push(lines[i].replace(/^>\s?/, ''));
        i++;
      }
      out.push(
        <blockquote
          key={key++}
          className="my-4 border-l-4 border-violet-300 bg-violet-50/50 px-4 py-2 text-sm italic text-slate-700"
        >
          {renderInline(quoteLines.join(' '))}
        </blockquote>,
      );
      continue;
    }

    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ''));
        i++;
      }
      out.push(
        <ul key={key++} className="my-3 list-disc space-y-1.5 pl-6 text-base leading-relaxed text-slate-700">
          {items.map((it, k) => (
            <li key={k}>{renderInline(it)}</li>
          ))}
        </ul>,
      );
      continue;
    }

    // Numbered list. Blank lines BETWEEN numbered items must keep them in
    // the SAME <ol> (markdown "loose list") — splitting there gives every
    // item its own single-item <ol> whose CSS counter restarts, so a
    // 1./2./3. list renders as 1., 1., 1. The author's first number is
    // honored via <ol start> so a list interrupted by prose resumes
    // where it left off instead of snapping back to 1.
    if (/^\s*\d+\.\s+/.test(line)) {
      const start = parseInt(/^\s*(\d+)\.\s+/.exec(line)![1], 10);
      const items: string[] = [];
      while (i < lines.length) {
        if (/^\s*\d+\.\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+\.\s+/, ''));
          i++;
          continue;
        }
        if (!lines[i].trim()) {
          // Bridge blank lines only when another numbered item follows.
          let j = i + 1;
          while (j < lines.length && !lines[j].trim()) j++;
          if (j < lines.length && /^\s*\d+\.\s+/.test(lines[j])) {
            i = j;
            continue;
          }
        }
        break;
      }
      out.push(
        <ol
          key={key++}
          start={start !== 1 ? start : undefined}
          className="my-3 list-decimal space-y-1.5 pl-6 text-base leading-relaxed text-slate-700"
        >
          {items.map((it, k) => (
            <li key={k}>{renderInline(it)}</li>
          ))}
        </ol>,
      );
      continue;
    }

    // Regular paragraph — accumulate until blank line or block marker
    const paragraphLines: string[] = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !lines[i].startsWith('>') &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !lines[i].trim().startsWith('|') &&
      !/^---+$/.test(lines[i].trim())
    ) {
      paragraphLines.push(lines[i]);
      i++;
    }
    out.push(
      <p key={key++} className="my-3 text-base leading-relaxed text-slate-700">
        {renderInline(paragraphLines.join(' '))}
      </p>,
    );
  }

  return <>{out}</>;
}

// GitHub-style header slug: lowercased, alphanumerics + dashes, runs of
// non-word chars collapsed to one dash.
function slugifyHeader(text: string): string {
  return text
    .toLowerCase()
    .replace(/[`*_~]/g, '')        // strip basic md inline markers
    .replace(/[^a-z0-9\s-]/g, '')  // drop punctuation
    .trim()
    .replace(/\s+/g, '-')          // spaces → dashes
    .replace(/-+/g, '-');          // collapse runs of dashes
}

function renderHeader(level: number, text: string, k: number): ReactNode {
  const inline = renderInline(text);
  const id = slugifyHeader(text);
  if (level === 1) {
    return (
      <h1
        key={k}
        id={id}
        className="mt-6 mb-3 border-b border-slate-200 pb-2 text-2xl font-bold text-slate-900 scroll-mt-16"
      >
        {inline}
      </h1>
    );
  }
  if (level === 2) {
    return (
      <h2
        key={k}
        id={id}
        className="mt-8 mb-3 border-b border-slate-100 pb-1.5 text-xl font-semibold text-slate-900 scroll-mt-16"
      >
        {inline}
      </h2>
    );
  }
  if (level === 3) {
    return (
      <h3 key={k} id={id} className="mt-5 mb-2 text-base font-semibold text-slate-900 scroll-mt-16">
        {inline}
      </h3>
    );
  }
  return (
    <h4 key={k} id={id} className="mt-3 mb-1 text-sm font-semibold text-slate-700 scroll-mt-16">
      {inline}
    </h4>
  );
}

function renderTable(tableLines: string[], k: number): ReactNode {
  // Row 0: header cells, Row 1: separator, Rows 2+: body.
  const parseRow = (line: string) => {
    const trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    return trimmed.split('|').map((c) => c.trim());
  };
  const header = parseRow(tableLines[0]);
  const body = tableLines.slice(2).map(parseRow);

  return (
    <div key={k} className="my-4 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-300 bg-slate-50">
            {header.map((cell, i) => (
              <th
                key={i}
                className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-slate-600"
              >
                {renderInline(cell)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {body.map((row, ri) => (
            <tr key={ri} className="border-b border-slate-100 last:border-0">
              {row.map((cell, ci) => (
                <td key={ci} className="px-3 py-2 align-top text-slate-700">
                  {renderInline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Inline formatting: **bold**, *italic*, `code`, [text](url).
function renderInline(text: string): ReactNode {
  const tokens: Array<{ kind: string; text: string; href?: string }> = [];
  let rest = text;

  while (rest.length > 0) {
    const codeMatch = /^([\s\S]*?)`([^`]+)`/.exec(rest);
    const boldMatch = /^([\s\S]*?)\*\*([^*]+)\*\*/.exec(rest);
    const italicMatch = /^([\s\S]*?)(?<![*\w])\*([^*]+)\*/.exec(rest);
    const linkMatch = /^([\s\S]*?)\[([^\]]+)\]\(([^)]+)\)/.exec(rest);

    const candidates = [
      codeMatch && { kind: 'code', m: codeMatch },
      boldMatch && { kind: 'bold', m: boldMatch },
      italicMatch && { kind: 'italic', m: italicMatch },
      linkMatch && { kind: 'link', m: linkMatch },
    ].filter(Boolean) as Array<{ kind: string; m: RegExpExecArray }>;

    if (candidates.length === 0) {
      tokens.push({ kind: 'text', text: rest });
      break;
    }
    candidates.sort((a, b) => a.m[1].length - b.m[1].length);
    const first = candidates[0];
    if (first.m[1]) tokens.push({ kind: 'text', text: first.m[1] });

    if (first.kind === 'code') {
      tokens.push({ kind: 'code', text: first.m[2] });
    } else if (first.kind === 'bold') {
      tokens.push({ kind: 'bold', text: first.m[2] });
    } else if (first.kind === 'italic') {
      tokens.push({ kind: 'italic', text: first.m[2] });
    } else if (first.kind === 'link') {
      tokens.push({ kind: 'link', text: first.m[2], href: first.m[3] });
    }
    rest = rest.slice(first.m[0].length);
  }

  return tokens.map((t, i) => {
    if (t.kind === 'code') {
      return (
        <code
          key={i}
          className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[12px] text-slate-800"
        >
          {t.text}
        </code>
      );
    }
    if (t.kind === 'bold') {
      return (
        <strong key={i} className="font-semibold text-slate-900">
          {t.text}
        </strong>
      );
    }
    if (t.kind === 'italic') {
      return <em key={i}>{t.text}</em>;
    }
    if (t.kind === 'link') {
      const isExternal = /^https?:\/\//.test(t.href || '');
      return (
        <a
          key={i}
          href={t.href}
          target={isExternal ? '_blank' : undefined}
          rel={isExternal ? 'noopener noreferrer' : undefined}
          className="text-violet-600 underline hover:text-violet-700"
        >
          {t.text}
        </a>
      );
    }
    return <span key={i}>{t.text}</span>;
  });
}
