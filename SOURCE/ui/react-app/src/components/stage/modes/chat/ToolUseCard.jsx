import { useState } from 'react';
import PropTypes from 'prop-types';
import { ChevronDownIcon, ChevronRightIcon } from '../../../icons';
import { humanizeIdentifier } from '../../../../utils/humanizeIdentifier';

// Tool names arrive as the identifiers the code uses (`fill_payment_details`),
// and plugins/MCP servers can register any name at all, so they are humanised
// rather than looked up. Statuses are a closed set, so they get real words.
const TOOL_STATUS_LABELS = {
  ok: 'done',
  success: 'done',
  done: 'done',
  running: 'working',
  in_progress: 'working',
  pending: 'waiting',
  error: 'failed',
  failed: 'failed',
  cancelled: 'cancelled',
  denied: 'not allowed',
};

function formatValue(value) {
  if (value === undefined || value === null || value === '') return '(empty)';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function ToolUseCard({ tool }) {
  const [open, setOpen] = useState(false);
  const status = tool.status || 'ok';
  const title = humanizeIdentifier(tool.tool_name || tool.name) || 'Tool';
  const statusLabel = tool.progress || TOOL_STATUS_LABELS[status] || '';
  return (
    <div className={`chat-tool-card ${open ? 'is-open' : ''}`}>
      <button type="button" className="chat-tool-summary" onClick={() => setOpen((value) => !value)}>
        <span className={`chat-tool-dot status-${status}`} />
        <span className="chat-tool-name">{title}</span>
        <span className="chat-tool-status">{statusLabel}</span>
        <span className="chat-tool-chevron">{open ? <ChevronDownIcon /> : <ChevronRightIcon />}</span>
      </button>
      {open && (
        <div className="chat-tool-detail">
          <div>
            <span>Input</span>
            <pre>{formatValue(tool.tool_input)}</pre>
          </div>
          <div>
            <span>Output</span>
            <pre>{formatValue(tool.tool_output || tool.output)}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

ToolUseCard.propTypes = {
  tool: PropTypes.shape({
    tool_name: PropTypes.string,
    name: PropTypes.string,
    status: PropTypes.string,
    progress: PropTypes.string,
    step_number: PropTypes.number,
    stream_id: PropTypes.string,
    tool_input: PropTypes.any,
    tool_output: PropTypes.any,
    output: PropTypes.any,
  }).isRequired,
};
