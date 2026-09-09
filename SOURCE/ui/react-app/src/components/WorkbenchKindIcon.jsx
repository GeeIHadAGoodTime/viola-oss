import PropTypes from 'prop-types';
import { THEME } from '../config';

export const WORKBENCH_KINDS = [
  'resume',
  'id_card',
  'insurance',
  'recipe',
  'note',
  'photo',
  'email',
  'webpage',
  'receipt',
  'lease',
  'doc',
  'transcript',
];

export const WORKBENCH_KIND_LABELS = {
  resume: 'resume',
  id_card: 'ID card',
  insurance: 'insurance',
  recipe: 'recipe',
  note: 'note',
  photo: 'photo',
  email: 'email',
  webpage: 'webpage',
  receipt: 'receipt',
  lease: 'lease',
  doc: 'document',
  transcript: 'transcript',
};

function getKindColor(kind) {
  if (kind === 'receipt' || kind === 'recipe') return THEME.colors.statusGreen;
  if (kind === 'id_card' || kind === 'insurance' || kind === 'lease') return THEME.colors.statusYellow;
  if (kind === 'email' || kind === 'webpage') return THEME.colors.textSecondary;
  return THEME.colors.accent;
}

const iconPaths = {
  resume: (
    <>
      <rect x="5" y="7" width="14" height="11" rx="2" />
      <path d="M9 7V5.8A1.8 1.8 0 0 1 10.8 4h2.4A1.8 1.8 0 0 1 15 5.8V7" />
      <path d="M5 11h14" />
      <path d="M10 11v1h4v-1" />
    </>
  ),
  id_card: (
    <>
      <rect x="4" y="6" width="16" height="12" rx="2" />
      <circle cx="9" cy="11" r="2" />
      <path d="M7 15c.8-1 3.2-1 4 0" />
      <path d="M13 10h4" />
      <path d="M13 14h3" />
    </>
  ),
  insurance: (
    <>
      <path d="M12 4 18 6v5c0 4-2.4 6.7-6 8-3.6-1.3-6-4-6-8V6l6-2Z" />
      <path d="M9 12l2 2 4-5" />
    </>
  ),
  recipe: (
    <>
      <path d="M8 4v16" />
      <path d="M5 4v4a3 3 0 0 0 6 0V4" />
      <path d="M16 4v16" />
      <path d="M16 4c2 1.5 3 3.3 3 5.5S18 13 16 14" />
    </>
  ),
  note: (
    <>
      <path d="M7 4h8l3 3v13H7z" />
      <path d="M15 4v4h4" />
      <path d="M9 11h6" />
      <path d="M9 14h6" />
      <path d="M9 17h4" />
    </>
  ),
  photo: (
    <>
      <rect x="4" y="5" width="16" height="14" rx="2" />
      <circle cx="9" cy="10" r="1.5" />
      <path d="m6 17 4-4 3 3 2-2 3 3" />
    </>
  ),
  email: (
    <>
      <rect x="4" y="6" width="16" height="12" rx="2" />
      <path d="m5 8 7 5 7-5" />
    </>
  ),
  webpage: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M4 12h16" />
      <path d="M12 4c2 2.2 3 4.8 3 8s-1 5.8-3 8" />
      <path d="M12 4c-2 2.2-3 4.8-3 8s1 5.8 3 8" />
    </>
  ),
  receipt: (
    <>
      <path d="M7 4h10v16l-2-1-2 1-2-1-2 1-2-1z" />
      <path d="M9 9h6" />
      <path d="M9 12h6" />
      <path d="M9 15h4" />
    </>
  ),
  lease: (
    <>
      <circle cx="8.5" cy="11.5" r="3.5" />
      <path d="m11 14 7 7" />
      <path d="m16 19 2-2" />
      <path d="m14 17 2-2" />
    </>
  ),
  transcript: (
    <>
      <path d="M7 4h10v16H7z" />
      <path d="M10 8h4" />
      <path d="M9 12h6" />
      <path d="M9 15h6" />
      <path d="M9 18h4" />
    </>
  ),
  doc: (
    <>
      <path d="M7 4h7l4 4v12H7z" />
      <path d="M14 4v5h4" />
      <path d="M9 13h6" />
      <path d="M9 16h5" />
    </>
  ),
};

export default function WorkbenchKindIcon({ kind, size = 22, title = '' }) {
  const resolvedKind = iconPaths[kind] ? kind : 'doc';
  const color = getKindColor(resolvedKind);

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? 'img' : undefined}
    >
      {title && <title>{title}</title>}
      {iconPaths[resolvedKind]}
    </svg>
  );
}

WorkbenchKindIcon.propTypes = {
  kind: PropTypes.string,
  size: PropTypes.number,
  title: PropTypes.string,
};
