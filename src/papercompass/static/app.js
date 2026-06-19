const state = {
  q: "",
  year: "",
  keyword: "",
  venue: "",
  sort: "relevance",
  workspace: "",
  workspaces: [],
  limit: 80,
  total: 0,
  results: [],
  loading: false,
  hasMore: false,
  selected: "",
  detailTab: "overview",
  selectedPaper: null,
  view: "library",
  buildStatus: null,
};

const $ = (id) => document.getElementById(id);

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function resolveMarkdownUrl(url, paper) {
  const trimmed = String(url || "").trim();
  if (!trimmed) return "";
  if (/^https?:\/\//i.test(trimmed) || trimmed.startsWith("#")) return trimmed;
  if (trimmed.startsWith("assets/") && paper?.paper_key) {
    const assetName = trimmed.replace(/^assets\//, "");
    return apiUrl(`/api/fulltext-asset/${encodeURIComponent(paper.paper_key)}/${encodeURIComponent(assetName)}`);
  }
  return trimmed;
}

function renderInlineMarkdown(value, paper) {
  const placeholders = [];
  const hold = (html) => {
    const token = `\u0000MD${placeholders.length}\u0000`;
    placeholders.push(html);
    return token;
  };
  let text = String(value ?? "");
  text = text.replace(/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, alt, url) => {
    const src = resolveMarkdownUrl(url, paper);
    return hold(`<img src="${escapeAttr(src)}" alt="${escapeAttr(alt)}" loading="lazy" />`);
  });
  text = text.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, label, url) => {
    const href = resolveMarkdownUrl(url, paper);
    return hold(`<a href="${escapeAttr(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`);
  });
  text = text.replace(/`([^`]+)`/g, (_, code) => hold(`<code>${escapeHtml(code)}</code>`));
  text = escapeHtml(text);
  text = text
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
  placeholders.forEach((html, index) => {
    text = text.replaceAll(`\u0000MD${index}\u0000`, html);
  });
  return text;
}

function tableCells(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function isTableStart(lines, index) {
  if (index + 1 >= lines.length) return false;
  return lines[index].includes("|") && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1]);
}

function renderMarkdown(markdown, paper) {
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }

    if (/^```/.test(line.trim())) {
      const codeLines = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i].trim())) {
        codeLines.push(lines[i]);
        i += 1;
      }
      i += 1;
      html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      const level = Math.min(6, heading[1].length);
      html.push(`<h${level}>${renderInlineMarkdown(heading[2], paper)}</h${level}>`);
      i += 1;
      continue;
    }

    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      html.push("<hr />");
      i += 1;
      continue;
    }

    if (isTableStart(lines, i)) {
      const headers = tableCells(lines[i]);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(tableCells(lines[i]));
        i += 1;
      }
      html.push(`
        <div class="table-wrap">
          <table>
            <thead><tr>${headers.map((cell) => `<th>${renderInlineMarkdown(cell, paper)}</th>`).join("")}</tr></thead>
            <tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${renderInlineMarkdown(cell, paper)}</td>`).join("")}</tr>`).join("")}</tbody>
          </table>
        </div>
      `);
      continue;
    }

    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i += 1;
      }
      html.push(`<ul>${items.map((item) => `<li>${renderInlineMarkdown(item, paper)}</li>`).join("")}</ul>`);
      continue;
    }

    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i += 1;
      }
      html.push(`<ol>${items.map((item) => `<li>${renderInlineMarkdown(item, paper)}</li>`).join("")}</ol>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      html.push(`<blockquote>${renderInlineMarkdown(quote.join(" "), paper)}</blockquote>`);
      continue;
    }

    const paragraph = [line.trim()];
    i += 1;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```/.test(lines[i].trim()) &&
      !/^(#{1,6})\s+/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i]) &&
      !isTableStart(lines, i)
    ) {
      paragraph.push(lines[i].trim());
      i += 1;
    }
    html.push(`<p>${renderInlineMarkdown(paragraph.join(" "), paper)}</p>`);
  }

  return html.join("");
}

async function fetchJson(url) {
  const res = await fetch(url);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function optionList(items, emptyLabel) {
  const head = `<option value="">${escapeHtml(emptyLabel)}</option>`;
  return head + items.map((item) => (
    `<option value="${escapeHtml(item.value)}">${escapeHtml(item.value)} · ${item.count}</option>`
  )).join("");
}

function setText(id, value) {
  const el = $(id);
  if (el) el.textContent = value ?? "-";
}

function statusTone(value) {
  const text = String(value || "").toLowerCase();
  if (["passed_authoritative", "passed_complete", "completed", "passed", "safe"].includes(text)) return "ok";
  if (["usable_with_caveats", "warning", "partial_due_to_budget", "partial_not_authoritative", "in_progress"].includes(text)) return "warn";
  if (["needs_rule_repair", "needs_seed_or_query_repair", "failed", "error", "not_safe"].includes(text)) return "danger";
  return "muted";
}

function apiUrl(path, params = {}) {
  const query = new URLSearchParams(params);
  if (state.workspace) query.set("workspace", state.workspace);
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function resetSelection() {
  state.selected = "";
  state.selectedPaper = null;
  state.detailTab = "overview";
}

function resetPaging() {
  state.total = 0;
  state.results = [];
  state.loading = false;
  state.hasMore = false;
}

function resetFilters() {
  state.q = "";
  state.year = "";
  state.keyword = "";
  state.venue = "";
  state.sort = "relevance";
  resetSelection();
  resetPaging();
  $("searchInput").value = "";
  $("sortMode").value = "relevance";
}

function renderWorkspaceSelect(payload) {
  const select = $("workspaceSelect");
  const workspaces = payload.workspaces || [];
  select.innerHTML = workspaces.map((workspace) => {
    const label = `${workspace.name || workspace.id} · ${workspace.paper_count || 0}`;
    return `<option value="${escapeAttr(workspace.id)}">${escapeHtml(label)}</option>`;
  }).join("");
  select.value = state.workspace;
  const active = workspaces.find((workspace) => workspace.id === state.workspace);
  setText("workspacePath", active?.path || payload.root || "-");
}

async function loadWorkspaces() {
  const payload = await fetchJson(state.workspace ? apiUrl("/api/workspaces") : "/api/workspaces");
  state.workspaces = payload.workspaces || [];
  if (!state.workspace) {
    state.workspace = payload.current || payload.default || state.workspaces[0]?.id || "";
  }
  renderWorkspaceSelect(payload);
}

async function loadSummary() {
  const summary = await fetchJson(apiUrl("/api/summary"));
  setText("topicLabel", summary.topic_id || "workspace");
  setText("paperCount", summary.paper_count ?? "-");
  setText("fulltextCount", summary.fulltext_count ?? "-");
  setText("rawCount", summary.raw_candidate_count ?? "-");
  setText("catalogBuiltAt", summary.catalog_built_at || "-");
  const years = Object.keys(summary.years || {}).filter((year) => year !== "unknown");
  setText("yearRange", years.length ? `${years[years.length - 1]} - ${years[0]}` : "-");
}

async function loadBuildStatus() {
  state.buildStatus = await fetchJson(apiUrl("/api/build-status"));
  renderBuildStatus();
}

async function loadFilters() {
  const filters = await fetchJson(apiUrl("/api/filters"));
  $("yearFilter").innerHTML = optionList(filters.years || [], "All years");
  $("keywordFilter").innerHTML = optionList(filters.keywords || [], "All tags");
  $("venueFilter").innerHTML = optionList(filters.venues || [], "All venues");
  $("yearFilter").value = state.year;
  $("keywordFilter").value = state.keyword;
  $("venueFilter").value = state.venue;
}

function updateActiveSummary(total = null, loaded = null) {
  const parts = [];
  if (state.q) parts.push(`query: ${state.q}`);
  if (state.year) parts.push(`year: ${state.year}`);
  if (state.keyword) parts.push(`tag: ${state.keyword}`);
  if (state.venue) parts.push(`venue: ${state.venue}`);
  parts.push(`sort: ${state.sort}`);
  const prefix = total === null ? "" : `${loaded ?? total} / ${total} loaded · `;
  $("activeSummary").textContent = prefix + (parts.length ? parts.join(" · ") : "All papers");
}

function resultMeta(paper) {
  const pieces = [
    paper.year || "N/A",
    paper.venue || "N/A",
    `${paper.max_citation || 0} cites`,
  ];
  if (paper.ids?.arxiv) pieces.push(`arXiv ${paper.ids.arxiv}`);
  return pieces.map(escapeHtml).join(" · ");
}

function tag(value, tone = "") {
  return `<span class="tag ${tone}">${escapeHtml(value)}</span>`;
}

function tagList(values, tone = "", limit = 8) {
  return (values || []).slice(0, limit).map((value) => tag(value, tone)).join("");
}

function metricRow(label, value) {
  return `
    <div class="info-row">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value ?? "-")}</strong>
    </div>
  `;
}

function renderStatusPill(id, value, fallback = "-") {
  const el = $(id);
  if (!el) return;
  const text = value || fallback;
  el.textContent = text;
  el.className = `status-pill ${statusTone(text)}`;
}

function renderBuildStatus() {
  const payload = state.buildStatus;
  if (!payload) return;
  const summary = payload.summary || {};
  const counts = summary.counts || {};
  const quality = summary.quality || {};
  const coverage = summary.coverage || {};
  const environment = summary.environment || {};
  const modelMeta = summary.brain_usage?.metadata || {};
  setText("buildDirection", payload.state?.direction || summary.direction || "-");
  renderStatusPill("buildStatus", summary.deliverable_status || summary.status, payload.has_summary ? "unknown" : "not_run");
  renderStatusPill("buildSafe", summary.safe_for_default_llm_retrieval ? "safe" : payload.has_summary ? "not_safe" : "not_run");
  setText("buildPapers", counts.papers ?? "-");
  setText("buildAnchors", counts.anchors ?? "-");
  setText("buildPending", counts.pending ?? "-");
  setText("buildRejected", counts.rejected ?? "-");
  setText("buildSeeds", `${summary.seed_total ?? "-"} / missing ${summary.seed_missing ?? "-"}`);

  const stages = payload.stages || [];
  $("stageList").innerHTML = stages.length ? stages.map((stage) => {
    const countText = stage.counts && Object.keys(stage.counts).length
      ? Object.entries(stage.counts).slice(0, 4).map(([k, v]) => `${k}: ${v}`).join(" · ")
      : "";
    const extras = [
      stage.batches ? `${stage.batches} batches` : "",
      stage.brain_missing_scores ? `${stage.brain_missing_scores} missing scores` : "",
      stage.truncated ? `${stage.uncovered_capped || 0} uncovered` : "",
    ].filter(Boolean).join(" · ");
    return `
      <div class="stage-row">
        <div class="stage-dot ${statusTone(stage.status)}"></div>
        <div>
          <div class="stage-name">${escapeHtml(stage.label || stage.name)}</div>
          <div class="stage-meta">${escapeHtml([stage.status, countText, extras].filter(Boolean).join(" · "))}</div>
        </div>
      </div>
    `;
  }).join("") : `<div class="empty-state compact"><i data-lucide="workflow"></i><h2>No build state</h2></div>`;

  const recall = coverage.recall_pool || {};
  $("qualityGrid").innerHTML = [
    metricRow("QA", summary.qa_status || quality.qa_status || "-"),
    metricRow("Hard reasons", (quality.hard_reasons || []).length),
    metricRow("Truncations", (coverage.truncations || []).length),
    metricRow("Recall pool", recall.status || "-"),
    metricRow("Recall queue/.raw/final", [recall.review_queue_count, recall.raw_candidate_count, recall.final_paper_count].filter((v) => v !== undefined).join(" / ") || "-"),
    metricRow("Source risk", coverage.source_risk_count ?? "-"),
  ].join("");

  const warnings = [
    ...(quality.qa_warnings || []),
    ...(quality.environment_warnings || []),
    ...(quality.hard_reasons || []),
  ];
  $("buildWarnings").innerHTML = warnings.length ? tagList(warnings, "warn", 32) : tag("No warning", "muted");

  const channels = summary.channels_active || {};
  $("signalGrid").innerHTML = [
    metricRow("Brain", payload.state?.brain || summary.brain || "-"),
    metricRow("Model", (modelMeta.models || []).join(", ") || "-"),
    metricRow("Reasoning", (modelMeta.reasoning_efforts || []).join(", ") || "-"),
    metricRow("Embedding", channels.embedding ? "on" : "off"),
    metricRow("Metadata", channels.metadata ? "on" : "off"),
    metricRow("Weak batches", `${environment.weak_batches_effective ?? "-"} / ${environment.weak_batches_needed ?? "-"}`),
    metricRow("Boundary batches", environment.boundary_batches_effective ?? "-"),
  ].join("");

  const artifacts = summary.artifacts || {};
  $("artifactList").innerHTML = Object.entries(artifacts).map(([label, value]) => `
    <div class="artifact-row">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value || "-")}</strong>
    </div>
  `).join("") || `<div class="empty-state compact"><i data-lucide="folder-x"></i><h2>No artifacts</h2></div>`;
  refreshIcons();
}

function showView(view) {
  state.view = view;
  $("libraryView").classList.toggle("hidden", view !== "library");
  $("libraryTopbar").classList.toggle("hidden", view !== "library");
  $("buildView").classList.toggle("hidden", view !== "build");
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.view === view);
  });
  if (view === "build") {
    loadBuildStatus().catch(renderError);
  }
}

function listFooter() {
  if (state.loading && state.results.length) {
    return `<div class="list-footer"><i data-lucide="loader"></i><span>Loading more</span></div>`;
  }
  if (state.hasMore) {
    return `
      <button class="load-more-button" id="loadMoreButton" type="button">
        <span>Load more</span>
        <i data-lucide="chevron-down"></i>
      </button>
    `;
  }
  if (state.total > 0) {
    return `<div class="list-footer"><span>All ${state.total} loaded</span></div>`;
  }
  return "";
}

function renderResultsFromState() {
  $("resultCount").textContent = `${state.results.length} / ${state.total} loaded`;
  updateActiveSummary(state.total, state.results.length);
  const list = $("resultsList");
  if (!state.results.length && state.loading) {
    list.innerHTML = `<div class="loading-state"><i data-lucide="loader"></i><h2>Loading</h2></div>`;
    refreshIcons();
    return;
  }
  if (!state.results.length) {
    state.selected = "";
    state.selectedPaper = null;
    renderEmptyDetail();
    list.innerHTML = `<div class="empty-state"><i data-lucide="search-x"></i><h2>No results</h2></div>`;
    refreshIcons();
    return;
  }
  const visibleKeys = new Set(state.results.map((paper) => paper.paper_key));
  if (state.selected && !visibleKeys.has(state.selected)) {
    state.selected = "";
    state.selectedPaper = null;
  }
  list.innerHTML = state.results.map((paper) => `
    <button class="paper-row ${paper.paper_key === state.selected ? "active" : ""}" data-key="${escapeHtml(paper.paper_key)}" type="button">
      <div class="paper-main">
        <div class="paper-title">${escapeHtml(paper.title)}</div>
        <div class="paper-meta">${resultMeta(paper)}</div>
      </div>
      <div class="paper-badges">
        ${tagList((paper.tags || []).length ? paper.tags : paper.keyword_hits, "topic", 2)}
      </div>
    </button>
  `).join("") + listFooter();
  list.querySelectorAll(".paper-row").forEach((button) => {
    button.addEventListener("click", () => selectPaper(button.dataset.key));
  });
  const loadMoreButton = $("loadMoreButton");
  if (loadMoreButton) loadMoreButton.addEventListener("click", () => loadMoreResults().catch(renderError));
  if (!state.selected) renderEmptyDetail();
  refreshIcons();
}

async function runSearch(options = {}) {
  const append = Boolean(options.append);
  if (state.loading) return;
  if (!append) {
    resetPaging();
  }
  state.loading = true;
  if (!append) renderResultsFromState();
  const params = {
    q: state.q,
    year: state.year,
    keyword: state.keyword,
    venue: state.venue,
    sort: state.sort,
    limit: String(state.limit),
    offset: String(append ? state.results.length : 0),
  };
  let data;
  try {
    data = await fetchJson(apiUrl("/api/search", params));
  } catch (error) {
    state.loading = false;
    renderResultsFromState();
    throw error;
  }
  const incoming = data.results || [];
  if (append) {
    const seen = new Set(state.results.map((paper) => paper.paper_key));
    state.results = state.results.concat(incoming.filter((paper) => !seen.has(paper.paper_key)));
  } else {
    state.results = incoming;
  }
  state.total = data.total || 0;
  state.hasMore = state.results.length < state.total;
  state.loading = false;
  renderResultsFromState();
  if (!append) $("resultsList").scrollTop = 0;
}

async function loadMoreResults() {
  if (!state.hasMore || state.loading) return;
  await runSearch({append: true});
}

function linkButton(url, label, icon) {
  if (!url) return "";
  return `
    <a class="text-link" href="${escapeHtml(url)}" target="_blank" rel="noreferrer">
      <i data-lucide="${icon}"></i>
      <span>${escapeHtml(label)}</span>
    </a>
  `;
}

function infoRow(label, value) {
  if (!value) return "";
  return `
    <div class="info-row">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value)}</strong>
    </div>
  `;
}

function idTags(ids) {
  const rows = [];
  if (ids?.doi) rows.push(`DOI: ${ids.doi}`);
  if (ids?.arxiv) rows.push(`arXiv: ${ids.arxiv}`);
  if (ids?.acl) rows.push(`ACL: ${ids.acl}`);
  if (ids?.semantic_scholar) rows.push(`Semantic Scholar: ${ids.semantic_scholar}`);
  return rows.length ? rows.map((row) => tag(row, "id")).join("") : tag("No external ID", "muted");
}

function tabButton(name, label, icon) {
  return `
    <button class="tab-button ${state.detailTab === name ? "active" : ""}" data-tab="${name}" type="button">
      <i data-lucide="${icon}"></i>
      <span>${escapeHtml(label)}</span>
    </button>
  `;
}

async function selectPaper(key) {
  if (state.selected === key && state.selectedPaper) {
    state.selected = "";
    state.selectedPaper = null;
    document.querySelectorAll(".paper-row").forEach((item) => item.classList.remove("active"));
    renderEmptyDetail();
    return;
  }
  state.selected = key;
  state.detailTab = "overview";
  document.querySelectorAll(".paper-row").forEach((item) => {
    item.classList.toggle("active", item.dataset.key === key);
  });
  $("detailPane").innerHTML = `<div class="loading-state"><i data-lucide="loader"></i><h2>Loading</h2></div>`;
  refreshIcons();
  state.selectedPaper = await fetchJson(apiUrl(`/api/paper/${encodeURIComponent(key)}`));
  renderDetail();
}

function renderEmptyDetail() {
  $("detailPane").innerHTML = `
    <div class="empty-state">
      <i data-lucide="file-search"></i>
      <h2>Select a paper</h2>
    </div>
  `;
  refreshIcons();
}

function renderDetail() {
  const paper = state.selectedPaper;
  if (!paper) return;
  const fulltextLabel = paper.fulltext?.fulltext_path ? "Fulltext" : paper.fulltext?.pdf_path ? "PDF saved" : "No fulltext";
  $("detailPane").innerHTML = `
    <div class="detail-body">
      <div class="detail-kicker">
        <span>${escapeHtml(paper.year || "N/A")}</span>
        <span>${escapeHtml(paper.venue || "N/A")}</span>
        <span>${escapeHtml(fulltextLabel)}</span>
      </div>
      <h2 class="detail-title">${escapeHtml(paper.title)}</h2>
      <div class="detail-authors">${escapeHtml(paper.authors || "N/A")}</div>

      <div class="detail-tabs">
        ${tabButton("overview", "Overview", "panel-top")}
        ${tabButton("metadata", "Metadata", "database")}
        ${tabButton("fulltext", "Fulltext", "book-open-text")}
      </div>

      <div id="tabPanel"></div>
    </div>
  `;
  document.querySelectorAll(".tab-button").forEach((button) => {
    button.addEventListener("click", () => {
      state.detailTab = button.dataset.tab;
      renderDetail();
    });
  });
  renderTabPanel();
  refreshIcons();
}

function renderTabPanel() {
  const paper = state.selectedPaper;
  if (!paper) return;
  if (state.detailTab === "metadata") {
    renderMetadataTab(paper);
  } else if (state.detailTab === "fulltext") {
    renderFulltextTab(paper);
  } else {
    renderOverviewTab(paper);
  }
}

function renderOverviewTab(paper) {
  $("tabPanel").innerHTML = `
    <section class="tab-panel">
      <p class="abstract">${escapeHtml(paper.abstract || "N/A")}</p>

      <div class="signal-grid">
        <div class="signal-block">
          <h3>Tags</h3>
          <div class="tag-row">${tagList(paper.tags, "topic", 20) || tag("No tag", "muted")}</div>
        </div>
        <div class="signal-block">
          <h3>Sources</h3>
          <div class="tag-row">${tagList(paper.sources, "source", 16) || tag("No source", "muted")}</div>
        </div>
        <div class="signal-block">
          <h3>IDs</h3>
          <div class="tag-row">${idTags(paper.ids)}</div>
        </div>
      </div>

      <div class="detail-section">
        <h3>Links</h3>
        <div class="link-row">
          ${linkButton(paper.urls?.landing, paper.urls?.landing_label || "Publisher", "external-link")}
          ${linkButton(paper.urls?.semantic_scholar, "Semantic Scholar", "network")}
          ${linkButton(paper.urls?.doi, "DOI", "badge-check")}
          ${linkButton(paper.urls?.pdf, "PDF", "file-text")}
          ${linkButton(paper.urls?.code, "Code", "github")}
          ${linkButton(paper.urls?.project, "Project", "box")}
        </div>
      </div>
    </section>
  `;
}

function renderMetadataTab(paper) {
  const sourceRecords = (paper.source_records || []).slice(0, 12).map((record) => {
    const text = [record.source_name, record.source_type, record.query].filter(Boolean).join(" · ");
    return `<li>${escapeHtml(text || "source record")}</li>`;
  }).join("");
  $("tabPanel").innerHTML = `
    <section class="tab-panel">
      <div class="info-grid">
        ${infoRow("Paper key", paper.paper_key)}
        ${infoRow("Year", paper.year)}
        ${infoRow("Venue", paper.venue_raw && paper.venue_raw !== paper.venue ? `${paper.venue} (${paper.venue_raw})` : paper.venue)}
        ${infoRow("Authors", paper.authors)}
        ${infoRow("Citation", paper.max_citation)}
        ${infoRow("DOI", paper.ids?.doi)}
        ${infoRow("arXiv", paper.ids?.arxiv)}
        ${infoRow("ACL", paper.ids?.acl)}
        ${infoRow("Semantic Scholar", paper.ids?.semantic_scholar)}
        ${infoRow("Publisher", paper.urls?.landing)}
        ${infoRow("PDF", paper.urls?.pdf)}
        ${infoRow("Code", paper.urls?.code)}
      </div>

      <div class="detail-section">
        <h3>Raw Topic Signals</h3>
        <div class="tag-row">${tagList([...(paper.tags_raw || paper.tags || []), ...(paper.keyword_hits_raw || paper.keyword_hits || [])], "topic", 32)}</div>
      </div>

      <div class="detail-section">
        <h3>Source Records</h3>
        <ul class="record-list">${sourceRecords || "<li>N/A</li>"}</ul>
      </div>
    </section>
  `;
}

async function renderFulltextTab(paper) {
  const panel = $("tabPanel");
  if (!paper.fulltext?.fulltext_path) {
    panel.innerHTML = `
      <section class="tab-panel">
        <div class="empty-state compact">
          <i data-lucide="file-plus-2"></i>
          <h2>No Markdown fulltext</h2>
          <p>Try ar5iv Markdown first. If it is unavailable, only PDF links will be shown.</p>
          <button class="icon-button primary" id="fetchFulltextButton" type="button">
            <i data-lucide="download"></i>
            <span>Fetch Markdown</span>
          </button>
          <div class="link-row subtle-links">
            ${linkButton(paper.urls?.pdf, "PDF", "file-text")}
          </div>
        </div>
      </section>
    `;
    $("fetchFulltextButton").addEventListener("click", () => runFulltextFetch(paper));
    refreshIcons();
    return;
  }
  panel.innerHTML = `<section class="tab-panel"><div class="loading-state compact"><i data-lucide="loader"></i><h2>Loading</h2></div></section>`;
  refreshIcons();
  const data = await fetchJson(apiUrl(`/api/fulltext/${encodeURIComponent(paper.paper_key)}`));
  panel.innerHTML = `
    <section class="tab-panel">
      <article class="markdown-view">${renderMarkdown(data.markdown, paper)}</article>
    </section>
  `;
}

async function runFulltextFetch(paper) {
  const panel = $("tabPanel");
  panel.innerHTML = `<section class="tab-panel"><div class="loading-state compact"><i data-lucide="loader"></i><h2>Fetching Markdown</h2></div></section>`;
  refreshIcons();
  try {
    const data = await fetchJson(apiUrl(`/api/fulltext-fetch/${encodeURIComponent(paper.paper_key)}`));
    if (data.status === "fetched" || data.status === "exists") {
      state.selectedPaper.fulltext = data;
      await loadSummary();
      await renderFulltextTab(state.selectedPaper);
      return;
    }
    const links = (data.pdf_urls || []).map((url, index) => linkButton(url, `PDF ${index + 1}`, "file-text")).join("");
    panel.innerHTML = `
      <section class="tab-panel">
        <div class="empty-state compact">
          <i data-lucide="file-text"></i>
          <h2>Markdown unavailable</h2>
          <p>${escapeHtml((data.errors || []).join(" | ") || "No Markdown source was found.")}</p>
          <div class="link-row">${links || tag("No PDF link found", "muted")}</div>
        </div>
      </section>
    `;
  } catch (error) {
    panel.innerHTML = `
      <section class="tab-panel">
        <div class="error-state compact">
          <i data-lucide="circle-alert"></i>
          <h2>${escapeHtml(error.message)}</h2>
        </div>
      </section>
    `;
  }
  refreshIcons();
}

function bindEvents() {
  document.querySelectorAll(".nav-item[data-view]").forEach((button) => {
    button.addEventListener("click", () => showView(button.dataset.view));
  });
  $("workspaceSelect").addEventListener("change", async (event) => {
    state.workspace = event.target.value;
    resetFilters();
    await loadWorkspaces();
    await Promise.all([loadSummary(), loadFilters(), loadBuildStatus()]);
    await runSearch();
  });
  $("searchForm").addEventListener("submit", (event) => {
    event.preventDefault();
    state.q = $("searchInput").value.trim();
    resetSelection();
    runSearch().catch(renderError);
  });
  $("yearFilter").addEventListener("change", (event) => {
    state.year = event.target.value;
    resetSelection();
    runSearch().catch(renderError);
  });
  $("keywordFilter").addEventListener("change", (event) => {
    state.keyword = event.target.value;
    resetSelection();
    runSearch().catch(renderError);
  });
  $("venueFilter").addEventListener("change", (event) => {
    state.venue = event.target.value;
    resetSelection();
    runSearch().catch(renderError);
  });
  $("sortMode").addEventListener("change", (event) => {
    state.sort = event.target.value;
    resetSelection();
    runSearch().catch(renderError);
  });
  $("resetButton").addEventListener("click", () => {
    resetFilters();
    $("yearFilter").value = "";
    $("keywordFilter").value = "";
    $("venueFilter").value = "";
    runSearch().catch(renderError);
  });
  $("resultsList").addEventListener("scroll", (event) => {
    const el = event.currentTarget;
    if (state.hasMore && !state.loading && el.scrollTop + el.clientHeight >= el.scrollHeight - 220) {
      loadMoreResults().catch(renderError);
    }
  });
}

function renderError(error) {
  $("detailPane").innerHTML = `<div class="error-state"><i data-lucide="circle-alert"></i><h2>${escapeHtml(error.message)}</h2></div>`;
  refreshIcons();
}

async function boot() {
  bindEvents();
  await loadWorkspaces();
  await Promise.all([loadSummary(), loadFilters(), loadBuildStatus()]);
  await runSearch();
  showView("library");
  refreshIcons();
}

boot().catch(renderError);
