/**
 * Inline markdown for short assistant replies shown on the stage.
 *
 * The model writes markdown (`**12**`, `[FIFA](https://...)`), and the stage
 * reply line and content cards printed it literally (1.0.5 build 5). This turns
 * the inline subset into real text: bold, italic and code become styled text,
 * a markdown link becomes its label as a tappable link, a bare URL becomes a
 * short tappable host label, and block markers (`# `, `- `, `> `) are dropped
 * or turned into a bullet. Anything unsafe stays plain text: only http(s)
 * links are ever made clickable.
 */
import { THEME } from '../config';

const INLINE = /(\*\*[^*\n]+\*\*|__[^_\n]+__|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\)|https?:\/\/[^\s)<>]+|\*[^*\s][^*\n]*\*|(?<![\w])_[^_\s][^_\n]*_(?![\w]))/g;

function isSafeHref(href) {
  try {
    const url = new URL(href);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch {
    return false;
  }
}

/** "https://www.fifa.com/en/tournaments/..." -> "fifa.com" */
export function shortLinkLabel(href) {
  try {
    return new URL(href).hostname.replace(/^www\./, '');
  } catch {
    return href;
  }
}

function linkStyle() {
  return {
    color: THEME.colors.accent,
    textDecoration: 'underline',
    textUnderlineOffset: '2px',
    cursor: 'pointer',
    touchAction: 'manipulation',
  };
}

function StageLink({ href, children }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(event) => event.stopPropagation()}
      onPointerDown={(event) => event.stopPropagation()}
      style={linkStyle()}
    >
      {children}
    </a>
  );
}

/** Remove block-level markers a one-line reply area cannot show. */
function stripBlockMarkers(line) {
  return line
    .replace(/^\s{0,3}#{1,6}\s+/, '')
    .replace(/^\s{0,3}>\s?/, '')
    .replace(/^\s*[-*+]\s+(?=\S)/, '• ')
    .replace(/^\s*(\d+)[.)]\s+(?=\S)/, '$1. ');
}

function renderInline(text, keyPrefix) {
  const parts = [];
  let lastIndex = 0;
  INLINE.lastIndex = 0;
  let match = INLINE.exec(text);
  while (match) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    let token = match[0];
    const key = `${keyPrefix}-${match.index}`;
    if (token.startsWith('**') || token.startsWith('__')) {
      parts.push(<strong key={key}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith('`')) {
      parts.push(<code key={key}>{token.slice(1, -1)}</code>);
    } else if (token.startsWith('[')) {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      if (link && isSafeHref(link[2])) {
        parts.push(<StageLink key={key} href={link[2]}>{link[1]}</StageLink>);
      } else {
        parts.push(link ? link[1] : token);
      }
    } else if (/^https?:\/\//i.test(token)) {
      // A trailing sentence period is punctuation, not part of the URL.
      const trailing = /[.,;:!?]+$/.exec(token);
      if (trailing) token = token.slice(0, -trailing[0].length);
      if (isSafeHref(token)) {
        parts.push(<StageLink key={key} href={token}>{shortLinkLabel(token)}</StageLink>);
      } else {
        parts.push(token);
      }
      if (trailing) parts.push(trailing[0]);
    } else if (token.startsWith('*') || token.startsWith('_')) {
      parts.push(<em key={key}>{token.slice(1, -1)}</em>);
    } else {
      parts.push(token);
    }
    lastIndex = match.index + match[0].length;
    match = INLINE.exec(text);
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}

/**
 * Render a stage reply. Returns the input unchanged when it is not a string,
 * so callers can pass whatever they already rendered.
 */
export function renderStageMarkdown(text) {
  if (typeof text !== 'string' || !text) return text;
  const lines = text.split('\n');
  const out = [];
  lines.forEach((line, index) => {
    if (index > 0) out.push('\n');
    out.push(...renderInline(stripBlockMarkers(line), `l${index}`));
  });
  return out;
}

/** Plain-text form (for speech, titles, aria labels). */
export function stripStageMarkdown(text) {
  if (typeof text !== 'string' || !text) return text;
  return text
    .split('\n')
    .map((line) => stripBlockMarkers(line)
      .replace(/\*\*([^*\n]+)\*\*/g, '$1')
      .replace(/__([^_\n]+)__/g, '$1')
      .replace(/`([^`\n]+)`/g, '$1')
      .replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, '$1')
      .replace(/\*([^*\s][^*\n]*)\*/g, '$1')
      .replace(/(?<![\w])_([^_\s][^_\n]*)_(?![\w])/g, '$1'))
    .join('\n');
}

export default renderStageMarkdown;
