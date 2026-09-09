import { useCallback, useMemo, useState } from 'react';

function dedupeCommands(commands) {
  const seen = new Set();
  return commands.filter((command) => {
    if (!command?.id || seen.has(command.id)) return false;
    seen.add(command.id);
    return true;
  });
}

export default function useCommandRegistry(baseCommands = []) {
  const [contributions, setContributions] = useState({});

  const registerCommands = useCallback((sourceId, commands = []) => {
    if (!sourceId) return () => {};
    setContributions((current) => ({
      ...current,
      [sourceId]: Array.isArray(commands) ? commands : [],
    }));
    return () => {
      setContributions((current) => {
        if (!Object.prototype.hasOwnProperty.call(current, sourceId)) return current;
        const next = { ...current };
        delete next[sourceId];
        return next;
      });
    };
  }, []);

  const commands = useMemo(() => {
    const contributed = Object.keys(contributions)
      .sort()
      .flatMap((sourceId) => contributions[sourceId]);
    return dedupeCommands([...baseCommands, ...contributed]);
  }, [baseCommands, contributions]);

  return {
    commands,
    registerCommands,
  };
}
