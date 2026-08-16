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
  const SCHEDULE_OPTIONS = [
    { value: "every 60m", label: "Every hour" },
    { value: "every 180m", label: "Every 3 hours" },
    { value: "every 360m", label: "Every 6 hours" },
    { value: "every 720m", label: "Every 12 hours" },
    { value: "every 1440m", label: "Daily" },
    { value: "every 4320m", label: "Every 3 days" },
    { value: "every 10080m", label: "Weekly" },
  ];
  const RECOVERY_PHASES = new Set([
    "saving",
    "restoring",
    "adopting",
    "recovering",
    "applying",
    "verifying",
    "promoting",
    "rolling_back",
    "degraded",
    "blocked",
  ]);

  const CONFIG_ERROR_LABELS = {
    attestation_required: "Confirm that external maintenance is complete before adopting.",
    baseline_missing: "Adopt the verified running configuration before saving or applying changes.",
    browser_session_required: "Configuration changes require an authenticated browser session.",
    busy: "Another Buzz operation is in progress. Try again after it finishes.",
    degraded: "Rollback could not be verified. Follow the recovery runbook before continuing.",
    invalid_document: "The production environment needs manual repair before it can be edited here.",
    invalid_intent: "That confirmation expired or was already used. Review the action again.",
    manual_maintenance_required: "These changes require external maintenance and verified adoption.",
    no_pending_changes: "There are no saved changes to apply or restore.",
    origin_mismatch: "The configuration request did not come from this dashboard origin.",
    policy_changed: "The configuration changed after confirmation. Reload and review it again.",
    recovery_required: "An interrupted Buzz operation must be recovered before continuing.",
    relay_stopped: "Buzz is stopped. Use the separate Apply and start confirmation.",
    request_too_large: "The configuration request is too large.",
    runtime_unhealthy: "The running relay is not healthy enough to adopt.",
    stale_revision: "The configuration changed elsewhere. Your edits are still here; reload when ready.",
    unsafe_storage: "The protected configuration file or state directory is not safe.",
  };

  function api(path, options) {
    return SDK.fetchJSON(API_ROOT + path, options);
  }

  function cronApi() {
    return SDK.fetchJSON(CRON_URL);
  }

  function cronJobApi(job, action, options) {
    const suffix = action ? "/" + action : "";
    const profile = job && job.profile
      ? "?profile=" + encodeURIComponent(job.profile)
      : "";
    return SDK.fetchJSON(
      "/api/cron/jobs/" + encodeURIComponent(job.id) + suffix + profile,
      options,
    );
  }

  function errorMessage(error) {
    const raw = error && error.message ? String(error.message) : String(error || "Unknown error");
    const match = raw.match(/^\d{3}:\s*(.*)$/s);
    const body = match ? match[1] : raw;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") return parsed.detail;
      if (parsed && parsed.error && typeof parsed.error.code === "string") {
        return CONFIG_ERROR_LABELS[parsed.error.code] || "The Buzz configuration operation failed safely.";
      }
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

  function relayPresentation(relay) {
    const publicUrl = relay && relay.public_url;
    if (publicUrl) {
      try {
        const hostname = new window.URL(publicUrl).hostname.toLowerCase().replace(/\.$/, "");
        if (hostname.endsWith(".ts.net")) {
          return { label: "Tailscale configured", tone: "tailscale" };
        }
      } catch (_error) {
        // The backend owns URL validation; malformed display data uses the safe fallback.
      }
    }
    return { label: relay && relay.scope || "Local only", tone: "local" };
  }

  const RESULT_LABELS = {
    already_current: "Buzz is current and healthy.",
    baseline_missing: "Verify the running configuration, then adopt it before saving or applying changes.",
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

  function resultFailed(result) {
    return !!result && result !== "already_current" && result !== "updated";
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

  function scheduleMode(schedule) {
    if (!schedule) return "";
    if (schedule.status === "active") return "scheduled";
    if (schedule.status === "paused") return "manual";
    return "";
  }

  function scheduleExpression(schedule) {
    const value = schedule && schedule.schedule;
    if (value && value.kind === "interval") {
      const minutes = Number(value.minutes);
      if (Number.isInteger(minutes) && minutes > 0) return "every " + minutes + "m";
    }
    if (value && typeof value.expr === "string" && value.expr.trim()) {
      return value.expr.trim();
    }
    return schedule && typeof schedule.schedule_display === "string"
      ? schedule.schedule_display.trim()
      : "";
  }

  function scheduleCadenceLabel(schedule) {
    const value = schedule && schedule.schedule;
    const minutes = value && value.kind === "interval" ? Number(value.minutes) : 0;
    if (Number.isInteger(minutes) && minutes > 0) {
      if (minutes === 60) return "Every hour";
      if (minutes === 1440) return "Daily";
      if (minutes === 10080) return "Weekly";
      if (minutes % 1440 === 0) return "Every " + (minutes / 1440) + " days";
      if (minutes % 60 === 0) return "Every " + (minutes / 60) + " hours";
      return "Every " + minutes + " minutes";
    }
    return schedule && (schedule.schedule_display || (value && value.display)) || "—";
  }

  function initialDrafts(configuration) {
    const drafts = {};
    (configuration && configuration.fields || []).forEach(function (field) {
      if (field.disclosure !== "write_only") drafts[field.name] = field.value || "";
    });
    return drafts;
  }

  function buildConfigPatch(configuration, drafts) {
    const patch = {};
    (configuration && configuration.fields || []).forEach(function (field) {
      if (!field.editable) return;
      if (field.disclosure === "write_only") return;
      if ((drafts[field.name] || "") !== (field.value || "")) {
        patch[field.name] = drafts[field.name] || "";
      }
    });
    return patch;
  }

  function operationLabel(operation) {
    const phase = operation && operation.phase;
    return ({
      saved: "Changes saved; runtime unchanged",
      saving: "Saving protected configuration",
      applying: "Applying saved configuration",
      verifying: "Verifying Docker and HTTP health",
      promoting: "Promoting verified configuration",
      applied: "Running configuration verified",
      rolling_back: "Restoring the last applied configuration",
      rolled_back: "Rollback verified; saved changes remain pending",
      blocked: "Configuration operations blocked",
      degraded: "Manual recovery required",
      adopting: "Verifying externally maintained configuration",
      restoring: "Restoring the last applied configuration",
      recovering: "Verifying recovered configuration",
    }[phase] || "No configuration operation recorded");
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
    const [scheduleModeDraft, setScheduleModeDraft] = useState("");
    const [scheduleCadenceDraft, setScheduleCadenceDraft] = useState("");
    const scheduleModeDraftRef = useRef("");
    const scheduleCadenceDraftRef = useRef("");
    const scheduleModeTouchedRef = useRef(false);
    const scheduleCadenceTouchedRef = useRef(false);
    const scheduleJobIdentityRef = useRef("");
    const scheduleErrorSourceRef = useRef(null);
    const actionActiveRef = useRef(false);
    const generationRef = useRef(0);
    const [view, setView] = useState("dashboard");
    const [configuration, setConfiguration] = useState(null);
    const [drafts, setDrafts] = useState({});
    const [configBusy, setConfigBusy] = useState(null);
    const [configError, setConfigError] = useState(null);
    const [configNotice, setConfigNotice] = useState(null);
    const [intentReview, setIntentReview] = useState(null);
    const applyButtonRef = useRef(null);

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

    const acceptSchedule = useCallback(function (jobs, forceDrafts, preserveDrafts) {
      const next = managedSchedule(jobs);
      const identity = next && next.id
        ? String(next.profile || "") + ":" + String(next.id)
        : "";
      const identityChanged = identity !== scheduleJobIdentityRef.current;
      const mode = scheduleMode(next);
      const cadence = scheduleExpression(next);
      setSchedule(next);
      scheduleJobIdentityRef.current = identity;
      if (scheduleErrorSourceRef.current !== "mutation") setScheduleError(null);
      if (forceDrafts || (identityChanged && !preserveDrafts)) {
        scheduleModeDraftRef.current = mode;
        scheduleCadenceDraftRef.current = cadence;
        scheduleModeTouchedRef.current = false;
        scheduleCadenceTouchedRef.current = false;
        setScheduleModeDraft(mode);
        setScheduleCadenceDraft(cadence);
      } else {
        if (scheduleModeTouchedRef.current && scheduleModeDraftRef.current === mode) {
          scheduleModeTouchedRef.current = false;
        }
        if (scheduleCadenceTouchedRef.current && scheduleCadenceDraftRef.current === cadence) {
          scheduleCadenceTouchedRef.current = false;
        }
        if (!scheduleModeTouchedRef.current) {
          scheduleModeDraftRef.current = mode;
          setScheduleModeDraft(mode);
        }
        if (!scheduleCadenceTouchedRef.current) {
          scheduleCadenceDraftRef.current = cadence;
          setScheduleCadenceDraft(cadence);
        }
      }
      return next;
    }, []);

    const refreshSchedule = useCallback(function () {
      return cronApi()
        .then(function (jobs) {
          return acceptSchedule(jobs, false);
        })
        .catch(function (failure) {
          setScheduleError(errorMessage(failure));
          throw failure;
        });
    }, [acceptSchedule]);

    useEffect(function () {
      if (view !== "dashboard") return undefined;
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
    }, [view]);

    const configPatch = buildConfigPatch(configuration, drafts);
    const isConfigDirty = Object.keys(configPatch).length > 0;

    useEffect(function () {
      if (typeof window.addEventListener !== "function") return undefined;
      function warnBeforeUnload(event) {
        if (view !== "config" || !isConfigDirty) return;
        event.preventDefault();
        event.returnValue = "";
      }
      window.addEventListener("beforeunload", warnBeforeUnload);
      return function () { window.removeEventListener("beforeunload", warnBeforeUnload); };
    }, [view, isConfigDirty]);

    useEffect(function () {
      if (view !== "dashboard") return undefined;
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
    }, [acceptUpdates, view]);

    useEffect(function () {
      if (view !== "dashboard") return undefined;
      let active = true;
      let inFlight = false;
      function pollSchedule() {
        if (!active || inFlight || actionActiveRef.current) return;
        const generation = generationRef.current;
        inFlight = true;
        cronApi()
          .then(function (jobs) {
            if (active && !actionActiveRef.current && generation === generationRef.current) {
              acceptSchedule(jobs, false);
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
    }, [acceptSchedule, view]);

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

    function updateScheduleMode(nextMode) {
      if (busy) return;
      scheduleModeDraftRef.current = nextMode;
      scheduleModeTouchedRef.current = nextMode !== scheduleMode(schedule);
      setScheduleModeDraft(nextMode);
    }

    function updateScheduleCadence(nextCadence) {
      if (busy) return;
      scheduleCadenceDraftRef.current = nextCadence;
      scheduleCadenceTouchedRef.current = nextCadence !== scheduleExpression(schedule);
      setScheduleCadenceDraft(nextCadence);
    }

    function saveSchedule() {
      if (busy || actionActiveRef.current || !scheduleEditable) return;

      const cadenceChanged = scheduleCadenceTouchedRef.current
        && scheduleCadenceDraftRef.current
        && scheduleCadenceDraftRef.current !== scheduleExpression(schedule);
      const modeChanged = scheduleModeTouchedRef.current
        && scheduleModeDraftRef.current
        && scheduleModeDraftRef.current !== scheduleMode(schedule);
      if (!cadenceChanged && !modeChanged) return;

      const cadenceTarget = scheduleCadenceDraftRef.current;
      const modeTarget = scheduleModeDraftRef.current;

      actionActiveRef.current = true;
      generationRef.current += 1;
      setBusy("schedule");
      setNotice(null);
      scheduleErrorSourceRef.current = null;
      setScheduleError(null);

      let failedSetting = "";
      const appliedSettings = [];
      let request = Promise.resolve(schedule);
      if (cadenceChanged) {
        request = request.then(function (job) {
          failedSetting = "cadence";
          return cronJobApi(job, "", jsonOptions("PUT", {
            updates: { schedule: cadenceTarget },
          }));
        }).then(function (job) {
          appliedSettings.push("Cadence");
          acceptSchedule([job], false, true);
          return job;
        });
      }
      if (modeChanged) {
        request = request.then(function (job) {
          failedSetting = "update mode";
          return cronJobApi(job, modeTarget === "scheduled" ? "resume" : "pause", {
            method: "POST",
          });
        }).then(function (job) {
          appliedSettings.push("Update mode");
          acceptSchedule([job], false, true);
          return job;
        });
      }

      request
        .then(function (job) {
          acceptSchedule([job], true);
          setNotice("Buzz update settings saved.");
        })
        .catch(function (failure) {
          scheduleErrorSourceRef.current = "mutation";
          const detail = errorMessage(failure);
          if (appliedSettings.length) {
            setScheduleError(
              appliedSettings.join(" and ") + " saved, but " + failedSetting
              + " could not be saved: " + detail,
            );
          } else {
            setScheduleError(detail);
          }
        })
        .finally(function () { actionActiveRef.current = false; setBusy(null); });
    }

    function jsonOptions(method, payload) {
      return {
        method: method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      };
    }

    function acceptConfiguration(next, message) {
      setConfiguration(next);
      setDrafts(initialDrafts(next));
      setIntentReview(null);
      setConfigError(null);
      if (message) setConfigNotice(message);
      return next;
    }

    function openConfiguration() {
      if (busy || configBusy) return;
      actionActiveRef.current = true;
      generationRef.current += 1;
      setView("config");
      setConfigBusy("load");
      setConfigError(null);
      setConfigNotice(null);
      api("/config")
        .then(function (next) { acceptConfiguration(next); })
        .catch(function (failure) { setConfigError(errorMessage(failure)); })
        .finally(function () { setConfigBusy(null); });
    }

    function leaveConfiguration() {
      if (configBusy) return;
      if (isConfigDirty && !window.confirm("Discard your unsaved Buzz configuration edits?")) return;
      setView("dashboard");
      setConfiguration(null);
      setDrafts({});
      setIntentReview(null);
      setConfigError(null);
      setConfigNotice(null);
      actionActiveRef.current = false;
      generationRef.current += 1;
    }

    function updateDraft(name, value) {
      setDrafts(function (currentDrafts) {
        return Object.assign({}, currentDrafts, { [name]: value });
      });
    }

    function saveConfiguration() {
      if (!configuration || configBusy || !isConfigDirty) return;
      setConfigBusy("save");
      setConfigError(null);
      setConfigNotice(null);
      api("/config", jsonOptions("PUT", {
        base_revision: configuration.revision,
        replacements: configPatch,
      }))
        .then(function (next) {
          acceptConfiguration(next, next.wrote ? "Changes saved. Buzz was not restarted." : "No configuration values changed.");
        })
        .catch(function (failure) { setConfigError(errorMessage(failure)); })
        .finally(function () { setConfigBusy(null); });
    }

    function prepareApply() {
      if (!configuration || configBusy) return;
      const action = stopped ? "apply_start" : "apply";
      setConfigBusy("intent");
      setConfigError(null);
      api("/config/intent", jsonOptions("POST", {
        action: action,
        revision: configuration.revision,
      }))
        .then(function (intent) { setIntentReview(Object.assign({}, intent, { action: action })); })
        .catch(function (failure) { setConfigError(errorMessage(failure)); })
        .finally(function () { setConfigBusy(null); });
    }

    function cancelApply() {
      if (configBusy === "apply") return;
      setIntentReview(null);
      if (applyButtonRef.current && typeof applyButtonRef.current.focus === "function") {
        applyButtonRef.current.focus();
      }
    }

    function confirmApply() {
      if (!intentReview || configBusy) return;
      setConfigBusy("apply");
      setConfigError(null);
      api("/config/apply", jsonOptions("POST", {
        action: intentReview.action,
        intent: intentReview.intent,
        revision: intentReview.revision,
      }))
        .then(function (next) {
          const message = next.reconcile_result === "rolled_back"
            ? "Apply failed; the last applied runtime was restored. Your saved changes remain pending."
            : "Buzz configuration applied and verified.";
          acceptConfiguration(next, message);
        })
        .catch(function (failure) { setConfigError(errorMessage(failure)); setIntentReview(null); })
        .finally(function () { setConfigBusy(null); });
    }

    function restoreConfiguration() {
      if (!configuration || configBusy) return;
      if (!window.confirm("Restore the desired file from the last applied configuration? Buzz will not restart.")) return;
      setConfigBusy("restore");
      setConfigError(null);
      api("/config/intent", jsonOptions("POST", {
        action: "restore",
        revision: configuration.revision,
      }))
        .then(function (intent) {
          return api("/config/restore", jsonOptions("POST", {
            intent: intent.intent,
            revision: intent.revision,
          }));
        })
        .then(function (next) { acceptConfiguration(next, "Desired configuration restored. Buzz was not restarted."); })
        .catch(function (failure) { setConfigError(errorMessage(failure)); })
        .finally(function () { setConfigBusy(null); });
    }

    function adoptConfiguration() {
      if (!configuration || configBusy) return;
      if (!window.confirm("Confirm that external maintenance is complete and the current Buzz runtime is healthy. Adopt this exact configuration?")) return;
      setConfigBusy("adopt");
      setConfigError(null);
      api("/config/intent", jsonOptions("POST", {
        action: "adopt",
        revision: configuration.revision,
        attestation: "external_maintenance_complete",
      }))
        .then(function (intent) {
          return api("/config/adopt", jsonOptions("POST", {
            intent: intent.intent,
            revision: intent.revision,
          }));
        })
        .then(function (next) { acceptConfiguration(next, "Current healthy configuration adopted as the applied baseline."); })
        .catch(function (failure) { setConfigError(errorMessage(failure)); })
        .finally(function () { setConfigBusy(null); });
    }

    const statusView = statusPresentation(status);
    const updateView = updatePresentation(updates);
    const container = status && status.container;
    const probe = status && status.probe;
    const relay = status && status.relay;
    const relayView = relayPresentation(relay);
    const deployment = status && status.deployment;
    const current = updates && updates.current;
    const latest = updates && updates.latest;
    const updateState = updates && updates.state || {};
    const currentIdentity = current && (current.revision || current.image_id || current.digest);
    const latestIdentity = latest && (latest.revision || latest.image_id || latest.digest);
    const stopped = !!(container && !container.running);
    const scheduleLabel = !schedule ? "Checking" : ({
      active: "Scheduled",
      paused: "Manual only",
      missing: "Not installed",
      ambiguous: "Needs attention",
    }[schedule.status] || "Unknown");
    const scheduleEditable = !!(schedule
      && schedule.id
      && (schedule.status === "active" || schedule.status === "paused"));
    const scheduleDirty = scheduleEditable && (
      (scheduleModeTouchedRef.current && scheduleModeDraft !== scheduleMode(schedule))
      || (scheduleCadenceTouchedRef.current
        && scheduleCadenceDraft !== scheduleExpression(schedule))
    );
    const cadenceOptions = SCHEDULE_OPTIONS.slice();
    if (scheduleCadenceDraft && !cadenceOptions.some(function (option) {
      return option.value === scheduleCadenceDraft;
    })) {
      cadenceOptions.unshift({
        value: scheduleCadenceDraft,
        label: "Current cadence (" + scheduleCadenceLabel(schedule) + ")",
      });
    }
    const probeLabel = probe && probe.reachable
      ? "HTTP " + probe.status_code + (probe.response ? " · " + probe.response : "")
      : "Unreachable";

    if (view === "config") {
      const groups = [
        {
          id: "public_address",
          label: "Public address",
          description: "Where clients reach this Buzz deployment and its media endpoints.",
        },
        {
          id: "access_policy",
          label: "Access policy",
          description: "Authentication changes are high impact and receive an elevated Apply warning.",
        },
        {
          id: "owner_identity",
          label: "Owner identity",
          description: "The bootstrap owner is visible for verification but cannot be changed here.",
        },
      ];
      const baselineMissing = configuration && configuration.baseline_state === "baseline_missing";
      const operation = configuration && configuration.operation || {};
      const recoveryRequired = RECOVERY_PHASES.has(operation.phase);
      const mutationDisabled = !!configBusy || !!baselineMissing || recoveryRequired;

      function renderField(field) {
        const inputId = "buzz-config-" + field.name.toLowerCase().replace(/_/g, "-");
        const writeOnly = field.disclosure === "write_only";
        const readOnly = !field.editable;
        return h("div", { className: "buzz-control__config-field", key: field.name },
          h("div", { className: "buzz-control__config-label" },
            h("label", { htmlFor: inputId }, field.label),
            h("code", null, field.name),
          ),
          h("div", { className: "buzz-control__config-control" },
            writeOnly
              ? h("div", { className: "buzz-control__protected-control" },
                  h("span", { className: "buzz-control__configured-state" },
                    field.configured ? "Configured outside Hermes" : "Not configured",
                  ),
                )
              : field.kind === "bool" && !readOnly
                ? h("select", {
                    id: inputId,
                    value: drafts[field.name] || "false",
                    onChange: function (event) { updateDraft(field.name, event.target.value); },
                    disabled: mutationDisabled,
                    "aria-label": "Edit " + field.name,
                  },
                    h("option", { value: "true" }, "Enabled"),
                    h("option", { value: "false" }, "Disabled"),
                  )
                : h("input", {
                    id: inputId,
                    type: "text",
                    value: drafts[field.name] || "",
                    onChange: function (event) { updateDraft(field.name, event.target.value); },
                    readOnly: readOnly,
                    disabled: mutationDisabled && !readOnly,
                    "aria-label": (readOnly ? "Current " : "Edit ") + field.name,
                  }),
            h("div", { className: "buzz-control__field-meta" },
              h("span", null, field.impact.replace(/_/g, " ")),
              writeOnly ? h("span", null, "Protected") : null,
              readOnly ? h("span", null, "Read only") : null,
            ),
          ),
        );
      }

      function modalKeyDown(event) {
        if (event.key === "Escape") {
          event.preventDefault();
          if (configBusy !== "apply") cancelApply();
          return;
        }
        if (event.key !== "Tab" || !event.currentTarget.querySelectorAll) return;
        const focusable = Array.from(event.currentTarget.querySelectorAll("button:not([disabled])"));
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && event.target === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && event.target === last) {
          event.preventDefault();
          first.focus();
        }
      }

      return h("div", { className: "buzz-control buzz-control--config" },
        h("div", { className: "buzz-control__config-header" },
          h(Button, {
            variant: "outline",
            onClick: leaveConfiguration,
            disabled: !!configBusy,
          }, "Back to Buzz"),
          h("div", null,
            h("div", { className: "buzz-control__eyebrow" }, "PROTECTED PRODUCTION CONFIGURATION"),
            h("h1", null, "Configure Buzz"),
            h("p", null, "Manage the stable public-address and access-policy settings. Save and Apply remain separate reviewed operations."),
          ),
        ),

        h("div", { className: "buzz-control__scope-note", role: "note" },
          h("strong", null, "Advanced deployment settings stay outside Hermes."),
          h("p", null, "Buzz Control preserves secrets, storage, database, ports, image, and unknown assignments without returning or editing them in the browser."),
        ),

        configBusy === "load" && !configuration
          ? h(Card, { className: "buzz-control__card" }, h(CardContent, null, "Loading protected configuration…"))
          : null,

        baselineMissing ? h("div", { className: "buzz-control__message buzz-control__message--warning", role: "status" },
          h("strong", null, "Applied baseline required"),
          h("p", null, "This installation will not save, apply, or recreate the relay until you verify the current runtime and explicitly adopt this configuration."),
        ) : null,

        recoveryRequired ? h("div", { className: "buzz-control__message buzz-control__message--warning", role: "status" },
          h("strong", null, "Configuration recovery required"),
          h("p", null, "Verify the externally maintained runtime, then use recovery adoption before making more changes."),
        ) : null,

        configuration ? h(Card, { className: "buzz-control__card buzz-control__config-status" },
          h(CardHeader, null, h(CardTitle, null, "Configuration state")),
          h(CardContent, null,
            h("dl", { className: "buzz-control__details" },
              h(DetailRow, { label: "Desired revision", value: shortIdentity(configuration.revision) }),
              h(DetailRow, { label: "Applied baseline", value: configuration.baseline_state === "established" ? "Established" : "Missing", mono: false }),
              h(DetailRow, { label: "Runtime relationship", value: configuration.pending ? "Saved changes pending" : "Desired matches applied", mono: false }),
              h(DetailRow, { label: "Last operation", value: operationLabel(operation), mono: false }),
            ),
          ),
        ) : null,

        configuration ? groups.map(function (group) {
          const fields = configuration.fields.filter(function (field) { return field.group === group.id; });
          if (!fields.length) return null;
          return h(Card, { className: "buzz-control__card buzz-control__config-group", key: group.id },
            h(CardHeader, null,
              h(CardTitle, null, group.label),
              h("p", null, group.description),
            ),
            h(CardContent, null, fields.map(renderField)),
          );
        }) : null,

        configuration ? h("div", { className: "buzz-control__config-actions" },
          h(Button, {
            onClick: saveConfiguration,
            disabled: mutationDisabled || !isConfigDirty,
            "aria-busy": configBusy === "save",
          }, configBusy === "save" ? "Saving…" : "Save changes"),
          h(Button, {
            ref: applyButtonRef,
            variant: "outline",
            onClick: prepareApply,
            disabled: !!configBusy || recoveryRequired || isConfigDirty || !configuration.pending || !configuration.automatic_apply_allowed,
          }, stopped ? "Review Apply and start" : "Review Apply"),
          h(Button, {
            variant: "outline",
            onClick: restoreConfiguration,
            disabled: !!configBusy || recoveryRequired || isConfigDirty || !configuration.pending || baselineMissing,
          }, "Restore last applied"),
          (baselineMissing || recoveryRequired || (configuration.pending && !configuration.automatic_apply_allowed))
            ? h(Button, {
                variant: "outline",
                onClick: adoptConfiguration,
                disabled: !!configBusy || isConfigDirty,
              }, configBusy === "adopt"
                ? "Verifying and adopting…"
                : recoveryRequired
                  ? "Verify and recover configuration"
                  : "Adopt current healthy configuration")
            : null,
        ) : null,

        isConfigDirty ? h("p", { className: "buzz-control__dirty-note" }, "Unsaved edits stay in this browser view until you save, discard, or reload.") : null,
        configError ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, configError) : null,
        configNotice ? h("div", { className: "buzz-control__message buzz-control__message--ok", role: "status", "aria-live": "polite" }, configNotice) : null,

        intentReview ? h("div", { className: "buzz-control__modal-backdrop" },
          h("div", {
            className: "buzz-control__modal",
            role: "dialog",
            "aria-modal": "true",
            "aria-labelledby": "buzz-config-review-title",
            onKeyDown: modalKeyDown,
          },
            h("h2", { id: "buzz-config-review-title" }, intentReview.action === "apply_start" ? "Apply and start Buzz?" : "Apply saved configuration?"),
            h("p", null, "Only the relay will be recreated, pinned to its current immutable image. Save and Apply remain separate."),
            intentReview.review && intentReview.review.high_impact
              ? h("div", { className: "buzz-control__message buzz-control__message--warning" }, "This change affects relay startup behavior. Review it carefully.")
              : null,
            h("h3", null, "Changed fields"),
            h("ul", null, (intentReview.review && intentReview.review.changed_keys || []).map(function (name) {
              return h("li", { key: name }, h("code", null, name));
            })),
            h("p", { className: "buzz-control__modal-impact" }, "Impact: " + (intentReview.review && intentReview.review.impact_classes || []).join(", ").replace(/_/g, " ")),
            h("div", { className: "buzz-control__modal-actions" },
              h(Button, {
                variant: "outline",
                onClick: cancelApply,
                autoFocus: true,
                disabled: configBusy === "apply",
              }, "Cancel"),
              h(Button, { onClick: confirmApply, disabled: configBusy === "apply" }, configBusy === "apply" ? "Applying and verifying…" : "Confirm Apply"),
            ),
          ),
        ) : null,
      );
    }

    return h("div", { className: "buzz-control" },
      h("div", { className: "buzz-control__hero" },
        h("div", null,
          h("div", { className: "buzz-control__eyebrow" }, "LOCAL RELAY OPERATIONS"),
          h("h1", null, "Buzz"),
          h("p", null,
            "Verify the relay, review the last image check, and update the running service without leaving Hermes."
          ),
        ),
        h("div", { className: "buzz-control__hero-actions" },
          h(Badge, {
            className: "buzz-control__status buzz-control__status--" + statusView.tone,
          }, h("span", { className: "buzz-control__status-dot", "aria-hidden": "true" }), statusView.label),
          h(Button, {
            variant: "outline",
            onClick: openConfiguration,
            disabled: !!busy,
          }, "Configure Buzz"),
        ),
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
            h("div", {
              className: "buzz-control__relay-primary buzz-control__relay-primary--" + relayView.tone,
            },
              h("span", null, relayView.label),
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
                h(DetailRow, { label: "Latest check", value: formatDate(updateState.last_check_at || (updates && updates.checked_at)) }),
                h(DetailRow, { label: "Trigger", value: updateState.trigger, mono: false }),
                h(DetailRow, { label: "Last successful update", value: formatDate(updateState.last_successful_update_at) }),
              ),
              resultFailed(updateState.result) && updateState.error
                ? h("div", { className: "buzz-control__message buzz-control__message--error", role: "alert" }, updateState.error)
                : null,
            ),

            h("div", { className: "buzz-control__schedule" },
              h("div", null,
                h("h3", null, "Managed schedule"),
                h("p", null, "Hermes checks for a new relay image without using an agent."),
              ),
              h(Badge, null, scheduleLabel),
              h("div", { className: "buzz-control__schedule-controls" },
                h("fieldset", {
                  className: "buzz-control__schedule-mode",
                  disabled: !scheduleEditable || !!busy,
                },
                  h("legend", null, "Update mode"),
                  h("div", { className: "buzz-control__schedule-mode-options" },
                    h(Button, {
                      type: "button",
                      variant: scheduleModeDraft === "scheduled" ? "default" : "outline",
                      onClick: function () { updateScheduleMode("scheduled"); },
                      disabled: !scheduleEditable || !!busy,
                      "aria-pressed": scheduleModeDraft === "scheduled",
                    }, "Scheduled"),
                    h(Button, {
                      type: "button",
                      variant: scheduleModeDraft === "manual" ? "default" : "outline",
                      onClick: function () { updateScheduleMode("manual"); },
                      disabled: !scheduleEditable || !!busy,
                      "aria-pressed": scheduleModeDraft === "manual",
                    }, "Manual only"),
                  ),
                ),
                h("label", { className: "buzz-control__schedule-cadence" },
                  h("span", null, "Cadence"),
                  h("select", {
                    value: scheduleCadenceDraft,
                    onChange: function (event) { updateScheduleCadence(event.target.value); },
                    disabled: !scheduleEditable || !!busy || scheduleModeDraft !== "scheduled",
                    "aria-label": "Update cadence",
                  }, cadenceOptions.map(function (option) {
                    return h("option", { value: option.value, key: option.value }, option.label);
                  })),
                ),
                h(Button, {
                  variant: "outline",
                  onClick: saveSchedule,
                  disabled: !scheduleDirty || !!busy,
                  "aria-busy": busy === "schedule",
                }, busy === "schedule" ? "Saving…" : "Save"),
              ),
              h("dl", { className: "buzz-control__details" },
                h(DetailRow, { label: "Cadence", value: scheduleCadenceLabel(schedule) }),
                h(DetailRow, { label: "Last run", value: formatDate(schedule && schedule.last_run_at) }),
                h(DetailRow, { label: "Next run", value: formatDate(schedule && schedule.next_run_at) }),
              ),
              !scheduleEditable
                ? h("p", { className: "buzz-control__schedule-unavailable" }, "Create or repair it in Hermes Cron before changing update settings here.")
                : scheduleModeDraft === "manual"
                  ? h("p", { className: "buzz-control__schedule-hint" }, "Automatic checks are paused. Use Update Buzz whenever you want to check manually.")
                  : null,
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
