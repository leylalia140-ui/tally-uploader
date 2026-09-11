// Shared engine for all self-hosted upload-form pages (VA / German model /
// Spanish model). Handles: multi-file selection UI, resumable upload direct
// to Google Drive (bytes never touch our server), and the submit/retry flow.
//
// Page-specific markup (model field, content-type options, niche blocks for
// the VA form) stays in each page's own inline script; this file only knows
// about the generic file-list + upload mechanics.

function initUploadForm(opts) {
  const {
    apiBase,               // e.g. "/api/va-upload" or "/api/model-upload"
    getPayloadFields,      // () => { model, content_type, niche, va_name } — niche/va_name optional
    isValid,               // () => boolean — page-specific required-field check (excluding "has files")
    successText,
    errorPrefix,
    errorSuffix,
  } = opts;

  const submitBtn = document.getElementById("submit");
  const statusEl = document.getElementById("status");
  const dropEl = document.getElementById("drop");
  const fileInputEl = document.getElementById("file-input");
  const fileListEl = document.getElementById("file-list");

  let selectedFiles = [];
  let completedIndices = new Set();
  // Fixed once the first file's /init call returns, then reused for every
  // other file AND for every retry — so a whole batch (including a retry
  // after a partial failure) always lands in the same dated folder, even if
  // a retry happens to straddle midnight.
  const dateStrRef = { value: null };

  function addFiles(fileList) {
    selectedFiles.push(...Array.from(fileList));
    renderFileList();
    validate();
  }

  function removeFile(index) {
    if (completedIndices.has(index)) return; // safety net, button is disabled anyway
    selectedFiles.splice(index, 1);
    completedIndices = new Set(Array.from(completedIndices, i => (i > index ? i - 1 : i)));
    renderFileList();
    validate();
  }

  function renderFileList() {
    fileListEl.innerHTML = selectedFiles.map((f, i) => {
      const done = completedIndices.has(i);
      return `
      <div class="file-row" data-index="${i}">
        <div class="row-top">
          <span class="fname">${done ? "✅ " : ""}${f.name} <span style="color:#999;font-weight:400">(${(f.size / 1024 / 1024).toFixed(1)} MB)</span></span>
          <button type="button" class="remove" data-remove="${i}" ${done ? "disabled" : ""}>✕</button>
        </div>
        <div class="bar-track" style="${done ? "display:block" : ""}"><div class="bar-fill" style="${done ? "width:100%" : ""}"></div></div>
        <div class="bar-label" style="${done ? "display:block" : ""}">${done ? "100%" : ""}</div>
      </div>
    `;
    }).join("");
    fileListEl.querySelectorAll("[data-remove]").forEach(btn => {
      btn.addEventListener("click", () => removeFile(parseInt(btn.dataset.remove, 10)));
    });
  }

  function validate() {
    const ok = isValid() && selectedFiles.length > 0;
    submitBtn.disabled = !ok;
    return ok;
  }

  fileInputEl.addEventListener("change", () => {
    if (fileInputEl.files.length) addFiles(fileInputEl.files);
    fileInputEl.value = ""; // allow picking more on top of the existing selection
  });
  dropEl.addEventListener("dragover", e => { e.preventDefault(); dropEl.classList.add("dragover"); });
  dropEl.addEventListener("dragleave", () => dropEl.classList.remove("dragover"));
  dropEl.addEventListener("drop", e => {
    e.preventDefault();
    dropEl.classList.remove("dragover");
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });

  // ── Resumable upload to Google Drive (bytes go straight from this browser
  // to Google — never through our own server) ──────────────────────────────
  async function uploadResumable(uploadUrl, file, onProgress) {
    const chunkSize = 8 * 1024 * 1024; // 8 MiB
    let offset = 0;
    while (offset < file.size) {
      const end = Math.min(offset + chunkSize, file.size);
      const isFinalChunk = end === file.size;
      let attempt = 0;
      let advanced = false;
      while (!advanced) {
        try {
          const resp = await fetch(uploadUrl, {
            method: "PUT",
            headers: { "Content-Range": `bytes ${offset}-${end - 1}/${file.size}` },
            body: file.slice(offset, end),
          });
          if (resp.status === 200 || resp.status === 201) {
            onProgress(file.size, file.size);
            return await resp.json();
          }
          if (resp.status === 308) {
            onProgress(end, file.size);
            offset = end;
            advanced = true;
            break;
          }
          throw new Error("Unexpected status " + resp.status);
        } catch (err) {
          if (isFinalChunk) {
            // Google's resumable-upload completion response is missing CORS
            // headers for third-party browser origins (verified server-side:
            // the file is actually created even though the browser can't read
            // this particular response) — the upload itself succeeded, we
            // just can't read the file ID here. The backend looks the file
            // up by name in the destination folder instead.
            onProgress(file.size, file.size);
            return null;
          }
          attempt++;
          if (attempt > 6) throw err;
          try {
            const rangeResp = await fetch(uploadUrl, {
              method: "PUT",
              headers: { "Content-Range": `bytes */${file.size}` },
            });
            const range = rangeResp.headers.get("Range");
            if (range) offset = parseInt(range.split("-")[1], 10) + 1;
          } catch (e2) { /* network still down, just retry current offset */ }
          await new Promise(r => setTimeout(r, 1000 * attempt));
        }
      }
    }
  }

  async function uploadOneFile(file, rowEl, payload) {
    const bar = rowEl.querySelector(".bar-track");
    const fill = rowEl.querySelector(".bar-fill");
    const barLabel = rowEl.querySelector(".bar-label");
    bar.style.display = "block";
    barLabel.style.display = "block";

    const initResp = await fetch(`${apiBase}/init`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: payload.model, content_type: payload.content_type,
        file_name: file.name, mime_type: file.type || "application/octet-stream",
        date_str: dateStrRef.value,
      }),
    });
    if (!initResp.ok) throw new Error("init failed: " + await initResp.text());
    const initData = await initResp.json();
    dateStrRef.value = initData.date_str;

    const result = await uploadResumable(initData.upload_url, file, (done, total) => {
      const pct = Math.round((done / total) * 100);
      fill.style.width = pct + "%";
      barLabel.textContent = `${pct}% (${(done / 1024 / 1024).toFixed(1)} / ${(total / 1024 / 1024).toFixed(1)} MB)`;
    });

    await fetch(`${apiBase}/file-done`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: payload.model, content_type: payload.content_type,
        niche: payload.niche || "", va_name: payload.va_name || "",
        file_name: file.name, mime_type: file.type || "application/octet-stream",
        drive_file_id: result ? result.id : null,
        date_str: dateStrRef.value,
      }),
    });
  }

  submitBtn.addEventListener("click", async () => {
    if (!validate()) return;
    submitBtn.disabled = true;
    statusEl.className = "";
    statusEl.textContent = "Uploading...";

    const payload = getPayloadFields();

    try {
      for (let i = 0; i < selectedFiles.length; i++) {
        if (completedIndices.has(i)) continue; // already uploaded in a previous attempt
        const rowEl = fileListEl.querySelector(`.file-row[data-index="${i}"]`);
        await uploadOneFile(selectedFiles[i], rowEl, payload);
        completedIndices.add(i);
      }
      await fetch(`${apiBase}/finalize`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: payload.model, content_type: payload.content_type, date_str: dateStrRef.value }),
      });
      statusEl.className = "ok";
      statusEl.textContent = successText;
      selectedFiles = [];
      completedIndices = new Set();
      dateStrRef.value = null;
      renderFileList();
    } catch (err) {
      statusEl.className = "error";
      statusEl.textContent = errorPrefix + err.message + errorSuffix;
      renderFileList();
      submitBtn.disabled = false;
    }
  });

  return { validate };
}
