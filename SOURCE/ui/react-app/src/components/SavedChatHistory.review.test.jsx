import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import SavedChatHistory from './SavedChatHistory';
import { apiFetch } from '../hooks/useViolaApi';
vi.mock('../hooks/useViolaApi', () => ({ apiFetch: vi.fn() }));
class Boundary extends React.Component {
  state = { error: null };
  static getDerivedStateFromError(error) { return { error }; }
  render() { return this.state.error ? <p role="alert">Render crashed: {this.state.error.message}</p> : this.props.children; }
}
beforeEach(() => { vi.clearAllMocks(); });
it.each([{}, { threads: null }, { ok: false, error: { code: 'consent_required' } }])('does not claim empty history for missing/failed response %j', async payload => {
  apiFetch.mockResolvedValue(payload);
  render(<Boundary><SavedChatHistory /></Boundary>);
  await act(async () => {});
  expect(screen.queryByText('No saved chats yet.')).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent(/Could not load|Cloud Sync/);
  expect(screen.getByRole('button', { name: 'Retry saved chats' })).toBeInTheDocument();
});
it.each([{ threads: {} }, { threads: [null] }, { threads: [{ id: 'a', title: { bad: 'shape' } }] }])('handles malformed saved-list payload without render crash %j', async payload => {
  apiFetch.mockResolvedValue(payload);
  render(<Boundary><SavedChatHistory /></Boundary>);
  await act(async () => {});
  expect(screen.queryByText(/Render crashed:/)).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('Could not load saved chats');
  expect(screen.getByRole('button', { name: 'Retry saved chats' })).toBeInTheDocument();
});
it('handles malformed conversation content without a render crash', async () => {
  apiFetch.mockResolvedValueOnce({ threads: [{ id: 'a', title: 'Thread' }] }).mockResolvedValueOnce({ messages: [{ id: 'a', role: 'assistant', content: { bad: 'shape' } }] });
  render(<Boundary><SavedChatHistory /></Boundary>);
  fireEvent.click(await screen.findByRole('button', { name: 'Thread' }));
  await act(async () => {});
  expect(screen.queryByText(/Render crashed:/)).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('Could not load this conversation');
});
