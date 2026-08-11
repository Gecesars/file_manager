const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  view: "curation",
  pages: { curation: 1, catalog: 1, files: 1, transfers: 1 },
  session: null,
  sessionEpoch: 0,
  pollController: null,
  timer: null,
  transferTimer: null,
  transferRequest: 0,
  filesRequest: 0,
  curationRequest: 0,
  hls: null,
  metrics: null,
  policyPaused: false,
  userStarted: false,
  playPending: false,
  playAttempt: 0,
  mediaRecoveries: 0,
  hlsError: null,
  failedStreamUrl: null,
  notice: "",
  pendingTransfers: new Set(),
  filesItems: [],
  fileTorrents: new Map(),
  selectedTorrents: new Map(),
  selectedFiles: new Map(),
  selectedSource: "",
};

const video = $("[data-video]");
const startButton = $("[data-start]");
const LARGE_TRANSFER_BYTES = 10 * 1024 ** 3;

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(url, { ...options, headers });
  const contentType = response.headers.get("Content-Type") || "";
  const body = contentType.includes("json") ? await response.json().catch(() => ({})) : {};
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString("pt-BR");
}

function bytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let size = Number(value || 0);
  let index = 0;
  while (Math.abs(size) >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toLocaleString("pt-BR", { maximumFractionDigits: index ? 1 : 0 })} ${units[index]}`;
}

function optionalBytes(value) {
  return value === undefined || value === null || value === "" ? "—" : bytes(value);
}

function rate(value) { return `${bytes(value)}/s`; }

function toast(message, error = false) {
  const item = document.createElement("div");
  item.className = `toast${error ? " error" : ""}`;
  item.setAttribute("role", error ? "alert" : "status");
  item.textContent = message;
  $("[data-toasts]").append(item);
  window.setTimeout(() => item.remove(), 5200);
}

function button(label, className, handler) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = className;
  element.textContent = label;
  element.onclick = async () => {
    if (element.disabled) return;
    element.disabled = true;
    element.setAttribute("aria-busy", "true");
    try {
      await handler();
    } catch (error) {
      showError(error);
    } finally {
      element.disabled = false;
      element.removeAttribute("aria-busy");
    }
  };
  return element;
}

function sourceLabel(site) {
  if (site === "gdrive") return "Google Drive";
  if (site === "local") return "Local";
  if (site === "torrent") return "Torrents";
  if (site === "1337x") return "1337x";
  if (site === "filecr") return "FileCR";
  return site || "Origem desconhecida";
}

function sourceOf(item) { return item.source || item.site || item.origin || ""; }

function kindOf(item) { return item.file_kind || item.type || "other"; }

function kindLabel(kind) {
  return ({
    video: "Vídeo", audio: "Áudio", subtitle: "Legenda", image: "Imagem",
    document: "Documento", archive: "Compactado", software: "Software",
    dataset: "Dataset", other: "Outro", mixed: "Múltiplos tipos",
  })[kind] || "Outro";
}

function presenceLabel(item) {
  if (item.presence_confidence === "possible") return ["possible", "Possível no Drive"];
  const locations = Array.isArray(item.locations)
    ? item.locations.map((value) => typeof value === "string" ? value : value?.kind || value?.source || value?.location)
    : Object.keys(item.locations || {}).filter((key) => item.locations[key]);
  const onDrive = locations.includes("gdrive") || locations.includes("drive");
  const local = locations.includes("local");
  if (onDrive && local) return ["exact", "Local e Drive"];
  if (onDrive) return ["exact", "No Drive"];
  if (local) return ["local", "Somente local"];
  if (item.presence === "both") return ["exact", "Local e Drive"];
  if (item.gdrive_present || sourceOf(item) === "gdrive") {
    if (item.presence_confidence === "possible") return ["possible", "Possível no Drive"];
    return ["exact", "No Drive"];
  }
  if (item.presence === "local") return ["local", "Somente local"];
  return ["missing", "Somente torrent"];
}

function statusLabel(value) {
  const normalized = String(value || "cataloged").toLowerCase();
  return ({
    available: "Disponível", ready: "Disponível", busy: "Em atividade", cataloged: "Catalogado",
    possible: "Possível duplicata",
    indexed: "Catalogado", local: "Disponível localmente", remote: "No Drive",
    queued: "Na fila", downloading: "Baixando", uploading: "Enviando",
    verifying: "Verificando", completed: "Concluído", failed: "Falhou",
    unavailable: "Indisponível", empty: "Sem itens", online: "Online",
    partial: "Disponibilidade parcial", mixed: "Status misto",
  })[normalized] || String(value || "Catalogado");
}

function statusClass(value) {
  const normalized = String(value || "cataloged").toLowerCase().replace(/[^a-z0-9_-]/g, "-");
  if (normalized === "empty") return "empty";
  if (["failed", "unavailable", "offline", "error"].includes(normalized)) return "failed";
  if (["busy", "possible", "queued", "downloading", "uploading", "verifying", "syncing"].includes(normalized)) return "active";
  if (["available", "ready", "completed", "online", "local", "remote"].includes(normalized)) return "ready";
  return "cataloged";
}

function locationText(item) {
  if (Array.isArray(item.locations) && item.locations.length) {
    return item.locations.map((value) => {
      if (typeof value === "string") return sourceLabel(value === "drive" ? "gdrive" : value);
      const source = value?.kind || value?.source || value?.location_kind;
      const relative = value?.path || value?.location;
      const path = value?.destination_path && relative
        ? `${value.destination_path.replace(/[\\/]+$/, "")}/${String(relative).replace(/^[\\/]+/, "")}`
        : relative;
      const label = value?.label || (
        source === "gdrive" && item.presence_confidence === "possible"
          ? "Possível no Drive"
          : sourceLabel(source)
      );
      return path ? `${label}: ${path}` : label;
    }).filter(Boolean).join(" + ");
  }
  if (item.locations && typeof item.locations === "object") {
    const labels = Object.entries(item.locations).filter(([, value]) => value).map(([key, value]) => {
      const name = sourceLabel(key === "drive" ? "gdrive" : key);
      const path = typeof value === "string" ? value : value?.path || value?.location || value?.label;
      return path ? `${name}: ${path}` : name;
    });
    if (labels.length) return labels.join(" + ");
  }
  if (typeof item.location === "string" && item.location) return item.location;
  if (item.location?.label) return item.location.label;
  if (item.location?.path) return item.location.path;
  const [presenceClass, presenceText] = presenceLabel(item);
  return presenceClass === "missing" ? "Somente no inventário" : presenceText;
}

function renderPagination(name, data, loadPage) {
  const total = Number(data.total || 0);
  const page = Math.max(1, Number(data.page || state.pages[name] || 1));
  const perPage = Math.max(1, Number(data.per_page || data.page_size || 1));
  const pages = Math.max(1, Number(data.pages || Math.ceil(total / perPage) || 1));
  state.pages[name] = Math.min(page, pages);
  const navigation = $(`[data-${name}-pagination]`);
  const previous = $(`[data-${name}-prev]`);
  const next = $(`[data-${name}-next]`);
  const label = $(`[data-${name}-page]`);
  navigation.hidden = total === 0 || pages <= 1;
  label.textContent = `Página ${state.pages[name]} de ${pages}`;
  previous.disabled = state.pages[name] <= 1;
  next.disabled = state.pages[name] >= pages;
  previous.onclick = async () => {
    if (previous.disabled) return;
    state.pages[name] -= 1;
    try { await loadPage(); } catch (error) { showError(error); }
  };
  next.onclick = async () => {
    if (next.disabled) return;
    state.pages[name] += 1;
    try { await loadPage(); } catch (error) { showError(error); }
  };
}

function resetPage(name) { state.pages[name] = 1; }

function setView(view, updateHash = true) {
  if (!["curation", "library", "files", "transfers"].includes(view)) view = "curation";
  state.view = view;
  $$('[data-view]').forEach((section) => { section.hidden = section.dataset.view !== view; });
  $$('[data-view-button]').forEach((item) => {
    const selected = item.dataset.viewButton === view;
    item.classList.toggle("active", selected);
    item.setAttribute("aria-selected", String(selected));
    item.tabIndex = selected ? 0 : -1;
  });
  if (updateHash && window.location.hash !== `#${view}`) history.pushState(null, "", `#${view}`);
  if (state.transferTimer) window.clearTimeout(state.transferTimer);
  state.transferTimer = null;
  if (view === "curation") curation().catch(showError);
  if (view === "library") catalog().catch(showError);
  if (view === "files") files().catch(showError);
  if (view === "transfers") transfers().catch(showError);
}

function showError(error) { toast(error?.message || String(error), true); }

function setDomainHealth(domain, healthy, readyText, failedText) {
  const element = $(`[data-domain-health="${domain}"]`);
  if (!element) return;
  element.textContent = healthy ? readyText : failedText;
  element.classList.toggle("ok", Boolean(healthy));
  element.classList.toggle("bad", !healthy);
}

function setDomainInventoryState(domain, files, readyText, emptyText) {
  const element = $(`[data-domain-state="${domain}"]`);
  if (!element) return;
  const available = Number(files || 0) > 0;
  element.textContent = available ? readyText : emptyText;
  element.classList.toggle("ready", available);
  element.classList.toggle("empty-state", !available);
  element.classList.remove("active");
}

function domainSnapshot(data, name, fallback = {}) {
  const current = data.domains?.[name] || {};
  return {
    files: current.files ?? fallback.files,
    bytes: current.bytes ?? fallback.bytes,
    titles: current.titles ?? fallback.titles,
    types: current.types ?? fallback.types,
    location: current.location ?? fallback.location,
    location_kind: current.location_kind ?? fallback.location_kind,
    status: current.status ?? fallback.status,
    selectable: current.selectable ?? fallback.selectable ?? true,
    sources: current.sources ?? fallback.sources,
  };
}

function renderDomain(name, snapshot) {
  $(`[data-domain-metric="${name}-files"]`).textContent = snapshot.files == null ? "—" : formatNumber(snapshot.files);
  $(`[data-domain-metric="${name}-bytes"]`).textContent = optionalBytes(snapshot.bytes);
  $(`[data-domain-metric="${name}-titles"]`).textContent = snapshot.titles == null ? "—" : formatNumber(snapshot.titles);
  const location = $(`[data-domain-location="${name}"]`);
  if (location && snapshot.location) location.textContent = snapshot.location;
  renderDomainTypes(name, snapshot.types);
  const select = $(`[data-select-source="${name}"]`);
  if (select) {
    select.disabled = snapshot.selectable === false;
    select.textContent = snapshot.selectable === false
      ? "Indisponível"
      : (state.selectedSource === name ? "Selecionado" : "Selecionar");
  }
}

function typeEntries(types) {
  if (Array.isArray(types)) return types.map((entry) => {
    if (typeof entry === "string") return { value: entry, count: null };
    return { value: entry.value || entry.type || entry.key || entry.name, count: entry.count ?? entry.files ?? entry.total };
  }).filter((entry) => entry.value);
  if (types && typeof types === "object") return Object.entries(types).map(([value, count]) => ({
    value,
    count: typeof count === "object" ? count.count ?? count.files ?? count.total : count,
  }));
  return [];
}

function renderDomainTypes(name, types) {
  const target = $(`[data-domain-types="${name}"]`);
  if (!target) return;
  const entries = typeEntries(types)
    .sort((left, right) => Number(right.count || 0) - Number(left.count || 0))
    .slice(0, 4);
  target.replaceChildren();
  if (!entries.length) {
    const empty = document.createElement("span");
    empty.className = "type-pill muted";
    empty.textContent = "Tipos ainda não resumidos";
    target.append(empty);
    return;
  }
  entries.forEach((entry) => {
    const item = document.createElement("span");
    item.className = "type-pill";
    item.textContent = `${kindLabel(entry.value)}${entry.count == null ? "" : ` · ${formatNumber(entry.count)}`}`;
    target.append(item);
  });
}

function selectSource(source, openExplorer = false) {
  state.selectedSource = state.selectedSource === source && !openExplorer ? "" : source;
  $$('[data-source-card]').forEach((card) => {
    const selected = card.dataset.sourceCard === state.selectedSource;
    card.classList.toggle("selected", selected);
    card.dataset.selected = String(selected);
  });
  $$('[data-select-source]').forEach((control) => {
    const selected = control.dataset.selectSource === state.selectedSource;
    control.setAttribute("aria-pressed", String(selected));
    if (!control.disabled) control.textContent = selected ? "Selecionado" : "Selecionar";
  });
  if (openExplorer) openFilesDomain(source);
}

function normalizedFilterEntries(values) {
  if (Array.isArray(values)) return values.map((entry) => {
    if (typeof entry === "string") return { value: entry, label: statusLabel(entry), count: null };
    const value = entry.value || entry.status || entry.key || entry.name;
    return { value, label: entry.label || statusLabel(value), count: entry.count ?? entry.total };
  }).filter((entry) => entry.value);
  if (values && typeof values === "object") return Object.entries(values).map(([value, entry]) => ({
    value,
    label: typeof entry === "object" && entry.label ? entry.label : statusLabel(value),
    count: typeof entry === "object" ? entry.count ?? entry.total : entry,
  }));
  return [];
}

function renderDashboardFilters(filters = {}) {
  const statuses = normalizedFilterEntries(filters.statuses || filters.status);
  const statusSelect = $(`[data-files-status]`);
  const currentStatus = statusSelect.value;
  statusSelect.replaceChildren(new Option("Todos os status", ""));
  statuses.forEach((entry) => statusSelect.add(new Option(
    `${entry.label}${entry.count == null ? "" : ` (${formatNumber(entry.count)})`}`,
    entry.value,
  )));
  if ($$("option", statusSelect).some((option) => option.value === currentStatus)) statusSelect.value = currentStatus;

  const typeCounts = new Map(typeEntries(filters.types || filters.type).map((entry) => [entry.value, entry.count]));
  $$('[data-type-filter]').forEach((control) => {
    if (!control.dataset.baseLabel) control.dataset.baseLabel = control.textContent;
    const count = typeCounts.get(control.dataset.typeFilter);
    control.textContent = `${control.dataset.baseLabel}${count == null ? "" : ` · ${formatNumber(count)}`}`;
  });
}

async function health() {
  const element = $("[data-health]");
  try {
    const services = await api("/api/services");
    const entries = ["postgres", "redis", "torrent-engine", "gdrive-source", "transcoder"];
    const healthy = entries.filter((name) => services[name]?.healthy).length;
    element.textContent = `${healthy}/${entries.length} serviços saudáveis`;
    element.classList.toggle("bad", healthy !== entries.length);
    setDomainHealth("gdrive", services["gdrive-source"]?.healthy, "Drive online", "Drive indisponível");
    setDomainHealth("filecr", services["torrent-engine"]?.healthy, "FileCR indexado", "Motor torrent indisponível");
    setDomainHealth("1337x", services["torrent-engine"]?.healthy, "1337x indexado", "Motor torrent indisponível");
    const localHealthy = services.postgres?.healthy && services.redis?.healthy;
    setDomainHealth("local", localHealthy, "Área local pronta", "Área local indisponível");
  } catch {
    element.textContent = "Monitor indisponível";
    element.classList.add("bad");
    setDomainHealth("gdrive", false, "Drive online", "Status indisponível");
    setDomainHealth("filecr", false, "FileCR indexado", "Status indisponível");
    setDomainHealth("1337x", false, "1337x indexado", "Status indisponível");
    setDomainHealth("local", false, "Área local pronta", "Status indisponível");
  }
}

async function dashboard() {
  try {
    const data = await api("/api/dashboard");
    const totalFiles = Number(data.file_count ?? data.files ?? 0);
    const driveFiles = Number(data.gdrive_file_count ?? data.drive_files ?? data.drive ?? 0);
    const legacyTorrent = domainSnapshot(data, "torrent", {
      files: Math.max(0, totalFiles - driveFiles),
      bytes: data.bytes_total,
      titles: data.torrent_count ?? data.titles ?? data.torrents,
    });
    const sourceBreakdown = legacyTorrent.sources || data.torrent_sources_by_site || {};
    const sourceFallback = (source) => {
      const raw = sourceBreakdown[source] || {};
      if (typeof raw === "number") return { titles: raw };
      return {
        files: raw.files ?? raw.file_count,
        bytes: raw.bytes ?? raw.bytes_total,
        titles: raw.titles ?? raw.count ?? raw.torrents,
        types: raw.types,
      };
    };
    const cards = new Map((Array.isArray(data.source_cards) ? data.source_cards : [])
      .map((card) => [String(card.source || card.site || "").toLowerCase(), card]));
    const snapshots = {
      gdrive: { ...domainSnapshot(data, "gdrive", { files: driveFiles, location: "Google Drive" }), ...cards.get("gdrive") },
      filecr: { ...sourceFallback("filecr"), ...cards.get("filecr") },
      "1337x": { ...sourceFallback("1337x"), ...cards.get("1337x") },
      local: { ...domainSnapshot(data, "local", { files: data.local_file_count ?? 0, location: "Armazenamento local" }), ...cards.get("local") },
    };
    Object.entries(snapshots).forEach(([source, snapshot]) => {
      renderDomain(source, snapshot);
      if (snapshot.status) {
        const domainState = $(`[data-domain-state="${source}"]`);
        const stateClass = statusClass(snapshot.status);
        domainState.textContent = `${statusLabel(snapshot.status)}${snapshot.location_kind ? ` · ${snapshot.location_kind}` : ""}`;
        domainState.classList.toggle("ready", stateClass === "ready" || stateClass === "cataloged");
        domainState.classList.toggle("active", stateClass === "active");
        domainState.classList.toggle("empty-state", stateClass === "failed" || stateClass === "empty");
      } else {
        setDomainInventoryState(
          source,
          snapshot.files ?? snapshot.titles,
          source === "local" ? "Downloads locais validados" : "Inventário disponível",
          source === "local" ? "Nenhum download local concluído" : "Inventário ainda vazio",
        );
      }
    });
    renderDashboardFilters(data.filters);
    $("[data-stat='files']").textContent = formatNumber(totalFiles);
    $("[data-stat='bytes']").textContent = optionalBytes(data.bytes_total);
    $("[data-stat='subtitles']").textContent = formatNumber(data.subtitle_count);
    $("[data-stat='active']").textContent = formatNumber(data.active_transfers ?? data.active);
  } catch {
    $$('[data-stat]').forEach((item) => { item.textContent = "—"; });
    $$('[data-domain-metric]').forEach((item) => { item.textContent = "—"; });
    $$('[data-domain-state]').forEach((item) => {
      item.textContent = "Inventário temporariamente indisponível";
      item.classList.remove("ready");
      item.classList.add("empty-state");
    });
  }
}

function updateFilesContext() {
  const site = $("[data-files-site]").value;
  const presence = $("[data-presence]").value;
  const contexts = {
    gdrive: ["Torrents no Google Drive", "Acervo remoto organizado por torrent; expanda um card para ver seus arquivos."],
    torrent: ["Inventário de torrents", "FileCR e 1337x organizados por torrent, sem exigir download para explorar."],
    local: ["Torrents disponíveis localmente", "Downloads concluídos e validados, reunidos por torrent de origem."],
    filecr: ["Torrents do FileCR", "Pacotes do inventário FileCR com arquivos aninhados sob cada torrent."],
    "1337x": ["Torrents do 1337x", "Conteúdos do inventário 1337x com arquivos aninhados sob cada torrent."],
    all: ["Torrents de todas as fontes", "Drive, área local, FileCR e 1337x em uma navegação organizada por torrent."],
  };
  const selected = site === "gdrive"
    ? contexts.gdrive
    : site === "torrent"
      ? contexts.torrent
      : presence === "local"
        ? contexts.local
        : contexts[site] || contexts.all;
  $("[data-files-heading]").textContent = selected[0];
  $("[data-files-context]").textContent = selected[1];
}

function openFilesDomain(domain) {
  const filters = {
    all: { site: "", presence: "" },
    gdrive: { site: "gdrive", presence: "" },
    local: { site: "local", presence: "local" },
    filecr: { site: "filecr", presence: "" },
    "1337x": { site: "1337x", presence: "" },
    torrent: { site: "torrent", presence: "" },
  }[domain] || { site: "", presence: "" };
  state.selectedSource = domain === "all" ? "" : domain;
  $$('[data-source-card]').forEach((card) => card.classList.toggle("selected", card.dataset.sourceCard === state.selectedSource));
  $$('[data-select-source]').forEach((control) => {
    const selected = control.dataset.selectSource === state.selectedSource;
    control.setAttribute("aria-pressed", String(selected));
    if (!control.disabled) control.textContent = selected ? "Selecionado" : "Selecionar";
  });
  $("[data-files-query]").value = "";
  $("[data-files-site]").value = filters.site;
  $("[data-kind]").value = "";
  $("[data-files-status]").value = "";
  $("[data-presence]").value = filters.presence;
  syncTypeControls();
  resetPage("files");
  updateFilesContext();
  setView("files");
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.requestAnimationFrame(() => $("#panel-files").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" }));
}

function curationStatus(value) {
  return ({
    ready: "Pronto para publicar",
    found: "Encontrado, legenda pendente",
    missing: "Não localizado no inventário",
  })[value] || "Em análise";
}

function renderCurationPriorities(items) {
  const target = $("[data-curation-priorities]");
  target.replaceChildren();
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = `priority-card ${item.status}`;
    const rank = document.createElement("span");
    rank.className = "priority-rank";
    rank.textContent = `#${item.rank}`;
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const status = document.createElement("small");
    status.textContent = `${curationStatus(item.status)} · ${formatNumber(item.candidate_count)} candidato(s)`;
    copy.append(title, status);
    card.append(rank, copy);
    if (item.best_candidate) {
      const inspect = button("Ver", "priority-action", async () => {
        $("[data-curation-query]").value = item.title;
        resetPage("curation");
        await curation();
      });
      card.append(inspect);
    }
    target.append(card);
  });
}

function curationAvailability(item) {
  return ({
    torrent: "Somente no torrent",
    local: "Disponível localmente",
    drive: "Já validado no Drive",
    partial: "Disponibilidade parcial",
  })[item.availability] || "Localização desconhecida";
}

async function publishCurated(item) {
  const preview = await api(`/api/curation/media/${encodeURIComponent(item.site)}/${encodeURIComponent(item.infohash)}/preview`);
  const subtitleCount = Number(preview.embedded_subtitle_count || 0) + Number(preview.external_subtitle_count || 0);
  const warning = [
    `Publicar “${preview.title}”?`,
    `Destino: ${preview.drive_path}`,
    `${preview.video_count} vídeo(s), ${subtitleCount} legenda(s), ${bytes(preview.bytes_total)}.`,
    "O download só começará após esta confirmação e poderá consumir espaço e banda.",
  ].join("\n\n");
  if (!window.confirm(warning)) return;
  if (preview.large_confirmation_required && !window.confirm(
    `Transferência grande (${bytes(preview.bytes_total)}). Confirma novamente o download e o envio ao Drive?`,
  )) return;
  const result = await api(
    `/api/curation/media/${encodeURIComponent(item.site)}/${encodeURIComponent(item.infohash)}/publish`,
    {
      method: "POST",
      body: JSON.stringify({
        confirm: true,
        confirm_large: Boolean(preview.large_confirmation_required),
      }),
    },
  );
  toast(`${formatNumber(result.jobs?.length || 0)} job(s) criado(s) para ${result.drive_path}.`);
  setView("transfers");
  $("#tab-transfers")?.focus();
}

function renderCurationItem(item) {
  const card = document.createElement("article");
  card.className = `curation-card ${item.actionable ? "actionable" : "blocked"}`;
  const head = document.createElement("div");
  head.className = "curation-card-head";
  const badges = document.createElement("div");
  badges.className = "curation-badges";
  const kind = document.createElement("span");
  kind.className = "kind-badge";
  kind.textContent = item.media_kind === "tv" ? "TV / Série" : "Filme";
  const source = document.createElement("span");
  source.className = "source-badge 1337x";
  source.textContent = "1337x";
  badges.append(kind, source);
  const score = document.createElement("strong");
  score.className = "curation-score";
  score.textContent = `Score ${Number(item.popularity_score || 0).toLocaleString("pt-BR", { maximumFractionDigits: 1 })}`;
  head.append(badges, score);
  const title = document.createElement("h3");
  title.textContent = item.display_title || item.canonical_title || item.title;
  const release = document.createElement("p");
  release.className = "curation-release";
  release.textContent = `${item.release_year || "Ano não informado"} · ★ ${item.imdb_rating ?? "—"} · ${formatNumber(item.seeders)} seeds`;
  const recommendation = document.createElement("p");
  recommendation.className = "curation-recommendation";
  recommendation.textContent = item.recommendation_reason || "Popularidade no catálogo";
  const metrics = document.createElement("div");
  metrics.className = "curation-metrics";
  metrics.append(
    torrentMetric("Vídeos", formatNumber(item.video_count)),
    torrentMetric("Volume", bytes(item.video_bytes)),
    torrentMetric("Legendas", item.subtitles_ready ? `${formatNumber(item.subtitle_count)} pronta(s)` : "Pendente"),
  );
  const location = document.createElement("p");
  location.className = "curation-location";
  location.textContent = curationAvailability(item);
  const destination = document.createElement("code");
  destination.className = "curation-destination";
  destination.textContent = `#Avideos/${item.destination_path}`;
  const note = document.createElement("p");
  note.className = `curation-note ${item.subtitles_ready ? "ready" : "warning"}`;
  note.textContent = item.subtitles_ready
    ? `${formatNumber(item.embedded_subtitle_count)} no torrent · ${formatNumber(item.external_subtitle_count)} externa(s) validada(s)`
    : "Ação bloqueada até existir uma legenda separada e validada.";
  const actions = document.createElement("div");
  actions.className = "curation-actions";
  const inspect = button("Explorar torrent", "secondary", () => {
    openFilesDomain("1337x");
    $("[data-files-query]").value = item.infohash;
    return files();
  });
  const publish = button(
    item.availability === "drive" ? "Já está no Drive" : item.subtitles_ready ? "Revisar e publicar" : "Aguardando legenda",
    "primary",
    () => publishCurated(item),
  );
  publish.disabled = !item.actionable;
  actions.append(inspect, publish);
  card.append(head, title, release, recommendation, metrics, location, destination, note, actions);
  return card;
}

async function curation() {
  const epoch = ++state.curationRequest;
  const params = new URLSearchParams({
    q: $("[data-curation-query]").value,
    media_kind: $("[data-curation-kind]").value,
    subtitles: $("[data-curation-subtitles]").value,
    availability: $("[data-curation-availability]").value,
    page: String(state.pages.curation),
    per_page: "24",
  });
  const data = await api(`/api/curation/media?${params}`);
  if (epoch !== state.curationRequest) return;
  renderCurationPriorities(Array.isArray(data.priorities) ? data.priorities : []);
  $("[data-curation-total]").textContent = `${formatNumber(data.total)} candidato(s) de mídia`;
  const grid = $("[data-curation-grid]");
  grid.replaceChildren();
  (data.items || []).forEach((item) => grid.append(renderCurationItem(item)));
  $("[data-curation-empty]").hidden = Boolean(data.items?.length);
  renderPagination("curation", data, curation);
}

async function categories() {
  const site = $("[data-catalog-site]").value;
  const data = await api(`/api/categories?site=${encodeURIComponent(site)}`);
  const select = $("[data-category]");
  const current = select.value;
  select.replaceChildren(new Option("Todas as categorias", ""));
  data.items.forEach((item) => select.add(new Option(`${item.category} (${formatNumber(item.total)})`, item.category)));
  if ($$("option", select).some((option) => option.value === current)) select.value = current;
}

async function catalog() {
  const params = new URLSearchParams({
    q: $("[data-catalog-query]").value,
    site: $("[data-catalog-site]").value,
    category: $("[data-category]").value,
    sort: $("[data-sort]").value,
    page: String(state.pages.catalog),
    per_page: "60",
  });
  const data = await api(`/api/catalog?${params}`);
  $("[data-catalog-total]").textContent = `${formatNumber(data.total)} títulos`;
  const grid = $("[data-grid]");
  grid.replaceChildren();
  data.items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "card";
    const top = document.createElement("div");
    top.className = "card-source";
    const source = document.createElement("span");
    source.className = `source-badge${item.site === "gdrive" ? " drive" : ""}`;
    source.textContent = sourceLabel(item.site);
    const category = document.createElement("span");
    category.textContent = item.category || "Sem categoria";
    top.append(source, category);
    const title = document.createElement("h3");
    title.textContent = item.canonical_title || item.title;
    const meta = document.createElement("p");
    meta.textContent = `${item.video_count} vídeo(s) · ${bytes(item.total_size)}`;
    const score = document.createElement("p");
    score.className = "card-score";
    score.textContent = item.site === "gdrive"
      ? `Disponível no Drive · ${item.release_year || "catálogo local"}`
      : `★ ${item.imdb_rating ?? "—"} · ↑ ${formatNumber(item.seeders)} seeds`;
    const actions = document.createElement("div");
    actions.className = "card-actions";
    actions.append(button("Ver arquivos", "secondary", () => detail(item.site, item.infohash)));
    card.append(top, title, meta, score, actions);
    grid.append(card);
  });
  $("[data-catalog-empty]").hidden = data.items.length !== 0;
  renderPagination("catalog", data, catalog);
}

function fileIdOf(item) { return item.id ?? item.file_id; }

function transferSiteOf(item) {
  return [item.site, item.source_site, item.origin_site, item.source]
    .find((value) => ["gdrive", "filecr", "1337x"].includes(value)) || "";
}

function fileKey(item) {
  return [transferSiteOf(item), item.infohash || "", fileIdOf(item), kindOf(item)].join(":");
}

function hasLocation(item, target) {
  if (target === "gdrive" && (item.gdrive_present || sourceOf(item) === "gdrive")) return true;
  if (target === "gdrive" && item.presence_confidence === "possible") return false;
  if (target === "local" && item.presence === "local") return true;
  if (item.presence === "both") return true;
  const values = Array.isArray(item.locations)
    ? item.locations.map((entry) => typeof entry === "string" ? entry : entry?.kind || entry?.source)
    : Object.keys(item.locations || {}).filter((key) => item.locations[key]);
  return values.includes(target) || (target === "gdrive" && values.includes("drive"));
}

function canTransfer(item, target) {
  const source = transferSiteOf(item);
  if (!source || !item.infohash || fileIdOf(item) === undefined || fileIdOf(item) === null) return false;
  if (target === "gdrive") return source !== "gdrive" && !hasLocation(item, "gdrive");
  return !hasLocation(item, "local");
}

function syncTypeControls() {
  const selected = $("[data-kind]").value;
  $$('[data-type-filter]').forEach((control) => {
    const active = control.dataset.typeFilter === selected;
    control.classList.toggle("active", active);
    control.setAttribute("aria-pressed", String(active));
  });
}

function torrentSelectionKey(item) {
  return `${item.site || item.source_site || "unknown"}:${item.infohash || item.torrent_id || item.id || "unknown"}`;
}

function torrentTitle(item) {
  return item.title || item.canonical_title || item.display_name || item.torrent_title || item.infohash || "Torrent sem título";
}

function countEntries(counts) {
  if (!counts || typeof counts !== "object") return [];
  return Object.entries(counts).map(([key, value]) => ({
    key,
    count: Number(typeof value === "object" ? value.count ?? value.files ?? value.total : value) || 0,
    bytes: typeof value === "object" ? value.bytes ?? value.size : null,
  })).filter((entry) => entry.count > 0);
}

function torrentFileCount(item) {
  return Number(item.file_count ?? item.files ?? item.file_total ?? 0) || 0;
}

function torrentByteCount(item) {
  return Number(item.bytes ?? item.size ?? item.total_size ?? 0) || 0;
}

function torrentSource(item) {
  return item.source || item.site || "";
}

function torrentLocationSummary(item) {
  const entries = countEntries(item.location_counts);
  if (entries.length) {
    return entries.map((entry) => `${groupLabel(entry.key, "location")} ${formatNumber(entry.count)}`).join(" · ");
  }
  return groupLabel(item.location_group || item.location_kind || "torrent", "location");
}

function torrentStatusSummary(item) {
  const entries = countEntries(item.status_counts);
  if (entries.length === 1) return statusLabel(entries[0].key);
  if (entries.length > 1) return `${formatNumber(entries.length)} status`;
  return statusLabel(item.status || "cataloged");
}

function torrentMayTransfer(group, target) {
  const summary = group.summary;
  const total = torrentFileCount(summary);
  const locations = new Map(countEntries(summary.location_counts).map((entry) => [entry.key, entry.count]));
  if (target === "gdrive") {
    if (summary.site === "gdrive") return false;
    const onDrive = Number(locations.get("gdrive") || 0) + Number(locations.get("drive") || 0) + Number(locations.get("both") || 0);
    return total === 0 || onDrive < total;
  }
  if (torrentSource(summary) === "local") return false;
  const local = Number(locations.get("local") || 0) + Number(locations.get("both") || 0);
  return total === 0 || local < total;
}

function refreshLoadedFiles() {
  state.filesItems = Array.from(state.fileTorrents.values()).flatMap((group) => group.files || []);
}

function updateSelectionUI() {
  const selectedFiles = Array.from(state.selectedFiles.values());
  const selectedTorrents = Array.from(state.selectedTorrents.values());
  const estimatedFiles = selectedFiles.length + selectedTorrents.reduce((total, group) => total + torrentFileCount(group.summary), 0);
  const localCount = selectedFiles.filter((item) => canTransfer(item, "local")).length;
  const driveCount = selectedFiles.filter((item) => canTransfer(item, "gdrive")).length;
  const hasLocalTorrent = selectedTorrents.some((group) => torrentMayTransfer(group, "local"));
  const hasDriveTorrent = selectedTorrents.some((group) => torrentMayTransfer(group, "gdrive"));
  const hasTorrentSelection = selectedTorrents.length > 0;
  const bar = $("[data-bulk-bar]");
  bar.hidden = !hasTorrentSelection && selectedFiles.length === 0;
  $("[data-selected-count]").textContent = `${formatNumber(selectedTorrents.length)} torrent(s) · cerca de ${formatNumber(estimatedFiles)} arquivo(s)`;
  $("[data-bulk-local]").disabled = !hasLocalTorrent && localCount === 0;
  $("[data-bulk-local]").textContent = `Manter no Local (${formatNumber(estimatedFiles)})`;
  $("[data-bulk-drive]").disabled = !hasDriveTorrent && driveCount === 0;
  $("[data-bulk-drive]").textContent = `Disponibilizar no Drive (${formatNumber(estimatedFiles)})`;

  $$('[data-torrent-select]').forEach((control) => {
    const selected = state.selectedTorrents.has(control.dataset.torrentSelect);
    control.checked = selected;
    control.indeterminate = false;
    control.closest(".torrent-card")?.classList.toggle("selected", selected);
  });
  $$('[data-file-select]').forEach((control) => {
    const group = state.fileTorrents.get(control.dataset.torrentKey);
    const wholeTorrent = group && state.selectedTorrents.has(group.key);
    const checked = wholeTorrent || state.selectedFiles.has(control.dataset.fileSelect);
    control.checked = checked;
    control.closest(".torrent-file-item")?.classList.toggle("selected", checked);
  });

  const groups = Array.from(state.fileTorrents.values());
  const selectVisible = $("[data-select-visible]");
  const selectedVisible = groups.filter((group) => state.selectedTorrents.has(group.key)).length;
  selectVisible.disabled = groups.length === 0;
  selectVisible.checked = groups.length > 0 && selectedVisible === groups.length;
  selectVisible.indeterminate = selectedVisible > 0 && selectedVisible < groups.length;
  const loadedGroups = groups.filter((group) => group.loaded).length;
  $("[data-loaded-files-count]").textContent = state.filesItems.length
    ? `${formatNumber(state.filesItems.length)} arquivo(s) carregado(s) em ${formatNumber(loadedGroups)} torrent(s). A seleção do card sempre abrange o torrent filtrado inteiro.`
    : "Expanda um torrent para carregar seus arquivos; selecionar o card abrange o torrent filtrado inteiro.";
}

function clearFileSelection() {
  state.selectedTorrents.clear();
  state.selectedFiles.clear();
  updateSelectionUI();
}

function groupLabel(value, groupBy) {
  if (groupBy === "type") return kindLabel(value);
  if (groupBy === "source") return sourceLabel(value);
  if (groupBy === "status") return statusLabel(value);
  if (groupBy === "presence") return ({
    exact: "No Drive", possible: "Possível duplicata", gdrive: "No Drive",
    not_gdrive: "Fora do Drive", local: "Local", missing: "Somente inventário",
    both: "Local e Drive",
  })[value] || value;
  if (groupBy === "location") return ({
    torrent: "Somente inventário torrent", local: "Local", gdrive: "Google Drive",
    both: "Local e Google Drive", mixed: "Múltiplas localizações",
  })[value] || value;
  return value || "Sem localização";
}

function displayGroupLabel(group, groupBy) {
  const supplied = String(group.label || "").trim();
  return supplied && supplied.toLowerCase() !== String(group.value).toLowerCase()
    ? supplied
    : groupLabel(group.value, groupBy);
}

function normalizedGroups(groups) {
  if (Array.isArray(groups)) return groups.map((entry) => ({
    value: entry.value || entry.key || entry.type || entry.source || entry.status || entry.presence || entry.location,
    label: entry.label,
    count: entry.torrents ?? entry.count ?? entry.total,
    files: entry.files ?? entry.file_count,
    bytes: entry.bytes ?? entry.size,
  })).filter((entry) => entry.value);
  if (groups && typeof groups === "object") return Object.entries(groups).map(([value, entry]) => ({
    value,
    label: typeof entry === "object" ? entry.label : null,
    count: typeof entry === "object" ? entry.torrents ?? entry.count ?? entry.total : entry,
    files: typeof entry === "object" ? entry.files ?? entry.file_count : null,
    bytes: typeof entry === "object" ? entry.bytes ?? entry.size : null,
  }));
  return [];
}

function renderFileGroups(groups, groupBy) {
  const target = $("[data-file-groups]");
  target.replaceChildren();
  normalizedGroups(groups).forEach((group) => {
    const displayLabel = displayGroupLabel(group, groupBy);
    const card = document.createElement("article");
    card.className = "file-group";
    const label = document.createElement("strong");
    label.textContent = displayLabel;
    const count = document.createElement("span");
    count.textContent = `${formatNumber(group.count)} torrent(s)${group.files == null ? "" : ` · ${formatNumber(group.files)} arquivo(s)`}`;
    const hint = document.createElement("small");
    hint.textContent = group.bytes == null ? "Resumo do acervo filtrado" : bytes(group.bytes);
    card.append(label, count, hint);
    target.append(card);
  });
  target.hidden = target.childElementCount === 0;
}

function torrentFilters() {
  return {
    q: $("[data-files-query]").value,
    kind: $("[data-kind]").value,
    status: $("[data-files-status]").value,
    presence: $("[data-presence]").value,
  };
}

function torrentFileParams(group, page) {
  const summary = group.summary;
  const params = new URLSearchParams({
    view: "files",
    infohash: summary.infohash,
    q: group.filters.q,
    type: group.filters.kind,
    kind: group.filters.kind,
    status: group.filters.status,
    presence: group.filters.presence,
    page: String(page),
    per_page: "200",
  });
  if (torrentSource(summary) === "local") {
    params.set("source", "local");
    params.set("origin_site", summary.site);
  } else {
    params.set("source", summary.site);
  }
  return params;
}

async function fetchTorrentFilePage(group, page) {
  return api(`/api/files?${torrentFileParams(group, page)}`);
}

function torrentMetric(label, value) {
  const metric = document.createElement("div");
  const name = document.createElement("span");
  name.textContent = label;
  const content = document.createElement("strong");
  content.textContent = value;
  metric.append(name, content);
  return metric;
}

function renderSummaryPills(target, entries, labeler, className = "kind-badge") {
  entries.forEach((entry) => {
    const badge = document.createElement("span");
    badge.className = className;
    badge.textContent = `${labeler(entry.key)} ${formatNumber(entry.count)}`;
    target.append(badge);
  });
}

function rawFileStatus(item) {
  if (item.presence_confidence === "possible" && item.status !== "available") return "possible";
  return item.status || (hasLocation(item, "local") || hasLocation(item, "gdrive") ? "available" : "cataloged");
}

function renderTorrentFile(group, item) {
  const row = document.createElement("article");
  row.className = "torrent-file-item";
  const choice = document.createElement("label");
  choice.className = "torrent-file-choice";
  const select = document.createElement("input");
  select.type = "checkbox";
  select.dataset.fileSelect = fileKey(item);
  select.dataset.torrentKey = group.key;
  select.setAttribute("aria-label", `Selecionar somente ${item.path || item.display_name || "arquivo"}`);
  select.disabled = !canTransfer(item, "local") && !canTransfer(item, "gdrive");
  select.onchange = () => {
    if (state.selectedTorrents.has(group.key)) {
      state.selectedTorrents.delete(group.key);
      group.files.forEach((loadedItem) => {
        if (fileKey(loadedItem) !== fileKey(item) && (canTransfer(loadedItem, "local") || canTransfer(loadedItem, "gdrive"))) {
          state.selectedFiles.set(fileKey(loadedItem), loadedItem);
        }
      });
    }
    if (select.checked) state.selectedFiles.set(fileKey(item), item);
    else state.selectedFiles.delete(fileKey(item));
    updateSelectionUI();
  };
  choice.append(select);

  const main = document.createElement("div");
  main.className = "torrent-file-main";
  const name = document.createElement("strong");
  name.textContent = item.path?.split(/[\\/]/).at(-1) || item.display_name || item.path || "Arquivo sem nome";
  const path = document.createElement("small");
  path.textContent = item.path || "Caminho não informado";
  main.append(name, path);

  const facts = document.createElement("div");
  facts.className = "torrent-file-facts";
  const kind = document.createElement("span");
  kind.className = "kind-badge";
  kind.textContent = kindLabel(kindOf(item));
  const status = document.createElement("span");
  const rawStatus = rawFileStatus(item);
  status.className = `status-badge ${statusClass(rawStatus)}`;
  status.textContent = statusLabel(rawStatus);
  const source = document.createElement("span");
  source.className = `source-badge ${sourceOf(item)}`;
  source.textContent = sourceLabel(sourceOf(item));
  facts.append(kind, status, source);

  const location = document.createElement("div");
  location.className = "torrent-file-location";
  const locationLabel = document.createElement("span");
  locationLabel.textContent = "Localização";
  const locationValue = document.createElement("strong");
  locationValue.textContent = locationText(item);
  location.append(locationLabel, locationValue);
  const size = document.createElement("strong");
  size.className = "torrent-file-size";
  size.textContent = bytes(item.size);
  const actions = document.createElement("div");
  actions.className = "row-actions torrent-file-actions";
  if (canTransfer(item, "local")) actions.append(button("Manter local", "", () => createTransfer(item, "local")));
  if (canTransfer(item, "gdrive")) actions.append(button("Enviar ao Drive", "", () => createTransfer(item, "gdrive")));
  row.append(choice, main, facts, location, size, actions);
  return row;
}

function renderTorrentDetails(group) {
  const target = group.elements.details;
  target.replaceChildren();
  const scope = document.createElement("p");
  scope.className = "torrent-selection-scope";
  const knownTotal = group.total || torrentFileCount(group.summary);
  scope.textContent = `${formatNumber(group.files.length)} de ${formatNumber(knownTotal)} arquivo(s) carregado(s). Os checkboxes abaixo fazem seleção individual; o checkbox e as ações do card abrangem todos os arquivos filtrados do torrent.`;
  const actions = document.createElement("div");
  actions.className = "torrent-loaded-actions";
  if (torrentMayTransfer(group, "local")) actions.append(button("Torrent inteiro → Local", "secondary", () => createTorrentTransfers([group], "local")));
  if (torrentMayTransfer(group, "gdrive")) actions.append(button("Torrent inteiro → Drive", "primary", () => createTorrentTransfers([group], "gdrive")));
  const files = document.createElement("div");
  files.className = "torrent-files";
  group.files.forEach((item) => files.append(renderTorrentFile(group, item)));
  target.append(scope, actions, files);
  if (group.page < group.pages) {
    const more = button(`Carregar mais (${formatNumber(group.files.length)}/${formatNumber(knownTotal)})`, "secondary torrent-load-more", () => loadTorrentFiles(group, group.page + 1));
    target.append(more);
  }
  updateSelectionUI();
}

async function loadTorrentFiles(group, page = 1) {
  if (group.loading) return group.loading;
  group.elements.toggle.setAttribute("aria-busy", "true");
  group.elements.message.hidden = false;
  group.elements.message.textContent = page === 1 ? "Carregando arquivos do torrent…" : "Carregando próxima página…";
  group.loading = (async () => {
    const data = await fetchTorrentFilePage(group, page);
    const incoming = Array.isArray(data.items) ? data.items : [];
    const merged = new Map((page === 1 ? [] : group.files).map((item) => [fileKey(item), item]));
    incoming.forEach((item) => merged.set(fileKey(item), item));
    group.files = Array.from(merged.values());
    group.loaded = true;
    group.page = Number(data.page || page);
    group.total = Number(data.total ?? torrentFileCount(group.summary));
    group.pages = Number(data.pages || Math.max(1, Math.ceil(group.total / 200)));
    refreshLoadedFiles();
    renderTorrentDetails(group);
    group.elements.message.hidden = true;
    return data;
  })();
  try {
    return await group.loading;
  } catch (error) {
    group.elements.message.hidden = false;
    group.elements.message.textContent = `Não foi possível carregar os arquivos: ${error.message}`;
    throw error;
  } finally {
    group.loading = null;
    group.elements.toggle.removeAttribute("aria-busy");
  }
}

async function resolveTorrentFiles(group) {
  const resolved = new Map();
  let page = 1;
  let pages = 1;
  do {
    const data = await fetchTorrentFilePage(group, page);
    const items = Array.isArray(data.items) ? data.items : [];
    items.forEach((item) => resolved.set(fileKey(item), item));
    const total = Number(data.total ?? torrentFileCount(group.summary));
    pages = Number(data.pages || Math.max(1, Math.ceil(total / 200)));
    if (!items.length) break;
    page += 1;
  } while (page <= pages);
  return Array.from(resolved.values());
}

async function resolveTransferSelection(groups, individualFiles) {
  const resolved = new Map(individualFiles.map((item) => [fileKey(item), item]));
  for (const group of groups) {
    const items = await resolveTorrentFiles(group);
    items.forEach((item) => resolved.set(fileKey(item), item));
  }
  return Array.from(resolved.values());
}

async function createTorrentTransfers(groups, target, individualFiles = []) {
  if (groups.length) toast(`Resolvendo todos os arquivos filtrados de ${formatNumber(groups.length)} torrent(s)…`);
  const items = await resolveTransferSelection(groups, individualFiles);
  if (!items.length) {
    toast("Nenhum arquivo filtrado foi encontrado para a transferência.", true);
    return [];
  }
  return createTransfers(items, target);
}

async function createSelectedTransfers(target) {
  const groups = Array.from(state.selectedTorrents.values());
  const individualFiles = Array.from(state.selectedFiles.values());
  const localButton = $("[data-bulk-local]");
  const driveButton = $("[data-bulk-drive]");
  localButton.disabled = true;
  driveButton.disabled = true;
  localButton.setAttribute("aria-busy", "true");
  driveButton.setAttribute("aria-busy", "true");
  try {
    const results = await createTorrentTransfers(groups, target, individualFiles);
    if (results.length) {
      groups.forEach((group) => state.selectedTorrents.delete(group.key));
      updateSelectionUI();
      if (state.selectedTorrents.size === 0 && state.selectedFiles.size === 0) setView("transfers");
    }
    return results;
  } finally {
    localButton.removeAttribute("aria-busy");
    driveButton.removeAttribute("aria-busy");
    updateSelectionUI();
  }
}

function renderTorrentCard(group) {
  const item = group.summary;
  const card = document.createElement("article");
  card.className = "torrent-card";
  const head = document.createElement("div");
  head.className = "torrent-card-head";
  const choice = document.createElement("label");
  choice.className = "torrent-choice";
  const select = document.createElement("input");
  select.type = "checkbox";
  select.dataset.torrentSelect = group.key;
  select.setAttribute("aria-label", `Selecionar torrent completo ${torrentTitle(item)}`);
  select.onchange = () => {
    if (select.checked) {
      state.selectedTorrents.set(group.key, group);
      group.files.forEach((file) => state.selectedFiles.delete(fileKey(file)));
    } else {
      state.selectedTorrents.delete(group.key);
    }
    updateSelectionUI();
  };
  choice.append(select);
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "torrent-card-toggle";
  toggle.setAttribute("aria-expanded", "false");
  const arrow = document.createElement("span");
  arrow.className = "torrent-arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = "▶";
  const identity = document.createElement("span");
  identity.className = "torrent-identity";
  const badges = document.createElement("span");
  badges.className = "torrent-identity-badges";
  const source = document.createElement("span");
  source.className = `source-badge ${torrentSource(item)}`;
  source.textContent = sourceLabel(torrentSource(item));
  const category = document.createElement("span");
  category.className = "torrent-category";
  category.textContent = item.category || "Sem categoria";
  badges.append(source, category);
  const title = document.createElement("strong");
  title.textContent = torrentTitle(item);
  const hash = document.createElement("small");
  hash.textContent = item.infohash ? `Hash ${item.infohash}` : "Hash não informado";
  identity.append(badges, title, hash);
  toggle.append(arrow, identity);
  head.append(choice, toggle);

  const summary = document.createElement("div");
  summary.className = "torrent-summary-grid";
  const matchedFiles = torrentFileCount(item);
  const totalFiles = Number(item.total_files ?? matchedFiles);
  const fileMetric = totalFiles !== matchedFiles
    ? `${formatNumber(matchedFiles)} filtrados · ${formatNumber(totalFiles)} no total`
    : formatNumber(matchedFiles);
  const matchedBytes = torrentByteCount(item);
  const totalBytes = Number(item.total_bytes ?? matchedBytes);
  const volumeMetric = totalBytes !== matchedBytes
    ? `${bytes(matchedBytes)} filtrados · ${bytes(totalBytes)} no total`
    : bytes(matchedBytes);
  summary.append(
    torrentMetric("Arquivos", fileMetric),
    torrentMetric("Volume", volumeMetric),
    torrentMetric("Status", torrentStatusSummary(item)),
    torrentMetric("Localização", torrentLocationSummary(item)),
  );
  const typeLine = document.createElement("div");
  typeLine.className = "torrent-type-line";
  renderSummaryPills(typeLine, countEntries(item.types), kindLabel);
  if (!typeLine.childElementCount) {
    const fallback = document.createElement("span");
    fallback.className = "kind-badge";
    fallback.textContent = "Tipos não informados";
    typeLine.append(fallback);
  }

  const footer = document.createElement("div");
  footer.className = "torrent-card-footer";
  const scope = document.createElement("span");
  scope.textContent = "Selecionar o card inclui todos os arquivos filtrados deste torrent.";
  const cardActions = document.createElement("div");
  cardActions.className = "torrent-card-actions";
  const videoCount = Number(
    typeof item.types?.video === "object" ? item.types.video.count ?? item.types.video.files : item.types?.video,
  ) || 0;
  if (item.site && item.infohash && videoCount > 0) {
    cardActions.append(button("Detalhes de mídia", "secondary", () => detail(item.site, item.infohash)));
  }
  if (torrentMayTransfer(group, "local")) cardActions.append(button("Torrent → Local", "secondary", () => createTorrentTransfers([group], "local")));
  if (torrentMayTransfer(group, "gdrive")) cardActions.append(button("Torrent → Drive", "primary", () => createTorrentTransfers([group], "gdrive")));
  footer.append(scope, cardActions);

  const message = document.createElement("p");
  message.className = "torrent-load-message";
  message.hidden = true;
  const details = document.createElement("div");
  details.className = "torrent-card-details";
  details.id = `${group.token}-details`;
  details.hidden = true;
  toggle.setAttribute("aria-controls", details.id);
  group.elements = { card, toggle, message, details };
  toggle.onclick = async () => {
    const open = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", String(open));
    details.hidden = !open;
    if (open && !group.loaded) {
      try { await loadTorrentFiles(group); } catch (error) { showError(error); }
    }
  };
  card.append(head, summary, typeLine, footer, message, details);
  return card;
}

async function files() {
  updateFilesContext();
  syncTypeControls();
  const selectedSource = $("[data-files-site]").value;
  const selectedType = $("[data-kind]").value;
  const params = new URLSearchParams({
    view: "torrents",
    q: $("[data-files-query]").value,
    source: selectedSource,
    type: selectedType,
    kind: selectedType,
    status: $("[data-files-status]").value,
    presence: $("[data-presence]").value,
    group_by: "type",
    page: String(state.pages.files),
    per_page: "40",
  });
  const requestId = ++state.filesRequest;
  const data = await api(`/api/files?${params}`);
  if (requestId !== state.filesRequest) return;
  const items = Array.isArray(data.items) ? data.items : [];
  state.filesItems = [];
  state.fileTorrents.clear();
  state.selectedTorrents.clear();
  state.selectedFiles.clear();
  const totalTorrents = data.total_torrents ?? data.total;
  const pageFiles = items.reduce((total, item) => total + torrentFileCount(item), 0);
  $("[data-files-total]").textContent = `${formatNumber(totalTorrents)} torrent(s) · ${formatNumber(pageFiles)} arquivo(s) filtrado(s) nesta página`;
  renderFileGroups(data.groups, data.group_by || "type");
  const target = $("[data-torrent-list]");
  target.replaceChildren();
  const filters = torrentFilters();
  items.forEach((item, index) => {
    const key = torrentSelectionKey(item);
    const group = {
      key,
      token: `torrent-${requestId}-${index + 1}`,
      summary: item,
      filters: { ...filters },
      files: [],
      page: 0,
      pages: 1,
      total: torrentFileCount(item),
      loaded: false,
      loading: null,
      elements: null,
    };
    state.fileTorrents.set(key, group);
    target.append(renderTorrentCard(group));
  });
  $("[data-files-empty]").hidden = items.length !== 0;
  target.hidden = items.length === 0;
  updateSelectionUI();
  renderPagination("files", data, files);
}

async function detail(site, infohash) {
  const item = await api(`/api/catalog/${encodeURIComponent(site)}/${encodeURIComponent(infohash)}`);
  const body = $("[data-detail-body]");
  body.replaceChildren();
  const intro = document.createElement("div");
  intro.className = "detail-intro";
  const eyebrow = document.createElement("span");
  eyebrow.className = "eyebrow";
  eyebrow.textContent = `${sourceLabel(item.site)} · ${item.category || "Sem categoria"}`;
  const title = document.createElement("h2");
  title.id = "detail-title";
  title.textContent = item.canonical_title || item.title;
  const description = document.createElement("p");
  description.id = "detail-description";
  description.textContent = item.description || "Metadados descritivos ainda indisponíveis para este título.";
  const meta = document.createElement("div");
  meta.className = "detail-meta";
  [item.release_year, item.media_type, item.imdb_rating ? `★ ${item.imdb_rating}` : null, bytes(item.total_size)]
    .filter(Boolean)
    .forEach((value) => {
      const badge = document.createElement("span");
      badge.className = "kind-badge";
      badge.textContent = value;
      meta.append(badge);
    });
  intro.append(eyebrow, title, description, meta);
  body.append(intro);

  const fileItems = item.files || item.videos || [];
  fileItems.forEach((file) => {
    const row = document.createElement("div");
    row.className = "file-row";
    const label = document.createElement("div");
    const name = document.createElement("strong");
    name.textContent = file.path;
    const info = document.createElement("small");
    info.textContent = `${kindLabel(file.file_kind || (file.is_video !== false ? "video" : "other"))} · ${bytes(file.size)}`;
    label.append(name, info);
    const actions = document.createElement("div");
    actions.className = "file-actions";
    if (file.is_video !== false && (file.file_kind === "video" || file.file_kind === undefined)) {
      actions.append(button("Reproduzir", "primary", () => playFile(item, file)));
    }
    const gdrivePresent = file.gdrive_present ?? item.gdrive_present ?? item.site === "gdrive";
    if (item.site === "gdrive") {
      actions.append(button("Baixar local", "secondary", () => createTransfer({ ...item, ...file }, "local")));
    } else {
      actions.append(button("Baixar local", "secondary", () => createTransfer({ ...item, ...file }, "local")));
      if (!gdrivePresent) actions.append(button("Disponibilizar no Drive", "secondary", () => createTransfer({ ...item, ...file }, "gdrive")));
    }
    row.append(label, actions);
    body.append(row);
  });

  if (item.files_truncated) {
    const notice = document.createElement("div");
    notice.className = "detail-notice";
    const message = document.createElement("p");
    message.textContent = "Este título tem mais arquivos do que o detalhe pode exibir de uma vez.";
    const openExplorer = button("Ver todos no explorador", "secondary", async () => {
      $("[data-detail]").close();
      $("[data-files-query]").value = item.infohash;
      $("[data-files-site]").value = item.site;
      resetPage("files");
      setView("files");
    });
    notice.append(message, openExplorer);
    body.append(notice);
  }

  if (item.subtitles?.length) {
    const subtitles = document.createElement("section");
    subtitles.className = "subtitle-list";
    const heading = document.createElement("h3");
    heading.textContent = `Legendas disponíveis (${item.subtitles.length})`;
    subtitles.append(heading);
    item.subtitles.forEach((subtitle) => {
      const line = document.createElement("p");
      line.textContent = `${subtitle.language || "idioma desconhecido"} · ${subtitle.file_name} · ${subtitle.status}`;
      subtitles.append(line);
    });
    body.append(subtitles);
  }
  $("[data-detail]").showModal();
}

async function createTransfer(item, target) {
  return createTransfers([item], target);
}

function transferGroups(items, target) {
  const grouped = new Map();
  items.filter((item) => canTransfer(item, target)).forEach((item) => {
    const site = transferSiteOf(item);
    const kind = kindOf(item);
    const key = `${site}:${item.infohash}:${kind}`;
    if (!grouped.has(key)) grouped.set(key, { site, infohash: item.infohash, kind, items: [] });
    grouped.get(key).items.push(item);
  });
  const chunks = [];
  grouped.forEach((group) => {
    for (let index = 0; index < group.items.length; index += 200) {
      chunks.push({ ...group, items: group.items.slice(index, index + 200) });
    }
  });
  return chunks;
}

async function createTransfers(items, target) {
  const groups = transferGroups(items, target);
  if (!groups.length) {
    toast(target === "gdrive" ? "Os itens selecionados já estão no Drive ou não podem ser enviados." : "Os itens selecionados já estão disponíveis localmente.");
    return [];
  }
  const selectedItems = groups.flatMap((group) => group.items);
  const totalSize = selectedItems.reduce((total, item) => total + Number(item.size || 0), 0);
  const warnings = [
    `${formatNumber(selectedItems.length)} arquivo(s) serão divididos em ${formatNumber(groups.length)} transferência(s) por origem, torrent e tipo.`,
    "A raiz classificada e todos os caminhos relativos serão mantidos.",
  ];
  const ignoredCount = items.length - selectedItems.length;
  if (ignoredCount > 0) {
    warnings.push(`${formatNumber(ignoredCount)} item(ns) não são elegíveis para este destino e permanecerão selecionados.`);
  }
  if (selectedItems.some((item) => kindOf(item) === "software")) {
    warnings.push("A seleção contém software. A plataforma apenas armazena esses arquivos e nunca os executa.");
  }
  if (target === "gdrive" && selectedItems.some((item) => item.presence_confidence === "possible")) {
    warnings.push("Há possíveis duplicatas no Drive por nome e tamanho. A correspondência não foi confirmada por hash; revise antes de continuar.");
  }
  if (totalSize >= LARGE_TRANSFER_BYTES) warnings.push(`O volume selecionado é ${bytes(totalSize)} e pode consumir bastante disco, rede e tempo.`);
  if (target === "gdrive") warnings.push("O conteúdo será baixado para a área local antes da publicação na pasta classificada do Google Drive.");
  else warnings.push("O conteúdo será mantido na área local dentro da pasta classificada correspondente.");
  if (!window.confirm(`${warnings.join("\n\n")}\n\nContinuar?`)) return [];

  const results = [];
  const failures = [];
  const completedItems = [];
  for (const group of groups) {
    const fileIds = group.items.map(fileIdOf);
    const transferKey = `${group.site}:${group.infohash}:${group.kind}:${target}:${fileIds.join(",")}`;
    if (state.pendingTransfers.has(transferKey)) continue;
    state.pendingTransfers.add(transferKey);
    try {
      const payload = {
        site: group.site,
        infohash: group.infohash,
        file_ids: fileIds,
        target,
      };
      if (totalSize >= LARGE_TRANSFER_BYTES) payload.confirm_large = true;
      const result = await api("/api/transfers", { method: "POST", body: JSON.stringify(payload) });
      results.push(result);
      completedItems.push(...group.items);
    } catch (error) {
      failures.push(error);
    } finally {
      state.pendingTransfers.delete(transferKey);
    }
  }
  if (results.length) {
    toast(`${formatNumber(results.length)} transferência(s) enfileirada(s) para ${target === "gdrive" ? "o Drive" : "a área local"}.`);
    completedItems.forEach((item) => state.selectedFiles.delete(fileKey(item)));
    updateSelectionUI();
    await dashboard();
    resetPage("transfers");
    if (state.selectedFiles.size === 0 && state.selectedTorrents.size === 0) {
      setView("transfers");
      $("#tab-transfers")?.focus();
    }
    else toast(`${formatNumber(state.selectedTorrents.size)} torrent(s) e ${formatNumber(state.selectedFiles.size)} arquivo(s) permaneceram selecionados.`);
  }
  if (failures.length) toast(`${formatNumber(failures.length)} grupo(s) não puderam ser enfileirados: ${failures[0].message}`, true);
  return results;
}

function transferProgress(item) {
  if (item.bytes_total) return Math.max(0, Math.min(100, Number(item.bytes_done || 0) * 100 / Number(item.bytes_total)));
  return item.state === "completed" ? 100 : 0;
}

async function transfers() {
  if (state.transferTimer) window.clearTimeout(state.transferTimer);
  state.transferTimer = null;
  const requestId = ++state.transferRequest;
  const params = new URLSearchParams({ page: String(state.pages.transfers), per_page: "100" });
  const data = await api(`/api/transfers?${params}`);
  if (requestId !== state.transferRequest) return;
  const list = $("[data-transfer-list]");
  list.replaceChildren();
  data.items.forEach((item) => {
    const row = document.createElement("article");
    row.className = "transfer";
    const about = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = item.title || item.destination_path || `${item.source_site}:${item.infohash}`;
    const meta = document.createElement("p");
    meta.textContent = `${sourceLabel(item.source_site)} → ${item.target === "gdrive" ? "Google Drive" : "pasta local"} · ${formatNumber(item.file_count || item.selected_file_ids?.length || 0)} arquivo(s)`;
    about.append(title, meta);
    const progressWrap = document.createElement("div");
    progressWrap.className = "transfer-progress";
    const progress = document.createElement("progress");
    progress.className = "progress";
    const percentage = transferProgress(item);
    progress.max = 100;
    progress.value = percentage;
    progress.textContent = `${percentage.toFixed(0)}%`;
    progress.setAttribute("aria-label", `Progresso de ${title.textContent}`);
    const numbers = document.createElement("small");
    numbers.textContent = `${bytes(item.bytes_done)} de ${bytes(item.bytes_total)} · ${percentage.toFixed(0)}%${item.error ? ` · ${item.error}` : ""}`;
    progressWrap.append(progress, numbers);
    const status = document.createElement("span");
    status.className = `transfer-state ${item.state}`;
    status.textContent = item.state;
    row.append(about, progressWrap, status);
    list.append(row);
  });
  $("[data-transfer-empty]").hidden = data.items.length !== 0;
  renderPagination("transfers", data, transfers);
  if (state.view === "transfers" && data.items.some((item) => !["completed", "failed", "cancelled"].includes(item.state))) {
    state.transferTimer = window.setTimeout(() => transfers().catch(showError), 2500);
  }
}

async function syncDrive() {
  const trigger = $("[data-sync-drive]");
  const originalLabel = trigger.textContent;
  trigger.disabled = true;
  trigger.textContent = "Sincronizando…";
  try {
    const result = await api("/api/drive/sync", { method: "POST" });
    toast(result.status === "already_syncing"
      ? "A sincronização do Google Drive já está em andamento."
      : "Sincronização do Google Drive solicitada em segundo plano.");
    const driveState = $('[data-domain-state="gdrive"]');
    driveState.textContent = "Sincronização do inventário em andamento";
    driveState.classList.remove("empty-state");
    driveState.classList.add("ready");
  } catch (error) { showError(error); }
  finally {
    trigger.disabled = false;
    trigger.textContent = originalLabel;
  }
}

function removeTracks() { $$('track', video).forEach((track) => track.remove()); }

async function destroySession() {
  state.sessionEpoch += 1;
  const epoch = state.sessionEpoch;
  if (state.pollController) state.pollController.abort();
  state.pollController = null;
  if (state.timer) window.clearTimeout(state.timer);
  state.timer = null;
  if (state.hls) state.hls.destroy();
  state.hls = null;
  removeTracks();
  video.removeAttribute("src");
  video.load();
  startButton.hidden = true;
  state.policyPaused = false;
  state.userStarted = false;
  state.playPending = false;
  state.playAttempt += 1;
  state.mediaRecoveries = 0;
  state.hlsError = null;
  state.failedStreamUrl = null;
  state.notice = "";
  const old = state.session;
  state.session = null;
  if (old) await api(`/api/playback/${old.id}?token=${encodeURIComponent(old.token)}`, { method: "DELETE" }).catch(() => {});
  return epoch;
}

function attachSubtitleTracks(subtitles = []) {
  removeTracks();
  subtitles.filter((item) => item.track_id).forEach((item, index) => {
    const track = document.createElement("track");
    track.kind = "subtitles";
    track.label = item.language || "Legenda";
    track.srclang = (item.language || "pt-BR").split(/[-_]/)[0];
    track.default = index === 0 && /^pt/i.test(item.language || "");
    track.src = `/api/playback/${state.session.id}/subtitles/${item.track_id}.vtt?token=${encodeURIComponent(state.session.token)}`;
    video.append(track);
  });
}

async function playFile(item, file) {
  const epoch = await destroySession();
  if (epoch !== state.sessionEpoch) return;
  $("[data-detail]").close();
  $("[data-player]").hidden = false;
  $("[data-player-title]").textContent = item.canonical_title || item.title;
  $("[data-player-file]").textContent = file.path;
  $("[data-status]").textContent = "Validando mídia e preparando buffer…";
  const downlink = Number(navigator.connection?.downlink || 0);
  try {
    const session = await api("/api/playback", {
      method: "POST",
      body: JSON.stringify({
        site: item.site,
        infohash: item.infohash,
        file_id: file.id,
        mode: "adaptive",
        quality_cap_bps: downlink ? Math.floor(downlink * 800000) : 0,
      }),
    });
    if (epoch !== state.sessionEpoch) {
      await api(`/api/playback/${session.id}?token=${encodeURIComponent(session.token)}`, { method: "DELETE" }).catch(() => {});
      return;
    }
    state.session = session;
    attachSubtitleTracks(item.subtitles);
    poll(session.id, epoch);
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("[data-player]").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  } catch (error) {
    $("[data-status]").textContent = error.message;
    showError(error);
  }
}

async function startPlayback(fromUser = false) {
  if (state.playPending || (!state.hls && !video.src)) return;
  state.playPending = true;
  const attempt = ++state.playAttempt;
  if (fromUser) state.userStarted = true;
  alignToBufferedRange();
  const timeout = window.setTimeout(() => {
    if (state.playPending && state.playAttempt === attempt) {
      state.playPending = false;
      startButton.hidden = false;
      state.notice = "A mídia ainda não forneceu um quadro reproduzível.";
      $("[data-status]").textContent = state.notice;
    }
  }, 8000);
  try {
    await video.play();
    window.clearTimeout(timeout);
    state.userStarted = true;
    state.policyPaused = false;
    state.notice = "Reproduzindo";
    startButton.hidden = true;
  } catch (error) {
    window.clearTimeout(timeout);
    startButton.hidden = false;
    state.notice = error?.name === "NotAllowedError"
      ? "Pronto. O navegador bloqueou o autoplay; clique em Iniciar reprodução."
      : `Não foi possível iniciar: ${error?.message || error}`;
    $("[data-status]").textContent = state.notice;
  } finally {
    if (state.playAttempt === attempt) state.playPending = false;
  }
}

function ranges(value) {
  return Array.from({ length: value.length }, (_, index) => [value.start(index), value.end(index)]);
}

function playerDiagnostics() {
  return {
    paused: video.paused,
    ended: video.ended,
    ready_state: video.readyState,
    network_state: video.networkState,
    current_time: video.currentTime,
    duration: Number.isFinite(video.duration) ? video.duration : null,
    video_width: video.videoWidth,
    video_height: video.videoHeight,
    buffered: ranges(video.buffered),
    seekable: ranges(video.seekable),
    error: video.error ? { code: video.error.code, message: video.error.message } : null,
    hls_error: state.hlsError,
    media_recoveries: state.mediaRecoveries,
  };
}

function alignToBufferedRange() {
  if (!video.buffered.length) return;
  const inside = ranges(video.buffered).some(([start, end]) => start <= video.currentTime && end >= video.currentTime);
  if (!inside) video.currentTime = video.buffered.start(0) + 0.01;
}

function bufferedAhead() {
  for (let index = 0; index < video.buffered.length; index += 1) {
    if (video.buffered.start(index) <= video.currentTime && video.buffered.end(index) >= video.currentTime) {
      return video.buffered.end(index) - video.currentTime;
    }
  }
  return 0;
}

async function poll(sessionId = state.session?.id, epoch = state.sessionEpoch) {
  if (!state.session || state.session.id !== sessionId || state.sessionEpoch !== epoch) return;
  const controller = new AbortController();
  state.pollController = controller;
  try {
    const statusUrl = state.session.status_url;
    const data = await api(statusUrl, { signal: controller.signal });
    if (!state.session || state.session.id !== sessionId || state.sessionEpoch !== epoch) return;
    state.metrics = data;
    updatePlayer(data);
    if (data.stream_url && data.stream_url !== state.failedStreamUrl && !state.hls && !video.src) {
      await attach(data.stream_url, sessionId, epoch);
    }
    if (!["error", "closed"].includes(data.state) && state.session?.id === sessionId && state.sessionEpoch === epoch) {
      state.timer = window.setTimeout(() => poll(sessionId, epoch), 1500);
    }
  } catch (error) {
    if (error?.name === "AbortError" || state.session?.id !== sessionId || state.sessionEpoch !== epoch) return;
    $("[data-status]").textContent = error.message;
    state.timer = window.setTimeout(() => poll(sessionId, epoch), 2500);
  } finally {
    if (state.pollController === controller) state.pollController = null;
  }
}

async function attach(url, sessionId, epoch) {
  const module = await import("/vendor/hls.mjs");
  if (state.session?.id !== sessionId || state.sessionEpoch !== epoch) return;
  const Hls = module.default;
  if (Hls.isSupported()) {
    const hls = new Hls({
      maxBufferLength: state.metrics?.buffer?.target_seconds || 45,
      maxMaxBufferLength: 120,
      backBufferLength: 30,
      capLevelToPlayerSize: true,
    });
    state.hls = hls;
    const isCurrent = () => state.session?.id === sessionId && state.sessionEpoch === epoch && state.hls === hls;
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (!isCurrent()) return;
      startButton.hidden = false;
      state.notice = "Mídia pronta. Clique para iniciar.";
    });
    hls.on(Hls.Events.FRAG_BUFFERED, () => {
      if (!isCurrent()) return;
      state.hlsError = null;
      alignToBufferedRange();
    });
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!isCurrent()) return;
      state.hlsError = { type: data.type, details: data.details, fatal: Boolean(data.fatal), reason: data.reason || null };
      if (!data.fatal) return;
      state.notice = `Erro HLS: ${data.details || data.type}`;
      startButton.hidden = false;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) hls.startLoad();
      else if (data.type === Hls.ErrorTypes.MEDIA_ERROR && state.mediaRecoveries < 2) {
        state.mediaRecoveries += 1;
        hls.recoverMediaError();
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        state.failedStreamUrl = url;
        state.notice = `Fragmento incompatível: ${data.details || "erro de demux"}`;
        hls.destroy();
        if (state.hls === hls) state.hls = null;
      } else {
        state.failedStreamUrl = url;
        hls.destroy();
        if (state.hls === hls) state.hls = null;
      }
    });
  } else {
    if (state.session?.id !== sessionId || state.sessionEpoch !== epoch) return;
    video.src = url;
    startButton.hidden = false;
    state.notice = "Mídia pronta";
  }
}

function updatePlayer(data) {
  const torrent = data.torrent || {};
  $("[data-state]").textContent = data.state;
  $("[data-rate]").textContent = rate(torrent.download_bytes_per_second || 0);
  $("[data-swarm-label]").textContent = torrent.provider === "gdrive" ? "Origem" : "Seeds/peers";
  $("[data-swarm]").textContent = torrent.provider === "gdrive" ? "Google Drive" : `${torrent.seeds || 0}/${torrent.peers || 0}`;
  $("[data-buffer]").textContent = `${bufferedAhead().toFixed(1)}s / ${data.buffered_seconds_estimate}s`;
  $("[data-strategy]").textContent = data.strategy || data.transcode?.strategy || "—";
  $("[data-status]").textContent = data.error || state.notice || `${data.buffer.reason} · meta ${data.buffer.target_seconds}s`;
  $("[data-diagnostics]").textContent = JSON.stringify({ player: playerDiagnostics(), backend: data }, null, 2);
  if (state.hls) {
    state.hls.config.maxBufferLength = data.buffer.target_seconds;
    const cap = data.buffer.quality_cap_bps;
    const eligible = state.hls.levels
      .map((level, index) => ({ index, bitrate: level.bitrate || 0 }))
      .filter((level) => level.bitrate <= cap);
    state.hls.autoLevelCapping = eligible.length ? eligible.at(-1).index : 0;
  }
  if (data.buffer.should_pause && bufferedAhead() < data.buffer.startup_seconds && !video.paused) {
    state.policyPaused = true;
    state.notice = "Aguardando o buffer seguro";
    video.pause();
  } else if (state.policyPaused && bufferedAhead() >= data.buffer.startup_seconds) {
    state.policyPaused = false;
    startPlayback(false);
  }
}

$$('[data-view-button]').forEach((item, index, tabs) => {
  item.onclick = () => setView(item.dataset.viewButton);
  item.onkeydown = (event) => {
    let targetIndex = null;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) targetIndex = (index - 1 + tabs.length) % tabs.length;
    if (["ArrowRight", "ArrowDown"].includes(event.key)) targetIndex = (index + 1) % tabs.length;
    if (event.key === "Home") targetIndex = 0;
    if (event.key === "End") targetIndex = tabs.length - 1;
    if (targetIndex === null) return;
    event.preventDefault();
    tabs[targetIndex].focus();
    tabs[targetIndex].click();
  };
});
$("[data-health]").onclick = () => health();
$("[data-sync-drive]").onclick = syncDrive;
$("[data-open-curation]").onclick = () => setView("curation");
$("[data-open-all]").onclick = () => openFilesDomain("all");
$("[data-open-transfers]").onclick = () => setView("transfers");
$$('[data-select-source]').forEach((item) => { item.onclick = () => selectSource(item.dataset.selectSource, true); });
$$('[data-open-domain]').forEach((item) => { item.onclick = () => openFilesDomain(item.dataset.openDomain); });
$("[data-catalog-search]").onclick = () => { resetPage("catalog"); catalog().catch(showError); };
$("[data-catalog-query]").onkeydown = (event) => {
  if (event.key === "Enter") { resetPage("catalog"); catalog().catch(showError); }
};
$("[data-catalog-site]").onchange = async () => {
  resetPage("catalog");
  try { await categories(); await catalog(); } catch (error) { showError(error); }
};
$("[data-category]").onchange = () => { resetPage("catalog"); catalog().catch(showError); };
$("[data-sort]").onchange = () => { resetPage("catalog"); catalog().catch(showError); };
$("[data-curation-search]").onclick = () => { resetPage("curation"); curation().catch(showError); };
$("[data-curation-query]").onkeydown = (event) => {
  if (event.key === "Enter") { resetPage("curation"); curation().catch(showError); }
};
$("[data-curation-kind]").onchange = () => { resetPage("curation"); curation().catch(showError); };
$("[data-curation-subtitles]").onchange = () => { resetPage("curation"); curation().catch(showError); };
$("[data-curation-availability]").onchange = () => { resetPage("curation"); curation().catch(showError); };
$("[data-files-search]").onclick = () => { resetPage("files"); files().catch(showError); };
$("[data-files-query]").onkeydown = (event) => {
  if (event.key === "Enter") { resetPage("files"); files().catch(showError); }
};
$("[data-files-site]").onchange = () => {
  state.selectedSource = $("[data-files-site]").value;
  $$('[data-source-card]').forEach((card) => card.classList.toggle("selected", card.dataset.sourceCard === state.selectedSource));
  $$('[data-select-source]').forEach((control) => {
    const selected = control.dataset.selectSource === state.selectedSource;
    control.setAttribute("aria-pressed", String(selected));
    if (!control.disabled) control.textContent = selected ? "Selecionado" : "Selecionar";
  });
  resetPage("files");
  files().catch(showError);
};
$("[data-kind]").onchange = () => { syncTypeControls(); resetPage("files"); files().catch(showError); };
$("[data-files-status]").onchange = () => { resetPage("files"); files().catch(showError); };
$("[data-presence]").onchange = () => { resetPage("files"); files().catch(showError); };
$$('[data-type-filter]').forEach((control) => {
  control.onclick = () => {
    $("[data-kind]").value = control.dataset.typeFilter;
    syncTypeControls();
    resetPage("files");
    files().catch(showError);
  };
});
$("[data-select-visible]").onchange = (event) => {
  if (event.currentTarget.checked) {
    state.fileTorrents.forEach((group) => {
      state.selectedTorrents.set(group.key, group);
      group.files.forEach((item) => state.selectedFiles.delete(fileKey(item)));
    });
  } else {
    state.selectedTorrents.clear();
    state.selectedFiles.clear();
  }
  updateSelectionUI();
};
$("[data-clear-selection]").onclick = clearFileSelection;
$("[data-bulk-local]").onclick = () => createSelectedTransfers("local").catch(showError);
$("[data-bulk-drive]").onclick = () => createSelectedTransfers("gdrive").catch(showError);
$("[data-refresh-transfers]").onclick = () => transfers().catch(showError);
startButton.onclick = () => startPlayback(true);
$("[data-close]").onclick = async () => { await destroySession(); $("[data-player]").hidden = true; };
video.addEventListener("playing", () => { state.notice = "Reproduzindo"; state.mediaRecoveries = 0; startButton.hidden = true; });
video.addEventListener("loadedmetadata", alignToBufferedRange);
video.addEventListener("canplay", alignToBufferedRange);
video.addEventListener("waiting", () => { state.notice = "Carregando o próximo segmento…"; });
video.addEventListener("stalled", () => { state.notice = "Fluxo temporariamente interrompido; tentando retomar…"; });
video.addEventListener("pause", () => {
  if (!state.policyPaused && state.userStarted && !video.ended) {
    state.notice = "Pausado";
    startButton.hidden = false;
  }
});
video.addEventListener("error", () => {
  state.notice = `Erro do player: ${video.error?.message || video.error?.code || "desconhecido"}`;
  startButton.hidden = false;
});
window.addEventListener("beforeunload", () => {
  if (state.session) navigator.sendBeacon(`/api/playback/${state.session.id}/close?token=${encodeURIComponent(state.session.token)}`);
});
function syncViewFromLocation() {
  const requested = window.location.hash.replace("#", "");
  const selected = ["curation", "library", "files", "transfers"].includes(requested) ? requested : "curation";
  if (state.view !== selected) setView(selected, false);
}

window.addEventListener("hashchange", syncViewFromLocation);
window.addEventListener("popstate", syncViewFromLocation);

const initialView = window.location.hash.replace("#", "");
setView(initialView, false);
if (!["curation", "library", "files", "transfers"].includes(initialView)) history.replaceState(null, "", "#curation");
health();
dashboard();
categories().catch(showError);
window.setInterval(health, 30000);
window.setInterval(dashboard, 30000);
