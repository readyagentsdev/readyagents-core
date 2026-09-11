(function () {
  "use strict";

  var statusEl = document.getElementById("status");
  var unlock = document.getElementById("unlock");
  var tabs = document.getElementById("tabs");
  var fingerprint = null;
  var workflowPath = "";

  function setStatus(text, kind) {
    statusEl.textContent = text;
    if (kind) statusEl.setAttribute("data-kind", kind);
    else statusEl.removeAttribute("data-kind");
  }

  function showTab(name) {
    ["graph", "edit", "runs", "diff"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.hidden = id !== name;
    });
  }

  function api(method, path, body) {
    var opts = { method: method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = {};
        try { data = JSON.parse(text); } catch (err) { data = { raw: text }; }
        data._status = res.status;
        return data;
      });
    });
  }

  function renderText(id, value) {
    var el = document.getElementById(id);
    el.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  }

  document.getElementById("unlock-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var token = document.getElementById("token").value;
    fetch("/studio/session", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token }),
      redirect: "manual"
    }).then(function (res) {
      if (res.status === 303 || res.status === 200) {
        window.location.assign("/studio");
        return;
      }
      setStatus("Unlock failed.", "error");
    });
  });

  api("GET", "/studio/api/me").then(function (data) {
    if (data._status === 200 && data.ok) {
      unlock.hidden = true;
      tabs.hidden = false;
      showTab("graph");
      setStatus(data.read_only ? "Read-only studio." : "Studio unlocked.");
    } else {
      unlock.hidden = false;
      tabs.hidden = true;
      setStatus("Paste the bootstrap token from stderr.");
    }
  });

  tabs.addEventListener("click", function (ev) {
    var btn = ev.target.closest("button[data-tab]");
    if (!btn) return;
    showTab(btn.getAttribute("data-tab"));
  });

  document.getElementById("open-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    workflowPath = document.getElementById("wf-path").value;
    api("GET", "/studio/api/workflow?path=" + encodeURIComponent(workflowPath)).then(function (data) {
      if (!data.ok) {
        setStatus(data.message || "open failed", "error");
        return;
      }
      fingerprint = data.fingerprint;
      renderText("graph-view", data.graph);
      setStatus("Opened " + data.path);
    });
  });

  document.getElementById("edit-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var payload = {
      path: workflowPath,
      node_id: document.getElementById("edit-node").value,
      field: document.getElementById("edit-field").value,
      value: document.getElementById("edit-value").value,
      fingerprint: fingerprint
    };
    api("POST", "/studio/api/workflow/validate", payload).then(function (check) {
      renderText("edit-out", check);
      if (!check.ok) {
        setStatus(check.message || "invalid edit", "error");
        return;
      }
      return api("POST", "/studio/api/workflow/save", payload).then(function (saved) {
        renderText("edit-out", saved);
        if (saved.ok) {
          fingerprint = saved.fingerprint;
          setStatus("Saved " + saved.field);
        } else {
          setStatus(saved.message || "save refused", "error");
        }
      });
    });
  });

  document.getElementById("runs-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var status = document.getElementById("run-status").value;
    var q = status ? "?status=" + encodeURIComponent(status) : "";
    api("GET", "/studio/api/runs" + q).then(function (data) {
      renderText("runs-view", data);
    });
  });

  document.getElementById("diff-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    var a = document.getElementById("diff-a").value;
    var b = document.getElementById("diff-b").value;
    api("GET", "/studio/api/runs/diff?a=" + encodeURIComponent(a) + "&b=" + encodeURIComponent(b)).then(function (data) {
      renderText("diff-view", data);
    });
  });
})();
