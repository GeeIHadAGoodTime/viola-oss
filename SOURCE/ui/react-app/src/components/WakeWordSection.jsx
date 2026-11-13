/**
 * WakeWordSection - custom wake-word model switching UI.
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import PropTypes from 'prop-types';
import { authFetch } from '../hooks/useViolaApi';
import { THEME } from '../config';
import { isFeatureHidden } from '../utils/featureSurface';
import DesktopUpsell from './DesktopUpsell';

const SAMPLE_TARGET = 5;
const SAMPLE_LIMIT = 10;
const SAMPLE_MS = 1600;
const POLL_MS = 5000;
// One name for the one action that makes a word wake Viola. The list said "Switch"
// and the finished-training row said "Activate" for the same call, and the laptop
// test pressed one without seeing the other.
const USE_WORD_LABEL = 'Use this word';
// A sample whose loudest point is below this is a dead or muted microphone, not a
// quiet voice: normal speech peaks far above it even at a distance.
const MIN_SAMPLE_PEAK = 0.01;

// What the job is doing, in words. ViolaWake reports one progress number; its stages
// run 0-12 setup, 12-30 practice audio, 30-95 training, 95-100 the quality check.
function trainingStageText(status, progress) {
  if (status === 'queued') return 'Waiting for a training slot';
  if (status === 'done') return 'Finished';
  if (progress < 12) return 'Preparing your recordings';
  if (progress < 30) return 'Making practice audio';
  if (progress < 95) return 'Training your wake word';
  return 'Checking quality';
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function wakeRequest(path, options = {}) {
  const response = await authFetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok || payload?.ok === false) {
    const message = payload?.error?.message || 'Custom wake-words are temporarily unavailable.';
    throw new Error(message);
  }
  return payload?.data || payload || {};
}

function writeString(view, offset, value) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function mergeChunks(chunks) {
  const totalLength = chunks.reduce((total, chunk) => total + chunk.length, 0);
  const merged = new Float32Array(totalLength);
  let offset = 0;
  chunks.forEach((chunk) => {
    merged.set(chunk, offset);
    offset += chunk.length;
  });
  return merged;
}

function samplePeak(chunks) {
  let peak = 0;
  chunks.forEach((chunk) => {
    for (let index = 0; index < chunk.length; index += 1) {
      const value = Math.abs(chunk[index]);
      if (value > peak) peak = value;
    }
  });
  return peak;
}

function encodeWav(chunks, sampleRate) {
  const samples = mergeChunks(chunks);
  const dataSize = samples.length * 2;
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  writeString(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataSize, true);
  writeString(view, 8, 'WAVE');
  writeString(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeString(view, 36, 'data');
  view.setUint32(40, dataSize, true);

  let offset = 44;
  for (let index = 0; index < samples.length; index += 1) {
    const clamped = Math.max(-1, Math.min(1, samples[index]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buffer], { type: 'audio/wav' });
}

async function recordWavSample() {
  const AudioContextImpl = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextImpl || !navigator.mediaDevices?.getUserMedia) {
    throw new Error('Microphone recording is not available in this browser.');
  }

  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    },
  });
  const audioContext = new AudioContextImpl({ sampleRate: 16000 });
  const source = audioContext.createMediaStreamSource(stream);
  const processor = audioContext.createScriptProcessor(4096, 1, 1);
  const chunks = [];

  processor.onaudioprocess = (event) => {
    chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
  };
  source.connect(processor);
  processor.connect(audioContext.destination);

  try {
    await wait(SAMPLE_MS);
  } finally {
    processor.disconnect();
    source.disconnect();
    stream.getTracks().forEach((track) => track.stop());
    await audioContext.close();
  }

  if (!chunks.length) {
    throw new Error('No microphone audio was captured.');
  }
  if (samplePeak(chunks) < MIN_SAMPLE_PEAK) {
    throw new Error('That recording was silent. Check that your microphone is on and not muted, then try again.');
  }
  return encodeWav(chunks, audioContext.sampleRate || 16000);
}

function buttonStyle(theme, variant = 'secondary', disabled = false) {
  const primary = variant === 'primary';
  const danger = variant === 'danger';
  return {
    minHeight: 32,
    padding: '7px 12px',
    borderRadius: 8,
    border: `1px solid ${danger ? theme.colors.statusRed : theme.colors.borderSubtle}`,
    backgroundColor: disabled
      ? theme.colors.bgSurface
      : primary
        ? theme.colors.accent
        : theme.colors.bgElevated,
    color: disabled
      ? theme.colors.textMuted
      : primary
        ? '#fff'
        : danger
          ? theme.colors.statusRed
          : theme.colors.textPrimary,
    fontSize: 12,
    fontWeight: 600,
    cursor: disabled ? 'default' : 'pointer',
  };
}

function architectureLabel(architecture) {
  if (architecture === 'temporal_cnn') return 'Temporal CNN';
  if (architecture === 'mlp_on_oww') return 'Legacy MLP';
  if (!architecture) return 'Unknown architecture';
  return architecture.replaceAll('_', ' ');
}

// Tips grounded in what training actually checks: sample-count bounds enforced
// server-side (SAMPLE_TARGET/SAMPLE_LIMIT above), the fixed ~1.6s capture
// window in recordWavSample(), and the quality-gate behavior root-caused in
// CL-20260714-4c23 (grade F is frequently model-training variance, not bad
// audio -- retrying with the same recordings often passes on a later attempt).
function WakeWordTrainingTips({ reasonCode }) {
  return (
    <div>
      {reasonCode === 'service_outage' && (
        <p style={{ margin: '0 0 8px 0' }}>
          This one is on us, not your recordings. Nothing below will change it, so just try
          again later.
        </p>
      )}
      {reasonCode === 'quality_gate' && (
        <p style={{ margin: '0 0 8px 0' }}>
          A grade of F means the automated quality check did not pass. That does not mean your
          voice or recordings were the problem: the same recordings sometimes pass on a second
          try, since the check has some run-to-run variation built in. Try training again with
          the same recordings before you re-record anything.
        </p>
      )}
      <ul style={{ margin: 0, paddingLeft: 18 }}>
        <li>Pick a wake word with 2 to 4 syllables, like &quot;Athena&quot; or &quot;hey buddy&quot;. Avoid words you say often in normal conversation.</li>
        <li>Speak at a normal indoor volume. You do not need to whisper, and shouting can distort the recording.</li>
        <li>
          Say the whole phrase right after you press Record and finish within about a second and a
          half. Do not pause before or after it.
        </li>
        <li>Avoid recording next to a fan, TV, or music playing in the background.</li>
        <li>
          Record {SAMPLE_LIMIT} samples instead of the minimum {SAMPLE_TARGET}. A little variation
          in pace and distance from the microphone across samples helps more than recording the
          same way every time.
        </li>
        <li>
          If training fails several times in a row for the same wake word, wait a few minutes and
          try again, or try a different word. If it keeps failing, contact support.
        </li>
      </ul>
    </div>
  );
}

WakeWordTrainingTips.propTypes = {
  reasonCode: PropTypes.string,
};

WakeWordTrainingTips.defaultProps = {
  reasonCode: null,
};

export default function WakeWordSection({ theme = THEME }) {
  const [models, setModels] = useState([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [activeModelPath, setActiveModelPath] = useState('');
  const [loadError, setLoadError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [wizardOpen, setWizardOpen] = useState(false);
  const [wakeWordText, setWakeWordText] = useState('');
  const [samples, setSamples] = useState([]);
  const [recording, setRecording] = useState(false);
  const [trainingJobId, setTrainingJobId] = useState(null);
  const [trainingStatus, setTrainingStatus] = useState(null);
  const [trainingError, setTrainingError] = useState(null);
  const [trainingErrorReason, setTrainingErrorReason] = useState(null);
  const [completedModel, setCompletedModel] = useState(null);
  const [tipsOpen, setTipsOpen] = useState(false);
  const [trainingAvailable, setTrainingAvailable] = useState(false);
  const [trainingUnavailableReason, setTrainingUnavailableReason] = useState(null);
  const [wordRefusal, setWordRefusal] = useState(null);
  const [wordChecked, setWordChecked] = useState('');
  // One switch at a time. On the build-10 laptop the word was switched 8 times in
  // 2 seconds (each one a detector reload): the button stayed live while it worked.
  const [activating, setActivating] = useState(false);
  const activatingRef = useRef(false);

  const activeModel = useMemo(
    () => models.find((model) => model.is_active) || null,
    [models],
  );

  const reloadModels = useCallback(async () => {
    // #4226: `/v1/wake/*` is the LOCAL_ONLY `wake_training` route group
    // (backend/cloud_route_manifest.py). These routes switch and delete
    // wake-word model FILES on the user's own disk, and the browser voice path
    // uses a fixed cloud wake path instead. Ungated, this fired on every open
    // of Settings -> Customize on the cloud SPA and painted a red
    // "Custom wake-words are temporarily unavailable" over the 404.
    if (isFeatureHidden('wake_models')) {
      setModels([]);
      setActiveModelPath('');
      setLoadError(null);
      setModelsLoading(false);
      return;
    }
    setModelsLoading(true);
    try {
      const data = await wakeRequest('/v1/wake/models');
      setModels(data.models || []);
      setActiveModelPath(data.active_model_path || '');
      setLoadError(null);
    } catch (err) {
      setModels([]);
      setActiveModelPath('');
      setLoadError(err?.message || 'Custom wake-words are temporarily unavailable.');
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => {
    reloadModels();
  }, [reloadModels]);

  // Ask the service rather than assume: the button is offered only when a job could
  // finish, and a job that outlived this panel is picked back up, because the model
  // is imported while something polls it.
  useEffect(() => {
    if (isFeatureHidden('wake_models')) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const status = await wakeRequest('/v1/wake/training/status');
        if (cancelled) return;
        setTrainingAvailable(Boolean(status.available));
        setTrainingUnavailableReason(status.available ? null : status.message || null);
      } catch (err) {
        if (cancelled) return;
        setTrainingAvailable(false);
        setTrainingUnavailableReason(err?.message || null);
      }
      try {
        const current = await wakeRequest('/v1/wake/jobs/current');
        if (cancelled || !current.job) return;
        setWakeWordText(current.job.wake_word || '');
        setTrainingStatus({ status: current.job.status, progress: 0 });
        setTrainingJobId(current.job.job_id);
        setWizardOpen(true);
      } catch {
        // Nothing to resume is the common case, and a failed check changes nothing.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!trainingJobId) return undefined;

    let cancelled = false;
    const poll = async () => {
      try {
        const data = await wakeRequest(`/v1/wake/jobs/${encodeURIComponent(trainingJobId)}`);
        if (cancelled) return;
        setTrainingStatus(data);
        if (data.status === 'done') {
          setCompletedModel({
            model_id: data.model_id,
            model_path: data.model_path,
          });
          setTrainingJobId(null);
          await reloadModels();
        } else if (data.status === 'failed') {
          setTrainingError(data.error || 'Training failed.');
          setTrainingErrorReason(data.error_reason || null);
          setTrainingJobId(null);
        }
      } catch (err) {
        if (!cancelled) {
          setTrainingError(err?.message || 'Custom wake-words are temporarily unavailable.');
          setTrainingErrorReason(null);
          setTrainingJobId(null);
        }
      }
    };

    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [trainingJobId, reloadModels]);

  const resetWizard = useCallback(() => {
    setWizardOpen(false);
    setWakeWordText('');
    setSamples([]);
    setRecording(false);
    setTrainingJobId(null);
    setTrainingStatus(null);
    setTrainingError(null);
    setTrainingErrorReason(null);
    setCompletedModel(null);
    setWordRefusal(null);
    setWordChecked('');
  }, []);

  // A bad word is refused before anyone spends time recording it.
  const checkWord = useCallback(async () => {
    const wakeWord = wakeWordText.trim();
    if (!wakeWord) return false;
    if (wakeWord === wordChecked) return !wordRefusal;
    try {
      const verdict = await wakeRequest('/v1/wake/check-word', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ wake_word: wakeWord }),
      });
      setWordChecked(wakeWord);
      setWordRefusal(verdict.ok ? null : verdict.reason || 'That word will not work. Try a different one.');
      return Boolean(verdict.ok);
    } catch {
      // The service checks again at Train, so a failed check should not stop the user.
      return true;
    }
  }, [wakeWordText, wordChecked, wordRefusal]);

  const addSample = useCallback(async () => {
    if (recording || samples.length >= SAMPLE_LIMIT) return;
    setTrainingError(null);
    setTrainingErrorReason(null);
    if (!(await checkWord())) return;
    setRecording(true);
    try {
      const blob = await recordWavSample();
      setSamples((current) => [
        ...current,
        {
          blob,
          name: `sample_${String(current.length + 1).padStart(2, '0')}.wav`,
          size: blob.size,
        },
      ]);
    } catch (err) {
      setTrainingError(err?.message || 'Could not record microphone audio.');
    } finally {
      setRecording(false);
    }
  }, [checkWord, recording, samples.length]);

  const removeSample = useCallback((indexToRemove) => {
    setSamples((current) => current.filter((_, index) => index !== indexToRemove));
  }, []);

  const startTraining = useCallback(async () => {
    const wakeWord = wakeWordText.trim();
    if (!wakeWord || samples.length < SAMPLE_TARGET) return;

    const form = new FormData();
    form.append('wake_word', wakeWord);
    samples.forEach((sample) => {
      form.append('samples', sample.blob, sample.name);
    });

    setTrainingError(null);
    setTrainingErrorReason(null);
    setNotice(null);
    setCompletedModel(null);
    setTrainingStatus({ status: 'queued', progress: 0 });
    try {
      const data = await wakeRequest('/v1/wake/train', {
        method: 'POST',
        body: form,
      });
      setTrainingJobId(data.job_id);
      setTrainingStatus({ status: data.status || 'queued', progress: 0 });
    } catch (err) {
      setTrainingStatus(null);
      setTrainingError(err?.message || 'Custom wake-words are temporarily unavailable.');
      setTrainingErrorReason(null);
    }
  }, [samples, wakeWordText]);

  const activateModel = useCallback(async (model) => {
    if (!model || activatingRef.current) return false;
    activatingRef.current = true;
    setActivating(true);
    try {
      await wakeRequest('/v1/wake/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: model.model_id, model_path: model.model_path }),
      });
      setNotice(`Selected "${model.name || 'Viola'}" as your wake word.`);
      setLoadError(null);
      await reloadModels();
      return true;
    } catch (err) {
      setLoadError(err?.message || 'Could not activate that wake word.');
      return false;
    } finally {
      activatingRef.current = false;
      setActivating(false);
    }
  }, [reloadModels]);

  // The finished-training row is the same action as the list's button; once the word
  // is in use the wizard has nothing left to offer, so it closes and the list shows
  // the word as Selected.
  const applyCompletedModel = useCallback(async (model) => {
    if (await activateModel(model)) resetWizard();
  }, [activateModel, resetWizard]);

  const deleteModel = useCallback(async (model) => {
    if (!model || model.is_default) return;
    try {
      await wakeRequest(`/v1/wake/models/${encodeURIComponent(model.model_id)}`, {
        method: 'DELETE',
      });
      setNotice(`Deleted "${model.name}".`);
      await reloadModels();
    } catch (err) {
      setLoadError(err?.message || 'Could not delete that wake word.');
    }
  }, [reloadModels]);

  const discardCompleted = useCallback(async () => {
    if (!completedModel?.model_id) {
      resetWizard();
      return;
    }
    const model = models.find((row) => row.model_id === completedModel.model_id);
    if (model && !model.is_default) {
      await deleteModel(model);
    }
    resetWizard();
  }, [completedModel, deleteModel, models, resetWizard]);

  const completedModelRow = completedModel?.model_id
    ? models.find((model) => model.model_id === completedModel.model_id)
    : null;
  const canTrain = wakeWordText.trim() && samples.length >= SAMPLE_TARGET && !trainingJobId;
  const progressPct = Math.max(0, Math.min(100, Math.round(Number(trainingStatus?.progress || 0))));
  const statusText = trainingStageText(trainingStatus?.status || 'queued', progressPct);

  // Switching and deleting on-disk wake models has no cloud analog at all, so
  // the whole panel becomes the upsell rather than an empty model list with
  // live Switch/Delete buttons.
  if (isFeatureHidden('wake_models')) {
    return (
      <div style={{ padding: '14px 20px' }}>
        <DesktopUpsell feature="wake_models" compact />
      </div>
    );
  }

  return (
    <div style={{ padding: '14px 20px' }}>
      {loadError && (
        <p style={{ color: theme.colors.statusRed, fontSize: 12 }}>{loadError}</p>
      )}
      {notice && !loadError && (
        <p style={{ color: theme.colors.statusGreen, fontSize: 12 }}>{notice}</p>
      )}

      <div style={{ marginBottom: 18 }}>
        <div style={{ fontSize: 14, color: theme.colors.textSecondary, marginBottom: 6 }}>
          {modelsLoading ? 'Loading wake word selection…' : loadError ? 'Wake word selection unavailable' : activeModel ? (
            <>
              Selected wake word:{' '}
              <strong style={{ color: theme.colors.textPrimary }}>{activeModel.name}</strong>
            </>
          ) : 'No wake word selected'}
        </div>
        {activeModelPath && (
          <div style={{ fontSize: 11, color: theme.colors.textMuted, overflowWrap: 'anywhere' }}>
            {activeModelPath}
          </div>
        )}
      </div>

      <div style={{ marginBottom: 20 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12 }}>
          <h3 style={{ margin: 0, color: theme.colors.textPrimary, fontSize: 15 }}>My Wake Words</h3>
          <button
            onClick={() => {
              if (!trainingAvailable) return;
              setWizardOpen(true);
              setNotice(null);
              setTrainingError(null);
            }}
            disabled={!trainingAvailable}
            title={trainingAvailable ? 'Create a custom wake word' : 'Training is not available right now'}
            style={buttonStyle(theme, 'primary', !trainingAvailable)}
          >
            {trainingAvailable ? 'Create' : 'Unavailable'}
          </button>
        </div>
        {!trainingAvailable && (
          <div style={{ color: theme.colors.textSecondary, fontSize: 12, marginTop: 8 }}>
            {trainingUnavailableReason || 'Training is not available right now.'} You can still
            switch or delete the wake words you already have.
          </div>
        )}

        <div style={{ marginTop: 10 }}>
          <button
            onClick={() => setTipsOpen((open) => !open)}
            style={{
              background: 'none',
              border: 'none',
              padding: 0,
              color: theme.colors.textSecondary,
              fontSize: 12,
              fontWeight: 600,
              cursor: 'pointer',
              textDecoration: 'underline',
            }}
          >
            {tipsOpen ? 'Hide tips for training a good wake word' : 'Tips for training a good wake word'}
          </button>
          {tipsOpen && (
            <div
              style={{
                marginTop: 8,
                padding: 10,
                borderRadius: 8,
                border: `1px solid ${theme.colors.borderSubtle}`,
                backgroundColor: theme.colors.bgElevated,
                color: theme.colors.textSecondary,
                fontSize: 12,
                lineHeight: 1.5,
              }}
            >
              <WakeWordTrainingTips />
            </div>
          )}
        </div>

        <div style={{ display: 'grid', gap: 8, marginTop: 10 }}>
          {models.map((model) => (
            <div
              key={model.model_id}
              style={{
                display: 'grid',
                gridTemplateColumns: 'minmax(120px, 1fr) auto auto',
                alignItems: 'center',
                gap: 8,
                minHeight: 44,
                padding: '8px 10px',
                borderRadius: 8,
                border: `1px solid ${theme.colors.borderSubtle}`,
                backgroundColor: theme.colors.bgElevated,
              }}
            >
              <div style={{ minWidth: 0 }}>
                {(() => {
                  const canActivate = model.is_supported !== false;
                  return (
                    <>
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                  <span style={{ color: theme.colors.textPrimary, fontSize: 13, fontWeight: 600 }}>
                    {model.name}
                  </span>
                  {model.is_active && (
                    <span style={{
                      color: '#fff',
                      backgroundColor: theme.colors.accent,
                      borderRadius: 6,
                      padding: '2px 6px',
                      fontSize: 10,
                      fontWeight: 700,
                    }}>
                      Selected
                    </span>
                  )}
                  {!canActivate && (
                    <span style={{
                      color: theme.colors.statusRed,
                      backgroundColor: theme.colors.bgSurface,
                      borderRadius: 6,
                      padding: '2px 6px',
                      fontSize: 10,
                      fontWeight: 700,
                    }}>
                      Unsupported
                    </span>
                  )}
                </div>
                <div style={{ color: theme.colors.textMuted, fontSize: 11, overflowWrap: 'anywhere' }}>
                  {model.is_default ? 'Default Viola model' : model.model_id}
                </div>
                <div style={{ color: theme.colors.textSecondary, fontSize: 11, marginTop: 2 }}>
                  {architectureLabel(model.architecture)}
                  {model.quality_grade ? ` - Grade ${model.quality_grade}` : ''}
                </div>
                {!canActivate && model.unsupported_reason && (
                  <div style={{ color: theme.colors.statusRed, fontSize: 11, marginTop: 2 }}>
                    {model.unsupported_reason}
                  </div>
                )}
                    </>
                  );
                })()}
              </div>
              <button
                onClick={() => activateModel(model)}
                disabled={activating || model.is_active || model.is_supported === false}
                style={buttonStyle(
                  theme,
                  model.is_active || model.is_supported === false ? 'secondary' : 'primary',
                  activating || model.is_active || model.is_supported === false,
                )}
              >
                {model.is_supported === false ? 'Unsupported' : model.is_active ? 'In use' : USE_WORD_LABEL}
              </button>
              {!model.is_default ? (
                <button
                  onClick={() => deleteModel(model)}
                  style={buttonStyle(theme, 'danger')}
                >
                  Delete
                </button>
              ) : (
                <span style={{ width: 58 }} />
              )}
            </div>
          ))}
        </div>
      </div>

      {wizardOpen && (
        <div style={{
          padding: 14,
          backgroundColor: theme.colors.bgElevated,
          border: `1px solid ${theme.colors.borderSubtle}`,
          borderRadius: 8,
        }}>
          <label style={{ display: 'block', fontSize: 12, color: theme.colors.textSecondary, marginBottom: 6 }}>
            Wake word
          </label>
          <input
            type="text"
            value={wakeWordText}
            onChange={(event) => {
              setWakeWordText(event.target.value);
              setWordRefusal(null);
            }}
            onBlur={checkWord}
            placeholder="e.g. Athena"
            disabled={Boolean(trainingJobId)}
            style={{
              width: '100%',
              padding: '8px 10px',
              marginBottom: wordRefusal ? 4 : 12,
              borderRadius: 8,
              border: `1px solid ${theme.colors.borderLight}`,
              backgroundColor: theme.colors.bgCard,
              color: theme.colors.textPrimary,
              fontSize: 14,
              boxSizing: 'border-box',
            }}
          />

          {wordRefusal && (
            <div role="alert" style={{ color: theme.colors.statusRed, fontSize: 12, marginBottom: 12 }}>
              {wordRefusal}
            </div>
          )}

          <div style={{ color: theme.colors.textSecondary, fontSize: 12, marginBottom: 10 }}>
            Record yourself saying "{wakeWordText.trim() || 'your wake word'}" {SAMPLE_TARGET} times.
            Your recordings go to Viola's training service, are used only to build this wake word,
            and are deleted about a day after training.
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
            <button
              onClick={addSample}
              disabled={recording || samples.length >= SAMPLE_LIMIT || Boolean(trainingJobId)}
              style={buttonStyle(theme, 'primary', recording || samples.length >= SAMPLE_LIMIT || Boolean(trainingJobId))}
            >
              {recording ? 'Recording...' : 'Record Sample'}
            </button>
            <span style={{ color: theme.colors.textMuted, fontSize: 12 }}>
              {samples.length}/{SAMPLE_TARGET} required
            </span>
          </div>

          {samples.length > 0 && (
            <div style={{ display: 'grid', gap: 6, marginBottom: 12 }}>
              {samples.map((sample, index) => (
                <div
                  key={sample.name}
                  style={{
                    display: 'grid',
                    gridTemplateColumns: '1fr auto',
                    alignItems: 'center',
                    gap: 8,
                    color: theme.colors.textSecondary,
                    fontSize: 12,
                  }}
                >
                  <span>{sample.name} ({Math.round(sample.size / 1024)} KB)</span>
                  <button
                    onClick={() => removeSample(index)}
                    disabled={Boolean(trainingJobId)}
                    style={buttonStyle(theme, 'secondary', Boolean(trainingJobId))}
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
          )}

          {trainingStatus && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ color: theme.colors.textSecondary, fontSize: 12, marginBottom: 6 }}>
                {statusText}
                {trainingJobId && ' (usually about 10 minutes, and you can close Settings)'}
              </div>
              <div style={{
                height: 8,
                borderRadius: 4,
                backgroundColor: theme.colors.bgSurface,
                overflow: 'hidden',
              }}>
                <div style={{
                  height: '100%',
                  width: `${progressPct}%`,
                  backgroundColor: theme.colors.accent,
                  transition: 'width 0.3s',
                }} />
              </div>
            </div>
          )}

          {trainingError && (
            <div style={{ marginBottom: 12 }}>
              <div style={{ color: theme.colors.statusRed, fontSize: 12, marginBottom: 8 }}>
                {trainingError}
              </div>
              <div
                style={{
                  padding: 10,
                  borderRadius: 8,
                  border: `1px solid ${theme.colors.borderSubtle}`,
                  backgroundColor: theme.colors.bgSurface,
                  color: theme.colors.textSecondary,
                  fontSize: 12,
                  lineHeight: 1.5,
                }}
              >
                <WakeWordTrainingTips reasonCode={trainingErrorReason} />
              </div>
            </div>
          )}

          {completedModelRow ? (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <span style={{ color: theme.colors.textSecondary, fontSize: 12, width: '100%' }}>
                "{completedModelRow.name}" is ready. Nothing changes until you press "{USE_WORD_LABEL}".
              </span>
              <button
                onClick={() => applyCompletedModel(completedModelRow)}
                disabled={activating}
                style={buttonStyle(theme, 'primary', activating)}
              >
                {USE_WORD_LABEL}
              </button>
              <button
                onClick={discardCompleted}
                style={buttonStyle(theme, 'danger')}
              >
                Discard
              </button>
            </div>
          ) : (
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button
                onClick={startTraining}
                disabled={!canTrain || Boolean(wordRefusal)}
                style={buttonStyle(theme, 'primary', !canTrain || Boolean(wordRefusal))}
              >
                Train
              </button>
              {trainingError && (
                <button
                  onClick={() => {
                    setTrainingError(null);
                    setTrainingErrorReason(null);
                    setTrainingStatus(null);
                  }}
                  style={buttonStyle(theme)}
                >
                  Retry
                </button>
              )}
              <button
                onClick={resetWizard}
                disabled={Boolean(trainingJobId)}
                style={buttonStyle(theme, 'secondary', Boolean(trainingJobId))}
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

WakeWordSection.propTypes = {
  theme: PropTypes.object,
};
