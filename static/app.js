(() => {
  "use strict";

  const state = {
    mode: "file",
    file: null,
    jobId: null,
    report: null,
    highRiskOnly: false,
    readonly: false,
    localEditor: false,
    shareToken: null,
    activeRef: null,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  async function readJson(response) {
    const text = await response.text();
    try {
      return JSON.parse(text);
    } catch {
      throw new Error(`核验服务暂时不可用（HTTP ${response.status}），请刷新页面后重试。`);
    }
  }
  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  function toast(message) {
    const element = $("#toast");
    element.textContent = message;
    element.classList.add("show");
    window.clearTimeout(toast.timer);
    toast.timer = window.setTimeout(() => element.classList.remove("show"), 2400);
  }

  function showError(message) {
    $("#progressSection").hidden = true;
    $("#errorSection").hidden = false;
    $("#errorMessage").textContent = message;
    $("#errorSection").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function setMode(mode) {
    if (state.readonly) return;
    state.mode = mode;
    $$(".mode-tab").forEach((button) => {
      const active = button.dataset.mode === mode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    $("#filePanel").classList.toggle("active", mode === "file");
    $("#pastePanel").classList.toggle("active", mode === "paste");
    $("#errorSection").hidden = true;
  }

  function updateFile(file) {
    state.file = file;
    const row = $("#fileMeta");
    row.hidden = false;
    row.innerHTML = file
      ? `<span>${escapeHtml(file.name)}</span><span>${(file.size / 1024).toFixed(1)} KB</span>`
      : "";
    $("#dropTitle").textContent = file ? "已选择待核验论文" : "拖入论文，或点击选择文件";
    $("#dropHint").textContent = file
      ? "点击可重新选择文件"
      : "文件必须包含独立的“参考文献 / References”分节标题";
  }

  async function submitJob() {
    $("#errorSection").hidden = true;
    if (state.mode === "file" && !state.file) {
      showError("请先上传 PDF、DOCX、TXT 或 Markdown 文件。");
      return;
    }
    if (state.mode === "paste" && !$("#pasteText").value.trim()) {
      showError("请粘贴需要检查的参考文献列表。");
      return;
    }

    const form = new FormData();
    form.append("mode", state.mode);
    if (state.mode === "file") form.append("file", state.file);
    else form.append("text", $("#pasteText").value);

    $("#startButton").disabled = true;
    $("#progressSection").hidden = false;
    $("#progressBar").style.width = "0%";
    $("#progressNumber").textContent = "0";
    $("#progressTitle").textContent = "正在建立核验证据链";
    $("#progressDetail").textContent = "正在读取输入并检查结构。";
    $("#stepList").innerHTML = "";
    $("#progressSection").scrollIntoView({ behavior: "smooth", block: "center" });

    try {
      const response = await fetch("/api/jobs", { method: "POST", body: form });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(payload.detail || "任务创建失败。");
      state.jobId = payload.job_id;
      listenToJob(payload.job_id);
    } catch (error) {
      $("#startButton").disabled = false;
      showError(error.message);
    }
  }

  function listenToJob(jobId) {
    const source = new EventSource(`/api/jobs/${jobId}/events`);
    source.onmessage = async (event) => {
      const update = JSON.parse(event.data);
      $("#progressBar").style.width = `${update.progress}%`;
      $("#progressNumber").textContent = update.progress;
      $("#progressTitle").textContent = update.stage || "正在处理";
      const last = update.steps[update.steps.length - 1];
      $("#progressDetail").textContent = last?.detail || stageDetail(update.stage);
      renderSteps(update.steps);
      if (update.status === "completed") {
        source.close();
        $("#stepList").innerHTML += "";
        await loadReport(jobId);
        $("#startButton").disabled = false;
      } else if (update.status === "failed") {
        source.close();
        $("#startButton").disabled = false;
        showError(update.error || "核验失败，请检查输入。");
      }
    };
    source.onerror = () => {
      source.close();
      $("#startButton").disabled = false;
    };
  }

  function stageDetail(stage) {
    return (
      {
        解析论文文件: "正在提取文字并检查是否存在“参考文献”分节。",
        提取参考文献: "正在识别文献编号、标题、作者、年份和 DOI。",
        映射正文引用: "正在匹配正文标记与参考文献编号。",
        核验题录元数据: "正在查询 Crossref、OpenAlex 和 DataCite。",
        判断正文与证据关系: "没有摘要或全文证据时，将保持“证据不足”。",
        生成结构化报告: "正在整理结论、文献卡、证据矩阵和修订建议。",
      }[stage] || "正在处理。"
    );
  }

  function renderSteps(steps) {
    const canonical = [
      ["parse", "文件解析"],
      ["references", "文献识别"],
      ["citations", "引用映射"],
      ["metadata", "多源核验"],
      ["evidence", "证据判断"],
      ["report", "报告生成"],
    ];
    const byKey = Object.fromEntries(steps.map((step) => [step.key, step]));
    $("#stepList").innerHTML = canonical
      .map(([key, label]) => {
        const step = byKey[key];
        const cls = step?.state === "done" ? "done" : step?.state === "running" ? "running" : "";
        return `<div class="step-item ${cls}"><strong>${label}</strong>${escapeHtml(step?.detail || "")}</div>`;
      })
      .join("");
  }

  async function loadReport(jobId) {
    const response = await fetch(`/api/reports/${jobId}`);
    const payload = await readJson(response);
    if (!response.ok) {
      showError(payload.detail || "报告加载失败。");
      return;
    }
    state.report = payload;
    $("#progressSection").hidden = true;
    renderReport(payload, false);
  }

  function renderReport(report, shared) {
    state.readonly = shared;
    $("#reportSection").hidden = false;
    $("#sharedBanner").hidden = !shared;
    if (shared) {
      $("#sharedBanner").textContent = "这是一份只读分享报告。原文件未包含在分享链接中。";
    }
    $("#reportMeta").textContent = `${report.source_name || "未命名材料"} · ${
      report.mode === "paste" ? "粘贴模式" : report.mode === "shared" ? "分享报告" : "文件模式"
    } · ${new Date(report.created_at).toLocaleString("zh-CN")}`;
    $("#headline").textContent = report.narrative.headline;
    $("#overview").textContent = report.narrative.overview;

    const sources = [...new Set(report.references.flatMap((ref) => ref.sources.map((source) => source.source)))];
    $("#sourceMix").innerHTML = sources.length
      ? sources.map((source) => `<span>${escapeHtml(source)}</span>`).join("")
      : "<span>规则检查</span>";

    const summary = report.summary;
    const cards = [
      ["文献总数", summary.total, "本次识别条目", ""],
      ["已证实", summary.verified, "题录匹配成功", "good"],
      ["信息冲突", summary.conflict, "需优先核对", summary.conflict ? "risk" : ""],
      ["已撤稿", summary.retracted || 0, "需核查撤稿声明", summary.retracted ? "risk" : ""],
      ["暂未检出", summary.not_detected, "不等于不存在", ""],
      ["证据不足", summary.insufficient_evidence, "不能仅凭标题判断", summary.insufficient_evidence ? "risk" : ""],
    ];
    $("#summaryCards").innerHTML = cards
      .map(
        ([label, value, note, cls]) => `
          <div class="summary-card ${cls}">
            <span>${label}</span><strong>${value}</strong><small>${note}</small>
          </div>`
      )
      .join("");

    const segments = [
      ["已证实", summary.verified, "var(--green)"],
      ["可能匹配", summary.possible_match, "var(--amber)"],
      ["信息冲突", summary.conflict, "var(--red)"],
      ["暂未检出", summary.not_detected, "var(--slate)"],
      ["覆盖不足", summary.coverage_insufficient, "#7d7062"],
    ];
    const total = Math.max(1, summary.total);
    $("#statusBar").innerHTML = segments
      .filter(([, count]) => count > 0)
      .map(([, count, color]) => `<span class="status-segment" style="width:${(count / total) * 100}%;background:${color}"></span>`)
      .join("");
    $("#statusLegend").innerHTML = segments
      .map(([label, count, color]) => `<span style="--c:${color}">${label} ${count}</span>`)
      .join("");

    $("#issueCount").textContent = `${summary.high_risk} 个高风险 · ${summary.medium_risk} 个中风险`;
    renderIssues(report.issues);
    renderReferences();
    renderEvidence(report.evidence_matrix);
    renderClaims(report.citations);
    renderQuality(report);
    renderRevisions(report.revisions);
    $("#boundaryList").innerHTML = report.boundaries.map((item) => `<li>${escapeHtml(item)}</li>`).join("");

    $$("[data-export]").forEach((link) => {
      link.hidden = shared;
      link.href = `/api/reports/${report.id}/export/${link.dataset.export}`;
    });
    $("#shareButton").hidden = shared;
    $("#shareNavButton").disabled = false;
    setReportTab("overview");
    $("#reportSection").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderIssues(issues) {
    $("#issueList").innerHTML = issues.length
      ? issues
          .map(
            (issue) => `
          <article class="issue-item">
            <span class="severity ${issue.severity}">${escapeHtml(issue.severity_label)}</span>
            <div><p><strong>${escapeHtml(issue.type)}</strong>　${escapeHtml(issue.message)}</p>
            <small>建议：${escapeHtml(issue.action)}</small></div>
          </article>`
          )
          .join("")
      : `<div class="empty-state">没有识别到结构性风险。仍需人工核验全文语义。</div>`;
  }

  function renderReferences() {
    const report = state.report;
    if (!report) return;
    const search = $("#referenceSearch").value.trim().toLowerCase();
    const filtered = report.references.filter((ref) => {
      const haystack = `${ref.title} ${ref.authors} ${ref.doi}`.toLowerCase();
      return (!search || haystack.includes(search)) && (!state.highRiskOnly || ref.risk_level === "high");
    });
    $("#referenceList").innerHTML = filtered.length
      ? filtered
          .map((ref) => {
            const diffText = ref.field_diffs.length
              ? `字段差异：${ref.field_diffs.map((item) => item.field).join("、")}`
              : "未发现可自动比对的字段差异";
            const evidence = ref.evidence
              ? `<div class="ref-evidence">${escapeHtml(ref.evidence.slice(0, 700))}${
                  ref.evidence_url
                    ? `<br><a href="${escapeHtml(ref.evidence_url)}" target="_blank" rel="noreferrer">查看来源记录</a>`
                    : ""
                }</div>`
              : `<div class="ref-evidence empty-state">没有可用摘要或全文证据。</div>`;
            const supplement =
              state.readonly || !state.localEditor
                ? ""
                : `<button class="btn btn-ghost" type="button" data-supplement="${ref.ref_no}">补充 DOI</button>`;
            return `
              <details class="reference-card">
                <summary>
                  <span class="ref-index">${ref.ref_no}</span>
                  <span class="ref-title"><strong>${escapeHtml(ref.title || "未提供标题")}</strong>
                  <small>${escapeHtml(ref.authors || "作者未提供")} · ${escapeHtml(ref.year || "年份未提供")} · ${escapeHtml(ref.doi || "无 DOI")}</small></span>
                  <span class="ref-badges">
                    ${ref.retracted ? '<span class="status-pill status-conflict" title="公开来源标记为已撤稿">已撤稿</span>' : ""}
                    <span class="status-pill status-${ref.verification_status}">${escapeHtml(ref.status_label)}</span>
                  </span>
                </summary>
                <div class="ref-body">
                  <div class="ref-grid">
                    <div class="ref-field"><span>风险说明</span><b>${escapeHtml(ref.risk_reason)}</b></div>
                    <div class="ref-field"><span>来源覆盖</span><b>${escapeHtml(ref.coverage)}</b></div>
                    <div class="ref-field"><span>字段关系</span><b>${escapeHtml(diffText)}</b></div>
                    ${
                      ref.retracted
                        ? `<div class="ref-field"><span>撤稿状态</span><b>${escapeHtml(
                            ref.retraction_note || "公开来源标记为已撤稿"
                          )}</b></div>`
                        : ""
                    }
                  </div>
                  ${evidence}
                  <p><strong>修复建议：</strong>${escapeHtml(ref.repair_suggestion || "暂无")}</p>
                  <div class="ref-actions">${supplement}</div>
                </div>
              </details>`;
          })
          .join("")
      : `<div class="empty-state">没有符合当前筛选条件的文献。</div>`;

    $$("[data-supplement]").forEach((button) => {
      button.addEventListener("click", () => openSupplement(Number(button.dataset.supplement)));
    });
  }

  function renderEvidence(rows) {
    if (!rows.length) {
      $("#evidenceTableBody").innerHTML = `<tr><td colspan="6">没有可展示的来源响应。</td></tr>`;
      return;
    }
    $("#evidenceTableBody").innerHTML = rows
      .map(
        (row) => `
        <tr>
          <td>${row.ref_no}</td>
          <td>${escapeHtml(row.source)}</td>
          <td><span class="status-pill ${row.state === "成功" ? "status-verified" : "status-not_detected"}">${escapeHtml(row.state)}</span></td>
          <td>${escapeHtml(row.title || "—")}</td>
          <td>${escapeHtml(row.year || "—")}</td>
          <td>${escapeHtml(row.message || "已返回记录")}${row.url ? ` · <a href="${escapeHtml(row.url)}" target="_blank" rel="noreferrer">打开</a>` : ""}</td>
        </tr>`
      )
      .join("");
  }

  function renderClaims(citations) {
    $("#claimList").innerHTML = citations.length
      ? citations
          .map(
            (citation) => `
        <article class="claim-card">
          <div class="claim-top">
            <span class="claim-marker">${escapeHtml(citation.marker)}</span>
            <span class="status-pill ${supportClass(citation.support_status)}">${escapeHtml(citation.support_label)}</span>
            <small>${escapeHtml(citation.ref_title || "目标文献未匹配")}</small>
          </div>
          <h4>正文主张</h4><p>${escapeHtml(citation.claim)}</p>
          <div class="claim-evidence ${citation.evidence_quote ? "" : "empty"}">
            <strong>${citation.evidence_quote ? "证据原文" : "证据状态"}</strong><br>
            ${escapeHtml(citation.evidence_quote || "没有获得可用于判断的摘要或全文证据。")}
          </div>
          <p class="claim-source">${escapeHtml(citation.reason)}</p>
        </article>`
          )
          .join("")
      : `<div class="empty-state">当前为粘贴模式，未提供正文，因此不进行语义支持度判断。</div>`;
  }

  function renderQuality(report) {
    const quality = report.quality || {};
    const metrics = quality.metrics || {};
    const risk = quality.risk_counts || { high: 0, medium: 0, low: 0 };
    const riskActual = risk.high + risk.medium + risk.low + (risk.unverified || 0);
    const riskTotal = Math.max(1, riskActual);
    const highStop = (risk.high / riskTotal) * 100;
    const mediumStop = highStop + (risk.medium / riskTotal) * 100;
    const lowStop = mediumStop + (risk.low / riskTotal) * 100;
    const metricCards = [
      ["DOI 覆盖", metrics.doi_coverage, "标识符可核验比例"],
      ["摘要证据覆盖", metrics.evidence_coverage, "可进入语义判断比例"],
      ["正文映射", metrics.citation_mapping, "正文引用成功映射比例"],
      ["数据源响应", metrics.source_success, "成功响应占全部请求"],
    ]
      .map(
        ([label, value, note]) => `
          <div class="quality-metric">
            <span>${label}</span>
            <strong>${value == null ? "—" : `${value}%`}</strong>
            <div class="metric-track"><i style="width:${value || 0}%"></i></div>
            <span>${note}</span>
          </div>`
      )
      .join("");

    const sourceCoverage = Object.entries(quality.source_coverage || {})
      .map(([source, stats]) => {
        const percent = Math.round((stats.ok / Math.max(1, stats.total)) * 100);
        return `
          <div class="source-bar">
            <span>${escapeHtml(source)}</span>
            <div><i style="width:${percent}%"></i></div>
            <strong>${percent}%</strong>
          </div>`;
      })
      .join("");

    const fields = ["标题", "作者", "年份", "期刊", "DOI", "摘要证据"];
    const heatmap = (quality.field_heatmap || [])
      .map(
        (row) => `
          <tr>
            <td title="${escapeHtml(row.title || "未提供标题")}">${row.ref_no}. ${escapeHtml(row.title || "未提供标题")}</td>
            ${fields
              .map((field) => `<td class="${row.fields[field] ? "filled" : ""}">${row.fields[field] ? "有" : "缺"}</td>`)
              .join("")}
          </tr>`
      )
      .join("");

    $("#qualityDashboard").innerHTML = `
      <section class="quality-card">
        <h4>风险画像</h4>
        <div class="risk-profile">
          <div class="risk-donut" style="background:conic-gradient(var(--red) 0 ${highStop}%,var(--amber) ${highStop}% ${mediumStop}%,var(--green) ${mediumStop}% ${lowStop}%,#9a9184 ${lowStop}% 100%)">
            <span>${riskActual}<small>文献条目</small></span>
          </div>
          <div class="risk-legend">
            <span><b><i style="--c:var(--red)"></i>高风险</b><strong>${risk.high}</strong></span>
            <span><b><i style="--c:var(--amber)"></i>中风险</b><strong>${risk.medium}</strong></span>
            <span><b><i style="--c:var(--green)"></i>低风险</b><strong>${risk.low}</strong></span>
            <span><b><i style="--c:#9a9184"></i>待授权库核验</b><strong>${risk.unverified || 0}</strong></span>
          </div>
        </div>
      </section>
      <section class="quality-card">
        <h4>处理质量指标</h4>
        <div class="quality-metrics">${metricCards}</div>
      </section>
      <section class="quality-card quality-wide">
        <h4>字段完整度热力图</h4>
        <div class="heatmap-wrap">
          <table class="field-heatmap">
            <thead><tr><th>文献</th>${fields.map((field) => `<th>${field}</th>`).join("")}</tr></thead>
            <tbody>${heatmap || `<tr><td colspan="7">暂无可展示字段。</td></tr>`}</tbody>
          </table>
        </div>
      </section>
      <section class="quality-card quality-wide">
        <h4>数据源覆盖</h4>
        <div class="source-bars">${sourceCoverage || "<div class='empty-state'>本次没有取得外部数据源记录。</div>"}</div>
      </section>`;
  }

  function supportClass(status) {
    return {
      supported: "status-verified",
      partially_supported: "status-possible_match",
      not_supported: "status-conflict",
      insufficient_evidence: "status-coverage_insufficient",
      not_checked: "status-not_detected",
    }[status] || "status-not_detected";
  }

  function renderRevisions(revisions) {
    $("#revisionList").innerHTML = revisions.length
      ? revisions
          .map(
            (revision) => `
          <article class="revision-card">
            <div class="revision-side"><span>原文 · 第 ${revision.ref_no} 条</span>${escapeHtml(revision.original)}</div>
            <div class="revision-side"><span>建议动作</span>${escapeHtml(revision.suggested_action)}</div>
          </article>`
          )
          .join("")
      : `<div class="empty-state">当前没有需要展示的字段修订。</div>`;
  }

  function setReportTab(tab) {
    $$(".report-tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    $$(".report-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tab}`));
  }

  function openSupplement(refNo) {
    const reference = state.report.references.find((item) => item.ref_no === refNo);
    if (!reference) return;
    state.activeRef = refNo;
    $("#supplementTitle").textContent = `第 ${refNo} 条 · ${reference.title || "未提供标题"}`;
    $("#doiInput").value = reference.doi || "";
    $("#supplementDialog").showModal();
  }

  async function submitSupplement() {
    if (!state.activeRef || !state.jobId) return;
    const doi = $("#doiInput").value.trim();
    if (!doi) {
      toast("请输入 DOI。");
      return;
    }
    $("#supplementSubmit").disabled = true;
    try {
      const response = await fetch(`/api/reports/${state.jobId}/supplement`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ref_no: state.activeRef, doi }),
      });
      const payload = await readJson(response);
      if (!response.ok) throw new Error(payload.detail || "补证失败。");
      state.report = payload;
      $("#supplementDialog").close();
      renderReport(payload, false);
      toast("DOI 已补充，相关证据已更新。");
    } catch (error) {
      toast(error.message);
    } finally {
      $("#supplementSubmit").disabled = false;
    }
  }

  async function shareReport() {
    if (!state.jobId) {
      await shareApplication();
      return;
    }
    const response = await fetch("/api/share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: state.jobId, redacted: true }),
    });
    const payload = await readJson(response);
    if (!response.ok) {
      toast(payload.detail || "分享链接生成失败。");
      return;
    }
    state.shareToken = payload.token;
    $("#shareDialogTitle").textContent = "分享脱敏核验结果";
    $("#shareDialogContext").textContent = "链接只包含本次核验结果，不包含原文件；默认隐藏论文来源名称。";
    $("#shareQr").src = payload.qr_url;
    $("#shareLink").value = payload.url;
    $("#shareExpiry").textContent = `链接将在约 ${Math.round(payload.expires_in_seconds / 3600)} 小时后失效。`;
    $("#shareDialog").showModal();
  }

  async function shareApplication() {
    try {
      const response = await fetch("/api/app-share");
      const payload = await readJson(response);
      if (!response.ok) throw new Error(payload.detail || "公网入口尚未建立。");
      $("#shareDialogTitle").textContent = "转发悟真鉴引";
      $("#shareDialogContext").textContent = "这是完整应用入口。对方可上传自己的论文或粘贴文献列表，无需安装软件。";
      $("#shareQr").src = payload.qr_url;
      $("#shareLink").value = payload.url;
      $("#shareExpiry").textContent = payload.persistent
        ? "这是云端长期入口，不依赖你的电脑保持开机。"
        : "临时公网地址依赖运行 share.ps1 的电脑和窗口，关闭后失效。";
      $("#shareDialog").showModal();
    } catch (error) {
      toast(error.message);
    }
  }

  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      const area = document.createElement("textarea");
      area.value = value;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
  }

  function loadExample() {
    setMode("paste");
    $("#pasteText").value = [
      "[1] He, K., Zhang, X., Ren, S., & Sun, J. Deep Residual Learning for Image Recognition. 2016. DOI: 10.1109/CVPR.2016.90.",
      "[2] He, K., Zhang, X., Ren, S., & Sun, J. Deep Residual Learning for Image Recognition. 2016. DOI: 10.1109/CVPR.2016.90.",
      "[3] Smith, J. A fabricated citation for testing. 2020. DOI: 10.9999/notreal.",
    ].join("\n");
    $("#workbench").scrollIntoView({ behavior: "smooth" });
  }

  async function loadSharedReport() {
    const match = window.location.pathname.match(/^\/share\/([^/]+)/);
    if (!match) return false;
    state.readonly = true;
    try {
      const response = await fetch(`/api/share/${match[1]}`);
      const payload = await readJson(response);
      if (!response.ok) throw new Error(payload.detail || "分享链接已失效。");
      state.report = payload;
      $(".hero").hidden = true;
      $("#workbench").hidden = true;
      renderReport(payload, true);
      return true;
    } catch (error) {
      showError(error.message);
      return true;
    }
  }

  function bindEvents() {
    $$(".mode-tab").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
    $("#paperFile").addEventListener("change", (event) => updateFile(event.target.files[0] || null));
    $("#startButton").addEventListener("click", submitJob);
    $("#loadExampleButton").addEventListener("click", loadExample);
    $("#retryButton").addEventListener("click", () => $("#workbench").scrollIntoView({ behavior: "smooth" }));
    $("#copyButton").addEventListener("click", () => {
      if (!state.report) return;
      copyText(state.report.markdown || JSON.stringify(state.report, null, 2));
      toast("核验报告已复制。");
    });
    $("#shareButton").addEventListener("click", shareReport);
    $("#shareNavButton").addEventListener("click", shareApplication);
    $("#printButton").addEventListener("click", () => window.print());
    $("#exportToggle").addEventListener("click", () => $(".export-menu").classList.toggle("open"));
    $("#referenceSearch").addEventListener("input", renderReferences);
    $("#highRiskFilter").addEventListener("click", () => {
      state.highRiskOnly = !state.highRiskOnly;
      $("#highRiskFilter").classList.toggle("active", state.highRiskOnly);
      renderReferences();
    });
    $$(".report-tab").forEach((button) => button.addEventListener("click", () => setReportTab(button.dataset.tab)));
    $$("[data-close-dialog]").forEach((button) =>
      button.addEventListener("click", () => button.closest("dialog").close())
    );
    $("#supplementSubmit").addEventListener("click", submitSupplement);
    $("#copyShareLink").addEventListener("click", async () => {
      await copyText($("#shareLink").value);
      toast("分享链接已复制。");
    });

    const zone = $("#dropZone");
    ["dragenter", "dragover"].forEach((eventName) =>
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.add("dragging");
      })
    );
    ["dragleave", "drop"].forEach((eventName) =>
      zone.addEventListener(eventName, (event) => {
        event.preventDefault();
        zone.classList.remove("dragging");
      })
    );
    zone.addEventListener("drop", (event) => {
      const file = event.dataTransfer.files[0];
      if (!file) return;
      const transfer = new DataTransfer();
      transfer.items.add(file);
      $("#paperFile").files = transfer.files;
      updateFile(file);
    });
  }

  async function boot() {
    bindEvents();
    const shared = await loadSharedReport();
    if (shared) return;
    try {
      const response = await fetch("/api/health");
      const health = await readJson(response);
      state.localEditor = Boolean(health.local_editor);
      const modelLabel = health.llm_enabled
        ? `模型：${health.llm_model}`
        : "规则辅助模式";
      $("#modelBadge").textContent = health.local_editor
        ? `${modelLabel} · 本机增强`
        : `${modelLabel} · 免登录公开源`;
      $("#shareNavButton").disabled = false;
    } catch {
      $("#modelBadge").textContent = "服务连接异常";
    }
  }

  boot();
})();

