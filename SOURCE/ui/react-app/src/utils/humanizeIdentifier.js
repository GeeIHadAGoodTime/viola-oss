// Turn an internal identifier into words a person can read.
//
// Why this exists (#4421): names chosen for code keep reaching the screen as if
// they were English. A Content Card was titled `answer` because that is the
// pipeline's intent slug, and the chat tool cards are titled with raw tool
// names like `fill_payment_details`. Where the set of identifiers is small and
// known, write the words out in a lookup instead (see WAKE_STATUS_LABELS in
// components/voice/BottomRow.jsx). Use this only where the set is genuinely
// open-ended -- tool names, for instance, come from plugins and MCP servers we
// have never seen, so no lookup can cover them.
//
// Returns '' for anything that is not a usable string, so a caller can fall
// back to its own wording rather than print a slug.

// Words that look wrong in lower case once the slug is broken apart.
const ALWAYS_UPPER = new Set([
  'api', 'cpu', 'css', 'dns', 'html', 'http', 'https', 'id', 'ip', 'json',
  'llm', 'mcp', 'ocr', 'pdf', 'qr', 'sms', 'sql', 'ssl', 'stt', 'tts', 'ui',
  'url', 'usb', 'uuid', 'vad', 'xml',
]);

export function humanizeIdentifier(value) {
  if (typeof value !== 'string') return '';

  const spaced = value
    .trim()
    // snake_case, kebab-case, dotted.paths and namespaced::names all break on
    // their separator.
    .replace(/[_\-.:/\\]+/g, ' ')
    // camelCase and PascalCase break between the case change.
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/\s+/g, ' ')
    .trim();

  if (!spaced) return '';

  const words = spaced.split(' ').map((word) => {
    const lower = word.toLowerCase();
    return ALWAYS_UPPER.has(lower) ? lower.toUpperCase() : lower;
  });

  // Sentence case: only the first word is capitalised, so a tool name reads
  // like a short phrase rather than a Title Bar.
  const [first, ...rest] = words;
  const head = ALWAYS_UPPER.has(first.toLowerCase())
    ? first
    : first.charAt(0).toUpperCase() + first.slice(1);

  return [head, ...rest].join(' ');
}

export default humanizeIdentifier;
