(function () {
  "use strict";

  var RUNS_URL = "/approvals/api/runs";
  var POLL_MS = 5000;
  var loadInFlight = false;
  var pollTimer = null;

  function $(id) {
    return document.getElementById(id);
  }

  function setText(node, value) {
    if (!node) {
      return;
    }
    node.textContent = value == null ? "" : String(value);
  }

  function setStatus(message) {
    setText($("status"), message);
  }

  function actionToken(run, decision) {
    var actions = run && run.actions;
    if (actions && typeof actions === "object") {
      if (decision === "approve" && actions.approve_token) {
        return String(actions.approve_token);
      }
      if (decision === "reject" && actions.reject_token) {
        return String(actions.reject_token);
      }
    }
    if (run && run.action_token) {
      return String(run.action_token);
    }
    return "";
  }

  function addTerm(dl, label, value) {
    var dt = document.createElement("dt");
    var dd = document.createElement("dd");
    setText(dt, label);
    setText(dd, value == null ? "" : String(value));
    dl.appendChild(dt);
    dl.appendChild(dd);
  }

  function clearNode(node) {
    if (!node) {
      return;
    }
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function decisionButton(run, decision, label) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-" + decision;
    setText(btn, label);
    btn.addEventListener("click", function () {
      onDecide(run, decision, btn);
    });
    return btn;
  }

  function renderRun(run) {
    var item = document.createElement("li");
    item.className = "run";
    var dl = document.createElement("dl");
    addTerm(dl, "Run ID", run && run.run_id);
    addTerm(dl, "Workflow", run && run.workflow);
    addTerm(dl, "Node", run && run.node_id);
    addTerm(dl, "Prompt", run && run.prompt);
    addTerm(dl, "Started", run && run.started_at);
    addTerm(dl, "Actor", run && run.actor);
    item.appendChild(dl);
    var actions = document.createElement("div");
    actions.className = "actions";
    actions.appendChild(decisionButton(run, "approve", "Approve"));
    actions.appendChild(decisionButton(run, "reject", "Reject"));
    item.appendChild(actions);
    return item;
  }

  function renderRuns(runs) {
    var list = $("runs");
    var empty = $("empty");
    if (!list) {
      return;
    }
    clearNode(list);
    var items = Array.isArray(runs) ? runs : [];
    if (empty) {
      empty.hidden = items.length > 0;
    }
    var i;
    for (i = 0; i < items.length; i += 1) {
      list.appendChild(renderRun(items[i] || {}));
    }
  }

  function parseBody(res) {
    return res.json().catch(function () {
      return null;
    });
  }

  function loadRuns() {
    if (loadInFlight) {
      return;
    }
    loadInFlight = true;
    fetch(RUNS_URL, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (res) {
        if (res.status === 401) {
          setStatus("session required");
          renderRuns([]);
          return null;
        }
        if (res.status === 403) {
          setStatus("denied");
          return null;
        }
        if (!res.ok) {
          setStatus("Unable to load pending approvals.");
          return null;
        }
        return parseBody(res);
      })
      .then(function (body) {
        if (!body) {
          return;
        }
        renderRuns(body.runs);
        setStatus("");
      })
      .catch(function () {
        setStatus("Unable to load pending approvals.");
      })
      .then(function () {
        loadInFlight = false;
      });
  }

  function onDecide(run, decision, button) {
    var message =
      "Confirm " +
      decision +
      " for node '" +
      (run && run.node_id ? String(run.node_id) : "") +
      "'?";
    if (!window.confirm(message)) {
      return;
    }
    if (button) {
      button.disabled = true;
    }
    var runId = run && run.run_id ? String(run.run_id) : "";
    var revision = Number(run && run.revision);
    var payload = {
      node_id: run && run.node_id != null ? String(run.node_id) : "",
      decision: decision,
      revision: Number.isFinite(revision) ? revision : 0,
      action_token: actionToken(run, decision),
    };
    fetch("/approvals/api/runs/" + encodeURIComponent(runId) + "/decide", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify(payload),
    })
      .then(function (res) {
        if (res.status === 401) {
          setStatus("session required");
          if (button) {
            button.disabled = false;
          }
          return;
        }
        if (res.status === 403) {
          setStatus("denied");
          if (button) {
            button.disabled = false;
          }
          return;
        }
        if (res.status === 409) {
          setStatus("conflict");
          loadRuns();
          return;
        }
        if (!res.ok) {
          setStatus("Decision was not accepted.");
          if (button) {
            button.disabled = false;
          }
          return;
        }
        setStatus("");
        loadRuns();
      })
      .catch(function () {
        setStatus("Decision was not accepted.");
        if (button) {
          button.disabled = false;
        }
      });
  }

  function startPolling() {
    loadRuns();
    if (pollTimer !== null) {
      return;
    }
    pollTimer = window.setInterval(function () {
      if (document.hidden) {
        return;
      }
      loadRuns();
    }, POLL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startPolling);
  } else {
    startPolling();
  }
})();
