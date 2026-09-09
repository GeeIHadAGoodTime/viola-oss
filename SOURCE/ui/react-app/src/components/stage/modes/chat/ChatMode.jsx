import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import ErrorBoundary from '../../../ErrorBoundary.jsx';
import { THEME } from '../../../../config';
import { apiFetch, buildStreamUrl } from '../../../../hooks/useViolaApi';
import { useCloudConsentGate } from '../../../../hooks/cloudConsentGate';
import { useWebSocket } from '../../../../hooks/useWebSocket';
import { isFeatureHidden } from '../../../../utils/featureSurface';
import ChatInput from './ChatInput.jsx';
import ChatSidebar from './ChatSidebar.jsx';
import ChatThread from './ChatThread.jsx';
import './ChatMode.css';

let pendingNewChatRequests = 0;
const EMPTY_COMMANDS = [];
if (typeof window !== 'undefined' && !window.__violaChatModeNewChatListener) {
  window.__violaChatModeNewChatListener = true;
  window.addEventListener('viola:chat:new', () => {
    pendingNewChatRequests += 1;
  });
}

function makeTemporaryAssistant() {
  return {
    id: `streaming-${Date.now()}`,
    role: 'assistant',
    content: '',
    status: 'streaming',
    metadata: {},
    tools: [],
  };
}

function upsertTool(tools, tool) {
  const currentTools = Array.isArray(tools) ? tools : [];
  const keyFor = (item) => `${item.stream_id || 'stream'}-${item.tool_name || item.name || 'tool'}-${item.step_number || item.step || '0'}`;
  const nextTool = { ...tool };
  const nextKey = keyFor(nextTool);
  return [
    ...currentTools.filter((item) => keyFor(item) !== nextKey),
    nextTool,
  ];
}

function normalizeMessages(messages) {
  return (messages || []).map((message) => ({
    ...message,
    metadata: message.metadata || {},
    tools: message.metadata?.tools || message.tools || [],
  }));
}

function flattenModels(modelPayload) {
  const current = modelPayload?.current_model || '';
  const provider = modelPayload?.provider_name || modelPayload?.provider || '';
  const directModels = Array.isArray(modelPayload?.models) ? modelPayload.models : null;
  const providerModels = directModels
    ? [{ name: provider || 'Current', models: directModels }]
    : (modelPayload?.providers || []);
  const models = [];
  providerModels.forEach((item) => {
    (item.models || []).forEach((model) => {
      models.push({ id: model, label: `${item.name} · ${model}` });
    });
  });
  if (current && !models.some((item) => item.id === current)) {
    models.unshift({ id: current, label: `${provider || 'Current'} · ${current}` });
  }
  const displayModels = models.map((item) => ({
    ...item,
    label: `${provider || 'Current'} - ${item.id}`,
  }));
  return { current, models: displayModels, provider };
}

function downloadMarkdown(filename, markdown) {
  const blob = new Blob([markdown || ''], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename || 'chat.md';
  link.click();
  URL.revokeObjectURL(url);
}

function isServerBackedMessage(message) {
  const id = message?.id || '';
  return Boolean(id)
    && !id.startsWith('streaming-')
    && !id.startsWith('local-user-')
    && !message?.metadata?.optimistic;
}

function dragEventHasFiles(event) {
  const dataTransfer = event?.dataTransfer;
  if (!dataTransfer) return false;
  if (dataTransfer.files?.length > 0) return true;
  return Array.from(dataTransfer.types || []).includes('Files');
}

function getLastRegenerableAssistant(messages) {
  return [...(messages || [])]
    .reverse()
    .find((message) => message.role === 'assistant' && isServerBackedMessage(message));
}

function ChatModeInner({
  handlePTTStart = () => {},
  handlePTTEnd = () => {},
  onOpenSettings = () => {},
  profileName,
  commandRegistry = null,
  commandScopeActive = false,
  // Identity of the signed-in account (or 'device' when signed out / running
  // on the local device principal). ChatMode is mounted once for the life of
  // the stage and never remounted by its parent, so without this the thread
  // list + active thread fetched under one principal silently survives a
  // sign-in/sign-out that switches the workspace identity underneath it —
  // sending into the stale thread then 404s server-side (#2395, threads are
  // correctly scoped per-principal there; the frontend just never noticed
  // the switch). A change here re-runs the boot sequence from a clean slate.
  principalKey = 'device',
}) {
  const [threads, setThreads] = useState([]);
  const [activeThreadId, setActiveThreadId] = useState(null);
  const [activeThread, setActiveThread] = useState(null);
  const [messages, setMessages] = useState([]);
  const [search, setSearch] = useState('');
  // On a phone the sidebar opens as a full-width overlay that buries the chat
  // pane (the founder's "sidebar squeezing the main pane" report). Default it
  // collapsed to a slim rail on narrow viewports; the user can still expand it.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => typeof window !== 'undefined' && window.innerWidth <= 760
  );
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [streamingMessageId, setStreamingMessageId] = useState(null);
  const [activeStreamId, setActiveStreamId] = useState(null);
  const [modelOptions, setModelOptions] = useState([]);
  const [selectedModel, setSelectedModel] = useState('');
  const [titleDraft, setTitleDraft] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const [uploadingFileCount, setUploadingFileCount] = useState(0);
  const [uploadError, setUploadError] = useState('');
  // Tier-2 cloud-sync consent gate (services/sync/consent.py) blocks
  // /v1/chat/threads with 403 consent_required until the user opts in
  // (Settings > Privacy & Data > Cloud Sync). Surface this as an explicit,
  // actionable prompt rather than an unhandled rejection / stuck "Loading
  // chats..." — a signed-in browser user has no other way to discover why
  // the Chat tab looks empty.
  const [consentRequired, setConsentRequired] = useState(false);
  // The one shared first-run consent gate (hooks/cloudConsentGate.jsx).
  const interceptCloudConsent = useCloudConsentGate();
  const eventSourceRef = useRef(null);
  const activeThreadIdRef = useRef(null);
  const activeStreamIdRef = useRef(null);
  const streamingRef = useRef(false);
  const dragDepthRef = useRef(0);
  // Bumped every time the boot effect below re-runs (mount, or a principalKey
  // change from a sign-in/sign-out account switch, #2395/C-071). loadThreads/
  // loadThread/loadModels capture the generation in effect at call time and
  // check it again once their fetch resolves -- if it has moved on, the
  // response belongs to a principal that is no longer current and is
  // dropped instead of being applied. Without this, an in-flight request
  // from the OLD principal that resolves late (a slow server response
  // outliving the switch) would silently overwrite the NEW principal's
  // already-rendered thread list/thread/messages with the old principal's
  // data -- cross-account bleed on the same install.
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    activeThreadIdRef.current = activeThreadId;
  }, [activeThreadId]);

  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);

  useEffect(() => {
    activeStreamIdRef.current = activeStreamId;
  }, [activeStreamId]);

  const loadThreads = useCallback(async (query = '') => {
    const generation = requestGenerationRef.current;
    const params = new URLSearchParams();
    if (query) params.set('search', query);
    try {
      const data = await apiFetch(`/v1/chat/threads${params.toString() ? `?${params.toString()}` : ''}`);
      // Stale: a principal switch happened while this request was in
      // flight. Return the data to the (stale) caller but do not touch
      // shared state -- the current principal's own boot has already reset
      // and refetched it (#2395/C-071).
      if (requestGenerationRef.current !== generation) return data.threads || [];
      setConsentRequired(false);
      setThreads(data.threads || []);
      return data.threads || [];
    } catch (err) {
      if (requestGenerationRef.current !== generation) return [];
      if (err?.code === 'consent_required') {
        setConsentRequired(true);
        setThreads([]);
        return [];
      }
      throw err;
    }
  }, []);

  const loadThread = useCallback(async (threadId) => {
    const generation = requestGenerationRef.current;
    if (!threadId) {
      if (requestGenerationRef.current !== generation) return;
      setActiveThread(null);
      setMessages([]);
      return;
    }
    const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(threadId)}`);
    if (requestGenerationRef.current !== generation) return;
    setActiveThread(data.thread);
    setTitleDraft(data.thread?.title || 'New chat');
    setMessages(normalizeMessages(data.messages));
  }, []);

  const loadModels = useCallback(async () => {
    const generation = requestGenerationRef.current;
    const data = await apiFetch('/v1/chat/models');
    const flattened = flattenModels(data);
    if (requestGenerationRef.current !== generation) return flattened;
    setModelOptions(flattened.models);
    setSelectedModel((current) => (
      current && flattened.models.some((item) => item.id === current)
        ? current
        : flattened.current
    ));
    return flattened;
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Advance the generation synchronously, in the same tick the effect
    // re-runs for a new principalKey -- any request issued by a PRIOR
    // generation (still captured in its own closure inside loadThreads/
    // loadThread/loadModels) is now stale and will no-op instead of
    // applying its response when it eventually resolves (#2395/C-071).
    const generation = ++requestGenerationRef.current;
    async function boot() {
      try {
        setLoading(true);
        // A principalKey change means the signed-in identity switched (sign
        // in, sign out, or account swap). Drop everything carried over from
        // the previous principal before refetching -- the previous thread
        // list/active thread/messages/in-flight stream all belong to a
        // session the server no longer recognizes for this workspace (#2395).
        if (eventSourceRef.current) {
          eventSourceRef.current.close();
          eventSourceRef.current = null;
        }
        setStreaming(false);
        streamingRef.current = false;
        setStreamingMessageId(null);
        setActiveStreamId(null);
        activeStreamIdRef.current = null;
        setThreads([]);
        setActiveThreadId(null);
        activeThreadIdRef.current = null;
        setActiveThread(null);
        setMessages([]);
        setConsentRequired(false);
        const [threadList] = await Promise.all([
          loadThreads(''),
          loadModels().catch(() => null),
        ]);
        if (cancelled || requestGenerationRef.current !== generation) return;
        if (threadList.length > 0) {
          setActiveThreadId(threadList[0].id);
          await loadThread(threadList[0].id);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void boot();
    return () => {
      cancelled = true;
      if (eventSourceRef.current) eventSourceRef.current.close();
    };
  }, [principalKey, loadModels, loadThread, loadThreads]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      loadThreads(search).catch(() => {});
    }, 180);
    return () => window.clearTimeout(timer);
  }, [loadThreads, search]);

  useWebSocket(useCallback((msg) => {
    if (!streamingRef.current || msg.type !== 'agent_progress' || !msg.payload) return;
    const payload = msg.payload;
    if (payload.terminal || !payload.tool_name) return;
    if (!payload.stream_id || payload.stream_id !== activeStreamIdRef.current) return;
    setMessages((current) => current.map((message) => {
      if (message.id !== streamingMessageId) return message;
      return { ...message, tools: upsertTool(message.tools, payload) };
    }));
  }, [streamingMessageId]));

  const ensureThread = useCallback(async () => {
    if (activeThreadIdRef.current) return activeThreadIdRef.current;
    const data = await apiFetch('/v1/chat/threads', {
      method: 'POST',
      body: JSON.stringify({ title: 'New chat', model: selectedModel || null }),
    });
    setThreads((current) => [data.thread, ...current]);
    setActiveThread(data.thread);
    setActiveThreadId(data.thread.id);
    activeThreadIdRef.current = data.thread.id;
    setTitleDraft(data.thread.title);
    setMessages([]);
    return data.thread.id;
  }, [selectedModel]);

  const refreshActiveThread = useCallback(async () => {
    if (!activeThreadIdRef.current) return;
    await loadThread(activeThreadIdRef.current);
    await loadThreads(search);
  }, [loadThread, loadThreads, search]);

  const uploadFilesToWorkbench = useCallback(async (fileList) => {
    const files = Array.from(fileList || []).filter((file) => file?.name);
    if (files.length === 0) return;

    // Workbench files live on the desktop's local disk -- desktop-only
    // (#1064). Skip the dead upload on the cloud SPA and say so plainly
    // instead of a generic "File upload failed."
    if (isFeatureHidden('workbench')) {
      setUploadError('File attachments are available in the desktop app.');
      return;
    }

    setUploadError('');
    setUploadingFileCount(files.length);

    try {
      const uploadedNames = [];
      for (const file of files) {
        const formData = new FormData();
        formData.append('file', file, file.name);
        const result = await apiFetch('/api/workbench/files', {
          method: 'POST',
          body: formData,
        });
        uploadedNames.push(result?.name || file.name);
      }

      const referenceText = uploadedNames.length === 1
        ? `Workbench file: ${uploadedNames[0]}`
        : `Workbench files:\n${uploadedNames.map((name) => `- ${name}`).join('\n')}`;
      setDraft((current) => {
        const trimmed = current.trimEnd();
        return trimmed ? `${trimmed}\n\n${referenceText}` : referenceText;
      });
      setMessages((current) => [
        ...current,
        {
          id: `local-upload-${Date.now()}`,
          role: 'assistant',
          content: uploadedNames.length === 1
            ? `Uploaded ${uploadedNames[0]} to Workbench.`
            : `Uploaded ${uploadedNames.length} files to Workbench.`,
          status: 'complete',
          metadata: { optimistic: true, upload: true },
          tools: [],
        },
      ]);
    } catch {
      setUploadError('File upload failed.');
    } finally {
      setUploadingFileCount(0);
    }
  }, []);

  const handleDragEnter = useCallback((event) => {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current += 1;
    setDragActive(true);
  }, []);

  const handleDragOver = useCallback((event) => {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
    setDragActive(true);
  }, []);

  const handleDragLeave = useCallback((event) => {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) {
      setDragActive(false);
    }
  }, []);

  const handleDrop = useCallback((event) => {
    if (!dragEventHasFiles(event)) return;
    event.preventDefault();
    event.stopPropagation();
    dragDepthRef.current = 0;
    setDragActive(false);
    uploadFilesToWorkbench(event.dataTransfer.files).catch((err) => {
      console.error('[ChatMode] Drag-drop file upload failed:', err);
    });
  }, [uploadFilesToWorkbench]);

  const attachStream = useCallback(async (streamId, assistantMessageId) => {
    setActiveStreamId(streamId);
    activeStreamIdRef.current = streamId;
    const streamUrl = await buildStreamUrl(streamId);
    const source = new EventSource(streamUrl, { withCredentials: true });
    eventSourceRef.current = source;
    source.onmessage = (event) => {
      let payload = null;
      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }
      if (payload.tool) {
        setMessages((current) => current.map((message) => (
          message.id === assistantMessageId
            ? { ...message, tools: upsertTool(message.tools, payload.tool) }
            : message
        )));
      }
      if (payload.token) {
        setMessages((current) => current.map((message) => (
          message.id === assistantMessageId
            ? { ...message, content: `${message.content}${payload.token}` }
            : message
        )));
      }
      if (payload.done || payload.error) {
        source.close();
        eventSourceRef.current = null;
        setStreaming(false);
        setStreamingMessageId(null);
        setActiveStreamId(null);
        activeStreamIdRef.current = null;
        const finalContent = payload.content || payload.message || (payload.error ? 'Something went wrong while generating the response.' : '');
        setMessages((current) => current.map((message) => {
          if (message.id !== assistantMessageId) return message;
          const metadata = {
            ...(message.metadata || {}),
            ...(payload.streaming_mode ? {
              streaming: {
                mode: payload.streaming_mode,
                token_count: payload.token_count || 0,
                native: !payload.fallback,
              },
            } : {}),
          };
          return {
            ...message,
            content: finalContent || message.content,
            status: payload.error ? 'error' : 'complete',
            metadata,
          };
        }));
        window.setTimeout(() => {
          refreshActiveThread().catch(() => {});
        }, 120);
      }
    };
    source.onerror = () => {
      source.close();
      eventSourceRef.current = null;
      setStreaming(false);
      setStreamingMessageId(null);
      setActiveStreamId(null);
      activeStreamIdRef.current = null;
      setMessages((current) => current.map((message) => (
        message.id === assistantMessageId
          ? {
            ...message,
            content: message.content || 'The live response connection dropped.',
            status: 'error',
          }
          : message
      )));
      refreshActiveThread().catch(() => {});
    };
  }, [refreshActiveThread]);

  const sendText = useCallback(async (text) => {
    const clean = text.trim();
    if (!clean || streamingRef.current) return;
    // The SAME first-run consent gate every other turn entry point uses. This
    // composer had none: a brand-new cloud user who opened Chat first could not
    // run a single agent command, was never prompted, and saw nothing at all
    // (see the ensureThread note below). The gate resumes this exact message
    // once they accept, so the turn they asked for is not dropped.
    if (interceptCloudConsent({ kind: 'chat', text: clean, resume: () => sendText(clean) })) {
      setDraft('');
      return;
    }
    const temporaryAssistant = makeTemporaryAssistant();
    setDraft('');
    setStreaming(true);
    setStreamingMessageId(temporaryAssistant.id);
    setMessages((current) => [
      ...current,
      {
        id: `local-user-${Date.now()}`,
        role: 'user',
        content: clean,
        status: 'complete',
        metadata: { optimistic: true },
      },
      temporaryAssistant,
    ]);
    const postSend = (id) => apiFetch(`/v1/chat/threads/${encodeURIComponent(id)}/send`, {
      method: 'POST',
      body: JSON.stringify({ text: clean, model: selectedModel || null }),
    });
    try {
      // ensureThread() used to be awaited ABOVE this try, and `onSend` does not
      // catch, so a thread-creation refusal (403 consent_required for a user
      // who has not enabled cloud sync) escaped as an unhandled promise
      // rejection: the typed message vanished with no reply, no error, and no
      // prompt. Inside the try it becomes a message the user can act on.
      const threadId = await ensureThread();
      let sendResult;
      try {
        sendResult = await postSend(threadId);
      } catch (err) {
        // The thread we sent into doesn't exist server-side anymore -- most
        // commonly because the sidebar was still showing a thread created
        // under a previous signed-in/device principal (#2395; threads are
        // correctly scoped per-principal, the frontend just hadn't dropped
        // the stale one yet). Recover instead of dead-ending the user:
        // forget the dead thread and send into a fresh one.
        if (err?.status === 404 && err?.code === 'chat_thread_not_found') {
          setThreads((current) => current.filter((item) => item.id !== threadId));
          activeThreadIdRef.current = null;
          setActiveThreadId(null);
          setActiveThread(null);
          const freshThreadId = await ensureThread();
          sendResult = await postSend(freshThreadId);
        } else {
          throw err;
        }
      }
      await attachStream(sendResult.stream_id, temporaryAssistant.id);
    } catch (err) {
      setStreaming(false);
      setStreamingMessageId(null);
      setActiveStreamId(null);
      activeStreamIdRef.current = null;
      // Say the real reason when the server gave one. A refusal the server can
      // name ("you have not enabled cloud sync", "Viola's AI is not turned on
      // for this account") is actionable; "Something went wrong" sends the user
      // hunting for a fault that does not exist.
      let failure = 'Something went wrong while sending that message.';
      if (err?.code === 'consent_required') {
        setConsentRequired(true);
        failure = 'Viola needs your OK to store this conversation before it can reply. Turn on Cloud Sync in Settings, under Privacy and Data.';
      } else if (err?.code === 'cloud_consent_required' || err?.code === 'cloud_consent_unavailable') {
        failure = "Viola's AI is not turned on for this account yet. Start a turn again and accept the prompt to turn it on.";
      }
      setMessages((current) => current.map((message) => (
        message.id === temporaryAssistant.id
          ? { ...message, content: failure, status: 'error' }
          : message
      )));
    }
  }, [attachStream, ensureThread, selectedModel, interceptCloudConsent]);

  const stopStreaming = useCallback(async () => {
    const stoppedMessageId = streamingMessageId;
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    const streamId = activeStreamId;
    setStreaming(false);
    setStreamingMessageId(null);
    setActiveStreamId(null);
    activeStreamIdRef.current = null;
    if (stoppedMessageId) {
      setMessages((current) => current.map((message) => (
        message.id === stoppedMessageId
          ? {
            ...message,
            content: message.content || 'Stopped.',
            status: 'stopped',
          }
          : message
      )));
    }
    if (streamId) {
      await apiFetch(`/v1/chat/streams/${encodeURIComponent(streamId)}/cancel`, { method: 'POST' }).catch((err) => {
        console.error('[ChatMode] Stream cancel request failed; stream may keep running server-side:', err);
      });
      window.setTimeout(() => {
        refreshActiveThread().catch(() => {});
      }, 120);
    }
  }, [activeStreamId, refreshActiveThread, streamingMessageId]);

  const createNewChat = useCallback(async () => {
    const data = await apiFetch('/v1/chat/threads', {
      method: 'POST',
      body: JSON.stringify({ title: 'New chat', model: selectedModel || null }),
    });
    setThreads((current) => [data.thread, ...current]);
    setActiveThreadId(data.thread.id);
    activeThreadIdRef.current = data.thread.id;
    setActiveThread(data.thread);
    setTitleDraft(data.thread.title);
    setMessages([]);
  }, [selectedModel]);

  useEffect(() => {
    let cancelled = false;
    async function consumePending() {
      if (pendingNewChatRequests <= 0) return;
      pendingNewChatRequests = 0;
      await createNewChat();
    }
    consumePending().catch(() => {
      if (!cancelled) pendingNewChatRequests = 1;
    });
    const handleNewChat = () => {
      pendingNewChatRequests = Math.max(0, pendingNewChatRequests - 1);
      createNewChat().catch(() => {});
    };
    window.addEventListener('viola:chat:new', handleNewChat);
    return () => {
      cancelled = true;
      window.removeEventListener('viola:chat:new', handleNewChat);
    };
  }, [createNewChat]);

  const selectThread = useCallback(async (threadId) => {
    if (streamingRef.current) await stopStreaming();
    setActiveThreadId(threadId);
    activeThreadIdRef.current = threadId;
    await loadThread(threadId);
  }, [loadThread, stopStreaming]);

  const renameThread = useCallback(async (thread, nextTitle) => {
    const title = nextTitle ?? window.prompt('Rename chat', thread.title || 'New chat');
    if (!title || !title.trim()) return;
    const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}`, {
      method: 'PATCH',
      body: JSON.stringify({ title: title.trim() }),
    });
    setThreads((current) => current.map((item) => (item.id === thread.id ? data.thread : item)));
    if (activeThreadIdRef.current === thread.id) {
      setActiveThread(data.thread);
      setTitleDraft(data.thread.title);
    }
  }, []);

  const deleteThread = useCallback(async (thread) => {
    if (!window.confirm(`Delete "${thread.title || 'New chat'}"?`)) return;
    await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}`, { method: 'DELETE' });
    const nextThreads = threads.filter((item) => item.id !== thread.id);
    setThreads(nextThreads);
    if (activeThreadIdRef.current === thread.id) {
      const next = nextThreads[0];
      setActiveThreadId(next?.id || null);
      activeThreadIdRef.current = next?.id || null;
      if (next) await loadThread(next.id);
      else {
        setActiveThread(null);
        setMessages([]);
      }
    }
  }, [loadThread, threads]);

  const exportThread = useCallback(async (thread = activeThread) => {
    if (!thread) return;
    const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}/export`);
    downloadMarkdown(data.filename, data.markdown);
  }, [activeThread]);

  const submitTitle = useCallback(async () => {
    if (!activeThread || !titleDraft.trim() || titleDraft.trim() === activeThread.title) return;
    await renameThread(activeThread, titleDraft.trim());
  }, [activeThread, renameThread, titleDraft]);

  const handleModelChange = useCallback(async (event) => {
    const model = event.target.value;
    setSelectedModel(model);
    if (activeThreadIdRef.current) {
      const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(activeThreadIdRef.current)}`, {
        method: 'PATCH',
        body: JSON.stringify({ model }),
      });
      setActiveThread(data.thread);
      setThreads((current) => current.map((item) => (item.id === data.thread.id ? data.thread : item)));
    }
  }, []);

  const handleRegenerate = useCallback(async (message) => {
    if (!activeThreadIdRef.current || streamingRef.current) return;
    setStreaming(true);
    setStreamingMessageId(message.id);
    setMessages((current) => current.map((item) => (
      item.id === message.id
        ? { ...item, content: '', status: 'streaming', tools: [] }
        : item
    )));
    try {
      const data = await apiFetch(
        `/v1/chat/threads/${encodeURIComponent(activeThreadIdRef.current)}/regenerate/${encodeURIComponent(message.id)}`,
        {
          method: 'POST',
          body: JSON.stringify({ model: selectedModel || null }),
        }
      );
      setActiveThread(data.thread);
      setTitleDraft(data.thread?.title || 'New chat');
      setMessages(normalizeMessages(data.messages));
      await attachStream(data.stream_id, message.id);
    } catch {
      setStreaming(false);
      setStreamingMessageId(null);
      setActiveStreamId(null);
      activeStreamIdRef.current = null;
      setMessages((current) => current.map((item) => (
        item.id === message.id
          ? { ...item, content: 'Something went wrong while regenerating this response.', status: 'error' }
          : item
      )));
    }
  }, [attachStream, selectedModel]);

  const handleFork = useCallback(async (message) => {
    const startingText = message.role === 'user'
      ? message.content
      : [...messages.slice(0, messages.findIndex((item) => item.id === message.id))]
        .reverse()
        .find((item) => item.role === 'user')?.content || '';
    const content = window.prompt('Edit message and branch', startingText);
    if (!content || !activeThreadIdRef.current) return;
    const sourceMessageId = message.role === 'user'
      ? message.id
      : [...messages.slice(0, messages.findIndex((item) => item.id === message.id))]
        .reverse()
        .find((item) => item.role === 'user')?.id;
    if (!sourceMessageId) return;
    const data = await apiFetch(
      `/v1/chat/threads/${encodeURIComponent(activeThreadIdRef.current)}/messages/${encodeURIComponent(sourceMessageId)}/fork`,
      {
        method: 'POST',
        body: JSON.stringify({ content, model: selectedModel || null }),
      }
    );
    await loadThreads();
    const temporaryAssistant = makeTemporaryAssistant();
    setActiveThreadId(data.thread.id);
    activeThreadIdRef.current = data.thread.id;
    setActiveThread(data.thread);
    setTitleDraft(data.thread.title);
    setStreaming(true);
    setStreamingMessageId(temporaryAssistant.id);
    setMessages([...normalizeMessages(data.messages), temporaryAssistant]);
    await attachStream(data.stream_id, temporaryAssistant.id);
  }, [attachStream, loadThreads, messages, selectedModel]);

  const handleFeedback = useCallback(async (message, rating) => {
    if (!activeThreadIdRef.current) return;
    const data = await apiFetch(
      `/v1/chat/threads/${encodeURIComponent(activeThreadIdRef.current)}/messages/${encodeURIComponent(message.id)}/feedback`,
      {
        method: 'POST',
        body: JSON.stringify({ rating }),
      }
    );
    setMessages((current) => current.map((item) => (item.id === message.id ? data.message : item)));
  }, []);

  const lastRegenerableMessage = useMemo(
    () => (streaming ? null : getLastRegenerableAssistant(messages)),
    [messages, streaming]
  );
  const registerCommands = commandRegistry?.registerCommands;
  const activeChatCommands = useMemo(() => {
    const commands = [];
    if (streaming) {
      commands.push({
        id: 'chat.stop-streaming',
        label: 'Stop streaming',
        group: 'Chat',
        keywords: ['cancel', 'response', 'generation'],
        perform: () => {
          stopStreaming().catch(() => {});
        },
      });
    }
    if (lastRegenerableMessage) {
      commands.push({
        id: 'chat.regenerate-last-message',
        label: 'Regenerate last message',
        group: 'Chat',
        keywords: ['retry', 'rerun', 'assistant', 'response'],
        perform: () => {
          handleRegenerate(lastRegenerableMessage).catch(() => {});
        },
      });
    }
    if (activeThread) {
      commands.push({
        id: 'chat.export-current-thread',
        label: 'Export current thread',
        group: 'Chat',
        keywords: ['download', 'markdown', 'conversation'],
        perform: () => {
          exportThread().catch(() => {});
        },
      });
    }
    commands.push({
      id: 'chat.toggle-sidebar',
      label: sidebarCollapsed ? 'Show all chats' : 'Hide chat list',
      group: 'Chat',
      keywords: ['sidebar', 'threads', 'conversations', 'toggle'],
      perform: () => setSidebarCollapsed((value) => !value),
    });
    return commands;
  }, [
    activeThread,
    exportThread,
    handleRegenerate,
    lastRegenerableMessage,
    sidebarCollapsed,
    stopStreaming,
    streaming,
  ]);
  const registeredChatCommands = commandScopeActive ? activeChatCommands : EMPTY_COMMANDS;

  useEffect(() => {
    if (typeof registerCommands !== 'function') return undefined;
    return registerCommands('stage.mode.chat.context', registeredChatCommands);
  }, [registeredChatCommands, registerCommands]);

  const accentStyle = useMemo(() => ({
    '--chat-theme-bg': THEME.colors.bgCard,
    '--chat-theme-surface': THEME.colors.bgSurface,
    '--chat-theme-elevated': THEME.colors.bgElevated,
    '--chat-theme-border': THEME.colors.borderHover,
    '--chat-theme-text': THEME.colors.textPrimary,
    '--chat-theme-muted': THEME.colors.textSecondary,
  }), []);

  return (
    <div
      className={`chat-mode ${dragActive ? 'is-drag-active' : ''}`}
      style={accentStyle}
      data-testid="chat-mode"
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {dragActive && (
        <div className="chat-drop-overlay" data-testid="chat-file-drop-overlay" aria-hidden="true">
          <span>Drop files</span>
        </div>
      )}
      <ChatSidebar
        threads={threads}
        activeThreadId={activeThreadId}
        search={search}
        collapsed={sidebarCollapsed}
        onSearch={setSearch}
        onToggleCollapsed={() => setSidebarCollapsed((value) => !value)}
        onNewChat={createNewChat}
        onSelectThread={selectThread}
        onRename={renameThread}
        onDelete={deleteThread}
        onExport={exportThread}
        onOpenSettings={onOpenSettings}
        profileName={profileName}
      />
      <main className="chat-main">
        {consentRequired && (
          <div className="chat-consent-banner" data-testid="chat-consent-required">
            <span>
              Chat needs Cloud Sync turned on to store your conversations. Enable it in
              Settings to use Chat.
            </span>
            <button type="button" onClick={onOpenSettings}>Open Settings</button>
          </div>
        )}
        <header className="chat-topbar">
          <input
            className="chat-title-input"
            value={titleDraft}
            onChange={(event) => setTitleDraft(event.target.value)}
            onBlur={submitTitle}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                event.currentTarget.blur();
              }
            }}
            disabled={!activeThread}
            aria-label="Conversation title"
          />
          <div className="chat-topbar-actions">
            <select
              value={selectedModel}
              onChange={handleModelChange}
              onFocus={() => { loadModels().catch(() => {}); }}
              aria-label="Model"
            >
              <option value="">Default model</option>
              {modelOptions.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
            <button type="button" onClick={() => exportThread()} disabled={!activeThread}>Export</button>
          </div>
        </header>
        <ChatThread
          messages={messages}
          streamingMessageId={streamingMessageId}
          onSuggestion={(suggestion) => {
            setDraft(suggestion);
            void sendText(suggestion);
          }}
          onRegenerate={handleRegenerate}
          onFork={handleFork}
          onFeedback={handleFeedback}
        />
        <div className="chat-composer-row">
          {loading && <span className="chat-loading">Loading chats...</span>}
          {uploadingFileCount > 0 && (
            <span className="chat-upload-status">
              Uploading {uploadingFileCount} {uploadingFileCount === 1 ? 'file' : 'files'}...
            </span>
          )}
          {uploadError && <span className="chat-upload-status is-error">{uploadError}</span>}
          <ChatInput
            value={draft}
            onChange={setDraft}
            onSend={() => sendText(draft)}
            onStop={stopStreaming}
            onAttachFiles={uploadFilesToWorkbench}
            streaming={streaming}
            disabled={loading}
            handlePTTStart={handlePTTStart}
            handlePTTEnd={handlePTTEnd}
          />
        </div>
      </main>
    </div>
  );
}

ChatModeInner.propTypes = {
  handlePTTStart: PropTypes.func,
  handlePTTEnd: PropTypes.func,
  onOpenSettings: PropTypes.func,
  profileName: PropTypes.string,
  commandRegistry: PropTypes.shape({
    registerCommands: PropTypes.func.isRequired,
  }),
  commandScopeActive: PropTypes.bool,
  principalKey: PropTypes.string,
};

export default function ChatMode(props) {
  return (
    <ErrorBoundary name="ChatMode">
      <ChatModeInner {...props} />
    </ErrorBoundary>
  );
}
