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
  return site === "gdrive" ? "Google Drive" : site === "1337x" ? "1337x" : "FileCR";
}

function kindLabel(kind) {
  return ({
    video: "Vídeo", audio: "Áudio", subtitle: "Legenda", image: "Imagem",
    document: "Documento", archive: "Compactado", software: "Software",
    dataset: "Dataset", other: "Outro",
  })[kind] || "Outro";
}

function presenceLabel(item) {
  if (item.presence === "both") return ["exact", "Local e Drive"];
  if (item.gdrive_present || item.site === "gdrive") {
    if (item.presence_confidence === "possible") return ["possible", "Possível no Drive"];
    return ["exact", "No Drive"];
  }
  if (item.presence === "local") return ["local", "Somente local"];
  return ["missing", "Falta no Drive"];
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

async function health() {
  const element = $("[data-health]");
  try {
    const services = await api("/api/services");
    const entries = ["postgres", "redis", "torrent-engine", "gdrive-source", "transcoder"];
    const healthy = entries.filter((name) => services[name]?.healthy).length;
    element.textContent = `${healthy}/${entries.length} serviços saudáveis`;
    element.classList.toggle("bad", healthy !== entries.length);
  } catch {
    element.textContent = "Monitor indisponível";
    element.classList.add("bad");
  }
}

async function dashboard() {
  try {
    const data = await api("/api/dashboard");
    $("[data-stat='titles']").textContent = formatNumber(data.titles ?? data.torrents);
    $("[data-stat='files']").textContent = formatNumber(data.files);
    $("[data-stat='drive']").textContent = formatNumber(data.drive_files ?? data.drive);
    $("[data-stat='active']").textContent = formatNumber(data.active_transfers ?? data.active);
  } catch {
    $$('[data-stat]').forEach((item) => { item.textContent = "—"; });
  }
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

async function files() {
  const params = new URLSearchParams({
    q: $("[data-files-query]").value,
    site: $("[data-files-site]").value,
    kind: $("[data-kind]").value,
    presence: $("[data-presence]").value,
    page: String(state.pages.files),
    per_page: "100",
  });
  const data = await api(`/api/files?${params}`);
  $("[data-files-total]").textContent = `${formatNumber(data.total)} arquivos`;
  const body = $("[data-files-body]");
  body.replaceChildren();
  data.items.forEach((item) => {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "file-main";
    const name = document.createElement("strong");
    name.textContent = item.path?.split(/[\\/]/).at(-1) || item.path;
    const context = document.createElement("small");
    context.textContent = `${item.title || item.display_name || item.infohash} · ${item.path}`;
    nameCell.append(name, context);
    const kindCell = document.createElement("td");
    const kind = document.createElement("span");
    kind.className = "kind-badge";
    kind.textContent = kindLabel(item.file_kind);
    kindCell.append(kind);
    const sourceCell = document.createElement("td");
    sourceCell.textContent = sourceLabel(item.site);
    const sizeCell = document.createElement("td");
    sizeCell.textContent = bytes(item.size);
    const presenceCell = document.createElement("td");
    const [presenceClass, presenceText] = presenceLabel(item);
    const presence = document.createElement("span");
    presence.className = `presence-badge ${presenceClass}`;
    presence.textContent = presenceText;
    presenceCell.append(presence);
    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "row-actions";
    actions.append(button("Abrir", "", () => detail(item.site, item.infohash)));
    if (item.site === "gdrive") {
      actions.append(button("Baixar local", "", () => createTransfer(item, "local")));
    } else {
      actions.append(button("Baixar local", "", () => createTransfer(item, "local")));
      if (!item.gdrive_present) actions.append(button("Enviar ao Drive", "", () => createTransfer(item, "gdrive")));
    }
    actionsCell.append(actions);
    row.append(nameCell, kindCell, sourceCell, sizeCell, presenceCell, actionsCell);
    body.append(row);
  });
  $("[data-files-empty]").hidden = data.items.length !== 0;
  $(".table-shell").hidden = data.items.length === 0;
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
  const size = Number(item.size || 0);
  const isLarge = size >= LARGE_TRANSFER_BYTES;
  const fileId = item.id ?? item.file_id;
  if (fileId === undefined || fileId === null || fileId === "") {
    throw new Error("O arquivo selecionado não possui um identificador válido.");
  }
  const transferKey = `${item.site}:${item.infohash}:${fileId}:${target}`;
  if (state.pendingTransfers.has(transferKey)) {
    toast("Esta transferência já está sendo enviada.");
    return;
  }
  const warnings = [];
  if (item.file_kind === "software") warnings.push("Este arquivo é software. A plataforma apenas o armazena e nunca o executa.");
  if (isLarge) warnings.push(`A transferência tem ${bytes(size)} e pode consumir bastante disco, rede e tempo.`);
  if (target === "gdrive") warnings.push("O conteúdo será baixado para a área local e publicado na pasta classificada do Google Drive.");
  if (warnings.length && !window.confirm(`${warnings.join("\n\n")}\n\nContinuar?`)) return;
  const payload = {
    site: item.site,
    infohash: item.infohash,
    file_ids: [fileId],
    target,
  };
  if (isLarge) payload.confirm_large = true;
  state.pendingTransfers.add(transferKey);
  try {
    const result = await api("/api/transfers", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(target === "gdrive" ? "Transferência para o Drive enfileirada." : "Download local enfileirado.");
    await dashboard();
    resetPage("transfers");
    setView("transfers");
    return result;
  } catch (error) { showError(error); }
  finally { state.pendingTransfers.delete(transferKey); }
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
  trigger.disabled = true;
  trigger.textContent = "Sincronizando…";
  try {
    const result = await api("/api/drive/sync", { method: "POST" });
    toast(result.status === "already_syncing"
      ? "A sincronização do Google Drive já está em andamento."
      : "Sincronização do Google Drive solicitada em segundo plano.");
  } catch (error) { showError(error); }
  finally {
    trigger.disabled = false;
    trigger.textContent = "Sincronizar Drive";
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
$("[data-files-site]").onchange = () => { resetPage("files"); files().catch(showError); };
$("[data-kind]").onchange = () => { resetPage("files"); files().catch(showError); };
$("[data-presence]").onchange = () => { resetPage("files"); files().catch(showError); };
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
