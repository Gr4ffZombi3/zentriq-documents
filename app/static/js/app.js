(function () {
  var ACTIVE_ANALYSIS_STATUSES = {
    pending: true,
    ocr_processing: true,
    ocr_done: true,
    ai_processing: true,
  };

  function updateUploadFilename(widget, input) {
    var target = widget.querySelector("[data-upload-filename]");
    if (!target) {
      return;
    }

    var file = input && input.files && input.files[0];
    target.textContent = file ? file.name : "Noch keine Datei ausgewaehlt.";
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
      updateUploadFilename(widget, input);
      if (input.files && input.files.length > 0) {
        form.requestSubmit();
      }
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

  function initAnalysisMonitor() {
    var monitor = document.querySelector("[data-analysis-monitor]");
    if (!monitor || monitor.dataset.analysisMonitorBound === "1") {
      return;
    }

    var status = monitor.dataset.analysisStatus;
    if (!ACTIVE_ANALYSIS_STATUSES[status]) {
      return;
    }

    var refreshMs = Number.parseInt(monitor.dataset.analysisRefreshMs || "3000", 10);
    monitor.dataset.analysisMonitorBound = "1";
    window.setTimeout(function () {
      window.location.reload();
    }, Number.isFinite(refreshMs) ? refreshMs : 3000);
  }

  function boot(root) {
    initUploadWidgets(root);
    initAnalysisMonitor();
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
