<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { DownloadIcon, PauseIcon, PlayIcon } from 'lucide-vue-next';
import TextStatistics from '../components/TextStatistics.vue';
import SpeedControl from '../components/SpeedControl.vue';
import AudioChunk from '../components/AudioChunk.vue';
import ModelSelector from '../components/ModelSelector.vue';
import VoiceSelector from '../components/VoiceSelector.vue';
import { fetchAvailableModels } from '../utils/model-detector.js';
import { DEFAULT_MODEL } from '../config.js';

const chapterFrom = ref(1);
const chapterTo = ref(5);
const batchNote = ref('');
const url = ref('');
const glossary = ref('');
const mode = ref('auto');
const text = ref('');
const job = ref(null);
const jobs = ref([]);
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
const currentChunkIndex = ref(-1);
const lastGeneration = ref(null);

let pollTimer = null;
let saveTimer = null;

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
    jobs.value = data.jobs || [];
  } catch {
    serverOk.value = false;
  }
}

function applyJob(next, { replaceText = true } = {}) {
  job.value = next;
  if (next.glossary && !glossary.value) glossary.value = next.glossary;
  if (replaceText && !dirty.value) text.value = next.text || '';
  if (next.part) partIndex.value = next.part;
  partCount.value = Number(next.partCount || 0);
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
    const payload = { url: url.value.trim(), glossary: glossary.value, mode: mode.value };
    if (bookLink.value) {
      payload.fromChapter = Number(chapterFrom.value) || 1;
      payload.toChapter = Number(chapterTo.value) || payload.fromChapter;
    }
    const response = await fetch('/pipeline/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      formError.value = data.error || 'Không tạo được job';
      return;
    }
    if (!bookLink.value) url.value = data.url || url.value;
    batchNote.value = data.batch > 1
      ? `Đã xếp ${data.batch} chương (${data.batchFrom}–${data.batchTo}). Chúng chạy lần lượt.`
      : '';
    applyJob(data);
    if (data.batch > 1) startPoll();
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
  partIndex.value = 1;
  resetAudio();
  url.value = item.url || '';
  await refreshJob(item.id, 1);
  glossary.value = job.value?.glossary || '';
  if (job.value?.mode) mode.value = job.value.mode;
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
  status.value = 'idle';
  isPlaying.value = false;
  currentChunkIndex.value = -1;
  if (worker.value) worker.value.postMessage({ type: 'stop' });
}

async function showPart(next) {
  if (!job.value || running.value) return;
  const target = Math.min(Math.max(next, 1), partCount.value || 1);
  if (target === partIndex.value && text.value) return;
  if (dirty.value) await saveText();
  dirty.value = false;
  resetAudio();
  await refreshJob(job.value.id, target);
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
    case 'stream':
      chunks.value = [...chunks.value, data.chunk];
      break;
    case 'complete':
      status.value = 'ready';
      result.value = data.audio;
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

const handleModelChange = (modelName) => {
  if (modelName === selectedModel.value) return;
  selectedModel.value = modelName;
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

const handlePlayPause = () => {
  if (!isPlaying.value && status.value === 'ready' && !processed.value) {
    status.value = 'generating';
    chunks.value = [];
    currentChunkIndex.value = 0;
    const params = { text: text.value, voice: selectedVoice.value, speed: speed.value };
    lastGeneration.value = params;
    worker.value?.postMessage(params);
  }
  if (currentChunkIndex.value === -1) currentChunkIndex.value = 0;
  isPlaying.value = !isPlaying.value;
};

const handleChunkEnd = () => {
  if (status.value !== 'generating' && currentChunkIndex.value === chunks.value.length - 1) {
    isPlaying.value = false;
    currentChunkIndex.value = -1;
  } else {
    currentChunkIndex.value += 1;
  }
};

const downloadAudio = () => {
  if (!result.value) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(result.value);
  link.download = `${job.value?.id || 'audio'}-phan-${partIndex.value}.wav`;
  link.click();
  URL.revokeObjectURL(link.href);
};

onMounted(async () => {
  await checkServer();
  await loadJobs();
  await fetchModels();
});

onUnmounted(() => {
  stopPoll();
  if (saveTimer) clearTimeout(saveTimer);
  if (worker.value) worker.value.terminate();
});
</script>

<template>
  <div class="space-y-6">
    <div
      v-if="!serverOk"
      class="p-4 rounded-xl bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200 text-sm"
    >
      Quy trình này chạy trên máy, không upload bản dịch hay audio đi đâu. Server chưa bật — trong thư mục project chạy
      <span class="font-mono">npm run workflow</span>.
    </div>

    <div class="bg-white/70 dark:bg-gray-900/70 backdrop-blur-xl rounded-2xl shadow-xl border border-white/20 dark:border-gray-700/50 p-6 space-y-4">
      <div>
        <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">Link YouTube hoặc truyện Sangtacviet</h2>
        <p class="text-sm text-gray-500 dark:text-gray-400">
          YouTube tự nhận ngôn ngữ. Video tiếng Việt mà lời là Hán Việt thì được viết lại cho dễ đọc, giống chương Sangtacviet. Link trang truyện thì chọn khoảng chương, khỏi copy từng chương.
        </p>
      </div>
      <input
        v-model="url"
        type="url"
        :placeholder="novelLink ? 'https://sangtacviet.com/truyen/fanqie/1/.../...' : 'https://youtu.be/...'"
        class="w-full p-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
      />
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
        :disabled="starting || !url.trim() || running || queueBusy"
        @click="startJob"
      >
        {{ running ? 'Đang chạy' : starting ? 'Đang gửi' : 'Bắt đầu' }}
      </button>
      <p v-if="batchNote" class="text-sm text-gray-600 dark:text-gray-300">{{ batchNote }}</p>
      <p v-if="formError" class="text-sm text-red-600 dark:text-red-400">{{ formError }}</p>

      <div v-if="job" class="space-y-2">
        <div class="flex items-baseline justify-between gap-3">
          <p class="font-medium text-gray-900 dark:text-gray-100">{{ job.title || job.id }}</p>
          <p class="text-xs text-gray-500">{{ job.step }}</p>
        </div>
        <p class="text-sm text-gray-600 dark:text-gray-300">{{ job.message }}</p>
        <div class="h-2 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
          <div class="h-full bg-blue-700 transition-all" :style="{ width: `${percent}%` }"></div>
        </div>
        <p v-if="job.error" class="text-sm text-red-600 dark:text-red-400">{{ job.error }}</p>
      </div>
    </div>

    <div v-if="jobs.length" class="flex flex-wrap gap-2">
      <button
        v-for="item in jobs"
        :key="item.id"
        type="button"
        class="px-3 py-1.5 rounded-full text-sm bg-white/80 dark:bg-gray-800 border border-gray-200 dark:border-gray-700"
        :class="job?.id === item.id ? 'text-blue-700 dark:text-blue-300 font-medium' : 'text-gray-700 dark:text-gray-300'"
        @click="openJob(item)"
      >
        {{ item.title || item.id }}
      </button>
    </div>

    <div
      v-if="text || job?.status === 'done'"
      class="bg-white/70 dark:bg-gray-900/70 backdrop-blur-xl rounded-2xl shadow-xl border border-white/20 dark:border-gray-700/50 p-6 space-y-4"
    >
      <div class="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ job?.language === 'vi' ? 'Lời thoại tiếng Việt' : 'Bản dịch' }}</h2>
          <p v-if="partCount > 1" class="text-sm text-gray-500 dark:text-gray-400">Mỗi phần khoảng 1.500 từ. Tạo audio từng phần.</p>
        </div>
        <div class="flex items-center gap-3">
          <span class="text-xs text-gray-500">{{ saveState }}</span>
          <button
            type="button"
            class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600"
            @click="downloadText"
          >
            Tải phần này
          </button>
          <button
            type="button"
            class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600"
            @click="downloadAll"
          >
            Tải cả bản
          </button>
        </div>
      </div>
      <div v-if="partCount > 1" class="flex items-center justify-between gap-3">
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 disabled:opacity-40"
          :disabled="partIndex <= 1 || running"
          @click="showPart(partIndex - 1)"
        >
          Phần trước
        </button>
        <span class="text-sm text-gray-600 dark:text-gray-300">Phần {{ partIndex }} / {{ partCount }}</span>
        <button
          type="button"
          class="px-3 py-1.5 rounded-lg text-sm border border-gray-300 dark:border-gray-600 disabled:opacity-40"
          :disabled="partIndex >= partCount || running"
          @click="showPart(partIndex + 1)"
        >
          Phần sau
        </button>
      </div>
      <textarea
        v-model="text"
        class="w-full min-h-[220px] p-4 rounded-xl border-2 border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800"
        :readonly="running"
        @input="onTextInput"
      ></textarea>
      <div class="flex justify-end">
        <TextStatistics :text="text" />
      </div>

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
        <SpeedControl :speed="speed" @speed-change="(value) => { speed = value; }" />
      </div>
      <p v-else-if="ttsError" class="text-sm text-red-600 dark:text-red-400">{{ ttsError }}</p>
      <div v-else-if="status === 'loading'" class="text-sm text-gray-500">Đang tải model giọng nói... {{ Math.round(loadingProgress) }}%</div>

      <div class="flex flex-col sm:flex-row gap-3">
        <button
          type="button"
          class="flex items-center justify-center gap-2 px-6 py-3 rounded-xl font-semibold text-white bg-blue-800 disabled:opacity-50"
          :disabled="running || (status === 'ready' && !isPlaying && !text) || (status !== 'ready' && chunks.length === 0)"
          @click="handlePlayPause"
        >
          <PauseIcon v-if="isPlaying" class="w-5 h-5" />
          <PlayIcon v-else class="w-5 h-5" />
          {{ isPlaying ? 'Tạm dừng' : processed || status === 'generating' ? 'Nghe' : partCount > 1 ? 'Tạo audio phần này' : 'Tạo audio' }}
        </button>
        <button
          type="button"
          class="flex items-center justify-center gap-2 px-6 py-3 rounded-xl border-2 border-gray-200 dark:border-gray-700 disabled:opacity-50"
          :disabled="!result || status !== 'ready'"
          @click="downloadAudio"
        >
          <DownloadIcon class="w-4 h-4" />
          {{ partCount > 1 ? 'Tải WAV phần này' : 'Tải WAV' }}
        </button>
      </div>

      <div class="w-0 h-0 hidden">
        <AudioChunk
          v-for="(chunk, index) in chunks"
          :key="index"
          :audio="chunk.audio"
          :active="currentChunkIndex === index"
          :playing="isPlaying"
          @pause="() => { if (currentChunkIndex === index) isPlaying = false; }"
          @end="handleChunkEnd"
        />
      </div>
    </div>
  </div>
</template>
