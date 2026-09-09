import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '../../test/test-utils';
import AdvancedSettingsWindow from './AdvancedSettingsWindow';

vi.mock('../extensions/ExtensionsSection', () => ({
  default: () => <div>Extensions body</div>,
}));

const baseSettings = {
  ai_source: 'byok',
  api_port: 8756,
  log_level: 'INFO',
  routing_reasoning_effort: 'low',
  agent_reasoning_effort: 'medium',
  codex_reasoning_effort: 'medium',
};

describe('AdvancedSettingsWindow', () => {
  beforeEach(() => {
    // #4226: this window is the `system_controls` surface — on the cloud SPA
    // its whole body collapses to one DesktopUpsell card. These cases are
    // about the desktop sections, so declare the desktop surface.
    window.viola = {};
  });

  afterEach(() => {
    delete window.viola;
  });

  it('renders the shell and ordered sections', () => {
    render(
      <AdvancedSettingsWindow
        isOpen
        onClose={vi.fn()}
        settings={baseSettings}
        onSettingChange={vi.fn()}
        onSettingsChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('dialog', { name: /advanced settings/i })).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: /search advanced settings/i })).toBeInTheDocument();
    expect(screen.getByText('Extensions body')).toBeInTheDocument();
    expect(screen.getByText('Provider tuning')).toBeInTheDocument();
    expect(screen.getByText('Network')).toBeInTheDocument();
    expect(screen.getByText('Danger Zone')).toBeInTheDocument();
  });

  it('filters sections and exposes writable API port', () => {
    const onSettingChange = vi.fn();
    render(
      <AdvancedSettingsWindow
        isOpen
        onClose={vi.fn()}
        settings={baseSettings}
        onSettingChange={onSettingChange}
        onSettingsChange={vi.fn()}
      />,
    );

    fireEvent.change(screen.getByRole('searchbox', { name: /search advanced settings/i }), {
      target: { value: 'network' },
    });

    expect(screen.getByText('Network')).toBeInTheDocument();
    expect(screen.queryByText('Provider tuning')).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/api port/i), { target: { value: '8765' } });
    expect(onSettingChange).toHaveBeenCalledWith('api_port', 8765);
  });
});
