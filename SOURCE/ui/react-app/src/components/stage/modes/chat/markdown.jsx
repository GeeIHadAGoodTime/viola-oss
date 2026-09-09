import { useState } from 'react';
import PropTypes from 'prop-types';

const KEYWORDS = new Set([
  'async',
  'await',
  'class',
  'const',
  'def',
  'else',
  'export',
  'for',
  'from',
  'function',
  'if',
  'import',
  'let',
  'return',
  'try',
  'while',
]);

function copyText(text) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(text).catch(() => {});
}

function isSafeHref(href) {
  return /^https?:\/\//i.test(href) || href.startsWith('/') || href.startsWith('#');
}

function InlineText({ text }) {
  const parts = [];
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\*[^*]+\*)/g;
  let lastIndex = 0;
  let match = pattern.exec(text);
  while (match) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith('**')) {
      parts.push(<strong key={`${match.index}-strong`}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('`')) {
      parts.push(<code key={`${match.index}-code`}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('[')) {
      const linkMatch = /^\[([^\]]+)\]\(([^)]+)\)$/.exec(token);
      if (linkMatch && isSafeHref(linkMatch[2])) {
        parts.push(
          <a key={`${match.index}-link`} href={linkMatch[2]} target="_blank" rel="noreferrer">
            {linkMatch[1]}
          </a>
        );
      } else {
        parts.push(token);
      }
    } else if (token.startsWith('*')) {
      parts.push(<em key={`${match.index}-em`}>{token.slice(1, -1)}</em>);
    } else {
      parts.push(token);
    }
    lastIndex = match.index + token.length;
    match = pattern.exec(text);
  }
  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }
  return <>{parts}</>;
}

InlineText.propTypes = {
  text: PropTypes.string.isRequired,
};

function highlightCode(code) {
  return code.split(/(\b[A-Za-z_][A-Za-z0-9_]*\b|"[^"]*"|'[^']*'|\d+)/g).map((part, index) => {
    if (KEYWORDS.has(part)) return <span key={index} className="chat-code-keyword">{part}</span>;
    if (/^["']/.test(part)) return <span key={index} className="chat-code-string">{part}</span>;
    if (/^\d+$/.test(part)) return <span key={index} className="chat-code-number">{part}</span>;
    return part;
  });
}

function CodeBlock({ code, language }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    copyText(code);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1100);
  };
  return (
    <div className="chat-code-block">
      <div className="chat-code-bar">
        <span>{language || 'text'}</span>
        <button type="button" onClick={onCopy}>{copied ? 'Copied' : 'Copy'}</button>
      </div>
      <pre><code>{highlightCode(code)}</code></pre>
    </div>
  );
}

CodeBlock.propTypes = {
  code: PropTypes.string.isRequired,
  language: PropTypes.string,
};

function renderTable(lines, key) {
  const rows = lines
    .filter((line, index) => index !== 1)
    .map((line) => line.split('|').map((cell) => cell.trim()).filter(Boolean));
  const [headers, ...body] = rows;
  return (
    <div className="chat-table-wrap" key={key}>
      <table>
        <thead>
          <tr>{headers.map((cell) => <th key={cell}><InlineText text={cell} /></th>)}</tr>
        </thead>
        <tbody>
          {body.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {row.map((cell, cellIndex) => <td key={`${rowIndex}-${cellIndex}`}><InlineText text={cell} /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function flushParagraph(paragraph, output, keySeed) {
  if (!paragraph.length) return;
  output.push(
    <p key={`p-${keySeed}-${output.length}`}>
      <InlineText text={paragraph.join(' ')} />
    </p>
  );
  paragraph.length = 0;
}

function renderMarkdownBlocks(text, keySeed) {
  const lines = text.split('\n');
  const output = [];
  const paragraph = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      flushParagraph(paragraph, output, keySeed);
      index += 1;
      continue;
    }
    if (/^\|.+\|$/.test(line) && /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(lines[index + 1] || '')) {
      flushParagraph(paragraph, output, keySeed);
      const tableLines = [line, lines[index + 1]];
      index += 2;
      while (index < lines.length && /^\|.+\|$/.test(lines[index])) {
        tableLines.push(lines[index]);
        index += 1;
      }
      output.push(renderTable(tableLines, `table-${keySeed}-${output.length}`));
      continue;
    }
    const heading = /^(#{1,4})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph(paragraph, output, keySeed);
      const level = heading[1].length;
      const Tag = `h${Math.min(level + 1, 5)}`;
      output.push(<Tag key={`h-${keySeed}-${output.length}`}><InlineText text={heading[2]} /></Tag>);
      index += 1;
      continue;
    }
    if (line.startsWith('> ')) {
      flushParagraph(paragraph, output, keySeed);
      const quote = [];
      while (index < lines.length && lines[index].startsWith('> ')) {
        quote.push(lines[index].slice(2));
        index += 1;
      }
      output.push(<blockquote key={`q-${keySeed}-${output.length}`}><InlineText text={quote.join(' ')} /></blockquote>);
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      flushParagraph(paragraph, output, keySeed);
      const items = [];
      while (index < lines.length && /^\s*[-*]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*]\s+/, ''));
        index += 1;
      }
      output.push(
        <ul key={`ul-${keySeed}-${output.length}`}>
          {items.map((item, itemIndex) => <li key={itemIndex}><InlineText text={item} /></li>)}
        </ul>
      );
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      flushParagraph(paragraph, output, keySeed);
      const items = [];
      while (index < lines.length && /^\s*\d+\.\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+\.\s+/, ''));
        index += 1;
      }
      output.push(
        <ol key={`ol-${keySeed}-${output.length}`}>
          {items.map((item, itemIndex) => <li key={itemIndex}><InlineText text={item} /></li>)}
        </ol>
      );
      continue;
    }
    paragraph.push(line.trim());
    index += 1;
  }
  flushParagraph(paragraph, output, keySeed);
  return output;
}

export default function Markdown({ content }) {
  const blocks = [];
  const fence = /```([A-Za-z0-9_+-]*)\n?([\s\S]*?)```/g;
  let lastIndex = 0;
  let match = fence.exec(content);
  while (match) {
    if (match.index > lastIndex) {
      blocks.push(...renderMarkdownBlocks(content.slice(lastIndex, match.index), blocks.length));
    }
    blocks.push(
      <CodeBlock
        key={`code-${blocks.length}`}
        language={match[1] || 'text'}
        code={match[2].replace(/\n$/, '')}
      />
    );
    lastIndex = match.index + match[0].length;
    match = fence.exec(content);
  }
  if (lastIndex < content.length) {
    blocks.push(...renderMarkdownBlocks(content.slice(lastIndex), blocks.length));
  }
  return <div className="chat-markdown">{blocks}</div>;
}

Markdown.propTypes = {
  content: PropTypes.string.isRequired,
};
