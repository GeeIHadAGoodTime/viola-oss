import { useEffect, useState } from 'react';
import { authFetch } from './useViolaApi';
import { isFeatureHidden } from '../utils/featureSurface';

/**
 * The word the user says to reach Viola: "Viola", or the one they trained. For text
 * about the wake word only; her name is Viola everywhere else. The browser's hands-free
 * path always listens for "Viola", so on the cloud this stays "Viola".
 */
export default function useWakeWordName() {
  const [name, setName] = useState('Viola');

  useEffect(() => {
    if (isFeatureHidden('wake_models')) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const response = await authFetch('/v1/wake/models');
        const payload = await response.json();
        const active = (payload?.data?.models || []).find((model) => model.is_active);
        if (!cancelled && active?.name) {
          setName(active.name.charAt(0).toUpperCase() + active.name.slice(1));
        }
      } catch {
        // The built-in word is the safe thing to show.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return name;
}
