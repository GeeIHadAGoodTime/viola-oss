/**
 * Test-only mount for the #2607 hermetic Playwright proof. NOT imported by
 * main.jsx/App.jsx/SmartDisplay.jsx -- production wiring is untouched. Wires
 * the real CloudWelcome.jsx + useCloudWelcome.js exactly the way
 * SmartDisplay.jsx does (open when `completed === false`; finish/skip both
 * call `complete()` then close), so this harness proves the actual
 * production contract against a hermetic backend
 * (tests/e2e/web/cloud_welcome/backend_harness.py). See
 * cloud-welcome-harness.html for how this gets served (vite dev only, never
 * part of `npm run build`).
 */
import React, { useCallback, useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';
import CloudWelcome from '../components/CloudWelcome';
import { useCloudWelcome } from '../hooks/useCloudWelcome';

function Harness() {
  const cloudWelcome = useCloudWelcome({ enabled: true });
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (cloudWelcome.completed === false) setOpen(true);
  }, [cloudWelcome.completed]);

  const handleFinish = useCallback(async () => {
    await cloudWelcome.complete();
    setOpen(false);
  }, [cloudWelcome]);

  const handleSkip = useCallback(async () => {
    await cloudWelcome.complete();
    setOpen(false);
  }, [cloudWelcome]);

  return (
    <div style={{ padding: 24, fontFamily: 'sans-serif', color: '#fff', background: '#000', minHeight: '100vh' }}>
      <div data-testid="harness-status">
        completed={String(cloudWelcome.completed)} loading={String(cloudWelcome.loading)} saving={String(cloudWelcome.saving)}
      </div>
      <CloudWelcome isOpen={open} saving={cloudWelcome.saving} onFinish={handleFinish} onSkip={handleSkip} />
    </div>
  );
}

const rootElement = document.getElementById('root');
if (rootElement) {
  ReactDOM.createRoot(rootElement).render(<Harness />);
}
