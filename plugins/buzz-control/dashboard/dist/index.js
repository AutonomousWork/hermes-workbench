/** Buzz Control — Hermes dashboard plugin. */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const registry = window.__HERMES_PLUGINS__;
  if (!SDK || !registry || typeof registry.register !== "function") return;

  const { React } = SDK;
  const h = React.createElement;
  const { Card, CardContent, CardHeader, CardTitle, Badge, Button } = SDK.components;
  const { useCallback, useEffect, useRef, useState } = SDK.hooks;
  const API_ROOT = "/api/plugins/buzz-control";
  const CRON_URL = "/api/cron/jobs?profile=all";
  const JOB_NAME = "buzz-control-image-update";

  function api(path, options) {
    return SDK.fetchJSON(API_ROOT + path, options);
  }

  function cronApi() {
    return SDK.fetchJSON(CRON_URL);
  }

  function errorMessage(error) {
    const raw = error && error.message ? String(error.message) : String(error || "Unknown error");
    const match = raw.match(/^\d{3}:\s*(.*)$/s);
    const body = match ? match[1] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
    } catch (_error) {
      // Plain text is already suitable for display.
    }
    return body;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function shortIdentity(value) {
    if (!value) return "—";
    return String(value).replace(/^sha256:/, "").slice(0, 12);
  }

  function statusPresentation(status) {
    if (!status) return { label: "Checking", tone: "checking" };
    if (status.healthy) return { label: "Healthy", tone: "healthy" };
    if (status.container && status.container.running) {
      return { label: "Running / unhealthy", tone: "warning" };
    }
    if (status.container && status.container.error) {
      return { label: "Unavailable", tone: "warning" };
    }
    return { label: "Stopped", tone: "stopped" };
  }

  function updatePresentation(updates) {
    if (!updates) return { label: "Loading saved check", tone: "checking" };
    if (updates.update_available === true) return { label: "Update available", tone: "available" };
    if (updates.update_available === false) return { label: "Current", tone: "current" };
    return { label: "Version unknown", tone: "warning" };
  }

  const RESULT_LABELS = {
    already_current: "Buzz is current and healthy.",
    updated: "Buzz was updated and is healthy.",
    stopped: "Automatic update failed because Buzz is stopped.",
    timed_out: "The Buzz update exceeded its safe execution window.",
    pull_failed: "Buzz could not check or pull the relay image.",
    apply_failed: "Buzz could not apply the pulled relay image.",
    unhealthy: "The applied relay did not become healthy.",
    verification_failed: "The running relay does not match the pulled image.",
    state_write_failed: "The operation finished, but Hermes could not save its result.",
  };

  function resultLabel(result) {
    return RESULT_LABELS[result] || "Hermes has no reliable saved update result.";
  }

  function managedSchedule(jobs) {
    const matches = Array.isArray(jobs)
      ? jobs.filter(function (job) { return job && job.name === JOB_NAME; })
      : [];
    if (matches.length > 1) return { status: "ambiguous" };
    if (!matches.length) return { status: "missing" };
    const job = matches[0];
    const active = job.enabled !== false && job.state !== "paused";
    return Object.assign({}, job, { status: active ? "active" : "paused" });
  }

  function DetailRow(props) {
    return h("div", { className: "buzz-control__detail-row" },
      h("dt", null, props.label),
      h("dd", { className: props.mono === false ? "buzz-control__plain" : null },
        props.value == null || props.value === "" ? "—" : String(props.value),
      ),
    );
  }

  function Identity(props) {
    return h("span", { className: "buzz-control__revision" }, shortIdentity(props.value));
  }

  function BuzzControlPage() {
    const [status, setStatus] = useState(null);
    const [updates, setUpdates] = useState(null);
    const [busy, setBusy] = useState(null);
    const [notice, setNotice] = useState(null);
    const [statusError, setStatusError] = useState(null);
    const [updatesError, setUpdatesError] = useState(null);
    const [schedule, setSchedule] = useState(null);
    const [scheduleError, setScheduleError] = useState(null);
    const actionActiveRef = useRef(false);
    const generationRef = useRef(0);

    const acceptUpdates = useCallback(function (next) {
      setUpdates(next);
      const errors = next && Array.isArray(next.errors) ? next.errors.filter(Boolean) : [];
      setUpdatesError(errors.length ? errors.join(" ") : null);
      return next;
    }, []);

    const refreshStatus = useCallback(function () {
      return api("/status")
        .then(function (next) {
          setStatus(next);
          setStatusError(null);
          return next;
        })
        .catch(function (failure) {
          setStatusError(errorMessage(failure));
          throw failure;
        });
    }, []);

    const refreshUpdates = useCallback(function () {
      return api("/updates")
        .then(acceptUpdates)
        .catch(function (failure) {
          setUpdatesError(errorMessage(failure));
          throw failure;
        });
    }, [acceptUpdates]);

    const refreshSchedule = useCallback(function () {
      return cronApi()
        .then(function (jobs) {
          const next = managedSchedule(jobs);
          setSchedule(next);
          setScheduleError(null);
          return next;
        })
        .catch(function (failure) {
          setScheduleError(errorMessage(failure));
          throw failure;
        });
    }, []);

    useEffect(function () {
      let active = true;
      let inFlight = false;
      function pollStatus() {
        if (!active || inFlight || actionActiveRef.current) return;
        const generation = generationRef.current;
        inFlight = true;
        api("/status")
          .then(function (next) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              setStatus(next);
              setStatusError(null);
            }
          })
          .catch(function (failure) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              setStatusError(errorMessage(failure));
            }
          })
          .finally(function () { inFlight = false; });
      }
      pollStatus();
      const timer = window.setInterval(pollStatus, 15000);
      return function () { active = false; window.clearInterval(timer); };
    }, []);

    useEffect(function () {
      let active = true;
      let inFlight = false;
      function pollUpdates() {
        if (!active || inFlight || actionActiveRef.current) return;
        const generation = generationRef.current;
        inFlight = true;
        api("/updates")
          .then(function (next) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              acceptUpdates(next);
            }
          })
          .catch(function (failure) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              setUpdatesError(errorMessage(failure));
            }
          })
          .finally(function () { inFlight = false; });
      }
      pollUpdates();
      const timer = window.setInterval(pollUpdates, 300000);
      return function () { active = false; window.clearInterval(timer); };
    }, [acceptUpdates]);

    useEffect(function () {
      let active = true;
      let inFlight = false;
      function pollSchedule() {
        if (!active || inFlight || actionActiveRef.current) return;
        const generation = generationRef.current;
        inFlight = true;
        cronApi()
          .then(function (jobs) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              setSchedule(managedSchedule(jobs));
              setScheduleError(null);
            }
          })
          .catch(function (failure) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              setScheduleError(errorMessage(failure));
            }
          })
          .finally(function () { inFlight = false; });
      }
      pollSchedule();
      const timer = window.setInterval(pollSchedule, 60000);
      return function () { active = false; window.clearInterval(timer); };
    }, []);

    function runUpdate() {
      if (busy || actionActiveRef.current) return;
      const stopped = !!(status && status.container && !status.container.running);
      const confirmation = stopped
        ? "Buzz is stopped. Updating will start the relay and end the maintenance stop. Continue?"
        : "Update Buzz to the latest published relay image? The relay will restart briefly only when the image changed.";
      if (!window.confirm(confirmation)) return;

      actionActiveRef.current = true;
      generationRef.current += 1;
      setBusy("update");
      setNotice(null);
      setStatusError(null);
      setUpdatesError(null);
      api("/update", { method: "POST" })
        .then(function (result) {
          if (result && result.status) setStatus(result.status);
          if (result && result.updates) acceptUpdates(result.updates);
          setNotice(result && result.message ? result.message : "Buzz update completed.");
        })
        .catch(function (failure) { setUpdatesError(errorMessage(failure)); })
        .finally(function () { actionActiveRef.current = false; setBusy(null); });
    }

    function refreshAll() {
      if (busy || actionActiveRef.current) return;
      actionActiveRef.current = true;
      generationRef.current += 1;
      setBusy("refresh");
      setNotice(null);
      Promise.allSettled([refreshStatus(), refreshUpdates(), refreshSchedule()])
        .then(function (results) {
          if (results.every(function (result) { return result.status === "fulfilled"; })) {
            setNotice("Buzz status, updates, and schedule refreshed.");
          }
        })
        .finally(function () { actionActiveRef.current = false; setBusy(null); });
    }

    const statusView = statusPresentation(status);
    const updateView = updatePresentation(updates);
    const container = status && status.container;
    const probe = status && status.probe;
    const relay = status && status.relay;
    const deployment = status && status.deployment;
    const current = updates && updates.current;
    const latest = updates && updates.latest;
    const updateState = updates && updates.state || {};
    const currentIdentity = current && (current.revision || current.image_id || current.digest);
    const latestIdentity = latest && (latest.revision || latest.image_id || latest.digest);
    const stopped = !!(container && !container.running);
    const scheduleLabel = !schedule ? "Checking" : ({
      active: "Active",
      paused: "Paused",
      missing: "Not installed",
      ambiguous: "Needs attention",
    }[schedule.status] || "Unknown");
    const probeLabel = probe && probe.reachable
      ? "HTTP " + probe.status_code + (probe.response ? " · " + probe.response : "")
      : "Unreachable";

    return h("div", { className: "buzz-control" },
      h("div", { className: "buzz-control__hero" },
        h("div", null,
          h("div", { className: "buzz-control__eyebrow" }, "LOCAL RELAY OPERATIONS"),
          h("h1", null, "Buzz"),
          h("p", null,
            "Verify the relay, review the last image check, and update the running service without leaving Hermes."
          ),
        ),
        h(Badge, {
          className: "buzz-control__status buzz-control__status--" + statusView.tone,
        }, h("span", { className: "buzz-control__status-dot", "aria-hidden": "true" }), statusView.label),
      ),

      h("div", { className: "buzz-control__grid" },
        h(Card, { className: "buzz-control__card" },
          h(CardHeader, null, h(CardTitle, null, "Server health")),
          h(CardContent, null,
            h("dl", { className: "buzz-control__details" },
              h(DetailRow, { label: "Container", value: container && container.name }),
              h(DetailRow, { label: "Runtime", value: container && container.state }),
              h(DetailRow, { label: "Docker health", value: container && container.health }),
              h(DetailRow, { label: "Liveness", value: probeLabel }),
              h(DetailRow, { label: "Started", value: formatDate(container && container.started_at) }),
              h(DetailRow, { label: "Checked", value: formatDate(status && status.checked_at) }),
            ),
            statusError ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, statusError) : null,
            container && container.error ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, container.error) : null,
          ),
        ),

        h(Card, { className: "buzz-control__card" },
          h(CardHeader, null, h(CardTitle, null, "Relay location")),
          h(CardContent, null,
            h("div", { className: "buzz-control__relay-primary" },
              h("span", null, relay && relay.scope || "Relay"),
              h("code", null, relay && relay.public_url || "—"),
            ),
            h("dl", { className: "buzz-control__details" },
              h(DetailRow, { label: "Local listener", value: relay && relay.local_url }),
              h(DetailRow, { label: "Compose project", value: deployment && deployment.project }),
              h(DetailRow, { label: "Service", value: deployment && deployment.service }),
              h(DetailRow, { label: "Image", value: deployment && deployment.image }),
            ),
          ),
        ),

        h(Card, { className: "buzz-control__card buzz-control__card--updates" },
          h(CardHeader, { className: "buzz-control__updates-header" },
            h(CardTitle, null, "Latest updates"),
            h(Badge, { className: "buzz-control__release buzz-control__release--" + updateView.tone }, updateView.label),
          ),
          h(CardContent, null,
            h("div", { className: "buzz-control__versions" },
              h("div", { className: "buzz-control__version" },
                h("span", null, "Running image"),
                h(Identity, { value: currentIdentity }),
                h("small", null, formatDate(current && current.created_at)),
              ),
              h("div", { className: "buzz-control__version-arrow", "aria-hidden": "true" }, "→"),
              h("div", { className: "buzz-control__version" },
                h("span", null, "Latest image observed"),
                h(Identity, { value: latestIdentity }),
                h("small", null, formatDate(latest && latest.created_at)),
              ),
            ),

            h("div", { className: "buzz-control__operation" },
              h("strong", null, resultLabel(updateState.result)),
              h("dl", { className: "buzz-control__details" },
                h(DetailRow, { label: "Last check", value: formatDate(updateState.last_check_at || (updates && updates.checked_at)) }),
                h(DetailRow, { label: "Trigger", value: updateState.trigger, mono: false }),
                h(DetailRow, { label: "Last successful update", value: formatDate(updateState.last_successful_update_at) }),
                h(DetailRow, { label: "Latest failure", value: updateState.latest_failure_result || "None", mono: false }),
              ),
              updateState.latest_failure_error ? h("p", { className: "buzz-control__failure-detail" }, updateState.latest_failure_error) : null,
            ),

            h("div", { className: "buzz-control__schedule" },
              h("div", null,
                h("h3", null, "Managed schedule"),
                h("p", null, "Hermes checks for a new relay image without using an agent."),
              ),
              h(Badge, null, scheduleLabel),
              h("dl", { className: "buzz-control__details" },
                h(DetailRow, { label: "Cadence", value: schedule && (schedule.schedule_display || (schedule.schedule && schedule.schedule.display)) }),
                h(DetailRow, { label: "Last run", value: formatDate(schedule && schedule.last_run_at) }),
                h(DetailRow, { label: "Next run", value: formatDate(schedule && schedule.next_run_at) }),
              ),
              h("a", { href: "/cron", className: "buzz-control__cron-link" }, "Manage in Hermes Cron"),
            ),

            h("div", { className: "buzz-control__update-actions" },
              h(Button, {
                onClick: runUpdate,
                disabled: !!busy,
                "aria-busy": busy === "update",
              }, busy === "update" ? "Updating Buzz…" : (stopped ? "Update and start Buzz" : "Update Buzz")),
              h(Button, {
                variant: "outline",
                onClick: refreshAll,
                disabled: !!busy,
                "aria-busy": busy === "refresh",
              }, busy === "refresh" ? "Checking…" : "Refresh all"),
            ),

            updatesError ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, updatesError) : null,
            scheduleError ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, scheduleError) : null,
            notice ? h("div", { className: "buzz-control__message buzz-control__message--ok", role: "status", "aria-live": "polite" }, notice) : null,
          ),
        ),
      ),
    );
  }

  registry.register("buzz-control", BuzzControlPage);
})();
