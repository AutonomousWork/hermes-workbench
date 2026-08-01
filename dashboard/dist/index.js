/**
 * NesQuena WebUI Control — Hermes dashboard plugin.
 *
 * Plain IIFE using the host's React and authenticated fetchJSON helper.
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry || typeof registry.register !== "function") return;

  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardContent, CardHeader, CardTitle, Badge, Button } = SDK.components;
  const { useCallback, useEffect, useState } = SDK.hooks;
  const API_ROOT = "/api/plugins/nesquena-webui-control";

  function api(path, options) {
    return SDK.fetchJSON(API_ROOT + path, options);
  }

  function errorMessage(error) {
    const raw = error && error.message ? String(error.message) : String(error || "Unknown error");
    const match = raw.match(/^\d{3}:\s*(.*)$/s);
    const body = match ? match[1] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch (_error) {
      // Non-JSON failures are already useful as plain text.
    }
    return body;
  }

  function statusPresentation(status) {
    if (!status) return { label: "Checking", tone: "checking" };
    if (status.healthy) return { label: "Healthy", tone: "healthy" };
    if (status.running) return { label: "Starting / unhealthy", tone: "warning" };
    if (status.loaded) return { label: "Loaded / stopped", tone: "warning" };
    return { label: "Stopped", tone: "stopped" };
  }

  function DetailRow(props) {
    return h("div", { className: "nesquena-control__detail-row" },
      h("dt", null, props.label),
      h("dd", null, props.value == null || props.value === "" ? "—" : String(props.value)),
    );
  }

  function NesQuenaControlPage() {
    const [status, setStatus] = useState(null);
    const [busy, setBusy] = useState(null);
    const [notice, setNotice] = useState(null);
    const [error, setError] = useState(null);

    const refresh = useCallback(function () {
      setError(null);
      return api("/status")
        .then(function (next) {
          setStatus(next);
          return next;
        })
        .catch(function (failure) {
          setError(errorMessage(failure));
          throw failure;
        });
    }, []);

    useEffect(function () {
      let active = true;
      api("/status")
        .then(function (next) { if (active) setStatus(next); })
        .catch(function (failure) { if (active) setError(errorMessage(failure)); });
      const timer = window.setInterval(function () {
        api("/status")
          .then(function (next) { if (active) setStatus(next); })
          .catch(function (failure) { if (active) setError(errorMessage(failure)); });
      }, 10000);
      return function () {
        active = false;
        window.clearInterval(timer);
      };
    }, []);

    function runAction(action) {
      if (busy) return;
      if (action === "stop" && !window.confirm(
        "Stop the NesQuena WebUI? You can start it again from this page."
      )) return;

      setBusy(action);
      setError(null);
      setNotice(null);
      api("/" + action, { method: "POST" })
        .then(function (result) {
          if (result && result.status) setStatus(result.status);
          setNotice(result && result.message ? result.message : "Action completed.");
        })
        .catch(function (failure) {
          setError(errorMessage(failure));
        })
        .then(function () {
          setBusy(null);
        });
    }

    function refreshStatus() {
      if (busy) return;
      setBusy("status");
      setNotice(null);
      refresh().then(function () {
        setNotice("Status refreshed.");
      }).catch(function () {
        // refresh() already populated the visible error.
      }).then(function () {
        setBusy(null);
      });
    }

    const presentation = statusPresentation(status);
    const httpStatus = status && status.http && status.http.reachable
      ? "HTTP " + status.http.status_code
      : "Unreachable";
    const checkedAt = status && status.checked_at
      ? new Date(status.checked_at).toLocaleString()
      : null;

    return h("div", { className: "nesquena-control" },
      h("div", { className: "nesquena-control__hero" },
        h("div", null,
          h("div", { className: "nesquena-control__eyebrow" }, "MAC MINI SERVICE CONTROL"),
          h("h1", null, "NesQuena Web UI"),
          h("p", null,
            "Control the local WebUI LaunchAgent after Hermes updates or whenever the interface needs recovery."
          ),
        ),
        h(Badge, {
          className: "nesquena-control__status nesquena-control__status--" + presentation.tone,
        },
          h("span", { className: "nesquena-control__status-dot", "aria-hidden": "true" }),
          presentation.label,
        ),
      ),

      h("div", { className: "nesquena-control__grid" },
        h(Card, { className: "nesquena-control__card" },
          h(CardHeader, null,
            h(CardTitle, null, "Status"),
          ),
          h(CardContent, null,
            h("dl", { className: "nesquena-control__details" },
              h(DetailRow, { label: "LaunchAgent", value: status && status.service }),
              h(DetailRow, { label: "State", value: status && status.state }),
              h(DetailRow, { label: "PID", value: status && status.pid }),
              h(DetailRow, { label: "Endpoint", value: status && status.endpoint }),
              h(DetailRow, { label: "Health", value: httpStatus }),
              h(DetailRow, { label: "Last exit", value: status && status.last_exit_code }),
              h(DetailRow, { label: "Checked", value: checkedAt }),
            ),
          ),
        ),

        h(Card, { className: "nesquena-control__card" },
          h(CardHeader, null,
            h(CardTitle, null, "Commands"),
          ),
          h(CardContent, null,
            h("p", { className: "nesquena-control__command-help" },
              "Start loads the existing LaunchAgent. Stop unloads it so KeepAlive cannot immediately respawn it."
            ),
            h("div", { className: "nesquena-control__actions" },
              h(Button, {
                onClick: function () { runAction("start"); },
                disabled: !!busy || !!(status && status.running),
              }, busy === "start" ? "Starting…" : "Start"),
              h(Button, {
                variant: "outline",
                onClick: function () { runAction("stop"); },
                disabled: !!busy || !!(status && !status.loaded),
                className: "nesquena-control__stop",
              }, busy === "stop" ? "Stopping…" : "Stop"),
              h(Button, {
                variant: "outline",
                onClick: function () { runAction("restart"); },
                disabled: !!busy,
              }, busy === "restart" ? "Restarting…" : "Restart"),
              h(Button, {
                variant: "ghost",
                onClick: refreshStatus,
                disabled: !!busy,
              }, busy === "status" ? "Checking…" : "Refresh status"),
            ),
            h("div", { className: "nesquena-control__feedback", "aria-live": "polite" },
              error ? h("div", { className: "nesquena-control__message nesquena-control__message--error" }, error) : null,
              notice ? h("div", { className: "nesquena-control__message nesquena-control__message--ok" }, notice) : null,
            ),
          ),
        ),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("nesquena-webui-control", NesQuenaControlPage);
})();
