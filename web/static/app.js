/* Live log streaming and small progressive-enhancement helpers. */
(function () {
  "use strict";

  var ACTIVE = { queued: true, running: true, cancelling: true };

  function statusClass(status) {
    return (
      {
        queued: "warn",
        running: "info",
        cancelling: "warn",
        completed: "ok",
        failed: "err",
        cancelled: "muted",
      }[status] || "muted"
    );
  }

  function wireLog() {
    var card = document.querySelector("[data-job-id]");
    if (!card || !window.EventSource) {
      return;
    }

    var jobId = card.getAttribute("data-job-id");
    var initialStatus = card.getAttribute("data-job-status");
    var log = document.getElementById("log");
    var badge = document.getElementById("job-status");
    var bar = document.getElementById("progress-bar");
    var label = document.getElementById("progress-label");
    var percent = document.getElementById("progress-percent");
    var detail = document.getElementById("progress-detail");
    var outputDir = document.getElementById("output-dir");
    var follow = true;

    if (log) {
      log.addEventListener("scroll", function () {
        follow =
          log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
      });
    }

    function append(text) {
      if (!log) {
        return;
      }
      log.textContent += text;
      if (follow) {
        log.scrollTop = log.scrollHeight;
      }
    }

    var source = new EventSource(
      "/api/jobs/" + encodeURIComponent(jobId) + "/events"
    );

    source.addEventListener("chunk", function (event) {
      append(JSON.parse(event.data).text);
    });

    source.addEventListener("status", function (event) {
      var data = JSON.parse(event.data);
      if (badge) {
        badge.textContent = data.status;
        badge.className = "badge " + statusClass(data.status);
      }
    });

    source.addEventListener("progress", function (event) {
      var data = JSON.parse(event.data);
      if (label) {
        label.textContent = data.label;
      }
      if (percent) {
        percent.textContent = data.percent + "%";
      }
      if (detail) {
        detail.textContent = data.detail;
      }
      if (outputDir && data.output_dir) {
        outputDir.textContent = data.output_dir;
      }
      if (bar) {
        bar.classList.toggle("pending", Boolean(data.indeterminate));
        if (data.indeterminate) {
          bar.removeAttribute("value");
        } else {
          bar.value = data.percent;
        }
      }
    });

    source.addEventListener("end", function () {
      source.close();
      if (ACTIVE[initialStatus]) {
        window.setTimeout(function () {
          window.location.reload();
        }, 700);
      }
    });
  }

  function wireConfirms() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || !form.getAttribute) {
        return;
      }
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        event.preventDefault();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      wireLog();
      wireConfirms();
    });
  } else {
    wireLog();
    wireConfirms();
  }
})();
