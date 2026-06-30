// static/js/voiceRecorder.js

/**
 * Voice recording with optional Speech-to-Text transcription.
 *
 * STT providers:
 *   "disabled"       — record audio as file attachment (original behavior)
 *   "browser"        — use Web Speech API for real-time transcription
 *   "local"          — send recording to server /api/stt/transcribe (Whisper)
 *   "endpoint:<id>"  — send recording to server /api/stt/transcribe (API)
 */

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let recordingInterval = null;
let silenceTimer = null;
let silenceAudioContext = null;
let continuousState = null;

// Browser STT state
let _recognition = null;
let _browserTranscript = '';

// Cached STT provider — refreshed on settings change
let _sttProvider = 'disabled';

/**
 * Fetch current STT provider from server settings
 */
async function refreshSttProvider() {
  try {
    const res = await fetch('/api/stt/stats', { credentials: 'same-origin' });
    if (res.ok) {
      const stats = await res.json();
      _sttProvider = stats.provider || 'disabled';
      // Notify the send button to update its icon
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  } catch (e) {
    console.warn('Failed to fetch STT stats:', e);
  }
}

/**
 * Format seconds as MM:SS
 */
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

/**
 * Reset UI state after recording ends
 */
function _resetRecordingUI() {
  isRecording = false;
  if (recordingInterval) {
    clearInterval(recordingInterval);
    recordingInterval = null;
  }
  if (silenceTimer) {
    clearInterval(silenceTimer);
    silenceTimer = null;
  }
  if (silenceAudioContext) {
    silenceAudioContext.close().catch(() => {});
    silenceAudioContext = null;
  }
  // Reset send button via global callback
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) {
    sendBtn.classList.remove('recording');
    sendBtn.dataset.mode = '';
  }
  if (window._updateSendBtnIcon) {
    setTimeout(window._updateSendBtnIcon, 50);
  }
}

/**
 * Start browser speech recognition alongside recording
 */
function startBrowserSTT() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;

  _browserTranscript = '';
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = false;
  _recognition.lang = '';

  _recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        _browserTranscript += event.results[i][0].transcript + ' ';
      }
    }
  };

  _recognition.onerror = (e) => {
    console.warn('Browser STT error:', e.error);
  };

  _recognition.start();
}

function stopBrowserSTT() {
  if (_recognition) {
    try { _recognition.stop(); } catch (e) { /* ignore */ }
    _recognition = null;
  }
  return _browserTranscript.trim();
}

/**
 * Send audio to server for transcription
 */
async function transcribeOnServer(audioBlob) {
  const formData = new FormData();
  formData.append('file', audioBlob, 'audio.webm');

  const res = await fetch('/api/stt/transcribe', {
    method: 'POST',
    credentials: 'same-origin',
    body: formData,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Transcription failed');
  }

  const data = await res.json();
  return data.text || '';
}

function _stopTracks(stream) {
  if (stream) stream.getTracks().forEach(track => track.stop());
}

/**
 * Insert transcribed text into the chat input
 */
function insertTranscription(text, showToast) {
  if (!text) return;
  const input = document.getElementById('message');
  if (!input) return;

  const existing = input.value.trim();
  input.value = existing ? existing + ' ' + text : text;

  // Trigger auto-resize and icon update
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();

  if (showToast) showToast('Transcribed');
}

/**
 * Start voice recording
 */
export function startRecording(onFileCreated, showToast, showError, options = {}) {
  // Check for secure context (getUserMedia requires HTTPS or localhost)
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    _resetRecordingUI();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    _resetRecordingUI();
    return;
  }

  audioChunks = [];

  // Detect best supported mime type — Safari doesn't support audio/webm
  const _mimeTypeCandidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/aac',
  ];
  const _detectedMime = _mimeTypeCandidates.find(t => MediaRecorder.isTypeSupported(t)) || '';

  navigator.mediaDevices.getUserMedia({ audio: true })
    .then(stream => {
      mediaRecorder = _detectedMime
        ? new MediaRecorder(stream, { mimeType: _detectedMime })
        : new MediaRecorder(stream);

      mediaRecorder.ondataavailable = event => {
        if (event.data.size > 0) {
          audioChunks.push(event.data);
        }
      };

      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach(track => track.stop());

        const audioBlob = new Blob(audioChunks, { type: _detectedMime || 'audio/webm' });
        const provider = _sttProvider;

        if (provider === 'browser') {
          const transcript = stopBrowserSTT();
          if (transcript) {
            insertTranscription(transcript, showToast);
            if (options.onTranscript) options.onTranscript(transcript);
          } else {
            if (showToast) showToast('No speech detected');
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
          }
        } else if (provider === 'local' || provider.startsWith('endpoint:')) {
          // Show "Transcribing..." feedback
          if (showToast) showToast('Transcribing...', 5000);
          try {
            const transcript = await transcribeOnServer(audioBlob);
            if (transcript) {
              insertTranscription(transcript, showToast);
              if (options.onTranscript) options.onTranscript(transcript);
            } else {
              if (showToast) showToast('No speech detected');
            }
          } catch (e) {
            console.error('STT transcription error:', e);
            if (showError) showError('Transcription failed: ' + e.message);
            // Fallback: attach as file
            const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
            if (onFileCreated) onFileCreated(audioFile);
          }
        } else {
          // STT disabled — attach audio file
          const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
          if (onFileCreated) onFileCreated(audioFile);
        }

        _resetRecordingUI();
        if (options.onStop) options.onStop();
      };

      mediaRecorder.start();
      isRecording = true;
      recordingStartTime = new Date();

      if (options.autoStopOnSilence) {
        try {
          const AudioContext = window.AudioContext || window.webkitAudioContext;
          silenceAudioContext = AudioContext ? new AudioContext() : null;
          if (silenceAudioContext) {
            const source = silenceAudioContext.createMediaStreamSource(stream);
            const analyser = silenceAudioContext.createAnalyser();
            analyser.fftSize = 1024;
            source.connect(analyser);
            const data = new Uint8Array(analyser.fftSize);
            let speechStarted = false;
            let quietSince = null;
            const minMs = options.minRecordMs || 800;
            const quietMs = options.silenceMs || 1200;
            const maxMs = options.maxRecordMs || 20000;
            const threshold = options.silenceThreshold || 0.025;
            silenceTimer = setInterval(() => {
              if (!mediaRecorder || mediaRecorder.state !== 'recording') return;
              analyser.getByteTimeDomainData(data);
              let sum = 0;
              for (let i = 0; i < data.length; i++) {
                const v = (data[i] - 128) / 128;
                sum += v * v;
              }
              const rms = Math.sqrt(sum / data.length);
              const elapsed = Date.now() - recordingStartTime.getTime();
              if (rms > threshold) {
                speechStarted = true;
                quietSince = null;
              } else if (speechStarted && elapsed > minMs) {
                if (!quietSince) quietSince = Date.now();
                if (Date.now() - quietSince >= quietMs) stopRecording();
              }
              if (elapsed >= maxMs) stopRecording();
            }, 200);
          }
        } catch (e) {
          console.warn('Silence auto-stop unavailable:', e);
        }
      }

      // Start browser STT if that's the provider
      if (_sttProvider === 'browser') {
        startBrowserSTT();
      }

      if (showToast) {
        showToast('Recording...');
      }
    })
    .catch(error => {
      console.error('Microphone access error:', error);
      if (showError) {
        if (error.name === 'NotAllowedError') {
          showError('Microphone access denied. Check browser permissions.');
        } else if (error.name === 'NotFoundError') {
          showError('No microphone found.');
        } else {
          showError('Microphone error: ' + error.message);
        }
      }
      _resetRecordingUI();
    });
}

/**
 * Stop voice recording
 */
export function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    mediaRecorder.stop();
    // isRecording will be set to false in _resetRecordingUI called from onstop
  } else {
    _resetRecordingUI();
  }
}

export async function startContinuousRecording(showToast, showError, options = {}) {
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    return false;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    return false;
  }

  if (_sttProvider === 'disabled') {
    if (showError) showError('Turn on Speech to Text before starting voice mode.');
    return false;
  }
  if (_sttProvider === 'browser') {
    if (showError) showError('Continuous voice mode needs local Whisper or an STT endpoint.');
    return false;
  }

  if (continuousState?.active) return true;

  // Create AudioContext synchronously before any await — Safari suspends it
  // if created after an await, causing the analyser to always read silence.
  const AudioContext = window.AudioContext || window.webkitAudioContext;
  const audioContext = AudioContext ? new AudioContext() : null;
  if (!audioContext) {
    if (showError) showError('Audio analysis is not supported in this browser.');
    return false;
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  if (audioContext.state === 'suspended') {
    await audioContext.resume();
  }

  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 1024;
  audioContext.createMediaStreamSource(stream).connect(analyser);

  const mimeTypeCandidates = [
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
    'audio/aac',
  ];
  const mimeType = mimeTypeCandidates.find(type => MediaRecorder.isTypeSupported(type)) || '';
  const vadData = new Uint8Array(analyser.fftSize);
  const useChunkedTranscription = options.chunkedTranscription === true;
  const tickMs = options.tickMs || 500;

  continuousState = {
    active: true,
    stream,
    audioContext,
    analyser,
    mimeType,
    recorder: null,
    vadData,
    isSpeaking: false,
    speechStarted: false,
    quietSince: null,
    noiseFloor: null,
    noiseSamples: 0,
    utteranceStartedAt: null,
    chunkSeq: 0,
    rollingChunks: [],
    utteranceChunks: [],
    maxPreludeChunks: options.preludeChunks || 4,
    segmentMs: 0,
    segmentTimer: null,
    tickMs,
    lastTickAt: Date.now(),
    tickRestarting: false,
    finalizeTimer: null,
    vadTimer: null,
    transcribeCount: 0,
    // Dedicated single-recorder for the current utterance — avoids concatenating
    // blobs from multiple MediaRecorder instances which produces invalid audio.
    utteranceRecorder: null,
    utteranceRecorderChunks: [],
    options,
    showToast,
    showError,
  };

  const maxUtteranceMs = options.maxUtteranceMs || 30000;

  if (useChunkedTranscription) {
    _startContinuousChunking(options.continuousChunkMs || 3500);
  } else {
    // VAD (silence-based) mode: capture audio in short rotating ticks (same
    // Safari-safe stop()-per-tick approach as chunked mode) purely to keep a
    // rolling buffer; the VAD loop below decides when an utterance actually
    // starts/ends based on real silence, not a fixed clock — so a whole
    // sentence accumulates before anything is sent, instead of each ~3.5s
    // window being treated as a complete thought.
    _startTickCapture(tickMs);
    _startContinuousVad(maxUtteranceMs);
  }
  if (showToast) showToast('Voice mode listening...');
  return true;
}

function _startTickCapture(tickMs) {
  const st = continuousState;
  if (!st?.active) return;

  const chunks = [];
  const recorder = st.mimeType ? new MediaRecorder(st.stream, { mimeType: st.mimeType }) : new MediaRecorder(st.stream);
  st.recorder = recorder;
  st.tickMs = tickMs;
  st.lastTickAt = Date.now();

  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };
  recorder.onerror = (event) => {
    console.error('Continuous recorder error:', event.error || event);
    if (st.showError) st.showError('Voice mode recorder error.');
    stopContinuousRecording();
  };
  recorder.onstop = () => {
    if (continuousState !== st || !st.active) return;
    const shouldRestart = st.recorder === recorder;
    st.lastTickAt = Date.now();
    st.tickRestarting = false;
    let chunkSize = 0;
    chunks.forEach((blob) => {
      chunkSize += blob.size;
      const item = { blob, seq: ++st.chunkSeq };
      st.rollingChunks.push(item);
      if (st.rollingChunks.length > st.maxPreludeChunks) st.rollingChunks.shift();
      if (st.speechStarted) st.utteranceChunks.push(item);
    });
    if (chunkSize && st.options.onAudioChunk) st.options.onAudioChunk(chunkSize);
    if (shouldRestart) _startTickCapture(tickMs);
  };

  recorder.start();
  st.segmentTimer = setTimeout(() => {
    if (st.recorder === recorder && recorder.state === 'recording') {
      try { recorder.stop(); } catch (e) { /* ignore */ }
    }
  }, tickMs);
}

function _restartTickCapture(st) {
  if (!st?.active || st.tickRestarting) return;
  st.tickRestarting = true;
  if (st.segmentTimer) {
    clearTimeout(st.segmentTimer);
    st.segmentTimer = null;
  }
  const oldRecorder = st.recorder;
  if (oldRecorder && oldRecorder.state === 'recording') {
    try { oldRecorder.stop(); } catch (e) { /* ignore */ }
  }
  setTimeout(() => {
    if (continuousState !== st || !st.active) return;
    if (st.recorder === oldRecorder || !st.recorder || st.recorder.state === 'inactive') {
      _startTickCapture(st.tickMs || 500);
    }
    st.tickRestarting = false;
  }, 250);
}

// Records one fixed-length segment with a dedicated MediaRecorder, then (if
// still active) immediately starts the next one. A single long-lived
// recorder driven by start(timeslice)/requestData() doesn't reliably flush
// periodic `dataavailable` chunks in Safari, so continuous mode captured
// zero audio there even though the UI looked active. stop() is
// spec-guaranteed to flush all buffered data in every browser, and a fresh
// recorder per segment also means each segment is a complete,
// independently-decodable file (no fragmented-container-header problem on
// segments after the first, which a shared-recorder approach has for
// container formats like mp4).
function _startContinuousChunking(segmentMs) {
  const st = continuousState;
  if (!st) return;
  st.segmentMs = segmentMs;
  _recordNextSegment();
}

function _recordNextSegment() {
  const st = continuousState;
  if (!st?.active) return;

  const chunks = [];
  const recorder = st.mimeType ? new MediaRecorder(st.stream, { mimeType: st.mimeType }) : new MediaRecorder(st.stream);
  st.recorder = recorder;

  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };
  recorder.onerror = (event) => {
    console.error('Continuous recorder error:', event.error || event);
    if (st.showError) st.showError('Voice mode recorder error.');
    stopContinuousRecording();
  };
  recorder.onstop = () => {
    if (chunks.length && st.options.onAudioChunk) {
      st.options.onAudioChunk(chunks.reduce((n, c) => n + c.size, 0));
    }
    _finalizeContinuousSegment(st, chunks, recorder.mimeType || st.mimeType);
    if (st.active && continuousState === st) _recordNextSegment();
  };

  recorder.start();
  st.segmentTimer = setTimeout(() => {
    if (st.recorder === recorder && recorder.state === 'recording') {
      try { recorder.stop(); } catch (e) { /* ignore */ }
    }
  }, st.segmentMs);
}

function _finalizeContinuousSegment(st, chunks, mimeType) {
  if (!chunks || !chunks.length) return;

  const audioBlob = new Blob(chunks, { type: mimeType || 'audio/webm' });
  st.transcribeCount += 1;
  if (st.options.onTranscriptionStart) st.options.onTranscriptionStart(st.transcribeCount);

  transcribeOnServer(audioBlob)
    .then((text) => {
      if (continuousState !== st) return;
      const trimmed = (text || '').trim();
      if (trimmed) {
        if (st.options.onSpeechStart) st.options.onSpeechStart();
        if (st.options.onTranscript) st.options.onTranscript(trimmed);
        if (st.options.onSpeechEnd) st.options.onSpeechEnd();
      }
    })
    .catch((e) => {
      console.error('Continuous STT transcription error:', e);
      if (st.showError) st.showError('Transcription failed: ' + e.message);
    })
    .finally(() => {
      if (continuousState === st && st.options.onTranscriptionEnd) st.options.onTranscriptionEnd();
    });
}

function _startContinuousVad(maxUtteranceMs) {
  const st = continuousState;
  if (!st) return;

  const minSpeechMs = st.options.minSpeechMs || 350;
  const silenceMs = st.options.silenceMs || 8000;
  const manualThreshold = Number(st.options.silenceThreshold || 0);
  const minThreshold = Number(st.options.minSilenceThreshold || 0.010);
  const thresholdMultiplier = Number(st.options.thresholdMultiplier || 3.25);
  const maxThreshold = Number(st.options.maxSilenceThreshold || 0.055);
  const thresholdCeiling = Math.max(minThreshold, maxThreshold);

  let loudSince = null;
  st.vadTimer = setInterval(() => {
    const current = continuousState;
    if (!current?.active) return;

    // Skip VAD when TTS is playing or in cooldown — prevents mic from picking
    // up speaker output and falsely triggering a new recording.
    if (current.suppressUntil && Date.now() < current.suppressUntil) {
      loudSince = null;
      return;
    }

    if (current.lastTickAt && Date.now() - current.lastTickAt > 2500) {
      _restartTickCapture(current);
    }

    current.analyser.getByteTimeDomainData(current.vadData);
    let sum = 0;
    for (let i = 0; i < current.vadData.length; i++) {
      const v = (current.vadData[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / current.vadData.length);
    const now = Date.now();
    if (!current.speechStarted && !loudSince) {
      if (current.noiseFloor == null) current.noiseFloor = rms;
      else current.noiseFloor = current.noiseFloor * 0.92 + rms * 0.08;
      current.noiseSamples += 1;
    }

    const adaptiveBase = current.noiseFloor == null
      ? minThreshold
      : current.noiseFloor * thresholdMultiplier;
    const adaptiveThreshold = Math.min(thresholdCeiling, Math.max(minThreshold, adaptiveBase));
    const threshold = manualThreshold > 0 ? manualThreshold : adaptiveThreshold;
    const loud = rms > threshold;
    if (current.options.onVadLevel) current.options.onVadLevel({ rms, threshold, loud, speaking: current.speechStarted });

    if (loud) {
      if (current.finalizeTimer) {
        clearTimeout(current.finalizeTimer);
        current.finalizeTimer = null;
      }
      if (!loudSince) loudSince = now;
      current.quietSince = null;
      if (!current.speechStarted && now - loudSince >= minSpeechMs) {
        current.speechStarted = true;
        current.isSpeaking = true;
        current.utteranceStartedAt = now;
        // Start a single dedicated recorder for this utterance so we produce one
        // valid, self-contained audio file instead of concatenating blobs from
        // multiple per-tick MediaRecorder instances (which produces invalid audio).
        current.utteranceChunks = [];
        current.utteranceRecorderChunks = [];
        if (current.utteranceRecorder && current.utteranceRecorder.state !== 'inactive') {
          try { current.utteranceRecorder.stop(); } catch (_e) {}
        }
        try {
          const uRec = current.mimeType
            ? new MediaRecorder(current.stream, { mimeType: current.mimeType })
            : new MediaRecorder(current.stream);
          current.utteranceRecorder = uRec;
          uRec.ondataavailable = (ev) => {
            // Don't guard against current.utteranceRecorder === uRec here —
            // _finalizeContinuousUtterance nulls utteranceRecorder before calling
            // uRec.stop(), so ondataavailable would see null === uRec → false and
            // silently drop the audio data. Safe to push unconditionally because
            // utteranceRecorderChunks is cleared at every new utterance start.
            if (ev.data.size > 0) current.utteranceRecorderChunks.push(ev.data);
          };
          uRec.onerror = () => {};
          uRec.start();
        } catch (e) {
          console.warn('Utterance recorder start failed:', e);
          current.utteranceRecorder = null;
        }
        if (current.options.onSpeechStart) current.options.onSpeechStart();
      }
      return;
    }

    loudSince = null;
    if (!current.speechStarted) return;

    if (!current.quietSince) current.quietSince = now;
    const utteranceAge = current.utteranceStartedAt ? now - current.utteranceStartedAt : 0;
    if ((now - current.quietSince >= silenceMs || utteranceAge >= maxUtteranceMs) && !current.finalizeTimer) {
      // No requestData() needed — _startTickCapture's rotating recorders
      // already flush continuously, so utteranceChunks is already current.
      current.finalizeTimer = setTimeout(() => {
        if (continuousState) continuousState.finalizeTimer = null;
        _finalizeContinuousUtterance();
      }, 120);
    }
  }, 100);
}

function _finalizeContinuousUtterance() {
  const st = continuousState;
  if (!st?.active || !st.speechStarted) return;

  st.speechStarted = false;
  st.isSpeaking = false;
  st.quietSince = null;
  st.utteranceStartedAt = null;
  st.utteranceChunks = [];

  if (st.options.onSpeechEnd) st.options.onSpeechEnd();

  // Grab and clear the dedicated utterance recorder
  const uRec = st.utteranceRecorder;
  st.utteranceRecorder = null;

  if (!uRec) return;

  const _sendChunks = () => {
    const chunks = st.utteranceRecorderChunks.splice(0);
    if (!chunks.length) return;
    const audioBlob = new Blob(chunks, { type: st.mimeType || 'audio/webm' });
    st.transcribeCount += 1;
    if (st.options.onTranscriptionStart) st.options.onTranscriptionStart(st.transcribeCount);
    transcribeOnServer(audioBlob)
      .then((text) => {
        if (!continuousState?.active) return;
        const trimmed = (text || '').trim();
        if (trimmed && st.options.onTranscript) st.options.onTranscript(trimmed);
        else if (st.showToast) st.showToast('No speech detected');
      })
      .catch((e) => {
        console.error('Continuous STT transcription error:', e);
        if (st.showError) st.showError('Transcription failed: ' + e.message);
      })
      .finally(() => {
        if (continuousState?.active && st.options.onTranscriptionEnd) st.options.onTranscriptionEnd();
      });
  };

  if (uRec.state === 'recording') {
    uRec.onstop = _sendChunks;
    try { uRec.stop(); } catch (e) { _sendChunks(); }
  } else {
    _sendChunks();
  }
}

export function stopContinuousRecording() {
  const st = continuousState;
  if (!st) return;
  st.active = false;
  if (st.vadTimer) clearInterval(st.vadTimer);
  if (st.segmentTimer) clearTimeout(st.segmentTimer);
  if (st.finalizeTimer) clearTimeout(st.finalizeTimer);
  // Flush the tick recorder
  if (st.recorder && st.recorder.state !== 'inactive') {
    try { st.recorder.stop(); } catch (e) { /* ignore */ }
  }
  // Finalize any in-progress utterance (stops the dedicated utterance recorder)
  if (st.speechStarted) {
    try { _finalizeContinuousUtterance(); } catch (e) { console.warn('Failed to finalize utterance:', e); }
  } else if (st.utteranceRecorder && st.utteranceRecorder.state !== 'inactive') {
    try { st.utteranceRecorder.stop(); } catch (e) { /* ignore */ }
  }
  _stopTracks(st.stream);
  if (st.audioContext) st.audioContext.close().catch(() => {});
  continuousState = null;
}

export function getIsContinuousRecording() {
  return !!continuousState?.active;
}

export function suppressVad(ms) {
  if (continuousState) {
    continuousState.suppressUntil = Date.now() + (ms || 0);
  }
}

/**
 * Check if currently recording
 */
export function getIsRecording() {
  return isRecording;
}

/**
 * Initialize recording state
 */
export function init() {
  isRecording = false;
  refreshSttProvider();
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  startContinuousRecording,
  stopContinuousRecording,
  getIsContinuousRecording,
  suppressVad,
  getIsRecording,
  init,
  refreshSttProvider,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

export default voiceRecorderModule;
