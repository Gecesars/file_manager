const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const state = {
  view: "library",
  pages: { catalog: 1, files: 1, transfers: 1 },
  session: null,
  sessionEpoch: 0,
  pollController: null,
  timer: null,
  transferTimer: null,
  transferRequest: 0,
  filesRequest: 0,
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
    dataset: "Dataset", other: "Outro",
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
  if (!["library", "files", "transfers"].includes(view)) view = "library";
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
    gdrive: ["Arquivos do Google Drive", "Conteúdo remoto catalogado e disponível para streaming ou download local."],
    torrent: ["Inventário de torrents", "Arquivos descritos nos metadados FileCR e 1337x, ainda sem exigir download."],
    local: ["Arquivos locais", "Downloads concluídos e validados na área de armazenamento local."],
    filecr: ["Inventário FileCR", "Arquivos descobertos pelo coletor FileCR."],
    "1337x": ["Inventário 1337x", "Arquivos descobertos pelo coletor 1337x."],
    all: ["Todos os arquivos", "Drive, área local e inventário torrent em uma única busca."],
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

function updateSelectionUI() {
  const selected = Array.from(state.selectedFiles.values());
  const localCount = selected.filter((item) => canTransfer(item, "local")).length;
  const driveCount = selected.filter((item) => canTransfer(item, "gdrive")).length;
  const bar = $("[data-bulk-bar]");
  bar.hidden = selected.length === 0;
  $("[data-selected-count]").textContent = `${formatNumber(selected.length)} arquivo(s) selecionado(s)`;
  $("[data-bulk-local]").disabled = localCount === 0;
  $("[data-bulk-local]").textContent = `Manter no Local (${formatNumber(localCount)})`;
  $("[data-bulk-drive]").disabled = driveCount === 0;
  $("[data-bulk-drive]").textContent = `Disponibilizar no Drive (${formatNumber(driveCount)})`;
  $$('[data-file-select]').forEach((control) => {
    const checked = state.selectedFiles.has(control.dataset.fileSelect);
    control.checked = checked;
    control.closest("tr")?.classList.toggle("selected", checked);
  });
  const selectable = state.filesItems.filter((item) => canTransfer(item, "local") || canTransfer(item, "gdrive"));
  const selectedVisible = selectable.filter((item) => state.selectedFiles.has(fileKey(item))).length;
  const selectVisible = $("[data-select-visible]");
  selectVisible.disabled = selectable.length === 0;
  selectVisible.checked = selectable.length > 0 && selectedVisible === selectable.length;
  selectVisible.indeterminate = selectedVisible > 0 && selectedVisible < selectable.length;
}

function clearFileSelection() {
  state.selectedFiles.clear();
  updateSelectionUI();
}

function groupValue(item, groupBy) {
  if (groupBy === "type") return kindOf(item);
  if (groupBy === "source") return sourceOf(item);
  if (groupBy === "status") return item.status || "cataloged";
  if (groupBy === "presence") return item.presence || presenceLabel(item)[0];
  if (groupBy === "location") return item.location_group || item.location_kind || item.location || locationText(item);
  return "";
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
    both: "Local e Google Drive",
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
    count: entry.count ?? entry.files ?? entry.total,
    bytes: entry.bytes ?? entry.size,
  })).filter((entry) => entry.value);
  if (groups && typeof groups === "object") return Object.entries(groups).map(([value, entry]) => ({
    value,
    label: typeof entry === "object" ? entry.label : null,
    count: typeof entry === "object" ? entry.count ?? entry.files ?? entry.total : entry,
    bytes: typeof entry === "object" ? entry.bytes ?? entry.size : null,
  }));
  return [];
}

function renderFileGroups(groups, groupBy) {
  const target = $("[data-file-groups]");
  target.replaceChildren();
  normalizedGroups(groups).forEach((group) => {
    const displayLabel = displayGroupLabel(group, groupBy);
    const card = document.createElement("button");
    card.type = "button";
    card.className = "file-group";
    card.setAttribute("aria-label", `Selecionar itens visíveis do grupo ${displayLabel}`);
    const label = document.createElement("strong");
    label.textContent = displayLabel;
    const count = document.createElement("span");
    count.textContent = `${formatNumber(group.count)} arquivo(s)${group.bytes == null ? "" : ` · ${bytes(group.bytes)}`}`;
    const hint = document.createElement("small");
    hint.textContent = "Selecionar visíveis";
    card.append(label, count, hint);
    card.onclick = () => {
      const matches = state.filesItems.filter((item) => String(groupValue(item, groupBy)).toLowerCase() === String(group.value).toLowerCase());
      matches.forEach((item) => {
        if (canTransfer(item, "local") || canTransfer(item, "gdrive")) state.selectedFiles.set(fileKey(item), item);
      });
      updateSelectionUI();
      if (!matches.length) toast("Nenhum item visível pertence a este grupo.");
    };
    target.append(card);
  });
  target.hidden = target.childElementCount === 0;
}

async function files() {
  updateFilesContext();
  syncTypeControls();
  const selectedSource = $("[data-files-site]").value;
  const selectedType = $("[data-kind]").value;
  const params = new URLSearchParams({
    q: $("[data-files-query]").value,
    source: selectedSource,
    site: selectedSource === "local" ? "" : selectedSource,
    type: selectedType,
    kind: selectedType,
    status: $("[data-files-status]").value,
    presence: $("[data-presence]").value,
    group_by: $("[data-group-by]").value,
    page: String(state.pages.files),
    per_page: "100",
  });
  const requestId = ++state.filesRequest;
  const data = await api(`/api/files?${params}`);
  if (requestId !== state.filesRequest) return;
  const items = Array.isArray(data.items) ? data.items : [];
  state.filesItems = items;
  state.selectedFiles.clear();
  $("[data-files-total]").textContent = `${formatNumber(data.total)} arquivos`;
  renderFileGroups(data.groups, data.group_by || $("[data-group-by]").value);
  const body = $("[data-files-body]");
  body.replaceChildren();
  items.forEach((item) => {
    const row = document.createElement("tr");
    const selectCell = document.createElement("td");
    selectCell.className = "select-column";
    const select = document.createElement("input");
    select.type = "checkbox";
    select.dataset.fileSelect = fileKey(item);
    select.setAttribute("aria-label", `Selecionar ${item.path || item.display_name || "arquivo"}`);
    select.disabled = !canTransfer(item, "local") && !canTransfer(item, "gdrive");
    select.onchange = () => {
      if (select.checked) state.selectedFiles.set(fileKey(item), item);
      else state.selectedFiles.delete(fileKey(item));
      updateSelectionUI();
    };
    selectCell.append(select);
    const nameCell = document.createElement("td");
    nameCell.className = "file-main";
    const name = document.createElement("strong");
    name.textContent = item.path?.split(/[\\/]/).at(-1) || item.display_name || item.path || "Arquivo sem nome";
    const context = document.createElement("small");
    context.textContent = `${item.title || item.display_name || item.infohash || "Sem título"} · ${item.path || "caminho não informado"}`;
    nameCell.append(name, context);
    const kindCell = document.createElement("td");
    const kind = document.createElement("span");
    kind.className = "kind-badge";
    kind.textContent = kindLabel(kindOf(item));
    kindCell.append(kind);
    const sourceCell = document.createElement("td");
    const source = document.createElement("span");
    source.className = `source-badge ${sourceOf(item)}`;
    source.textContent = sourceLabel(sourceOf(item));
    sourceCell.append(source);
    const statusCell = document.createElement("td");
    const status = document.createElement("span");
    const rawStatus = item.presence_confidence === "possible" && item.status !== "available"
      ? "possible"
      : item.status || (hasLocation(item, "local") || hasLocation(item, "gdrive") ? "available" : "cataloged");
    status.className = `status-badge ${statusClass(rawStatus)}`;
    status.textContent = statusLabel(rawStatus);
    statusCell.append(status);
    const locationCell = document.createElement("td");
    locationCell.className = "file-location";
    locationCell.textContent = locationText(item);
    const sizeCell = document.createElement("td");
    sizeCell.textContent = bytes(item.size);
    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    const detailSite = transferSiteOf(item);
    if (detailSite && item.infohash) actions.append(button("Abrir", "", () => detail(detailSite, item.infohash)));
    if (canTransfer(item, "local")) actions.append(button("Manter local", "", () => createTransfer(item, "local")));
    if (canTransfer(item, "gdrive")) actions.append(button("Enviar ao Drive", "", () => createTransfer(item, "gdrive")));
    actionsCell.append(actions);
    row.append(selectCell, nameCell, kindCell, sourceCell, statusCell, locationCell, sizeCell, actionsCell);
    body.append(row);
  });
  $("[data-files-empty]").hidden = items.length !== 0;
  $(".table-shell").hidden = items.length === 0;
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
    if (state.selectedFiles.size === 0) {
      setView("transfers");
      $("#tab-transfers")?.focus();
    }
    else toast(`${formatNumber(state.selectedFiles.size)} item(ns) permaneceram selecionados para outra ação ou nova tentativa.`);
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
$("[data-group-by]").onchange = () => { resetPage("files"); files().catch(showError); };
$$('[data-type-filter]').forEach((control) => {
  control.onclick = () => {
    $("[data-kind]").value = control.dataset.typeFilter;
    syncTypeControls();
    resetPage("files");
    files().catch(showError);
  };
});
$("[data-select-visible]").onchange = (event) => {
  state.filesItems.forEach((item) => {
    const key = fileKey(item);
    if (event.currentTarget.checked && (canTransfer(item, "local") || canTransfer(item, "gdrive"))) state.selectedFiles.set(key, item);
    else state.selectedFiles.delete(key);
  });
  updateSelectionUI();
};
$("[data-clear-selection]").onclick = clearFileSelection;
$("[data-bulk-local]").onclick = () => createTransfers(Array.from(state.selectedFiles.values()), "local").catch(showError);
$("[data-bulk-drive]").onclick = () => createTransfers(Array.from(state.selectedFiles.values()), "gdrive").catch(showError);
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
  const selected = ["library", "files", "transfers"].includes(requested) ? requested : "library";
  if (state.view !== selected) setView(selected, false);
}

window.addEventListener("hashchange", syncViewFromLocation);
window.addEventListener("popstate", syncViewFromLocation);

const initialView = window.location.hash.replace("#", "");
setView(initialView, false);
if (!["library", "files", "transfers"].includes(initialView)) history.replaceState(null, "", "#library");
health();
dashboard();
categories().catch(showError);
window.setInterval(health, 30000);
window.setInterval(dashboard, 30000);
