<script setup>
import { computed, ref, watch } from "vue";
import {
  BookOpenIcon,
  CheckIcon,
  ChevronLeft,
  ChevronRight,
  FileTextIcon,
  HeadphonesIcon,
  SearchIcon,
  VideoIcon,
  XIcon,
} from "lucide-vue-next";

const props = defineProps({
  jobs: { type: Array, default: () => [] },
  selectedId: { type: String, default: "" },
});
const emit = defineEmits(["open"]);
const search = ref("");
const filter = ref("all");
const sort = ref("recent");
const page = ref(1);
const pageSize = 6;

const isActive = (item) => ["running", "queued"].includes(item.status);
const isListening = (item) =>
  item.status === "done" && item.listen?.part && !item.listen.done;
const filters = computed(() => [
  { value: "all", label: "Tất cả", count: props.jobs.length },
  {
    value: "done",
    label: "Đã hoàn thành",
    count: props.jobs.filter((item) => item.status === "done").length,
  },
  {
    value: "listening",
    label: "Đang nghe dở",
    count: props.jobs.filter(isListening).length,
  },
  {
    value: "active",
    label: "Đang xử lý",
    count: props.jobs.filter(isActive).length,
  },
  {
    value: "error",
    label: "Có lỗi",
    count: props.jobs.filter((item) => item.status === "error").length,
  },
]);

function normalize(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/đ/g, "d")
    .replace(/Đ/g, "D")
    .toLocaleLowerCase("vi");
}

const filteredJobs = computed(() => {
  const query = normalize(search.value.trim());
  return props.jobs
    .filter((item) => {
      const matchesFilter =
        filter.value === "all" ||
        (filter.value === "done" && item.status === "done") ||
        (filter.value === "listening" && isListening(item)) ||
        (filter.value === "active" && isActive(item)) ||
        (filter.value === "error" && item.status === "error");
      return (
        matchesFilter &&
        (!query ||
          normalize(
            [item.title, item.id, item.filename, item.url].join(" "),
          ).includes(query))
      );
    })
    .sort((a, b) =>
      sort.value === "title"
        ? (a.title || a.id).localeCompare(b.title || b.id, "vi", {
            numeric: true,
          })
        : Number(b.updated || 0) - Number(a.updated || 0),
    );
});
const pageCount = computed(() =>
  Math.max(1, Math.ceil(filteredJobs.value.length / pageSize)),
);
const visibleJobs = computed(() =>
  filteredJobs.value.slice((page.value - 1) * pageSize, page.value * pageSize),
);
const rangeStart = computed(() =>
  filteredJobs.value.length ? (page.value - 1) * pageSize + 1 : 0,
);

watch(
  [search, filter, sort],
  () => {
    page.value = 1;
  },
  { flush: "sync" },
);
watch(pageCount, (count) => {
  page.value = Math.min(page.value, count);
});

function statusLabel(item) {
  return (
    {
      done: "Hoàn thành",
      running: "Đang xử lý",
      queued: "Đang chờ",
      error: "Có lỗi",
    }[item.status] || "Chưa rõ"
  );
}

function formatDate(timestamp) {
  if (!timestamp) return "";
  const date = new Date(Number(timestamp) * 1000);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleString("vi-VN", {
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
}

function resetFilters() {
  search.value = "";
  filter.value = "all";
}
</script>

<template>
  <section
    aria-labelledby="job-library-title"
    class="flex min-h-0 flex-col overflow-hidden rounded-2xl border border-gray-200/80 bg-white/90 shadow-sm lg:h-full lg:max-h-full dark:border-gray-700 dark:bg-gray-900/80"
  >
    <div class="shrink-0 space-y-3 p-4">
      <div class="flex items-start gap-3">
        <span
          class="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-blue-50 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300"
        >
          <BookOpenIcon class="h-5 w-5" aria-hidden="true" />
        </span>
        <div>
          <h2
            id="job-library-title"
            class="text-base font-semibold text-gray-900 dark:text-gray-100"
          >
            Thư viện jobs
            <span
              class="ml-1 text-sm font-normal text-gray-500 dark:text-gray-400"
              >({{ jobs.length }})</span
            >
          </h2>
          <p class="mt-0.5 text-sm text-gray-500 lg:hidden dark:text-gray-400">
            Mở lại bản đã xử lý hoặc nghe tiếp từ chỗ đang dở.
          </p>
        </div>
      </div>

      <div class="flex flex-col gap-2 sm:flex-row">
        <div class="relative min-w-0 flex-1">
          <SearchIcon
            class="pointer-events-none absolute left-3 top-3 h-4 w-4 text-gray-400"
            aria-hidden="true"
          />
          <input
            v-model="search"
            type="search"
            aria-label="Tìm jobs"
            placeholder="Tìm theo tên, file hoặc đường dẫn…"
            class="h-10 w-full rounded-lg border border-gray-200 bg-gray-50 pl-9 pr-10 text-sm text-gray-900 outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-500/20 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-100 [&::-webkit-search-cancel-button]:appearance-none"
          />
          <button
            v-if="search"
            type="button"
            aria-label="Xóa tìm kiếm"
            class="absolute right-1 top-1 rounded-md p-2 text-gray-500 hover:bg-gray-200 focus-visible:outline-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:bg-gray-700"
            @click="search = ''"
          >
            <XIcon class="h-4 w-4" />
          </button>
        </div>
        <select
          v-model="sort"
          aria-label="Sắp xếp jobs"
          class="h-10 shrink-0 rounded-lg border border-gray-200 bg-white px-2 text-xs text-gray-700 focus-visible:outline-blue-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-200"
        >
          <option value="recent">Mới nhất</option>
          <option value="title">Tên A → Z</option>
        </select>
      </div>

      <div
        class="flex flex-wrap gap-1.5"
        role="group"
        aria-label="Lọc jobs theo trạng thái"
      >
        <button
          v-for="tab in filters"
          :key="tab.value"
          type="button"
          :aria-pressed="filter === tab.value"
          class="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium transition-colors focus-visible:outline-2 focus-visible:outline-blue-500"
          :class="
            filter === tab.value
              ? 'bg-blue-800 text-white dark:bg-blue-600'
              : 'bg-gray-100 text-gray-600 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700'
          "
          @click="filter = tab.value"
        >
          {{ tab.label
          }}<span
            class="rounded px-1 tabular-nums"
            :class="
              filter === tab.value
                ? 'bg-white/15'
                : 'bg-gray-200/70 dark:bg-gray-700'
            "
            >{{ tab.count }}</span
          >
        </button>
      </div>
    </div>

    <ul
      v-if="visibleJobs.length"
      class="min-h-0 overflow-y-auto overscroll-contain divide-y divide-gray-100 border-y border-gray-200 dark:divide-gray-800 dark:border-gray-700"
    >
      <li v-for="item in visibleJobs" :key="item.id">
        <button
          type="button"
          class="group flex w-full items-center gap-3 border-l-2 px-4 py-3.5 text-left transition-colors focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-blue-500 sm:px-5"
          :class="
            selectedId === item.id
              ? 'border-blue-600 bg-blue-50/80 dark:bg-blue-950/40'
              : 'border-transparent hover:bg-gray-50 dark:hover:bg-gray-800/60'
          "
          :aria-current="selectedId === item.id ? 'true' : undefined"
          @click="emit('open', item)"
        >
          <span
            class="hidden h-10 w-10 shrink-0 items-center justify-center rounded-lg border border-gray-200 bg-gray-50 text-gray-400 sm:flex dark:border-gray-700 dark:bg-gray-800 dark:text-gray-500"
          >
            <component
              :is="item.source === 'epub' ? FileTextIcon : VideoIcon"
              class="h-5 w-5"
              aria-hidden="true"
            />
          </span>
          <div class="min-w-0 flex-1">
            <div
              class="mb-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] font-medium"
            >
              <span class="text-gray-500 dark:text-gray-400">{{
                item.source === "epub" ? "EPUB" : "YouTube"
              }}</span>
              <span
                class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5"
                :class="
                  item.status === 'done'
                    ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300'
                    : item.status === 'error'
                      ? 'bg-red-50 text-red-700 dark:bg-red-950/50 dark:text-red-300'
                      : 'bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300'
                "
              >
                <CheckIcon
                  v-if="item.status === 'done'"
                  class="h-3 w-3"
                  aria-hidden="true"
                />{{ statusLabel(item) }}
              </span>
              <span
                v-if="selectedId === item.id"
                class="text-blue-700 dark:text-blue-300"
                >Đang mở</span
              >
            </div>
            <p
              class="line-clamp-2 break-words text-sm font-semibold leading-5 text-gray-900 dark:text-gray-100 [overflow-wrap:anywhere]"
              :title="item.title || item.id"
            >
              {{ item.title || item.id }}
            </p>
            <div
              class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-500 dark:text-gray-400"
            >
              <span
                v-if="item.listen?.part"
                class="inline-flex items-center gap-1"
                :class="
                  !item.listen.done ? 'text-blue-700 dark:text-blue-300' : ''
                "
                ><HeadphonesIcon class="h-3.5 w-3.5" aria-hidden="true" />{{
                  item.listen.done
                    ? "Đã nghe hết"
                    : `Nghe dở · Phần ${item.listen.part}`
                }}</span
              >
              <span v-else>Chưa nghe</span>
              <span v-if="formatDate(item.updated)"
                >Cập nhật {{ formatDate(item.updated) }}</span
              >
            </div>
          </div>
          <ChevronRight
            class="h-4 w-4 shrink-0 text-gray-400 group-hover:text-blue-600 dark:group-hover:text-blue-300"
            aria-hidden="true"
          />
        </button>
      </li>
    </ul>
    <div
      v-else
      class="border-y border-gray-200 px-4 py-10 text-center dark:border-gray-700"
    >
      <SearchIcon
        class="mx-auto mb-3 h-7 w-7 text-gray-400"
        aria-hidden="true"
      />
      <p class="text-sm font-medium text-gray-800 dark:text-gray-200">
        Không có job phù hợp
      </p>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Thử từ khóa khác hoặc chọn trạng thái khác.
      </p>
      <button
        type="button"
        class="mt-3 rounded-lg px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-50 focus-visible:outline-2 focus-visible:outline-blue-500 dark:text-blue-300 dark:hover:bg-blue-900/30"
        @click="resetFilters"
      >
        Xóa bộ lọc
      </button>
    </div>

    <div
      class="flex shrink-0 flex-wrap items-center justify-between gap-3 px-4 py-3"
    >
      <p
        class="text-xs tabular-nums text-gray-500 dark:text-gray-400"
        role="status"
      >
        {{ rangeStart }}–{{ Math.min(page * pageSize, filteredJobs.length) }} /
        {{ filteredJobs.length }} jobs
      </p>
      <nav class="flex items-center gap-2" aria-label="Phân trang jobs">
        <button
          type="button"
          aria-label="Trang jobs trước"
          :disabled="page <= 1"
          class="rounded-lg border border-gray-200 p-2 text-gray-600 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-35 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
          @click="page--"
        >
          <ChevronLeft class="h-4 w-4" />
        </button>
        <span class="text-xs tabular-nums text-gray-600 dark:text-gray-300"
          >Trang {{ page }} / {{ pageCount }}</span
        >
        <button
          type="button"
          aria-label="Trang jobs sau"
          :disabled="page >= pageCount"
          class="rounded-lg border border-gray-200 p-2 text-gray-600 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-35 dark:border-gray-700 dark:text-gray-300 dark:hover:bg-gray-800"
          @click="page++"
        >
          <ChevronRight class="h-4 w-4" />
        </button>
      </nav>
    </div>
  </section>
</template>
