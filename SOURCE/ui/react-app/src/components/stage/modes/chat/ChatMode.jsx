import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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

function createThreadListRead() {
  let retire;
  const retired = new Promise(resolve => { retire = resolve; });
  return { retired, retire, promise: null };
}

let pendingNewChatRequests = 0;
const EMPTY_COMMANDS = [];
const STREAM_STATUS_POLL_MS = 2000;
const STREAM_STATUS_TIMEOUT_MS = 5000;
const STREAM_STATUS_MAX_FAILURES = 3;
const MODEL_SAVE_TIMEOUT_MS = 15000;
const THREAD_READ_TIMEOUT_MS = 15000;
const STREAM_ACCEPTANCE_WARNING_MS = 15000;
if (typeof window !== 'undefined' && !window.__violaChatModeNewChatListener) {
  window.__violaChatModeNewChatListener = true;
  window.addEventListener('viola:chat:new', () => {
    pendingNewChatRequests += 1;
  });
}

function isUnconfirmedRequest(error) {
  return !error?.status || error.status >= 500;
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
  const [streamNotice, setStreamNotice] = useState(null);
  const streamSessionRef = useRef(null);
  const streamAttachRef = useRef(null);
  const threadRequestRef = useRef(0);
  const threadReadRef = useRef(null);
  const [threadReadState, setThreadReadState] = useState(null);
  const pendingStopRef = useRef(false);
  const [modelOptions, setModelOptions] = useState([]);
  const [modelError, setModelError] = useState('');
  const [modelsLoading, setModelsLoading] = useState(false);
  const modelRequestRef = useRef(0);
  const [selectedModel, setSelectedModel] = useState('');
  const [modelSaving, setModelSaving] = useState(false);
  const [modelSaveError, setModelSaveError] = useState('');
  const modelSaveRef = useRef(null);
  const confirmedModelRef = useRef(null);
  const modelPrincipalRef = useRef(principalKey);
  const [titleDraft, setTitleDraft] = useState('');
  const [dragActive, setDragActive] = useState(false);
  const [uploadingFileCount, setUploadingFileCount] = useState(0);
  const [uploadError, setUploadError] = useState('');
  const [threadActionError, setThreadActionError] = useState('');
  const [exporting, setExporting] = useState(false);
  const exportRequestRef = useRef(null);
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
  const threadListReadRef = useRef(null);
  const threadSearchRef = useRef({ query: '' });

  useLayoutEffect(() => {
    const reads = threadListReadRef;
    const searches = threadSearchRef;
    searches.current = { query: '' };
    reads.current?.retire();
    reads.current = null;
    return () => {
      searches.current = { query: '' };
      reads.current?.retire();
      reads.current = null;
    };
  }, [principalKey]);

  const updateSearch = useCallback((query) => {
    if (threadSearchRef.current.query !== query) {
      // Retire the old query now, before its replacement fetch is debounced.
      threadSearchRef.current = { query };
      threadListReadRef.current?.retire();
      threadListReadRef.current = null;
    }
    setSearch(query);
  }, []);

  const retireThreadRead = useCallback(() => {
    const read = threadReadRef.current;
    threadReadRef.current = null;
    window.clearTimeout(read?.timer);
    read?.retire();
  }, []);

  const threadReadBlocksSend = useCallback(() => {
    const read = threadReadRef.current;
    return read?.generation === requestGenerationRef.current
      && read.threadId === activeThreadIdRef.current && read.blocksSend;
  }, []);

  const retireThreadRefresh = useCallback(() => {
    const read = threadReadRef.current;
    if (!read || read.blocksSend) return;
    window.clearTimeout(read.timer);
    read.retire();
    read.status = 'ready';
    setThreadReadState({ threadId: read.threadId, generation: read.generation, status: 'ready', blocksSend: false });
  }, []);

  const retireModelSave = useCallback(() => {
    window.clearTimeout(modelSaveRef.current?.timer);
    modelSaveRef.current = null;
    setModelSaving(false);
    setModelSaveError('');
  }, []);

  useLayoutEffect(() => {
    // A fast thread read can settle before the matching render commits.
    // Keep that same-scope snapshot while retiring previous thread/account data.
    if (modelPrincipalRef.current !== principalKey || confirmedModelRef.current?.threadId !== activeThreadId) {
      confirmedModelRef.current = null;
    }
    modelPrincipalRef.current = principalKey;
    if (threadReadRef.current?.threadId !== activeThreadId) retireThreadRead();
    retireModelSave();
    return () => {
      window.clearTimeout(modelSaveRef.current?.timer);
      modelSaveRef.current = null;
    };
  }, [principalKey, activeThreadId, retireModelSave, retireThreadRead]);


  useEffect(() => {
    activeThreadIdRef.current = activeThreadId;
  }, [activeThreadId]);

  useEffect(() => {
    streamingRef.current = streaming;
  }, [streaming]);


  useEffect(() => {
    if (!streaming || !streamingMessageId) return undefined;
    const timer = window.setTimeout(() => {
      if (streamSessionRef.current || !streamingRef.current) return;
      setStreamNotice({ text: 'The response request has not been confirmed. It may have started on the server. Cancellation needs a confirmed stream ID. Reload this view to check its status before retrying.', retry: false });
      setMessages((current) => current.map((message) => message.id === streamingMessageId
        ? { ...message, status: 'unknown' } : message));
    }, STREAM_ACCEPTANCE_WARNING_MS);
    return () => window.clearTimeout(timer);
  }, [streaming, streamingMessageId]);

  const loadThreads = useCallback(async (query = threadSearchRef.current.query) => {
    const generation = requestGenerationRef.current;
    const queryScope = threadSearchRef.current;
    if (query !== queryScope.query) return [];
    const read = createThreadListRead();
    const previous = threadListReadRef.current;
    threadListReadRef.current = read;
    previous?.retire();
    const sameQuery = () => requestGenerationRef.current === generation
      && threadSearchRef.current === queryScope;
    const followCurrentRead = async () => {
      while (sameQuery()) {
        const current = threadListReadRef.current;
        if (!current || current === read) return [];
        // A pending current result is unknown, not an empty list. Only the
        // current caller publishes errors; retired callers cannot replay them.
        const threads = await current.promise.catch(() => []);
        if (!sameQuery()) return [];
        if (threadListReadRef.current === current) return threads;
      }
      return [];
    };
    const params = new URLSearchParams();
    if (query) params.set('search', query);
    // Retirement releases UI ownership; it does not cancel the HTTP request.
    // Capture late failures so retired network work cannot reject unobserved.
    const network = Promise.resolve()
      .then(() => apiFetch(`/v1/chat/threads${params.toString() ? `?${params.toString()}` : ''}`))
      .then(data => ({ data }), error => ({ error }));
    read.promise = (async () => {
      const outcome = await Promise.race([network, read.retired]);
      if (!sameQuery()) return [];
      if (threadListReadRef.current !== read) return followCurrentRead();
      if (!outcome) return [];
      if (outcome.error) {
        if (outcome.error.code === 'consent_required') {
          setConsentRequired(true);
          setThreads([]);
          return [];
        }
        throw outcome.error;
      }
      const threads = outcome.data.threads || [];
      setConsentRequired(false);
      setThreads(threads);
      return threads;
    })();
    return read.promise;
  }, []);

  const loadThread = useCallback(async (threadId) => {
    const generation = requestGenerationRef.current;
    const previousRead = threadReadRef.current;
    const alreadyConfirmed = previousRead?.generation === generation
      && previousRead.threadId === threadId && !previousRead.blocksSend;
    retireThreadRead();
    const request = ++threadRequestRef.current;
    if (!threadId) {
      setThreadReadState(null);
      setActiveThread(null);
      setMessages([]);
      return;
    }
    let retire;
    const retired = new Promise((resolve) => { retire = resolve; });
    const read = { generation, threadId, status: 'pending', blocksSend: !alreadyConfirmed, deadline: performance.now() + THREAD_READ_TIMEOUT_MS, retire };
    threadReadRef.current = read;
    setThreadReadState({ threadId, generation, status: 'pending', blocksSend: read.blocksSend });
    const isCurrent = () => requestGenerationRef.current === generation
      && threadRequestRef.current === request && activeThreadIdRef.current === threadId && threadReadRef.current === read;
    const currentModelOwner = () => {
      const owner = confirmedModelRef.current;
      return owner?.generation === generation && owner.threadId === threadId ? owner : null;
    };
    const modelOwnerAtRead = currentModelOwner();
    const expired = new Promise((_, reject) => {
      read.timer = window.setTimeout(() => reject(new Error('Thread read deadline expired')), THREAD_READ_TIMEOUT_MS);
    });
    try {
      const data = await Promise.race([apiFetch(`/v1/chat/threads/${encodeURIComponent(threadId)}`), expired, retired]);
      if (!isCurrent()) return;
      if (performance.now() >= read.deadline || data?.ok === false || data?.thread?.id !== threadId) {
        throw new Error('Current conversation could not be confirmed');
      }
      const storedModel = data.thread?.model;
      if (data.thread?.id === threadId && (typeof storedModel === 'string' || storedModel === null)
        && currentModelOwner() === modelOwnerAtRead) {
        const restored = storedModel ?? '';
        confirmedModelRef.current = { generation, threadId, value: restored };
        setSelectedModel(restored);
        if (restored) setModelOptions((current) => current.some((item) => item.id === restored)
          ? current : [...current, { id: restored, label: restored }]);
      }
      const owner = currentModelOwner();
      setActiveThread(owner && data.thread?.id === threadId ? { ...data.thread, model: owner.value || null } : data.thread);
      setTitleDraft(data.thread?.title || 'New chat');
      const restoredMessages = normalizeMessages(data.messages);
      const runningId = data.active_stream_ids?.[0];
      if (runningId && !streamingRef.current) {
        // A same-principal remount/navigation must not orphan a task that outlived
        // its viewer. Replay into a fresh buffer, never append history twice.
        const existing = restoredMessages.find((message) => message.role === 'assistant'
          && message.metadata?.stream_id === runningId);
        const assistant = existing || makeTemporaryAssistant();
        setMessages(existing ? restoredMessages.map((message) => message.id === assistant.id
          ? { ...message, content: '', tools: [], status: 'streaming' } : message)
          : [...restoredMessages, assistant]);
        streamingRef.current = true;
        setStreaming(true);
        setStreamingMessageId(assistant.id);
        void streamAttachRef.current?.(runningId, assistant.id, generation, threadId);
      } else {
        setMessages(restoredMessages);
      }
      read.status = 'ready';
      read.blocksSend = false;
      setThreadReadState({ threadId, generation, status: 'ready', blocksSend: false });
    } catch {
      if (!isCurrent()) return;
      read.status = 'error';
      setThreadReadState({ threadId, generation, status: 'error', blocksSend: read.blocksSend });
    } finally {
      window.clearTimeout(read.timer);
    }
  }, [retireThreadRead]);

  const loadModels = useCallback(async () => {
    const generation = requestGenerationRef.current;
    const request = ++modelRequestRef.current;
    const isCurrent = () => requestGenerationRef.current === generation && modelRequestRef.current === request;
    setModelsLoading(true);
    try {
      const data = await apiFetch('/v1/chat/models');
      const flattened = flattenModels(data);
      if (!isCurrent()) return flattened;
      const confirmed = confirmedModelRef.current;
      const ownsSelection = confirmed?.generation === generation && confirmed.threadId === activeThreadIdRef.current;
      const options = ownsSelection && confirmed.value && !flattened.models.some((item) => item.id === confirmed.value)
        ? [...flattened.models, { id: confirmed.value, label: confirmed.value }] : flattened.models;
      setModelOptions(options);
      // A catalog owns available choices, not an acknowledged thread setting.
      setSelectedModel((current) => ownsSelection ? confirmed.value : (
        current && flattened.models.some((item) => item.id === current)
          ? current
          : flattened.current
      ));
      setModelError('');
      return flattened;
    } catch {
      if (isCurrent()) setModelError("Couldn't load the model list. Please try again.");
      return null;
    } finally {
      if (isCurrent()) setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    // Advance the generation synchronously, in the same tick the effect
    // re-runs for a new principalKey -- any request issued by a PRIOR
    // generation (still captured in its own closure inside loadThreads/
    // loadThread/loadModels) is now stale and will no-op instead of
    // applying its response when it eventually resolves (#2395/C-071).
    const generation = ++requestGenerationRef.current;
    retireThreadRead();
    setThreadReadState(null);
    const modelRequests = modelRequestRef;
    const generations = requestGenerationRef;
    streamSessionRef.current?.dispose();
    streamSessionRef.current = null;
    pendingStopRef.current = false;
    setStreamNotice(null);
    exportRequestRef.current = null;
    setExporting(false);
    setThreadActionError('');
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
        activeStreamIdRef.current = null;
        setThreads([]);
        setActiveThreadId(null);
        activeThreadIdRef.current = null;
        setActiveThread(null);
        setMessages([]);
        setDraft('');
        setTitleDraft('');
        setSearch('');
        setModelOptions([]);
        setSelectedModel('');
        setModelError('');
        setUploadError('');
        setUploadingFileCount(0);
        setDragActive(false);
        dragDepthRef.current = 0;
        setConsentRequired(false);
        const [threadList] = await Promise.all([
          loadThreads(''),
          loadModels().catch(() => null),
        ]);
        if (cancelled || requestGenerationRef.current !== generation) return;
        if (threadList.length > 0) {
          setActiveThreadId(threadList[0].id);
          activeThreadIdRef.current = threadList[0].id;
          await loadThread(threadList[0].id);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    void boot();
    return () => {
      cancelled = true;
      ++generations.current;
      retireThreadRead();
      streamSessionRef.current?.dispose();
      streamSessionRef.current = null;
      ++modelRequests.current;
      exportRequestRef.current = null;
      if (eventSourceRef.current) eventSourceRef.current.close();
    };
  }, [principalKey, loadModels, loadThread, loadThreads, retireThreadRead]);

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

  const ensureThread = useCallback(async (pendingMessages = []) => {
    const generation = requestGenerationRef.current;
    if (activeThreadIdRef.current) return activeThreadIdRef.current;
    const data = await apiFetch('/v1/chat/threads', {
      method: 'POST',
      body: JSON.stringify({ title: 'New chat', model: selectedModel || null }),
    });
    if (requestGenerationRef.current !== generation) return null;
    setThreads((current) => [data.thread, ...current]);
    setActiveThread(data.thread);
    setActiveThreadId(data.thread.id);
    activeThreadIdRef.current = data.thread.id;
    setTitleDraft(data.thread.title);
    // A first send already has optimistic messages. Keep them when thread
    // creation completes; clearing here made the first turn and its errors vanish.
    // On stale-thread recovery this also drops messages from the old thread.
    setMessages(pendingMessages);
    return data.thread.id;
  }, [selectedModel]);

  const refreshActiveThread = useCallback(async () => {
    const generation = requestGenerationRef.current;
    if (!activeThreadIdRef.current) return;
    await loadThread(activeThreadIdRef.current);
    if (requestGenerationRef.current !== generation) return;
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

    const generation = requestGenerationRef.current;
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
        if (requestGenerationRef.current !== generation) return;
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
      if (requestGenerationRef.current === generation) setUploadError('File upload failed.');
    } finally {
      if (requestGenerationRef.current === generation) setUploadingFileCount(0);
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

  const attachStream = useCallback(async (streamId, assistantMessageId, generation = requestGenerationRef.current, threadId = activeThreadIdRef.current) => {
    if (requestGenerationRef.current !== generation) return;
    // A success transport response without an identity cannot establish which
    // producer accepted the request. Leave it unknown; never cancel `undefined`.
    if (typeof streamId !== 'string' || !streamId.trim()) throw new Error('Stream acceptance is unconfirmed');
    streamSessionRef.current?.dispose();
    const session = { streamId, assistantMessageId, threadId, failures: 0 };
    streamSessionRef.current = session;
    const isCurrent = () => streamSessionRef.current === session && requestGenerationRef.current === generation;
    const closeSource = () => {
      session.source?.close();
      if (eventSourceRef.current === session.source) eventSourceRef.current = null;
      session.source = null;
    };
    session.dispose = () => {
      closeSource();
      window.clearTimeout(session.timer);
      session.controller?.abort();
    };
    const finish = () => {
      if (!isCurrent()) return;
      session.dispose();
      streamSessionRef.current = null;
      streamingRef.current = false;
      setStreaming(false);
      setStreamingMessageId(null);
      activeStreamIdRef.current = null;
      pendingStopRef.current = false;
      setStreamNotice(null);
    };
    const boundedRequest = async (action) => {
      const controller = new AbortController();
      session.controller = controller;
      const timer = window.setTimeout(() => controller.abort(), STREAM_STATUS_TIMEOUT_MS);
      try {
        return await Promise.race([
          action(controller.signal),
          new Promise((_, reject) => {
            controller.signal.addEventListener('abort', () => reject(new Error('Request interrupted')), { once: true });
          }),
        ]);
      } finally {
        window.clearTimeout(timer);
        if (session.controller === controller) session.controller = null;
      }
    };
    const request = (path, options = {}) => boundedRequest((signal) => apiFetch(path, { ...options, signal }));
    const markUnknown = (text) => {
      setStreamNotice({ text, retry: true });
      setMessages((current) => current.map((message) => message.id === assistantMessageId
        ? { ...message, status: 'unknown' } : message));
    };
    // SSE loss says nothing about the producer. Reconcile against the task
    // registry via the existing thread endpoint, keeping cancellation available.
    // Consecutive failed checks stop automatically; no timer claims completion.
    session.recover = async (reset = false) => {
      if (!isCurrent() || session.checking || session.cancelling) return false;
      window.clearTimeout(session.timer);
      if (reset) session.failures = 0;
      const check = {};
      session.checking = check;
      try {
        const data = await request(`/v1/chat/threads/${encodeURIComponent(session.threadId)}`);
        if (!isCurrent() || session.cancelling || session.checking !== check) return false;
        if (!Array.isArray(data.active_stream_ids)) throw new Error('Stream status unavailable');
        session.failures = 0;
        if (data.active_stream_ids.includes(streamId)) {
          setStreamNotice({ text: session.stopRequested
            ? 'Stop requested. Waiting for the response to finish stopping.'
            : 'The live response connection dropped. The response is still running; checking for its result.', retry: false });
          setMessages((current) => current.map((message) => message.id === assistantMessageId
            ? { ...message, status: 'streaming' } : message));
          session.timer = window.setTimeout(() => { void session.recover(); }, STREAM_STATUS_POLL_MS);
          return false;
        }
        const result = data.messages?.find((message) => message.role === 'assistant'
          && message.metadata?.stream_id === streamId && ['complete', 'stopped', 'error'].includes(message.status));
        if (result) {
          setMessages(normalizeMessages(data.messages));
        } else {
          setMessages((current) => current.map((message) => message.id === assistantMessageId
            ? { ...message, status: 'error', content: message.content || 'This response is no longer running, but its result could not be recovered. You can try again.' }
            : message));
        }
        finish();
        loadThreads(search).catch(() => {});
        return true;
      } catch {
        if (!isCurrent() || session.cancelling || session.checking !== check) return false;
        session.failures += 1;
        if (session.failures >= STREAM_STATUS_MAX_FAILURES) {
          markUnknown('Cannot confirm the response status. It may still be running. Retry status or use Stop.');
        } else {
          setStreamNotice({ text: 'The live response connection dropped. Checking whether the response is still running…', retry: false });
          session.timer = window.setTimeout(() => { void session.recover(); }, STREAM_STATUS_POLL_MS);
        }
        return false;
      } finally {
        if (session.checking === check) session.checking = null;
      }
    };
    session.cancel = async () => {
      if (!isCurrent() || session.cancelling) return false;
      session.cancelling = true;
      session.checking = null;
      closeSource();
      window.clearTimeout(session.timer);
      session.controller?.abort();
      setStreamNotice({ text: 'Requesting Stop…', retry: false });
      try {
        const data = await request(`/v1/chat/streams/${encodeURIComponent(streamId)}/cancel`, { method: 'POST' });
        if (!isCurrent()) return false;
        if (typeof data.cancelled !== 'boolean') throw new Error('Cancellation status unavailable');
        session.stopRequested = data.cancelled;
      } catch (err) {
        if (!isCurrent()) return false;
        console.error('[ChatMode] Stream cancel request failed; stream may keep running server-side:', err);
        markUnknown('Could not confirm Stop. The response may still be running. Retry status or use Stop again.');
        return false;
      } finally {
        session.cancelling = false;
      }
      return session.recover(true);
    };
    activeStreamIdRef.current = streamId;
    setMessages((current) => current.map((message) => message.id === assistantMessageId
      ? { ...message, status: 'streaming' } : message));
    if (pendingStopRef.current) {
      await session.cancel();
      return;
    }
    try {
      const streamUrl = await boundedRequest(() => buildStreamUrl(streamId));
      if (!isCurrent() || pendingStopRef.current) return;
      const source = new EventSource(streamUrl, { withCredentials: true });
      session.source = source;
      eventSourceRef.current = source;
      source.onmessage = (event) => {
        if (!isCurrent() || session.source !== source) return;
        let payload;
        try { payload = JSON.parse(event.data); } catch { return; }
        // The SSE connection's own timeout is not a producer failure.
        if (payload.error && payload.message === 'Stream timeout') {
          closeSource();
          void session.recover();
          return;
        }
        if (payload.tool) {
          setMessages((current) => current.map((message) => message.id === assistantMessageId
            ? { ...message, tools: upsertTool(message.tools, payload.tool) } : message));
        }
        if (payload.token) {
          setMessages((current) => current.map((message) => message.id === assistantMessageId
            ? { ...message, content: `${message.content}${payload.token}` } : message));
        }
        if (payload.done || payload.error) {
          const finalContent = payload.content || payload.message || (payload.error ? 'Something went wrong while generating the response.' : '');
          setMessages((current) => current.map((message) => message.id === assistantMessageId ? {
            ...message,
            content: finalContent || message.content,
            status: payload.error ? (payload.message === 'Stopped.' ? 'stopped' : 'error') : 'complete',
            metadata: {
              ...(message.metadata || {}),
              ...(payload.streaming_mode ? { streaming: {
                mode: payload.streaming_mode, token_count: payload.token_count || 0, native: !payload.fallback,
              } } : {}),
            },
          } : message));
          finish();
          window.setTimeout(() => {
            if (requestGenerationRef.current === generation && activeThreadIdRef.current === session.threadId && !streamingRef.current) {
              refreshActiveThread().catch(() => {});
            }
          }, 120);
        }
      };
      source.onerror = () => {
        if (!isCurrent() || session.source !== source) return;
        closeSource();
        setStreamNotice({ text: 'The live response connection dropped. Checking whether the response is still running…', retry: false });
        void session.recover();
      };
    } catch {
      if (isCurrent()) void session.recover();
    }
  }, [refreshActiveThread, loadThreads, search]);
  streamAttachRef.current = attachStream;

  const showUnconfirmedRequest = useCallback((messageId) => {
    setStreamNotice({ text: 'Could not confirm whether the response request was accepted. It may still be running. Reload this view to check its status before retrying.', retry: false });
    setMessages((current) => current.map((message) => message.id === messageId
      ? { ...message, status: 'unknown' } : message));
  }, []);

  const sendText = useCallback(async (text) => {
    const clean = text.trim();
    if (!clean || streamingRef.current || threadReadBlocksSend()) return;
    // The SAME first-run consent gate every other turn entry point uses. This
    // composer had none: a brand-new cloud user who opened Chat first could not
    // run a single agent command, was never prompted, and saw nothing at all
    // (see the ensureThread note below). The gate resumes this exact message
    // once they accept, so the turn they asked for is not dropped.
    if (interceptCloudConsent({ kind: 'chat', text: clean, resume: () => sendText(clean) })) {
      setDraft('');
      return;
    }
    const generation = requestGenerationRef.current;
    ++threadRequestRef.current;
    retireThreadRefresh();
    pendingStopRef.current = false;
    setStreamNotice(null);
    streamingRef.current = true;
    const temporaryAssistant = makeTemporaryAssistant();
    const pendingMessages = [
      {
        id: `local-user-${Date.now()}`,
        role: 'user',
        content: clean,
        status: 'complete',
        metadata: { optimistic: true },
      },
      temporaryAssistant,
    ];
    setDraft('');
    setStreaming(true);
    setStreamingMessageId(temporaryAssistant.id);
    setMessages((current) => [...current, ...pendingMessages]);
    let dispatched = false;
    const postSend = (id) => {
      const owner = confirmedModelRef.current;
      const model = owner?.generation === generation && owner.threadId === id ? owner.value : selectedModel;
      dispatched = true;
      return apiFetch(`/v1/chat/threads/${encodeURIComponent(id)}/send`, {
        method: 'POST', body: JSON.stringify({ text: clean, model: model || null }),
      });
    };
    try {
      // ensureThread() used to be awaited ABOVE this try, and `onSend` does not
      // catch, so a thread-creation refusal (403 consent_required for a user
      // who has not enabled cloud sync) escaped as an unhandled promise
      // rejection: the typed message vanished with no reply, no error, and no
      // prompt. Inside the try it becomes a message the user can act on.
      const threadId = await ensureThread(pendingMessages);
      if (requestGenerationRef.current !== generation) return;
      let sendResult;
      try {
        sendResult = await postSend(threadId);
      } catch (err) {
        if (requestGenerationRef.current !== generation) return;
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
          const freshThreadId = await ensureThread(pendingMessages);
          if (requestGenerationRef.current !== generation) return;
          sendResult = await postSend(freshThreadId);
        } else {
          throw err;
        }
      }
      await attachStream(sendResult.stream_id, temporaryAssistant.id, generation);
    } catch (err) {
      if (requestGenerationRef.current !== generation) return;
      if (dispatched && isUnconfirmedRequest(err)) {
        showUnconfirmedRequest(temporaryAssistant.id);
        return;
      }
      streamingRef.current = false;
      setStreamNotice(null);
      setStreaming(false);
      setStreamingMessageId(null);
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
  }, [attachStream, ensureThread, selectedModel, interceptCloudConsent, showUnconfirmedRequest, threadReadBlocksSend, retireThreadRefresh]);

  const stopStreaming = useCallback(async () => {
    pendingStopRef.current = true;
    if (streamSessionRef.current) return streamSessionRef.current.cancel();
    // The send/regenerate POST may still be in flight. Keep ownership until it
    // returns a stream id, then cancel that exact task before opening SSE.
    setStreamNotice({ text: 'Stop requested. No stream ID is confirmed yet, so cancellation is not confirmed.', retry: false });
    return false;
  }, []);

  const createNewChat = useCallback(async () => {
    const generation = requestGenerationRef.current;
    if (streamingRef.current && !(await stopStreaming())) return;
    if (requestGenerationRef.current !== generation) return;
    retireModelSave();
    const data = await apiFetch('/v1/chat/threads', {
      method: 'POST',
      body: JSON.stringify({ title: 'New chat', model: selectedModel || null }),
    });
    if (requestGenerationRef.current !== generation) return;
    setThreads((current) => [data.thread, ...current]);
    setActiveThreadId(data.thread.id);
    activeThreadIdRef.current = data.thread.id;
    setActiveThread(data.thread);
    setTitleDraft(data.thread.title);
    setMessages([]);
  }, [selectedModel, stopStreaming, retireModelSave]);

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
    if (streamingRef.current && !(await stopStreaming())) return;
    retireModelSave();
    setActiveThreadId(threadId);
    activeThreadIdRef.current = threadId;
    await loadThread(threadId);
  }, [loadThread, stopStreaming, retireModelSave]);

  const renameThread = useCallback(async (thread, nextTitle) => {
    const title = nextTitle ?? window.prompt('Rename chat', thread.title || 'New chat');
    if (!title || !title.trim()) return;
    const trimmed = title.trim();
    if (Array.from(trimmed).length > 200) {
      setThreadActionError('Use 200 characters or fewer for the chat title.');
      return;
    }
    const generation = requestGenerationRef.current;
    setThreadActionError('');
    try {
      const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ title: trimmed }),
      });
      if (requestGenerationRef.current !== generation) return;
      setThreads((current) => current.map((item) => (item.id === thread.id ? data.thread : item)));
      if (activeThreadIdRef.current === thread.id) {
        setActiveThread(data.thread);
        setTitleDraft(data.thread.title);
      }
    } catch {
      if (requestGenerationRef.current === generation) {
        setThreadActionError('Could not confirm the new chat title. Please try again.');
      }
    }
  }, []);

  const deleteThread = useCallback(async (thread) => {
    const generation = requestGenerationRef.current;
    if (!window.confirm(`Delete "${thread.title || 'New chat'}"?`)) return;
    if (activeThreadIdRef.current === thread.id && streamingRef.current && !(await stopStreaming())) return;
    if (requestGenerationRef.current !== generation) return;
    await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}`, { method: 'DELETE' });
    if (requestGenerationRef.current !== generation) return;
    const nextThreads = threads.filter((item) => item.id !== thread.id);
    setThreads((current) => current.filter((item) => item.id !== thread.id));
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
  }, [loadThread, stopStreaming, threads]);

  const exportThread = useCallback(async (thread = activeThread) => {
    if (!thread || exportRequestRef.current) return;
    const request = {};
    const generation = requestGenerationRef.current;
    exportRequestRef.current = request;
    setExporting(true);
    setThreadActionError('');
    try {
      const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(thread.id)}/export`);
      if (requestGenerationRef.current !== generation || exportRequestRef.current !== request) return;
      if (typeof data?.markdown !== 'string') throw new Error('Invalid export response');
      downloadMarkdown(data.filename, data.markdown);
    } catch {
      if (requestGenerationRef.current === generation && exportRequestRef.current === request) {
        setThreadActionError('Could not export this chat. Please try again.');
      }
    } finally {
      if (exportRequestRef.current === request) {
        exportRequestRef.current = null;
        setExporting(false);
      }
    }
  }, [activeThread]);

  const submitTitle = useCallback(async () => {
    if (!activeThread || !titleDraft.trim() || titleDraft.trim() === activeThread.title) return;
    await renameThread(activeThread, titleDraft.trim());
  }, [activeThread, renameThread, titleDraft]);

  const handleModelChange = useCallback(async (event) => {
    if (modelSaveRef.current) return;
    const generation = requestGenerationRef.current;
    const threadId = activeThreadIdRef.current;
    const model = event.target.value;
    setModelSaveError('');
    if (!threadId) {
      // An unsaved new conversation has only a local model choice.
      setSelectedModel(model);
      return;
    }
    const request = { deadline: performance.now() + MODEL_SAVE_TIMEOUT_MS };
    modelSaveRef.current = request;
    setModelSaving(true);
    const isCurrent = () => modelSaveRef.current === request
      && requestGenerationRef.current === generation && activeThreadIdRef.current === threadId;
    const uncertain = () => {
      if (!isCurrent()) return;
      window.clearTimeout(request.timer);
      modelSaveRef.current = null;
      setModelSaving(false);
      setModelSaveError('Could not confirm the model change. It may have reached the server; you can try again.');
    };
    request.timer = window.setTimeout(uncertain, MODEL_SAVE_TIMEOUT_MS);
    try {
      const data = await apiFetch(`/v1/chat/threads/${encodeURIComponent(threadId)}`, {
        method: 'PATCH',
        body: JSON.stringify({ model }),
      });
      if (!isCurrent()) return;
      if (performance.now() >= request.deadline) {
        uncertain();
        return;
      }
      if (data?.ok === false || data.thread?.id !== threadId || !(typeof data.thread.model === 'string' || data.thread.model === null)) {
        throw new Error('Model acknowledgement unavailable');
      }
      const acknowledged = data.thread.model ?? '';
      confirmedModelRef.current = { generation, threadId, value: acknowledged };
      // An acknowledged selection supersedes catalog reads issued before it.
      modelRequestRef.current += 1;
      setModelsLoading(false);
      setModelError('');
      setSelectedModel(acknowledged);
      if (acknowledged) setModelOptions((current) => current.some((item) => item.id === acknowledged)
        ? current : [...current, { id: acknowledged, label: acknowledged }]);
      // This acknowledgement owns the model field, not concurrent title edits.
      setActiveThread((current) => current?.id === threadId ? { ...current, model: data.thread.model } : current);
      setThreads((current) => current.map((item) => item.id === threadId ? { ...item, model: data.thread.model } : item));
    } catch {
      uncertain();
    } finally {
      if (modelSaveRef.current === request) {
        window.clearTimeout(request.timer);
        modelSaveRef.current = null;
        setModelSaving(false);
      }
    }
  }, []);

  const handleRegenerate = useCallback(async (message) => {
    if (!activeThreadIdRef.current || streamingRef.current || threadReadBlocksSend()) return;
    const generation = requestGenerationRef.current;
    ++threadRequestRef.current;
    retireThreadRefresh();
    pendingStopRef.current = false;
    setStreamNotice(null);
    streamingRef.current = true;
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
      if (requestGenerationRef.current !== generation) return;
      setActiveThread(data.thread);
      setTitleDraft(data.thread?.title || 'New chat');
      setMessages(normalizeMessages(data.messages));
      await attachStream(data.stream_id, message.id, generation);
    } catch (err) {
      if (requestGenerationRef.current !== generation) return;
      if (isUnconfirmedRequest(err)) {
        showUnconfirmedRequest(message.id);
        return;
      }
      streamingRef.current = false;
      setStreamNotice(null);
      setStreaming(false);
      setStreamingMessageId(null);
      activeStreamIdRef.current = null;
      setMessages((current) => current.map((item) => (
        item.id === message.id
          ? { ...item, content: 'Something went wrong while regenerating this response.', status: 'error' }
          : item
      )));
    }
  }, [attachStream, selectedModel, showUnconfirmedRequest, threadReadBlocksSend, retireThreadRefresh]);

  const handleFork = useCallback(async (message) => {
    if (streamingRef.current || threadReadBlocksSend()) return;
    const generation = requestGenerationRef.current;
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
    ++threadRequestRef.current;
    retireThreadRefresh();
    pendingStopRef.current = false;
    setStreamNotice(null);
    streamingRef.current = true;
    const temporaryAssistant = makeTemporaryAssistant();
    setStreaming(true);
    setStreamingMessageId(temporaryAssistant.id);
    setMessages((current) => [...current, temporaryAssistant]);
    try {
      const data = await apiFetch(
        `/v1/chat/threads/${encodeURIComponent(activeThreadIdRef.current)}/messages/${encodeURIComponent(sourceMessageId)}/fork`,
        { method: 'POST', body: JSON.stringify({ content, model: selectedModel || null }) }
      );
      if (requestGenerationRef.current !== generation) return;
      setActiveThreadId(data.thread.id);
      activeThreadIdRef.current = data.thread.id;
      setActiveThread(data.thread);
      setTitleDraft(data.thread.title);
      setMessages([...normalizeMessages(data.messages), temporaryAssistant]);
      await attachStream(data.stream_id, temporaryAssistant.id, generation);
      if (requestGenerationRef.current === generation) loadThreads().catch(() => {});
    } catch (err) {
      if (requestGenerationRef.current !== generation) return;
      if (isUnconfirmedRequest(err)) {
        showUnconfirmedRequest(temporaryAssistant.id);
        return;
      }
      streamingRef.current = false;
      setStreaming(false);
      setStreamingMessageId(null);
      setStreamNotice(null);
      setMessages((current) => current.map((item) => item.id === temporaryAssistant.id
        ? { ...item, content: 'Something went wrong while branching this response.', status: 'error' } : item));
    }
  }, [attachStream, loadThreads, messages, selectedModel, showUnconfirmedRequest, threadReadBlocksSend, retireThreadRefresh]);

  const handleFeedback = useCallback(async (message, rating) => {
    const generation = requestGenerationRef.current;
    const threadId = activeThreadIdRef.current;
    if (!threadId) return;
    const data = await apiFetch(
      `/v1/chat/threads/${encodeURIComponent(threadId)}/messages/${encodeURIComponent(message.id)}/feedback`,
      {
        method: 'POST',
        body: JSON.stringify({ rating }),
      }
    );
    if (requestGenerationRef.current !== generation || activeThreadIdRef.current !== threadId) return;
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
        onSearch={updateSearch}
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
        {threadActionError && <div className="chat-upload-status is-error" role="alert">{threadActionError}</div>}
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
              value={modelError && !confirmedModelRef.current ? '' : selectedModel}
              disabled={Boolean(modelError) || modelSaving}
              aria-busy={modelSaving}
              onChange={handleModelChange}
              onFocus={() => { loadModels().catch(() => {}); }}
              aria-label="Model"
            >
              <option value="">{modelError && !confirmedModelRef.current ? 'Model list unavailable' : 'Default model'}</option>
              {(!modelError || confirmedModelRef.current) && modelOptions.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
            <button type="button" onClick={() => exportThread()} disabled={!activeThread || exporting}>{exporting ? 'Exporting…' : 'Export'}</button>
          </div>
        </header>
        {modelSaving && <div className="chat-upload-status" role="status">Saving chat model…</div>}
        {modelSaveError && <div className="chat-error" role="alert">{modelSaveError}</div>}
        {modelError && (
          <div className="chat-error" role="alert">
            <span>{modelError}</span>
            <button type="button" onClick={() => { void loadModels(); }} disabled={modelsLoading}>
              {modelsLoading ? 'Loading model list…' : 'Retry model list'}
            </button>
          </div>
        )}
        {streamNotice && (
          <div className="chat-upload-status" role="status">
            <span>{streamNotice.text}</span>
            {streamNotice.retry && <button type="button" onClick={() => { void streamSessionRef.current?.recover(true); }}>Retry response status</button>}
          </div>
        )}
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
          {threadReadState?.threadId === activeThreadId && threadReadState.generation === requestGenerationRef.current && (
            threadReadState.status === 'pending' ? <span role="status" className="chat-loading">{threadReadState.blocksSend ? 'Loading this conversation before sending...' : 'Refreshing this conversation...'}</span>
              : threadReadState.status === 'error' && <span role="alert" className="chat-upload-status is-error">
                {threadReadState.blocksSend
                  ? 'Could not confirm this conversation. Your draft is kept. Retry before sending.'
                  : 'Could not refresh this conversation. The confirmed conversation is kept.'}
                <button type="button" onClick={() => void loadThread(activeThreadIdRef.current)}>Retry conversation</button>
              </span>
          )}
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
            disabled={loading || (threadReadState?.threadId === activeThreadId
              && threadReadState.generation === requestGenerationRef.current && threadReadState.blocksSend)}
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
