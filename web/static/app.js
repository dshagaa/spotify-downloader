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
    var stats = document.getElementById("job-stats");
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
      if (stats) {
        stats.textContent =
          data.matched +
          " matched \u00b7 " +
          data.no_match +
          " not found";
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
