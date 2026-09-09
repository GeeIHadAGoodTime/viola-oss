/* eslint react/jsx-uses-vars: "error" */
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '../test/test-utils';
import WorkbenchDropZone from './WorkbenchDropZone';
import WorkbenchKindIcon from './WorkbenchKindIcon';
import WorkbenchToast from './WorkbenchToast';

function jsonResponse(payload, ok = true, status = 200) {
  return {
    ok,
    status,
    text: () => Promise.resolve(JSON.stringify(payload)),
    blob: () => Promise.resolve(new Blob([typeof payload === 'string' ? payload : JSON.stringify(payload)])),
  };
}

describe('WorkbenchKindIcon', () => {
  it('renders a titled icon for a known kind', () => {
    render(<WorkbenchKindIcon kind="resume" title="Resume" />);
    expect(screen.getByRole('img', { name: 'Resume' })).toBeInTheDocument();
  });
});

describe('WorkbenchToast', () => {
  it('renders ingest toast with filename and delete action', () => {
    render(
      <WorkbenchToast
        toasts={[
          {
            id: 1,
            type: 'ingest',
            result: { item_id: 'kn_1', filename: 'resume.pdf', byte_size: 12345 },
          },
        ]}
        onDismiss={vi.fn()}
        onError={vi.fn()}
      />
    );
    expect(screen.getByText(/Saved resume.pdf/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /delete/i })).toBeInTheDocument();
  });
});

describe('WorkbenchDropZone', () => {
  it('uploads a dropped file through the legacy Workbench endpoint', async () => {
    const fetchSpy = vi.fn(() =>
      Promise.resolve(
        jsonResponse({
          data: { item_id: 'kn_1', filename: 'resume.pdf', byte_size: 6 },
        })
      )
    );
    vi.stubGlobal('fetch', fetchSpy);

    render(<WorkbenchDropZone addToast={vi.fn()} />);

    const file = new File(['resume'], 'resume.pdf', { type: 'application/pdf' });
    fireEvent.dragEnter(document.body, { dataTransfer: { types: ['Files'] } });
    expect(screen.getByRole('region', { name: /workbench ingest/i })).toBeInTheDocument();

    fireEvent.drop(document.body, {
      dataTransfer: {
        types: ['Files'],
        files: [file],
      },
    });

    await waitFor(() =>
      expect(fetchSpy).toHaveBeenCalledWith(
        '/v1/knowledge',
        expect.objectContaining({ method: 'POST', body: expect.any(FormData) })
      )
    );
    expect(await screen.findByText(/Saved resume.pdf/i)).toBeInTheDocument();
  });

  it('prompts the user when the server reports a filename clash', async () => {
    let calls = 0;
    const fetchSpy = vi.fn(() => {
      calls += 1;
      if (calls === 1) {
        return Promise.resolve(
          jsonResponse({
            data: {
              action_required: 'filename_clash',
              filename: 'resume.pdf',
              existing: { item_id: 'kn_old', filename: 'resume.pdf', size_label: '12 KB' },
            },
          })
        );
      }
      return Promise.resolve(
        jsonResponse({
          data: { item_id: 'kn_new', filename: 'resume.pdf', replaced_id: 'kn_old', clash_outcome: 'replaced' },
        })
      );
    });
    vi.stubGlobal('fetch', fetchSpy);

    const { user } = render(<WorkbenchDropZone addToast={vi.fn()} />);

    const file = new File(['resume2'], 'resume.pdf', { type: 'application/pdf' });
    fireEvent.drop(document.body, {
      dataTransfer: { types: ['Files'], files: [file] },
    });

    expect(await screen.findByRole('dialog', { name: /existing file/i })).toBeInTheDocument();
    expect(screen.getByText(/You already have/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^replace$/i }));

    await waitFor(() => expect(calls).toBe(2));
    const secondBody = fetchSpy.mock.calls[1][1].body;
    expect(secondBody.get('on_filename_clash')).toBe('replace');
  });
});
