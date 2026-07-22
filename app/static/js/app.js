(function () {
  var ACTIVE_ANALYSIS_STATUSES = {
    pending: true,
    ocr_processing: true,
    ocr_done: true,
    ai_processing: true,
  };

  var DOCUMENT_POLL_INTERVAL_MS = 2500;
  var DETAIL_POLL_INTERVAL_MS = 2500;

  function parseJsonResponse(xhr) {
    if (xhr.response && typeof xhr.response === "object") {
      return xhr.response;
    }

    if (!xhr.responseText) {
      return null;
    }

    try {
      return JSON.parse(xhr.responseText);
    } catch (error) {
      return null;
    }
  }

  function htmlToElement(html) {
    var template = document.createElement("template");
    template.innerHTML = (html || "").trim();
    return template.content.firstElementChild;
  }

  function flashUpdatedElement(element) {
    if (!element) {
      return;
    }

    element.classList.add("live-swap-flash");
    window.setTimeout(function () {
      element.classList.remove("live-swap-flash");
    }, 900);
  }

  function updateUploadFilename(widget, input) {
    var target = widget.querySelector("[data-upload-filename]");
    if (!target) {
      return;
    }

    var file = input && input.files && input.files[0];
    target.textContent = file ? file.name : "Noch keine Datei ausgewählt.";
  }

  function toggleDropzone(widget, active) {
    var form = widget.querySelector("[data-upload-form]");
    var icon = widget.querySelector("[data-upload-icon]");

    if (form) {
      form.classList.toggle("upload-dropzone-active", !!active);
    }

    if (icon) {
      icon.classList.toggle("scale-110", !!active);
    }
  }

  function setUploadError(widget, message) {
    var error = widget.querySelector("[data-upload-error]");
    if (!error) {
      return;
    }

    var label = error.querySelector("span") || error;
    label.textContent = message || "";
    error.classList.toggle("hidden", !message);
  }

  function setUploadRuntime(widget, state) {
    var runtime = widget.querySelector("[data-upload-runtime]");
    var stage = widget.querySelector("[data-upload-stage]");
    var copy = widget.querySelector("[data-upload-copy]");
    var bar = widget.querySelector("[data-upload-progress-bar]");
    var percent = widget.querySelector("[data-upload-percent]");
    var submit = widget.querySelector("[data-upload-submit]");
    var value = Math.max(0, Math.min(Number(state.percent || 0), 100));

    if (!runtime || !stage || !copy || !bar || !percent) {
      return;
    }

    runtime.classList.toggle("hidden", !state.visible);
    stage.textContent = state.stage || "Bereit";
    copy.textContent = state.copy || "";
    percent.textContent = value + "%";
    bar.style.width = value + "%";
    runtime.classList.toggle("is-complete", !!state.complete);
    runtime.classList.toggle("is-error", !!state.error);

    if (submit) {
      submit.disabled = !!state.disabled;
    }
  }

  function resetUploadWidget(widget) {
    var input = widget.querySelector("[data-upload-input]");
    var form = widget.querySelector("[data-upload-form]");

    if (input) {
      input.value = "";
      updateUploadFilename(widget, input);
    }

    if (form) {
      form.classList.remove("upload-dropzone-active");
    }
  }

  function updateDocumentsSummary(html) {
    var target = document.getElementById("document-summary-cards");
    if (!target || !html) {
      return;
    }

    target.innerHTML = html;
  }

  function ensureDocumentListVisible() {
    var list = document.getElementById("document-list");
    var shell = document.getElementById("document-list-shell");
    var empty = document.getElementById("document-empty-state");

    if (list) {
      list.classList.remove("hidden");
    }

    if (shell) {
      shell.classList.remove("hidden");
    }

    if (empty) {
      empty.remove();
    }
  }

  function applyDocumentFilters() {
    var page = document.querySelector("[data-documents-page]");
    var searchInput;
    var filterInput;
    var emptyState;
    var rows;
    var visibleCount = 0;
    var searchValue;
    var filterValue;

    if (!page) {
      return;
    }

    searchInput = page.querySelector("[data-document-search]");
    filterInput = page.querySelector("[data-document-filter]");
    emptyState = document.getElementById("document-filter-empty");
    rows = page.querySelectorAll("[data-document-row]");
    searchValue = ((searchInput && searchInput.value) || "").trim().toLocaleLowerCase();
    filterValue = ((filterInput && filterInput.value) || "").trim();

    Array.prototype.forEach.call(rows, function (row) {
      var haystack = (row.textContent || "").toLocaleLowerCase();
      var matchesSearch = !searchValue || haystack.indexOf(searchValue) !== -1;
      var matchesFilter = !filterValue || row.dataset.documentStatus === filterValue;
      var visible = matchesSearch && matchesFilter;

      row.classList.toggle("hidden", !visible);
      if (visible) {
        visibleCount += 1;
      }
    });

    if (emptyState) {
      emptyState.classList.toggle("hidden", !(rows.length > 0 && visibleCount === 0));
    }
  }

  function upsertDocumentRow(html) {
    var list = document.getElementById("document-list");
    var next = htmlToElement(html);
    var current;

    if (!list || !next || !next.id) {
      return;
    }

    ensureDocumentListVisible();
    current = document.getElementById(next.id);

    if (current) {
      current.replaceWith(next);
    } else {
      list.prepend(next);
    }

    flashUpdatedElement(next);
    applyDocumentFilters();
  }

  function initDocumentToolbar(root) {
    var page = (root || document).querySelector("[data-documents-page]");
    var searchInput;
    var filterInput;

    if (!page || page.dataset.documentToolbarBound === "1") {
      applyDocumentFilters();
      return;
    }

    searchInput = page.querySelector("[data-document-search]");
    filterInput = page.querySelector("[data-document-filter]");

    if (searchInput) {
      searchInput.addEventListener("input", applyDocumentFilters);
    }

    if (filterInput) {
      filterInput.addEventListener("change", applyDocumentFilters);
    }

    page.dataset.documentToolbarBound = "1";
    applyDocumentFilters();
  }

  function requestJson(url, onSuccess) {
    return fetch(url, {
      headers: {
        Accept: "application/json",
        "X-Requested-With": "XMLHttpRequest",
      },
      credentials: "same-origin",
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("Request failed: " + response.status);
      }
      return response.json();
    }).then(onSuccess);
  }

  function scheduleDocumentsPoll(page) {
    if (!page) {
      return;
    }

    window.clearTimeout(page._documentsPollTimer);
    page._documentsPollTimer = window.setTimeout(function () {
      pollDocumentsPage(page);
    }, DOCUMENT_POLL_INTERVAL_MS);
  }

  function pollDocumentsPage(page) {
    if (!page || page.dataset.documentsPolling === "busy") {
      return;
    }

    if (document.hidden) {
      scheduleDocumentsPoll(page);
      return;
    }

    var rows = page.querySelectorAll("[data-document-row][data-document-active='1']");
    var ids = Array.prototype.map.call(rows, function (row) {
      return row.dataset.documentId;
    }).filter(Boolean);

    if (ids.length === 0) {
      page.dataset.documentsPolling = "idle";
      return;
    }

    page.dataset.documentsPolling = "busy";
    requestJson(page.dataset.liveUrl + "?ids=" + encodeURIComponent(ids.join(",")), function (data) {
      updateDocumentsSummary(data.summary_html);
      (data.rows || []).forEach(function (row) {
        upsertDocumentRow(row.html);
      });
      page.dataset.documentsPolling = "idle";
      if (data.active_ids && data.active_ids.length > 0) {
        scheduleDocumentsPoll(page);
      }
    }).catch(function () {
      page.dataset.documentsPolling = "idle";
      scheduleDocumentsPoll(page);
    });
  }

  function startDocumentsPolling() {
    var page = document.querySelector("[data-documents-page]");
    if (!page) {
      return;
    }

    pollDocumentsPage(page);
  }

  function scheduleDetailPoll(page) {
    if (!page) {
      return;
    }

    window.clearTimeout(page._detailPollTimer);
    page._detailPollTimer = window.setTimeout(function () {
      pollDetailPage(page);
    }, DETAIL_POLL_INTERVAL_MS);
  }

  function pollDetailPage(page) {
    if (!page || page.dataset.detailPolling === "busy") {
      return;
    }

    if (!ACTIVE_ANALYSIS_STATUSES[page.dataset.documentStatus]) {
      page.dataset.detailPolling = "idle";
      return;
    }

    if (document.hidden) {
      scheduleDetailPoll(page);
      return;
    }

    page.dataset.detailPolling = "busy";
    requestJson(page.dataset.liveUrl, function (data) {
      var analysis = document.getElementById("document-analysis-panel");
      var history = document.getElementById("document-history-panel");
      var comparison = document.getElementById("document-comparison-panel");

      if (analysis && data.analysis_html) {
        analysis.innerHTML = data.analysis_html;
      }

      if (history && data.history_html) {
        history.innerHTML = data.history_html;
      }

      if (comparison) {
        comparison.innerHTML = data.comparison_html || "";
      }

      page.dataset.documentStatus = data.status;
      page.dataset.detailPolling = "idle";
      if (data.active) {
        scheduleDetailPoll(page);
      }
    }).catch(function () {
      page.dataset.detailPolling = "idle";
      scheduleDetailPoll(page);
    });
  }

  function startDetailPolling() {
    var page = document.querySelector("[data-document-detail-page]");
    if (!page) {
      return;
    }

    pollDetailPage(page);
  }

  function uploadFile(widget, form) {
    var xhr = new XMLHttpRequest();
    var formData = new FormData(form);
    var input = widget.querySelector("[data-upload-input]");

    widget.dataset.uploadInFlight = "1";
    setUploadError(widget, "");
    setUploadRuntime(widget, {
      visible: true,
      stage: "Upload läuft",
      copy: "Die Datei wird übertragen.",
      percent: 2,
      disabled: true,
    });

    xhr.open(form.method || "POST", form.action);
    xhr.responseType = "json";
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");

    xhr.upload.addEventListener("progress", function (event) {
      var percent = 0;
      if (event.lengthComputable && event.total > 0) {
        percent = Math.max(5, Math.min(92, Math.round((event.loaded / event.total) * 100)));
      }
      setUploadRuntime(widget, {
        visible: true,
        stage: "Upload läuft",
        copy: "Die Datei wird übertragen und direkt vorbereitet.",
        percent: percent,
        disabled: true,
      });
    });

    xhr.addEventListener("load", function () {
      var response = parseJsonResponse(xhr);
      widget.dataset.uploadInFlight = "0";

      if (xhr.status >= 200 && xhr.status < 300 && response) {
        updateDocumentsSummary(response.summary_html);
        upsertDocumentRow(response.row_html);
        resetUploadWidget(widget);
        setUploadRuntime(widget, {
          visible: true,
          stage: "Analyse gestartet",
          copy: "Das Dokument ist in der Queue und läuft jetzt im Hintergrund weiter.",
          percent: 100,
          complete: true,
          disabled: false,
        });
        startDocumentsPolling();
        return;
      }

      setUploadError(widget, (response && response.error) || "Der Upload konnte nicht abgeschlossen werden.");
      setUploadRuntime(widget, {
        visible: true,
        stage: "Upload fehlgeschlagen",
        copy: "Bitte prüfe Datei und Eingaben und versuche es erneut.",
        percent: 100,
        error: true,
        disabled: false,
      });
      if (input) {
        updateUploadFilename(widget, input);
      }
    });

    xhr.addEventListener("error", function () {
      widget.dataset.uploadInFlight = "0";
      setUploadError(widget, "Netzwerkfehler beim Upload.");
      setUploadRuntime(widget, {
        visible: true,
        stage: "Upload fehlgeschlagen",
        copy: "Die Verbindung wurde unterbrochen. Bitte erneut versuchen.",
        percent: 100,
        error: true,
        disabled: false,
      });
    });

    xhr.send(formData);
  }

  function initUploadWidget(widget) {
    if (!widget || widget.dataset.uploadWidgetBound === "1") {
      return;
    }

    var form = widget.querySelector("[data-upload-form]");
    var input = widget.querySelector("[data-upload-input]");
    var label = widget.querySelector("[data-upload-label]");

    if (!form || !input || !label) {
      return;
    }

    widget.dataset.uploadWidgetBound = "1";
    updateUploadFilename(widget, input);

    input.addEventListener("change", function () {
      setUploadError(widget, "");
      updateUploadFilename(widget, input);
      if (input.files && input.files.length > 0) {
        form.requestSubmit();
      }
    });

    form.addEventListener("submit", function (event) {
      if (!window.XMLHttpRequest || !window.FormData || widget.dataset.uploadInFlight === "1") {
        return;
      }

      if (typeof form.reportValidity === "function" && !form.reportValidity()) {
        event.preventDefault();
        return;
      }

      event.preventDefault();
      uploadFile(widget, form);
    });

    ["dragenter", "dragover"].forEach(function (eventName) {
      widget.addEventListener(eventName, function (event) {
        event.preventDefault();
        toggleDropzone(widget, true);
      });
    });

    ["dragleave", "dragend", "drop"].forEach(function (eventName) {
      widget.addEventListener(eventName, function (event) {
        event.preventDefault();
        if (eventName !== "drop") {
          var related = event.relatedTarget;
          if (related && widget.contains(related)) {
            return;
          }
        }
        toggleDropzone(widget, false);
      });
    });

    widget.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (!files || files.length === 0) {
        return;
      }

      input.files = files;
      updateUploadFilename(widget, input);
      form.requestSubmit();
    });
  }

  function initUploadWidgets(root) {
    (root || document).querySelectorAll("[data-upload-widget]").forEach(initUploadWidget);
  }

  function parseSortableValue(value) {
    var normalized = (value || "").replace(/\s+/g, " ").trim();
    var numeric = normalized.replace(/\./g, "").replace(",", ".");
    var dateMatch = normalized.match(/^(\d{2})\.(\d{2})\.(\d{4})(?: (\d{2}):(\d{2}))?$/);

    if (dateMatch) {
      return new Date(
        Number(dateMatch[3]),
        Number(dateMatch[2]) - 1,
        Number(dateMatch[1]),
        Number(dateMatch[4] || 0),
        Number(dateMatch[5] || 0)
      ).getTime();
    }

    if (/^-?\d+(?:[.,]\d+)?$/.test(numeric)) {
      return Number(numeric);
    }

    return normalized.toLocaleLowerCase();
  }

  function initTableEnhancer(shell) {
    if (!shell || shell.dataset.tableEnhancedBound === "1") {
      return;
    }

    var table = shell.querySelector("table");
    var body = table && table.tBodies && table.tBodies[0];
    var headers = table ? Array.prototype.slice.call(table.querySelectorAll("thead th")) : [];
    var rows = body ? Array.prototype.slice.call(body.querySelectorAll("tr")) : [];

    if (!table || !body || headers.length === 0 || rows.length === 0) {
      return;
    }

    shell.dataset.tableEnhancedBound = "1";

    var searchPlaceholder = shell.dataset.tableSearchPlaceholder || "Tabelle filtern";
    var pageSize = Math.max(1, Number.parseInt(shell.dataset.tablePageSize || "8", 10));
    var rowModels = rows.map(function (row, index) {
      return {
        row: row,
        index: index,
        searchText: row.innerText.toLocaleLowerCase(),
      };
    });
    var state = {
      query: "",
      sortIndex: -1,
      direction: "asc",
      page: 1,
    };

    var controls = document.createElement("div");
    controls.className = "table-tools";

    var search = document.createElement("input");
    search.type = "search";
    search.placeholder = searchPlaceholder;
    search.className = "table-tools-search";

    var sort = document.createElement("select");
    sort.className = "table-tools-select";

    var defaultOption = document.createElement("option");
    defaultOption.value = "";
    defaultOption.textContent = "Originalreihenfolge";
    sort.appendChild(defaultOption);

    headers.forEach(function (header, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = "Sortieren nach " + header.innerText.trim();
      sort.appendChild(option);
    });

    var direction = document.createElement("button");
    direction.type = "button";
    direction.className = "table-tools-button";
    direction.textContent = "Aufsteigend";

    var summary = document.createElement("div");
    summary.className = "table-tools-summary";

    var pager = document.createElement("div");
    pager.className = "table-tools-pager";

    var prev = document.createElement("button");
    prev.type = "button";
    prev.className = "table-tools-button";
    prev.textContent = "Zurück";

    var pageLabel = document.createElement("span");
    pageLabel.className = "table-tools-page";

    var next = document.createElement("button");
    next.type = "button";
    next.className = "table-tools-button";
    next.textContent = "Weiter";

    controls.appendChild(search);
    controls.appendChild(sort);
    controls.appendChild(direction);
    controls.appendChild(summary);
    pager.appendChild(prev);
    pager.appendChild(pageLabel);
    pager.appendChild(next);
    controls.appendChild(pager);
    shell.parentNode.insertBefore(controls, shell);

    var emptyRow = document.createElement("tr");
    var emptyCell = document.createElement("td");
    emptyCell.colSpan = headers.length;
    emptyCell.className = "table-tools-empty";
    emptyCell.textContent = "Keine Einträge für diesen Filter.";
    emptyRow.appendChild(emptyCell);

    function render() {
      var filtered = rowModels.filter(function (model) {
        return !state.query || model.searchText.indexOf(state.query) !== -1;
      });

      if (state.sortIndex >= 0) {
        filtered.sort(function (left, right) {
          var leftCell = left.row.cells[state.sortIndex];
          var rightCell = right.row.cells[state.sortIndex];
          var leftValue = parseSortableValue(leftCell ? leftCell.innerText : "");
          var rightValue = parseSortableValue(rightCell ? rightCell.innerText : "");
          var compare;

          if (typeof leftValue === "number" && typeof rightValue === "number") {
            compare = leftValue - rightValue;
          } else {
            compare = String(leftValue).localeCompare(String(rightValue), "de", { numeric: true, sensitivity: "base" });
          }

          return state.direction === "asc" ? compare : compare * -1;
        });
      }

      var totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
      var safePage = Math.min(state.page, totalPages);
      var start = (safePage - 1) * pageSize;
      var end = start + pageSize;
      var visibleSet = new Set(filtered.slice(start, end).map(function (model) { return model.row; }));

      state.page = safePage;
      filtered.forEach(function (model) {
        body.appendChild(model.row);
      });
      rowModels.forEach(function (model) {
        model.row.hidden = !visibleSet.has(model.row);
      });

      if (filtered.length === 0) {
        if (!body.contains(emptyRow)) {
          body.appendChild(emptyRow);
        }
      } else if (body.contains(emptyRow)) {
        emptyRow.remove();
      }

      summary.textContent = filtered.length + " / " + rowModels.length + " Einträge";
      pageLabel.textContent = "Seite " + state.page + " / " + totalPages;
      prev.disabled = state.page <= 1;
      next.disabled = state.page >= totalPages;
      direction.textContent = state.direction === "asc" ? "Aufsteigend" : "Absteigend";
    }

    search.addEventListener("input", function () {
      state.query = search.value.trim().toLocaleLowerCase();
      state.page = 1;
      render();
    });

    sort.addEventListener("change", function () {
      state.sortIndex = sort.value === "" ? -1 : Number.parseInt(sort.value, 10);
      state.page = 1;
      render();
    });

    direction.addEventListener("click", function () {
      state.direction = state.direction === "asc" ? "desc" : "asc";
      render();
    });

    prev.addEventListener("click", function () {
      state.page = Math.max(1, state.page - 1);
      render();
    });

    next.addEventListener("click", function () {
      state.page += 1;
      render();
    });

    render();
  }

  function initTableEnhancers(root) {
    (root || document).querySelectorAll("[data-table-enhanced]").forEach(initTableEnhancer);
  }

  function boot(root) {
    initUploadWidgets(root);
    initTableEnhancers(root);
    initDocumentToolbar(root);
    startDocumentsPolling();
    startDetailPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      boot(document);
    });
  } else {
    boot(document);
  }

  document.body.addEventListener("htmx:afterSwap", function (event) {
    boot(event.target || document);
  });
})();
