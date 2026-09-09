import React from 'react';
import { THEME } from '../../config';

const theme = THEME;

/**
 * Injects scoped CSS used by the Settings modal.
 *
 * NOTE: The base scrollbar styling is now defined globally in
 * styles/variables.css (canonical, used everywhere). The `.viola-scrollbar`
 * class name kept here is a no-op visual — elements using it inherit the
 * global rules. What this component still provides:
 *   - `.viola-scrollbar-auto` — hover-reveal scrollbar variant for modals
 *     that want the thumb hidden until pointer-over.
 *   - `@keyframes spin` — used by loading spinners elsewhere in settings.
 */
const ScrollbarStyles = React.memo(() => (
  <style>{`
    /* Hover-only scrollbar: thumb hidden until pointer enters the host. */
    .viola-scrollbar-auto::-webkit-scrollbar-thumb {
      background: transparent;
    }
    .viola-scrollbar-auto:hover::-webkit-scrollbar-thumb {
      background: ${theme.colors.glassActive};
    }
    .viola-scrollbar-auto:hover::-webkit-scrollbar-thumb:hover {
      background: ${theme.colors.textFaint};
    }

    /* Spinner animation for LLM validation + loading states. */
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
  `}</style>
));

export default ScrollbarStyles;
