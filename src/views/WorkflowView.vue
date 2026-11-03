<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ChevronLeft, ChevronRight, DownloadIcon, PauseIcon, PlayIcon, RotateCcwIcon, TimerIcon } from 'lucide-vue-next';
import TextStatistics from '../components/TextStatistics.vue';
import SpeedControl from '../components/SpeedControl.vue';
import AudioChunk from '../components/AudioChunk.vue';
import ModelSelector from '../components/ModelSelector.vue';
import VoiceSelector from '../components/VoiceSelector.vue';
import JobLibrary from '../components/JobLibrary.vue';
import { fetchAvailableModels } from '../utils/model-detector.js';
import { textMatches } from '../utils/resume-match.js';
import { DEFAULT_MODEL } from '../config.js';

const chapterFrom = ref(1);
const chapterTo = ref(5);
const batchNote = ref('');
const url = ref('');
const epubFile = ref(null);
const epubName = ref('');
const epubInput = ref(null);
const glossary = ref('');
const mode = ref('auto');
const provider = ref('gemini');
const text = ref('');
const job = ref(null);
const jobs = ref([]);
const formOpen = ref(false);
const workspaceTab = ref('library');
const workspaceNav = ref(null);
watch(workspaceTab, () => {
  if (window.matchMedia('(max-width: 1023px)').matches) {
    workspaceNav.value?.scrollIntoView({ block: 'start' });
  }
}, { flush: 'post' });
const serverOk = ref(true);
const starting = ref(false);
const formError = ref('');
const saveState = ref('');
const dirty = ref(false);
const partIndex = ref(1);
const partCount = ref(0);

const speed = ref(1);
const status = ref('idle');
const ttsError = ref(null);
const worker = ref(null);
const voices = ref(null);
const selectedVoice = ref(0);
const chunks = ref([]);
const result = ref(null);
const availableModels = ref([]);
const selectedModel = ref('None');
const modelsLoading = ref(false);
const loadingProgress = ref(0);
const isPlaying = ref(false);
const restartingPlayback = ref(false);
const currentChunkIndex = ref(-1);
const lastGeneration = ref(null);
const autoNext = ref(localStorage.getItem('workflow-auto-next') === '1');
const stopAfter = ref(null);
const stopDraft = ref('');
const stopError = ref('');
const stopNotice = ref('');
const stopFocused = ref(false);
const listen = ref(null);
const pendingResume = ref(null);
const chunkOrigin = ref(0);
const jumpDraft = ref('1');
const jumpInput = ref(null);
const sliderDraft = ref(1);
const sliderDragging = ref(false);
const jumpFocused = ref(false);
let advancing = false;
let jumpLock = false;

let pollTimer = null;
let jobsTimer = null;
let saveTimer = null;
let progressTimer = null;
let pendingSnapshot = null;
let flushingProgress = false;
let playToken = 0;

const LISTEN_KEY = 'workflow-listen';
const STOP_KEY = 'workflow-stop-after';
const OPEN_JOB_KEY = 'workflow-open-job';

function readListenMap() {
  try {
    const data = JSON.parse(localStorage.getItem(LISTEN_KEY) || '{}');
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

function localListen(id) {
  if (!id) return null;
  return readListenMap()[id] || null;
}

function preferListen(current, incoming) {
  if (!current) return incoming || null;
  if (!incoming) return current;
  return (incoming.updated || 0) >= (current.updated || 0) ? incoming : current;
}

function mergeJobListen(item) {
  const mark = preferListen(localListen(item.id), item.listen || null);
  return mark ? { ...item, listen: mark } : item;
}

function ordinalAt(localIndex) {
  const saved = chunks.value[localIndex]?.index;
  if (Number.isInteger(saved)) return saved;
  return chunkOrigin.value + Math.max(0, localIndex);
}

function resumeTarget() {
  const mark = listen.value;
  if (!mark || mark.done || mark.part !== partIndex.value) return null;
  if (!mark.chunk && !mark.anchor) return null;
  return { chunk: mark.chunk || 0, anchor: mark.anchor || '' };
}

const running = computed(() => job.value && (job.value.status === 'queued' || job.value.status === 'running'));
const queueBusy = computed(() =>
  jobs.value.some((item) => item.status === 'queued' || item.status === 'running')
);
const bookLink = computed(() =>
  /sangtacviet\.(?:com|vip|app)\/truyen\/[^/?#]+\/\d+\/\d+\/?$/i.test(url.value.trim())
);

watch(url, (value) => {
  if (/sangtacviet\.(com|vip|app)\/truyen\//i.test(value)) mode.value = 'sangtacviet';
});
const novelLink = computed(() => mode.value === 'sangtacviet');
const canStart = computed(() => {
  if (starting.value || running.value || queueBusy.value) return false;
  if (epubFile.value) return true;
  if (url.value.trim()) return true;
  return job.value?.source === 'epub';
});
const percent = computed(() => {
  if (!job.value) return 0;
  if (job.value.status === 'done') return 100;
  if (!job.value.total) return job.value.status === 'running' ? 8 : 0;
  return Math.min(100, Math.round((Number(job.value.done || 0) / job.value.total) * 100));
});
const processed = computed(() => {
  return lastGeneration.value &&
    lastGeneration.value.text === text.value &&
    lastGeneration.value.speed === speed.value &&
    lastGeneration.value.voice === selectedVoice.value;
});
const canResumeHere = computed(() => {
  const mark = listen.value;
  return !!mark && !mark.done && mark.part === partIndex.value && (!!mark.chunk || !!mark.anchor);
});
const showResumeJump = computed(() => {
  const mark = listen.value;
  return !!mark && !mark.done && mark.part !== partIndex.value && mark.part >= 1;
});
const resumeLabel = computed(() => {
  const mark = listen.value;
  if (!mark?.part) return '';
  if (mark.done) return 'Đã nghe hết bản này.';
  const sentence = mark.chunk > 0 ? `, câu ${mark.chunk + 1}` : '';
  if (mark.part === partIndex.value && mark.chunk > 0) return `Đang dở ở phần ${mark.part}${sentence}.`;
  if (mark.part !== partIndex.value) return `Lần trước nghe tới phần ${mark.part}${sentence}.`;
  return '';
});
const playLabel = computed(() => {
  if (isPlaying.value) return 'Tạm dừng';
  if (canResumeHere.value) return 'Nghe tiếp';
  if (processed.value || status.value === 'generating') return 'Nghe';
  if (partCount.value > 1) return 'Tạo audio phần này';
  return 'Tạo audio';
});
const stopLabel = computed(() => {
  const target = stopAfter.value;
  if (!Number.isInteger(target) || target < partIndex.value) return '';
  if (target === partIndex.value) return 'Hết phần này sẽ dừng.';
  return `Dừng khi hết phần ${target}. Nghe tự sang phần sau cho tới đó.`;
});
async function checkServer() {
  try {
    const response = await fetch('/pipeline/health');
    serverOk.value = response.ok;
  } catch {
    serverOk.value = false;
  }
}

async function loadJobs() {
  if (!serverOk.value) return;
  try {
    const response = await fetch('/pipeline/jobs');
    if (!response.ok) return;
    const data = await response.json();
    jobs.value = (data.jobs || []).map(mergeJobListen);
    if (!job.value) {
      const active = jobs.value.find((item) => item.status === 'running' || item.status === 'queued');
      if (active) await refreshJob(active.id, active.listen?.part || 1);
    }
  } catch {
    serverOk.value = false;
  }
}

function applyJob(next, { replaceText = true } = {}) {
  const switched = !job.value || job.value.id !== next.id;
  job.value = next;
  if (next.id) localStorage.setItem(OPEN_JOB_KEY, next.id);
  if (next.glossary && !glossary.value) glossary.value = next.glossary;
  if (replaceText && !dirty.value) text.value = next.text || '';
  if (next.part) partIndex.value = next.part;
  partCount.value = Number(next.partCount || 0);
  listen.value = preferListen(switched ? localListen(next.id) : listen.value, next.listen || null);
  if (switched) {
    restoreListenSettings();
    restoreStop(next.id);
  }
  if (next.status === 'queued' || next.status === 'running') startPoll();
}

async function refreshJob(id, part = partIndex.value) {
  const response = await fetch(`/pipeline/jobs/${id}?part=${part || 1}`);
  if (!response.ok) return;
  const next = await response.json();
  applyJob(next);
}

function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const current = job.value;
    if (current?.id && (current.status === 'queued' || current.status === 'running')) {
      await refreshJob(current.id).catch(() => {});
    }
    await loadJobs().catch(() => {});
    const busy =
      jobs.value.some((item) => item.status === 'queued' || item.status === 'running') ||
      job.value?.status === 'queued' ||
      job.value?.status === 'running';
    if (!busy) stopPoll();
  }, 2000);
}

function stopPoll() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function startJob() {
  formError.value = '';
  starting.value = true;
  dirty.value = false;
  partIndex.value = 1;
  partCount.value = 0;
  resetAudio();
  try {
    await checkServer();
    if (!serverOk.value) {
      formError.value = 'Server trên máy chưa chạy. Dùng npm run workflow.';
      return;
    }
    let response;
    if (epubFile.value) {
      const body = new FormData();
      body.append('file', epubFile.value);
      body.append('glossary', glossary.value);
      body.append('mode', mode.value);
      body.append('provider', provider.value);
      response = await fetch('/pipeline/jobs/epub', { method: 'POST', body });
    } else if (!url.value.trim() && job.value?.source === 'epub') {
      response = await fetch('/pipeline/jobs/epub', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          id: job.value.id,
          glossary: glossary.value,
          mode: mode.value,
          provider: provider.value,
        }),
      });
    } else {
      const payload = { url: url.value.trim(), glossary: glossary.value, mode: mode.value, provider: provider.value };
      if (bookLink.value) {
        payload.fromChapter = Number(chapterFrom.value) || 1;
        payload.toChapter = Number(chapterTo.value) || payload.fromChapter;
      }
      response = await fetch('/pipeline/jobs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
    }
    const data = await response.json();
    if (!response.ok) {
      formError.value = data.error || 'Không tạo được job';
      return;
    }
    if (data.source === 'epub') {
      url.value = '';
      epubName.value = data.filename || epubName.value;
      epubFile.value = null;
      if (epubInput.value) epubInput.value.value = '';
      batchNote.value = '';
    } else {
      if (!bookLink.value) url.value = data.url || url.value;
      batchNote.value = data.batch > 1
        ? `Đã xếp ${data.batch} chương (${data.batchFrom}–${data.batchTo}). Chúng chạy lần lượt.`
        : '';
    }
    applyJob(data);
    if (data.batch > 1) startPoll();
    formOpen.value = false;
    workspaceTab.value = 'reader';
    await loadJobs();
  } catch {
    serverOk.value = false;
    formError.value = 'Không nối được server local.';
  } finally {
    starting.value = false;
  }
}

async function openJob(item) {
  formError.value = '';
  dirty.value = false;
  const mark = preferListen(localListen(item.id), item.listen || null);
  listen.value = mark;
  partIndex.value = mark?.part || 1;
  resetAudio();
  epubFile.value = null;
  if (epubInput.value) epubInput.value.value = '';
  if (item.source === 'epub') {
    url.value = '';
    epubName.value = item.filename || item.title || '';
  } else {
    url.value = item.url || '';
    epubName.value = '';
  }
  await refreshJob(item.id, partIndex.value);
  if (job.value?.id === item.id) {
    formOpen.value = false;
    workspaceTab.value = 'reader';
  }
  glossary.value = job.value?.glossary || '';
  if (job.value?.mode) mode.value = job.value.mode;
  if (job.value?.provider && job.value.provider !== 'local') provider.value = job.value.provider;
}

function onEpub(event) {
  const file = event.target.files?.[0] || null;
  epubFile.value = file;
  epubName.value = file?.name || (job.value?.source === 'epub' ? job.value.filename || job.value.title || '' : '');
}

function onTextInput() {
  if (running.value) return;
  dirty.value = true;
  saveState.value = 'Đang lưu...';
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(saveText, 800);
}

async function saveText() {
  if (!job.value?.id || running.value) return;
  const snapshot = text.value;
  try {
    const response = await fetch(`/pipeline/jobs/${job.value.id}/text`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: snapshot, part: partIndex.value }),
    });
    if (!response.ok) {
      saveState.value = 'Chưa lưu được';
      return;
    }
    if (text.value === snapshot) {
      dirty.value = false;
      saveState.value = 'Đã lưu trên máy';
    }
  } catch {
    saveState.value = 'Chưa lưu được';
  }
}

function downloadText() {
  const blob = new Blob([text.value], { type: 'text/plain;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${job.value?.id || 'ban-dich'}-phan-${partIndex.value}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function downloadAll() {
  if (!job.value) return;
  const response = await fetch(`/pipeline/jobs/${job.value.id}?full=1`);
  if (!response.ok) return;
  const data = await response.json();
  const blob = new Blob([data.text || ''], { type: 'text/plain;charset=utf-8' });
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = `${job.value.id}.txt`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function resetAudio() {
  chunks.value.forEach((chunk) => chunk.audioUrl && URL.revokeObjectURL(chunk.audioUrl));
  if (result.value?.audioUrl) URL.revokeObjectURL(result.value.audioUrl);
  chunks.value = [];
  result.value = null;
  lastGeneration.value = null;
  isPlaying.value = false;
  currentChunkIndex.value = -1;
  pendingResume.value = null;
  chunkOrigin.value = 0;
  playToken += 1;
  status.value = voices.value && selectedModel.value !== 'None' ? 'ready' : 'idle';
  if (worker.value) worker.value.postMessage({ type: 'stop' });
}

function rememberProgress({ part = partIndex.value, chunk = 0, anchor = '', done = false } = {}) {
  const id = job.value?.id;
  if (!id || part < 1) return;
  const next = {
    part,
    chunk: Math.max(0, chunk || 0),
    anchor: String(anchor || '').replace(/\s+/g, ' ').trim().slice(0, 240),
    done: !!done,
    model: selectedModel.value,
    speed: speed.value,
    updated: Date.now() / 1000,
  };
  listen.value = next;
  const map = readListenMap();
  map[id] = next;
  localStorage.setItem(LISTEN_KEY, JSON.stringify(map));
  const row = jobs.value.find((item) => item.id === id);
  if (row) row.listen = next;
  pendingSnapshot = { id, ...next };
  if (progressTimer) clearTimeout(progressTimer);
  progressTimer = setTimeout(() => flushProgress(), 500);
}

async function flushProgress(keepalive = false) {
  if (progressTimer) {
    clearTimeout(progressTimer);
    progressTimer = null;
  }
  const snapshot = pendingSnapshot;
  if (!snapshot || flushingProgress) return;
  pendingSnapshot = null;
  flushingProgress = true;
  try {
    await fetch(`/pipeline/jobs/${snapshot.id}/progress`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        part: snapshot.part,
        chunk: snapshot.chunk,
        anchor: snapshot.anchor,
        done: snapshot.done,
        model: snapshot.model,
        speed: snapshot.speed,
      }),
      keepalive,
    });
  } catch {
    // Trình duyệt vẫn giữ chỗ nghe dở khi server chưa nhận.
  } finally {
    flushingProgress = false;
    if (pendingSnapshot) flushProgress(keepalive);
  }
}

function saveSpot() {
  if (!job.value?.id) return;
  const pending = pendingResume.value;
  if (pending) {
    rememberProgress({ part: partIndex.value, chunk: pending.chunk, anchor: pending.anchor });
    return;
  }
  const index = currentChunkIndex.value;
  if (index < 0) return;
    rememberProgress({
    part: partIndex.value,
    chunk: ordinalAt(index),
    anchor: chunks.value[index]?.text || '',
  });
}

function onPageHide() {
  if (progressTimer) {
    clearTimeout(progressTimer);
    progressTimer = null;
  }
  const snapshot = pendingSnapshot;
  if (!snapshot) return;
  pendingSnapshot = null;
  fetch(`/pipeline/jobs/${snapshot.id}/progress`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      part: snapshot.part,
      chunk: snapshot.chunk,
      anchor: snapshot.anchor,
      done: snapshot.done,
      model: snapshot.model,
      speed: snapshot.speed,
    }),
    keepalive: true,
  }).catch(() => {});
}

async function showPart(next) {
  if (!job.value) return;
  const target = Math.min(Math.max(next, 1), partCount.value || 1);
  if (target === partIndex.value && text.value) return;
  if (dirty.value) await saveText();
  dirty.value = false;
  resetAudio();
  await refreshJob(job.value.id, target);
  if (Number.isInteger(stopAfter.value) && partIndex.value > stopAfter.value) clearStop();
}

watch(partIndex, (value) => {
  if (!sliderDragging.value) sliderDraft.value = value;
  if (!jumpFocused.value) jumpDraft.value = String(value);
  if (stopFocused.value) return;
  const draft = Number.parseInt(String(stopDraft.value).trim(), 10);
  if (Number.isFinite(draft) && draft < value) stopDraft.value = String(value);
});

function commitJump() {
  if (jumpLock) return;
  const raw = jumpInput.value?.value ?? jumpDraft.value;
  const next = Number.parseInt(String(raw).trim(), 10);
  if (!Number.isFinite(next)) {
    jumpDraft.value = String(partIndex.value);
    return;
  }
  const target = Math.min(Math.max(next, 1), Math.max(partCount.value, 1));
  jumpDraft.value = String(target);
  if (target === partIndex.value) return;
  jumpLock = true;
  sliderDraft.value = target;
  showPart(target).finally(() => {
    jumpLock = false;
  });
}

function onJumpBlur() {
  jumpFocused.value = false;
  window.setTimeout(() => {
    if (jumpLock || jumpFocused.value) return;
    jumpDraft.value = String(partIndex.value);
  }, 0);
}

function onSliderInput(event) {
  sliderDragging.value = true;
  const value = Number(event.target.value);
  sliderDraft.value = value;
  if (!jumpFocused.value) jumpDraft.value = String(value);
}

function onSliderCommit(event) {
  sliderDragging.value = false;
  const value = Number(event.target.value);
  sliderDraft.value = value;
  jumpDraft.value = String(value);
  if (value !== partIndex.value) showPart(value);
}

const restartWorker = (modelName = null) => {
  if (worker.value) worker.value.terminate();
  status.value = 'loading';
  loadingProgress.value = 0;
  voices.value = null;
  chunks.value = [];
  result.value = null;
  lastGeneration.value = null;
  isPlaying.value = false;
  currentChunkIndex.value = -1;
  const progressInterval = setInterval(() => {
    if (loadingProgress.value < 90) {
      loadingProgress.value = Math.min(90, loadingProgress.value + Math.random() * 5);
    }
  }, 200);
  worker.value = new Worker(new URL('../workers/tts-worker.js', import.meta.url), { type: 'module' });
  worker.value.addEventListener('message', onMessageReceived);
  worker.value.addEventListener('error', onErrorReceived);
  worker.value.postMessage({ type: 'init', model: modelName || selectedModel.value });
  worker.value._progressInterval = progressInterval;
};

const onMessageReceived = ({ data }) => {
  if ((data.status === 'stream' || data.status === 'complete' || data.status === 'playback-done' || data.status === 'error') && data.token != null && data.token !== playToken) {
    return;
  }
  switch (data.status) {
    case 'ready':
      if (worker.value?._progressInterval) clearInterval(worker.value._progressInterval);
      loadingProgress.value = 100;
      setTimeout(() => {
        status.value = 'ready';
        loadingProgress.value = 0;
      }, 300);
      voices.value = data.voices;
      break;
    case 'error':
      if (worker.value?._progressInterval) clearInterval(worker.value._progressInterval);
      loadingProgress.value = 0;
      status.value = 'error';
      ttsError.value = data.data;
      break;
    case 'stream': {
      const chunk = data.chunk;
      const absolute = Number.isInteger(data.index) ? data.index : chunkOrigin.value + chunks.value.length;
      if (chunks.value.length === 0) chunkOrigin.value = absolute;
      chunks.value = [...chunks.value, { ...chunk, index: absolute }];
      const pending = pendingResume.value;
      if (pending && isPlaying.value) {
        const matched = (pending.anchor && textMatches(chunk?.text, pending.anchor))
          || absolute >= (pending.chunk || 0);
        if (matched) {
          currentChunkIndex.value = chunks.value.length - 1;
          pendingResume.value = null;
        }
      }
      break;
    }
    case 'playback-done':
      status.value = 'ready';
      settlePlayback();
      break;
    case 'complete':
      result.value = data.audio;
      if (status.value === 'generating') {
        status.value = 'ready';
        settlePlayback();
      }
      break;
    case 'preview':
      if (data.audio) {
        const audioUrl = URL.createObjectURL(data.audio);
        const audio = new Audio(audioUrl);
        audio.play().then(() => {
          setTimeout(() => URL.revokeObjectURL(audioUrl), 1000);
        }).catch(() => {});
      }
      break;
    default:
      break;
  }
};

const onErrorReceived = (event) => {
  ttsError.value = event.message;
};

const fetchModels = async () => {
  modelsLoading.value = true;
  try {
    const models = await fetchAvailableModels();
    availableModels.value = models;
    restoreListenSettings();
    if (selectedModel.value && selectedModel.value !== 'None' && !models.includes(selectedModel.value)) {
      selectedModel.value = 'None';
    }
    if (selectedModel.value === 'None' && models.length > 0) {
      const defaultModel = DEFAULT_MODEL.vi && models.includes(DEFAULT_MODEL.vi) ? DEFAULT_MODEL.vi : models[0];
      selectedModel.value = defaultModel;
      restartWorker(defaultModel);
    }
  } catch (err) {
    ttsError.value = `Không tải được model: ${err.message}`;
  } finally {
    modelsLoading.value = false;
  }
};

function restoreListenSettings() {
  const mark = listen.value;
  if (Number.isFinite(mark?.speed) && mark.speed >= 0.5 && mark.speed <= 2) {
    speed.value = mark.speed;
  }
  if (mark?.model && availableModels.value.includes(mark.model)) {
    handleModelChange(mark.model, false);
  }
}

function rememberListenSettings() {
  const mark = listen.value;
  rememberProgress(mark || { part: partIndex.value });
}

function handleSpeedChange(value) {
  saveSpot();
  speed.value = value;
  rememberListenSettings();
}

const handleModelChange = (modelName, persist = true) => {
  if (modelName === selectedModel.value) return;
  if (persist) saveSpot();
  selectedModel.value = modelName;
  if (persist) rememberListenSettings();
  if (modelName === 'None') {
    if (worker.value) {
      worker.value.terminate();
      worker.value = null;
    }
    status.value = 'loading';
    voices.value = null;
    chunks.value = [];
    result.value = null;
    isPlaying.value = false;
    return;
  }
  restartWorker(modelName);
};

function setAutoNext(value) {
  autoNext.value = value;
  localStorage.setItem('workflow-auto-next', value ? '1' : '0');
}

function readStopMap() {
  try {
    const data = JSON.parse(localStorage.getItem(STOP_KEY) || '{}');
    return data && typeof data === 'object' ? data : {};
  } catch {
    return {};
  }
}

function persistStop(id, part) {
  if (!id) return;
  const map = readStopMap();
  if (part == null) delete map[id];
  else map[id] = part;
  localStorage.setItem(STOP_KEY, JSON.stringify(map));
}

function stopBounds() {
  const here = Math.max(partIndex.value, 1);
  const max = Math.max(partCount.value, here);
  return { here, max };
}

function restoreStop(id) {
  stopNotice.value = '';
  stopError.value = '';
  const { here, max } = stopBounds();
  const part = Number(readStopMap()[id]);
  if (Number.isInteger(part) && part >= here && part <= max) {
    stopAfter.value = part;
    stopDraft.value = String(part);
    return;
  }
  stopAfter.value = null;
  stopDraft.value = '';
  if (readStopMap()[id] != null) persistStop(id, null);
}

function armStop(part) {
  const { here, max } = stopBounds();
  const target = Math.min(Math.max(part, here), max);
  if (!Number.isInteger(target) || target < here) return;
  stopAfter.value = target;
  stopDraft.value = String(target);
  stopError.value = '';
  stopNotice.value = '';
  persistStop(job.value?.id, target);
}

function parsedStopDraft() {
  const raw = String(stopDraft.value).trim();
  if (!raw) return null;
  const next = Number.parseInt(raw, 10);
  return Number.isFinite(next) ? next : null;
}

function rejectStopBelowHere() {
  const { here } = stopBounds();
  stopDraft.value = String(stopAfter.value != null && stopAfter.value >= here ? stopAfter.value : here);
  stopError.value = `Phần dừng phải từ phần ${here} trở đi.`;
}

let stopAction = false;

function commitStop() {
  if (stopAction) return;
  stopAction = true;
  setTimeout(() => {
    stopAction = false;
  }, 0);
  const { here, max } = stopBounds();
  const next = parsedStopDraft();
  if (next == null) {
    stopError.value = 'Nhập số phần muốn dừng.';
    return;
  }
  if (next < here) {
    rejectStopBelowHere();
    return;
  }
  if (next > max) {
    stopDraft.value = String(max);
    stopError.value = `Bản này có ${max} phần.`;
    return;
  }
  armStop(next);
}

function onStopFocus(event) {
  stopFocused.value = true;
  if (!String(stopDraft.value).trim()) stopDraft.value = String(stopBounds().here);
  event.target.select();
}

function onStopBlur() {
  stopFocused.value = false;
  const { here, max } = stopBounds();
  const next = parsedStopDraft();
  if (next == null) {
    stopError.value = '';
    return;
  }
  if (next < here) {
    rejectStopBelowHere();
    return;
  }
  if (next > max) {
    stopDraft.value = String(max);
    stopError.value = `Bản này có ${max} phần.`;
    return;
  }
  stopDraft.value = String(next);
}

function clearStop() {
  stopAfter.value = null;
  stopDraft.value = '';
  stopError.value = '';
  persistStop(job.value?.id, null);
}

function stopStillAhead(part) {
  return Number.isInteger(stopAfter.value) && part <= stopAfter.value;
}

function startPlayback() {
  if (!text.value || !worker.value) return;
  const resume = resumeTarget();
  status.value = 'generating';
  chunks.value = [];
  result.value = null;
  chunkOrigin.value = resume?.chunk || 0;
  pendingResume.value = resume;
  currentChunkIndex.value = resume ? -1 : 0;
  isPlaying.value = true;
  stopNotice.value = '';
  if (!resume) rememberProgress({ part: partIndex.value, chunk: 0, anchor: '' });
  const token = ++playToken;
  const params = { text: text.value, voice: selectedVoice.value, speed: speed.value, token };
  lastGeneration.value = params;
  worker.value.postMessage(resume ? { ...params, resume } : params);
}

const handlePlayPause = () => {
  if (!isPlaying.value && status.value === 'ready' && !processed.value) {
    startPlayback();
    return;
  }
  if (currentChunkIndex.value === -1 && !pendingResume.value) currentChunkIndex.value = 0;
  isPlaying.value = !isPlaying.value;
  if (!isPlaying.value) saveSpot();
};

function onChunkPause(index) {
  if (currentChunkIndex.value !== index) return;
  if (isPlaying.value) isPlaying.value = false;
  saveSpot();
}

async function resumeListening() {
  const mark = listen.value;
  if (!mark?.part || mark.done || status.value !== 'ready') return;
  if (mark.part !== partIndex.value) await showPart(mark.part);
  if (!isPlaying.value) handlePlayPause();
}

function listenFromStart() {
  if (restartingPlayback.value || status.value !== 'ready' || !worker.value) return;
  restartingPlayback.value = true;
  formError.value = '';
  try {
    if (!text.value) {
      throw new Error('Chưa có nội dung chương này để nghe lại.');
    }
    resetAudio();
    rememberProgress({ part: partIndex.value, chunk: 0, anchor: '', done: false });
    startPlayback();
  } catch (error) {
    formError.value = error.message || 'Không bắt đầu nghe lại được.';
  } finally {
    restartingPlayback.value = false;
  }
}

function finishPart() {
  const atStop = Number.isInteger(stopAfter.value) && partIndex.value >= stopAfter.value;
  const nextPart = partIndex.value + 1;
  if (!atStop && nextPart <= partCount.value && (autoNext.value || stopStillAhead(nextPart))) {
    rememberProgress({ part: nextPart, chunk: 0, anchor: '' });
    continueNextPart();
    return;
  }
  if (atStop) {
    stopNotice.value = `Đã dừng sau phần ${partIndex.value}.`;
    clearStop();
  }
  if (partIndex.value < partCount.value) {
    rememberProgress({ part: partIndex.value + 1, chunk: 0, anchor: '' });
  } else {
    rememberProgress({
      part: partIndex.value,
      chunk: ordinalAt(Math.max(0, chunks.value.length - 1)),
      anchor: '',
      done: true,
    });
  }
  isPlaying.value = false;
  currentChunkIndex.value = -1;
}

async function continueNextPart() {
  if (advancing) return;
  advancing = true;
  try {
    await showPart(partIndex.value + 1);
    if (!text.value || !(autoNext.value || stopStillAhead(partIndex.value))) {
      isPlaying.value = false;
      return;
    }
    startPlayback();
  } finally {
    advancing = false;
  }
}

const handleChunkEnd = () => {
  const index = currentChunkIndex.value;
  const lastChunk = index >= chunks.value.length - 1;
  if (status.value !== 'generating' && lastChunk) {
    finishPart();
    return;
  }
  const next = index + 1;
  currentChunkIndex.value = next;
  rememberProgress({
    part: partIndex.value,
    chunk: ordinalAt(next),
    anchor: chunks.value[next]?.text || '',
  });
};

function settlePlayback() {
  if (pendingResume.value) {
    pendingResume.value = null;
    currentChunkIndex.value = 0;
    isPlaying.value = true;
  } else if (isPlaying.value && currentChunkIndex.value >= chunks.value.length) {
    finishPart();
  }
}

const downloadAudio = () => {
  if (!result.value) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(result.value);
  link.download = `${job.value?.id || 'audio'}-phan-${partIndex.value}.wav`;
  link.click();
  URL.revokeObjectURL(link.href);
};

onMounted(async () => {
  window.addEventListener('pagehide', onPageHide);
  await checkServer();
  await loadJobs();
  if (!job.value) {
    const savedId = localStorage.getItem(OPEN_JOB_KEY);
    const item = jobs.value.find((row) => row.id === savedId);
    if (item) await openJob(item);
  }
  jobsTimer = setInterval(() => {
    loadJobs();
  }, 2000);
  await fetchModels();
});

onUnmounted(() => {
  window.removeEventListener('pagehide', onPageHide);
  if (jobsTimer) clearInterval(jobsTimer);
  stopPoll();
  if (saveTimer) clearTimeout(saveTimer);
  flushProgress(true);
  if (worker.value) worker.value.terminate();
});
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold text-gray-900 dark:text-gray-100">Không gian nghe</h2>
        <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Chọn job trong thư viện để mở bản dịch và nghe tiếp.</p>
      </div>
      <button type="button" class="shrink-0 rounded-xl bg-blue-800 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500" :aria-expanded="formOpen" aria-controls="new-job-form" @click="formOpen = !formOpen">
        {{ formOpen ? 'Đóng biểu mẫu' : '+ Tạo job mới' }}
      </button>
    </div>
    <div
      v-if="!serverOk"
      class="p-4 rounded-xl bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200 text-sm"
    >
      Quy trình này chạy trên máy, không upload bản dịch hay audio đi đâu. Server chưa bật — trong thư mục project chạy
      <span class="font-mono">npm run workflow</span>.
    </div>

    <div v-show="formOpen" id="new-job-form" class="bg-white/70 dark:bg-gray-900/70 backdrop-blur-xl rounded-2xl shadow-xl border border-white/20 dark:border-gray-700/50 p-4 sm:p-6 space-y-4">
      <div>
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Link YouTube, truyện Sangtacviet hoặc EPUB</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          YouTube tự nhận ngôn ngữ. Video tiếng Việt mà lời là Hán Việt thì được viết lại cho dễ đọc, giống chương Sangtacviet. Link trang truyện thì chọn khoảng chương. Hoặc chọn file EPUB.
        </p>
      </div>
      <input
        v-model="url"
        type="url"
        :placeholder="novelLink ? 'https://sangtacviet.com/truyen/fanqie/1/.../...' : 'https://youtu.be/...'"
        class="w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
      />
      <div class="flex flex-wrap items-center gap-3">
        <label class="px-4 py-2.5 rounded-xl border-2 border-gray-200 dark:border-gray-700 cursor-pointer text-sm font-medium text-gray-800 dark:text-gray-100">
          Chọn file EPUB
          <input
            ref="epubInput"
            type="file"
            accept=".epub,application/epub+zip"
            class="hidden"
            @change="onEpub"
          />
        </label>
        <span class="text-sm text-gray-600 dark:text-gray-300 truncate">{{ epubName || 'Chưa chọn file' }}</span>
      </div>
      <p v-if="epubFile" class="text-xs text-gray-500">Sẽ chạy file EPUB này, không dùng link.</p>
      <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Chế độ
        <select
          v-model="mode"
          class="mt-1 w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 font-normal"
        >
          <option value="auto">YouTube — tự nhận: tiếng Anh thì dịch, tiếng Việt thường thì giữ, Hán Việt thì viết lại</option>
          <option value="en">YouTube — tiếng Anh, dịch sang tiếng Việt</option>
          <option value="vi">YouTube — tiếng Việt; nếu lời là Hán Việt thì viết lại cho dễ đọc</option>
          <option value="sangtacviet">Sangtacviet — lấy một khoảng chương, chuyển Hán Việt sang tiếng Việt tự nhiên</option>
        </select>
      </label>
      <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Máy dịch
        <select
          v-model="provider"
          class="mt-1 w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 font-normal"
        >
          <option value="gemini">Gemini (mạng)</option>
          <option value="deepseek">DeepSeek Flash (mạng)</option>
          <option value="deepseek_local">DeepSeek trên máy</option>
        </select>
      </label>
      <div v-if="bookLink" class="grid grid-cols-2 gap-3">
        <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
          Từ chương
          <input
            v-model.number="chapterFrom"
            type="number"
            min="1"
            class="mt-1 w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 font-normal"
          />
        </label>
        <label class="block text-sm font-medium text-gray-700 dark:text-gray-300">
          Đến chương
          <input
            v-model.number="chapterTo"
            type="number"
            min="1"
            class="mt-1 w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 font-normal"
          />
        </label>
        <p class="col-span-2 text-xs text-gray-500">Tối đa 30 chương mỗi lần. Các chương chạy nối tiếp.</p>
      </div>
      <label v-if="!novelLink" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
        Thuật ngữ (tuỳ chọn, mỗi dòng một mục)
        <textarea
          v-model="glossary"
          rows="3"
          class="mt-1 w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 font-normal"
          placeholder="Hikigaya Hachiman = Hikigaya Hachiman"
        ></textarea>
      </label>
      <button
        type="button"
        class="px-5 py-2.5 rounded-xl font-semibold text-white bg-blue-800 disabled:opacity-50"
        :disabled="!canStart"
        @click="startJob"
      >
        {{ running ? 'Đang chạy' : starting ? 'Đang gửi' : 'Bắt đầu' }}
      </button>
      <p v-if="batchNote" class="text-sm text-gray-600 dark:text-gray-300">{{ batchNote }}</p>
      <p v-if="formError" class="text-sm text-red-600 dark:text-red-400">{{ formError }}</p>

    </div>

    <div ref="workspaceNav" class="sticky top-20 z-40 grid scroll-mt-20 grid-cols-2 gap-1 rounded-xl border border-gray-200 bg-white/95 p-1 shadow-sm backdrop-blur lg:hidden dark:border-gray-700 dark:bg-gray-900/95" role="group" aria-label="Chuyển khu vực làm việc">
      <button type="button" class="rounded-lg px-3 py-2.5 text-sm font-medium" :class="workspaceTab === 'library' ? 'bg-blue-800 text-white' : 'text-gray-600 dark:text-gray-300'" :aria-pressed="workspaceTab === 'library'" @click="workspaceTab = 'library'">Thư viện ({{ jobs.length }})</button>
      <button type="button" class="rounded-lg px-3 py-2.5 text-sm font-medium" :class="workspaceTab === 'reader' ? 'bg-blue-800 text-white' : 'text-gray-600 dark:text-gray-300'" :aria-pressed="workspaceTab === 'reader'" @click="workspaceTab = 'reader'">Bản dịch &amp; nghe</button>
    </div>

    <div class="grid items-start gap-5 lg:grid-cols-[340px_minmax(0,1fr)] xl:grid-cols-[380px_minmax(0,1fr)]">
      <aside class="min-h-0 min-w-0 lg:sticky lg:top-24 lg:h-[calc(100dvh-7rem)]" :class="workspaceTab === 'library' ? 'block' : 'hidden lg:block'" aria-label="Thư viện jobs">
        <JobLibrary v-if="jobs.length" :jobs="jobs" :selected-id="job?.id || ''" @open="openJob" />
        <div v-else class="rounded-2xl border border-dashed border-gray-300 p-6 text-center text-sm text-gray-500 dark:border-gray-700 dark:text-gray-400">Chưa có jobs. Chọn “Tạo job mới” để bắt đầu.</div>
      </aside>
      <div class="min-w-0 space-y-4" :class="workspaceTab === 'reader' ? 'block' : 'hidden lg:block'">
      <div v-if="job" class="rounded-2xl border border-gray-200 bg-white/90 p-4 space-y-2 dark:border-gray-700 dark:bg-gray-900/80">
        <div class="flex items-baseline justify-between gap-3">
          <p class="font-medium text-gray-900 dark:text-gray-100">{{ job.title || job.id }}</p>
          <p class="shrink-0 text-xs text-gray-500">{{ job.status === 'done' ? 'Hoàn thành' : job.status === 'error' ? 'Có lỗi' : job.step }}</p>
        </div>
        <p class="text-sm text-gray-600 dark:text-gray-300">{{ job.message }}</p>
        <div v-if="running" class="h-2 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
          <div class="h-full bg-blue-700 transition-all" :style="{ width: `${percent}%` }"></div>
        </div>
        <p v-if="job.error" class="text-sm text-red-600 dark:text-red-400">{{ job.error }}</p>
      </div>
      <div v-if="!job" class="rounded-2xl border border-dashed border-gray-300 bg-white/60 px-6 py-16 text-center dark:border-gray-700 dark:bg-gray-900/50">
        <h3 class="text-lg font-semibold text-gray-800 dark:text-gray-100">Chọn một job để bắt đầu nghe</h3>
        <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">Bản dịch và trình nghe sẽ hiển thị tại đây.</p>
        <button type="button" class="mt-4 rounded-lg px-4 py-2 text-sm font-medium text-blue-700 dark:text-blue-300 lg:hidden" @click="workspaceTab = 'library'">Mở thư viện</button>
      </div>
    <div
      v-if="text || job?.status === 'done'"
      class="bg-white/90 dark:bg-gray-900/90 rounded-2xl border border-gray-200 dark:border-gray-700 p-4 sm:p-5 space-y-4"
    >
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ job?.language === 'vi' ? 'Lời thoại tiếng Việt' : 'Bản dịch' }}</h2>
          <p v-if="running" class="mt-0.5 text-sm text-gray-500 dark:text-gray-400">Có thể nghe các phần đã viết xong. Phần sau vẫn đang chạy.</p>
          <p v-else-if="partCount > 1" class="mt-0.5 text-sm text-gray-500 dark:text-gray-400">Mỗi phần khoảng 1.500 từ. Gõ số phần rồi bấm Tới, hoặc kéo thanh bên dưới.</p>
        </div>
        <div class="flex items-center gap-2 shrink-0">
          <span v-if="saveState" class="text-xs text-gray-500">{{ saveState }}</span>
          <button
            type="button"
            class="h-9 px-3 rounded-lg text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
            @click="downloadText"
          >
            Tải phần này
          </button>
          <button
            type="button"
            class="h-9 px-3 rounded-lg text-sm border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-gray-800"
            @click="downloadAll"
          >
            Tải cả bản
          </button>
        </div>
      </div>
      <div
        v-if="partCount > 1"
        class="rounded-xl border border-gray-200 dark:border-gray-700 bg-gray-50/80 dark:bg-gray-800/40 p-3 space-y-3"
      >
        <div class="flex items-center gap-2 flex-wrap">
          <button
            type="button"
            class="inline-flex items-center gap-1 h-9 px-2.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 disabled:opacity-40"
            :disabled="partIndex <= 1"
            @click="showPart(partIndex - 1)"
          >
            <ChevronLeft class="w-4 h-4" />
            Phần trước
          </button>
          <div class="inline-flex items-center h-9 rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 overflow-hidden">
            <span class="pl-2.5 text-xs text-gray-500">Phần</span>
            <input
              ref="jumpInput"
              v-model="jumpDraft"
              type="number"
              inputmode="numeric"
              min="1"
              :max="partCount"
              aria-label="Số phần"
              class="w-14 bg-transparent text-center text-sm font-semibold tabular-nums text-gray-900 dark:text-gray-100 outline-none [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
              @focus="jumpFocused = true; $event.target.select()"
              @keydown.enter.prevent="commitJump"
              @blur="onJumpBlur"
            />
            <span class="pr-2 text-xs text-gray-500 tabular-nums">/ {{ partCount }}</span>
            <button
              type="button"
              class="h-full px-3 text-sm font-medium text-white bg-blue-800 hover:bg-blue-900"
              @pointerdown="commitJump"
              @click="commitJump"
            >
              Tới
            </button>
          </div>
          <button
            type="button"
            class="inline-flex items-center gap-1 h-9 px-2.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900 text-gray-700 dark:text-gray-200 disabled:opacity-40"
            :disabled="partIndex >= partCount"
            @click="showPart(partIndex + 1)"
          >
            Phần sau
            <ChevronRight class="w-4 h-4" />
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-2 h-9 ml-auto px-3 rounded-full text-sm border"
            :class="autoNext ? 'bg-blue-800 text-white border-blue-800' : 'bg-white dark:bg-gray-900 border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-200'"
            @click="setAutoNext(!autoNext)"
          >
            <span
              class="relative w-8 h-4 rounded-full transition-colors"
              :class="autoNext ? 'bg-white/30' : 'bg-gray-300 dark:bg-gray-600'"
            >
              <span
                class="absolute top-0.5 w-3 h-3 rounded-full bg-white shadow-sm transition-all"
                :class="autoNext ? 'left-4' : 'left-0.5'"
              ></span>
            </span>
            Tự phát tiếp
          </button>
        </div>
        <div class="flex flex-wrap items-center gap-2 border-t border-gray-200 pt-3 dark:border-gray-700">
          <TimerIcon class="h-4 w-4 shrink-0 text-gray-500" />
          <span class="text-sm text-gray-600 dark:text-gray-300">Nghe tới hết phần</span>
          <input
            v-model="stopDraft"
            type="number"
            inputmode="numeric"
            :min="partIndex"
            :max="partCount"
            step="1"
            aria-label="Phần sẽ dừng, từ phần đang nghe trở đi"
            class="h-9 w-16 rounded-lg border border-gray-300 bg-white text-center text-sm font-semibold tabular-nums text-gray-900 outline-none dark:border-gray-600 dark:bg-gray-900 dark:text-gray-100 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
            @focus="onStopFocus"
            @blur="onStopBlur"
            @keydown.enter.prevent="commitStop"
          />
          <button
            type="button"
            class="h-9 rounded-lg bg-blue-800 px-3 text-sm font-medium text-white hover:bg-blue-900"
            @mousedown.prevent="commitStop"
            @click="commitStop"
          >
            Đặt
          </button>
          <button
            type="button"
            class="h-9 rounded-lg border px-3 text-sm"
            :class="stopAfter === partIndex ? 'border-blue-800 bg-blue-800 text-white' : 'border-gray-300 bg-white text-gray-700 dark:border-gray-600 dark:bg-gray-900 dark:text-gray-200'"
            @click="armStop(partIndex)"
          >
            Hết phần này
          </button>
          <button
            v-if="stopAfter"
            type="button"
            class="h-9 rounded-lg px-3 text-sm text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
            @click="clearStop"
          >
            Bỏ hẹn
          </button>
          <p v-if="stopError" class="w-full text-xs text-red-600 dark:text-red-400">{{ stopError }}</p>
          <p v-else-if="stopLabel" class="w-full text-xs text-blue-700 dark:text-blue-300">{{ stopLabel }}</p>
          <p v-else class="w-full text-xs text-gray-500 dark:text-gray-400">Chọn từ phần {{ partIndex }} đến phần {{ partCount }}. Hết phần đó thì dừng.</p>
        </div>
        <label class="block">
          <span class="sr-only">Kéo để chọn phần</span>
          <input
            type="range"
            class="w-full accent-blue-700 cursor-pointer"
            min="1"
            :max="partCount"
            :value="sliderDraft"
            :aria-valuetext="`Phần ${sliderDraft} trên ${partCount}`"
            @input="onSliderInput"
            @change="onSliderCommit"
          />
        </label>
      </div>
      <div class="sticky top-[9.5rem] z-30 flex flex-wrap gap-2 rounded-xl border border-gray-200 bg-white/95 p-2 shadow-sm backdrop-blur lg:top-24 dark:border-gray-700 dark:bg-gray-900/95">
        <button
          type="button"
          class="flex items-center justify-center gap-2 px-3 py-2.5 text-sm rounded-xl font-semibold text-white bg-blue-800 disabled:opacity-50"
          :disabled="restartingPlayback || (status === 'ready' && !isPlaying && !text) || (status !== 'ready' && chunks.length === 0)"
          @click="handlePlayPause"
        >
          <PauseIcon v-if="isPlaying" class="w-5 h-5" />
          <PlayIcon v-else class="w-5 h-5" />
          {{ playLabel }}
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 px-3 py-2.5 text-sm rounded-xl border-2 border-gray-200 dark:border-gray-700 disabled:opacity-50"
          :disabled="restartingPlayback || status !== 'ready' || !text"
          @click="listenFromStart"
        >
          <RotateCcwIcon class="w-5 h-5" />
          {{ restartingPlayback ? 'Đang phát lại...' : 'Nghe từ đầu chương' }}
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 px-3 py-2.5 text-sm rounded-xl border-2 border-gray-200 dark:border-gray-700 disabled:opacity-50"
          :disabled="!result || status !== 'ready'"
          @click="downloadAudio"
        >
          <DownloadIcon class="w-4 h-4" />
          {{ partCount > 1 ? 'Tải WAV phần này' : 'Tải WAV' }}
        </button>
        <span
          v-if="stopAfter"
          class="inline-flex items-center gap-1.5 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100"
        >
          <TimerIcon class="h-4 w-4" />
          Dừng sau phần {{ stopAfter }}
        </span>
      </div>
      <p v-if="stopNotice" class="text-sm text-amber-800 dark:text-amber-200">{{ stopNotice }}</p>


      <textarea
        v-model="text"
        class="w-full h-56 min-h-40 resize-y p-4 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
        :readonly="running"
        @input="onTextInput"
      ></textarea>
      <div class="flex justify-end">
        <TextStatistics :text="text" />
      </div>

      <details class="rounded-xl border border-gray-200 p-3 dark:border-gray-700">
        <summary class="cursor-pointer text-sm font-medium text-gray-700 dark:text-gray-200">Cài đặt giọng đọc &amp; tốc độ</summary>
        <div class="mt-3 space-y-3">
      <div v-if="availableModels.length > 0" class="flex items-center gap-2">
        <label class="text-sm font-medium text-gray-700 dark:text-gray-300">Model:</label>
        <ModelSelector :models="availableModels" :selected-model="selectedModel" @model-change="handleModelChange" />
      </div>
      <p v-if="modelsLoading" class="text-sm text-gray-500">Đang tải danh sách model...</p>
      <div v-if="voices" class="space-y-3">
        <VoiceSelector
          :voices="voices"
          :selected-voice="selectedVoice"
          @voice-change="(id) => { selectedVoice = id; }"
          @voice-preview="(id) => worker?.postMessage({ type: 'preview', text: 'Xin chào, đây là giọng đọc.', voice: id, speed })"
        />
        <SpeedControl :speed="speed" @speed-change="handleSpeedChange" />
      </div>
      <p v-else-if="ttsError" class="text-sm text-red-600 dark:text-red-400">{{ ttsError }}</p>
      <div v-else-if="status === 'loading'" class="text-sm text-gray-500">Đang tải model giọng nói... {{ Math.round(loadingProgress) }}%</div>
        </div>
      </details>

      <div v-if="resumeLabel" class="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-gray-600 dark:text-gray-300">
        <span>{{ resumeLabel }}</span>
        <span v-if="listen?.anchor && !listen.done" class="text-gray-500 dark:text-gray-400 truncate max-w-xl">{{ listen.anchor }}</span>
        <button
          v-if="showResumeJump"
          type="button"
          class="h-8 px-3 rounded-lg text-sm font-medium text-white bg-blue-800 disabled:opacity-50"
          :disabled="status !== 'ready'"
          @click="resumeListening"
        >
          Nghe tiếp
        </button>
      </div>


      <div class="w-0 h-0 hidden">
        <AudioChunk
          v-for="(chunk, index) in chunks"
          :key="index"
          :audio="chunk.audio"
          :active="currentChunkIndex === index"
          :playing="isPlaying"
          @pause="onChunkPause(index)"
          @end="handleChunkEnd"
        />
      </div>
    </div>
      </div>
    </div>
  </div>
</template>
