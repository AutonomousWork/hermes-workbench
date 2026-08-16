"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const bundle = fs.readFileSync(
  path.join(__dirname, "..", "dashboard", "dist", "index.js"),
  "utf8",
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise(function (resolvePromise, rejectPromise) {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function createHarness(statusRequests, updateRequests, actionRequests, cronRequests, configRequests) {
  const state = [];
  const refs = [];
  const effects = [];
  const intervals = [];
  const requests = [];
  const confirmations = [];
  const eventListeners = {};
  let component;
  let hookCursor = 0;

  function createElement(type, props) {
    const children = Array.prototype.slice.call(arguments, 2);
    return {
      type: type,
      props: Object.assign({}, props || {}, { children: children }),
    };
  }

  const hooks = {
    useCallback: function (callback) {
      hookCursor += 1;
      return callback;
    },
    useEffect: function (callback) {
      hookCursor += 1;
      effects.push(callback);
    },
    useRef: function (initial) {
      const index = hookCursor;
      hookCursor += 1;
      if (!refs[index]) refs[index] = { current: initial };
      return refs[index];
    },
    useState: function (initial) {
      const index = hookCursor;
      hookCursor += 1;
      if (!(index in state)) state[index] = initial;
      return [state[index], function (next) {
        state[index] = typeof next === "function" ? next(state[index]) : next;
      }];
    },
  };

  const components = {
    Badge: "Badge",
    Button: "Button",
    Card: "Card",
    CardContent: "CardContent",
    CardHeader: "CardHeader",
    CardTitle: "CardTitle",
  };
  cronRequests = cronRequests || [];
  configRequests = configRequests || [];
  const window = {
    __HERMES_PLUGINS__: {
      register: function (_name, registeredComponent) {
        component = registeredComponent;
      },
    },
    __HERMES_PLUGIN_SDK__: {
      React: { createElement: createElement },
      components: components,
      fetchJSON: function (url, options) {
        requests.push({ options: options, url: url });
        let queue;
        if (options && options.method && options.method !== "GET") queue = actionRequests;
        else if (url === "/api/cron/jobs?profile=all") queue = cronRequests;
        else if (url.endsWith("/config")) queue = configRequests;
        else if (url.endsWith("/updates")) queue = updateRequests;
        else queue = statusRequests;
        assert.ok(queue.length, "unexpected request: " + url);
        return queue.shift().promise;
      },
      hooks: hooks,
    },
    clearInterval: function () {},
    addEventListener: function (name, callback) { eventListeners[name] = callback; },
    confirm: function (message) { confirmations.push(message); return true; },
    removeEventListener: function (name) { delete eventListeners[name]; },
    setInterval: function (callback, milliseconds) {
      intervals.push({ callback: callback, milliseconds: milliseconds });
      return intervals.length;
    },
  };

  vm.runInNewContext(bundle, { window: window });
  assert.equal(typeof component, "function");
  let tree = component();

  function button(label) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (!node || typeof node !== "object") continue;
      if (node.type === "Button" && node.props.children.includes(label)) return node;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    throw new Error("button not found: " + label);
  }

  function detailValue(label) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (!node || typeof node !== "object") continue;
      if (
        typeof node.type === "function"
        && node.type.name === "DetailRow"
        && node.props.label === label
      ) return node.props.value;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    return undefined;
  }

  function nodeWithClassName(className) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (!node || typeof node !== "object") continue;
      if (node.props && node.props.className === className) return node;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    return undefined;
  }

  function input(label) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (!node || typeof node !== "object") continue;
      if (node.type === "input" && node.props["aria-label"] === label) return node;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    throw new Error("input not found: " + label);
  }

  function select(label) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (!node || typeof node !== "object") continue;
      if (node.type === "select" && node.props["aria-label"] === label) return node;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    throw new Error("select not found: " + label);
  }

  function textContent() {
    const text = [];
    const pending = [tree];
    while (pending.length) {
      const node = pending.shift();
      if (Array.isArray(node)) { pending.push.apply(pending, node); continue; }
      if (typeof node === "string" || typeof node === "number") {
        text.push(String(node));
        continue;
      }
      if (!node || typeof node !== "object") continue;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    return text.join(" ");
  }

  return {
    button: button,
    confirmations: confirmations,
    detailValue: detailValue,
    intervals: intervals,
    nodeWithClassName: nodeWithClassName,
    input: input,
    requests: requests,
    rerender: function () { hookCursor = 0; tree = component(); },
    runEffects: function () { return effects.map(function (effect) { return effect(); }); },
    select: select,
    state: state,
    textContent: textContent,
  };
}

async function drainPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function pollsHealthAndUpdatesAtDifferentIntervals() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const harness = createHarness([status], [updates], [], [cron]);

  harness.runEffects();
  assert.equal(harness.requests.length, 3);
  assert.equal(harness.intervals[0].milliseconds, 15000);
  assert.equal(harness.intervals[1].milliseconds, 300000);
  assert.equal(harness.intervals[2].milliseconds, 60000);
  assert.equal(harness.requests[2].url, "/api/cron/jobs?profile=all");

  status.resolve({ healthy: true, source: "status poll" });
  updates.resolve({ update_available: false, state: {}, errors: [] });
  cron.resolve([{ name: "buzz-control-image-update", enabled: true, state: "scheduled" }]);
  await drainPromises();

  assert.equal(harness.state[0].source, "status poll");
  assert.equal(harness.state[1].update_available, false);
  assert.equal(harness.state[6].state, "scheduled");
}

async function successfulStatusPollClearsTransientError() {
  const failed = deferred();
  const successful = deferred();
  const updates = deferred();
  const cron = deferred();
  const harness = createHarness([failed, successful], [updates], [], [cron]);

  harness.runEffects();
  failed.reject(new Error('503: {"detail":"temporarily offline"}'));
  updates.resolve({ update_available: false, state: {}, errors: [] });
  cron.resolve([]);
  await drainPromises();
  assert.equal(harness.state[4], "temporarily offline");

  harness.intervals[0].callback();
  successful.resolve({ healthy: true, source: "recovered poll" });
  await drainPromises();

  assert.equal(harness.state[0].source, "recovered poll");
  assert.equal(harness.state[4], null);
}

async function stalePollsCannotOverwriteUpdateResult() {
  const staleStatus = deferred();
  const staleUpdates = deferred();
  const action = deferred();
  const cron = deferred();
  const harness = createHarness([staleStatus], [staleUpdates], [action], [cron]);

  harness.runEffects();
  cron.resolve([]);
  harness.button("Update Buzz").props.onClick();
  harness.intervals[0].callback();
  harness.intervals[1].callback();
  harness.intervals[2].callback();
  assert.equal(harness.requests.length, 4, "polling should pause during an update");

  action.resolve({
    message: "Updated.",
    status: { healthy: true, source: "update action" },
    updates: { update_available: false, source: "update action", errors: [] },
  });
  await drainPromises();
  assert.equal(harness.state[0].source, "update action");
  assert.equal(harness.state[1].source, "update action");

  staleStatus.resolve({ healthy: false, source: "stale status" });
  staleUpdates.resolve({ update_available: true, source: "stale updates", errors: [] });
  await drainPromises();
  assert.equal(harness.state[0].source, "update action");
  assert.equal(harness.state[1].source, "update action");
  assert.equal(harness.state[3], "Updated.");
}

async function manualRefreshLoadsBothResources() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const harness = createHarness([status], [updates], [], [cron]);

  harness.button("Refresh all").props.onClick();
  status.resolve({ healthy: true, source: "manual status" });
  updates.resolve({ update_available: true, source: "manual updates", state: {}, errors: [] });
  cron.resolve([]);
  await drainPromises();

  assert.equal(harness.state[0].source, "manual status");
  assert.equal(harness.state[1].source, "manual updates");
  assert.equal(harness.state[3], "Buzz status, updates, and schedule refreshed.");
}

async function stalePollsCannotOverwriteRefreshResult() {
  const staleStatus = deferred();
  const freshStatus = deferred();
  const staleUpdates = deferred();
  const freshUpdates = deferred();
  const staleCron = deferred();
  const freshCron = deferred();
  const harness = createHarness(
    [staleStatus, freshStatus],
    [staleUpdates, freshUpdates],
    [],
    [staleCron, freshCron],
  );

  harness.runEffects();
  harness.button("Refresh all").props.onClick();
  harness.intervals.forEach(function (interval) { interval.callback(); });
  assert.equal(harness.requests.length, 6, "polling should pause during a manual refresh");

  freshStatus.resolve({ healthy: true, source: "manual status" });
  freshUpdates.resolve({ update_available: false, source: "manual updates", state: {}, errors: [] });
  freshCron.resolve([{ name: "buzz-control-image-update", schedule_display: "every 12h" }]);
  await drainPromises();
  assert.equal(harness.state[6].schedule_display, "every 12h");

  staleStatus.resolve({ healthy: false, source: "stale status" });
  staleUpdates.resolve({ update_available: true, source: "stale updates", state: {}, errors: [] });
  staleCron.resolve([{ name: "buzz-control-image-update", schedule_display: "every 30m" }]);
  await drainPromises();

  assert.equal(harness.state[0].source, "manual status");
  assert.equal(harness.state[1].source, "manual updates");
  assert.equal(harness.state[6].schedule_display, "every 12h");
}

async function stoppedRelayUsesExplicitStartCopy() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const action = deferred();
  const harness = createHarness([status], [updates], [action], [cron]);

  harness.runEffects();
  status.resolve({ healthy: false, container: { running: false } });
  updates.resolve({ update_available: null, state: {}, errors: [] });
  cron.resolve([]);
  await drainPromises();
  harness.rerender();

  harness.button("Update and start Buzz").props.onClick();
  assert.match(harness.confirmations[0], /will start the relay/i);
}

async function successfulLatestCheckHidesHistoricalFailure() {
  const cases = [
    {
      result: "updated",
      lastCheck: "latest-check-from-state",
      expectedMessage: /Buzz was updated and is healthy/i,
    },
    {
      result: "already_current",
      lastCheck: null,
      expectedMessage: /Buzz is current and healthy/i,
    },
  ];

  for (const testCase of cases) {
    const status = deferred();
    const updates = deferred();
    const cron = deferred();
    const harness = createHarness([status], [updates], [], [cron]);

    harness.runEffects();
    status.resolve({ healthy: true, container: { running: true } });
    updates.resolve({
      update_available: false,
      checked_at: "latest-check-fallback",
      errors: [],
      state: {
        result: testCase.result,
        error: null,
        last_check_at: testCase.lastCheck,
        latest_failure_result: "verification_failed",
        latest_failure_error: "An older verification failed.",
      },
    });
    cron.resolve([]);
    await drainPromises();
    harness.rerender();

    assert.equal(
      harness.detailValue("Latest check"),
      testCase.lastCheck || "latest-check-fallback",
    );
    assert.equal(harness.detailValue("Latest failure"), undefined);
    assert.doesNotMatch(harness.textContent(), /older verification failed/i);
    assert.match(harness.textContent(), testCase.expectedMessage);
  }
}

async function failedLatestCheckShowsOnlyTheCurrentError() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const harness = createHarness([status], [updates], [], [cron]);

  harness.runEffects();
  status.resolve({ healthy: true, container: { running: true } });
  updates.resolve({
    update_available: null,
    errors: [],
    state: {
      result: "pull_failed",
      error: "The current image pull failed.",
      latest_failure_result: "verification_failed",
      latest_failure_error: "An older verification failed.",
    },
  });
  cron.resolve([]);
  await drainPromises();
  harness.rerender();

  assert.equal(harness.detailValue("Latest failure"), undefined);
  assert.match(harness.textContent(), /current image pull failed/i);
  assert.doesNotMatch(harness.textContent(), /older verification failed/i);
}

async function baselineMissingReceiptOffersAdoptionGuidance() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const harness = createHarness([status], [updates], [], [cron]);

  harness.runEffects();
  status.resolve({ healthy: true, container: { running: true } });
  updates.resolve({
    update_available: false,
    errors: [],
    state: { result: "baseline_missing" },
  });
  cron.resolve([]);
  await drainPromises();
  harness.rerender();

  assert.match(harness.textContent(), /verify the running configuration.*adopt it before saving or applying/i);
  assert.doesNotMatch(harness.textContent(), /no reliable saved update result/i);
}

function configFixture(overrides) {
  return Object.assign({
    revision: "opaque_revision_abcdefghijklmnopqrstuvwxyz",
    baseline_state: "established",
    pending: false,
    changed_keys: [],
    impact_classes: [],
    automatic_apply_allowed: false,
    operation: {},
    fields: [
      {
        name: "BUZZ_DOMAIN",
        label: "Buzz domain",
        group: "public_address",
        configured: true,
        disclosure: "plain",
        impact: "relay_eligible",
        editable: true,
        kind: "text",
        value: "first.test",
      },
      {
        name: "BUZZ_REQUIRE_RELAY_MEMBERSHIP",
        label: "Require relay membership",
        group: "access_policy",
        configured: true,
        disclosure: "plain",
        impact: "relay_high_impact",
        editable: true,
        kind: "bool",
        value: "true",
      },
      {
        name: "RELAY_OWNER_PUBKEY",
        label: "Relay owner public key",
        group: "owner_identity",
        configured: true,
        disclosure: "plain",
        impact: "manual_maintenance",
        editable: false,
        kind: "text",
        value: "owner-public-key",
      },
    ],
  }, overrides || {});
}

async function opensProtectedEditorAndPausesDashboardPolling() {
  const status = deferred();
  const updates = deferred();
  const cron = deferred();
  const config = deferred();
  const harness = createHarness([status], [updates], [], [cron], [config]);

  harness.runEffects();
  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture());
  await drainPromises();
  harness.rerender();

  assert.match(harness.textContent(), /Configure Buzz/);
  assert.match(harness.textContent(), /Public address/);
  assert.match(harness.textContent(), /Access policy/);
  assert.match(harness.textContent(), /Owner identity/);
  assert.doesNotMatch(harness.textContent(), /Postgres password/);
  assert.doesNotMatch(harness.textContent(), /Unknown \/ local extensions/);
  assert.equal(
    harness.select("Edit BUZZ_REQUIRE_RELAY_MEMBERSHIP").props.value,
    "true",
  );
  const requestCount = harness.requests.length;
  harness.intervals.forEach(function (interval) { interval.callback(); });
  assert.equal(harness.requests.length, requestCount, "dashboard polling must pause in the editor");
}

async function saveStagesOnlyManagedSettingsWithoutCallingApply() {
  const config = deferred();
  const save = deferred();
  const harness = createHarness([], [], [save], [], [config]);

  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture());
  await drainPromises();
  harness.rerender();
  harness.input("Edit BUZZ_DOMAIN").props.onChange({ target: { value: "second.test" } });
  harness.rerender();
  harness.button("Save changes").props.onClick();

  const request = harness.requests[harness.requests.length - 1];
  assert.equal(request.url, "/api/plugins/buzz-control/config");
  assert.equal(request.options.method, "PUT");
  const body = JSON.parse(request.options.body);
  assert.deepEqual(body.replacements, { BUZZ_DOMAIN: "second.test" });
  assert.equal(harness.requests.some(function (item) { return item.url.endsWith("/config/apply"); }), false);

  save.resolve(configFixture({
    pending: true,
    changed_keys: ["BUZZ_DOMAIN"],
    impact_classes: ["relay_eligible"],
    automatic_apply_allowed: true,
    wrote: true,
    fields: configFixture().fields.map(function (field) {
      return field.name === "BUZZ_DOMAIN" ? Object.assign({}, field, { value: "second.test" }) : field;
    }),
  }));
  await drainPromises();
  harness.rerender();
  assert.match(harness.textContent(), /Changes saved. Buzz was not restarted/);
}

async function cancellingApplyReviewNeverSubmitsRuntimeMutation() {
  const config = deferred();
  const intent = deferred();
  const harness = createHarness([], [], [intent], [], [config]);
  const pending = configFixture({
    pending: true,
    changed_keys: ["BUZZ_DOMAIN"],
    impact_classes: ["relay_eligible"],
    automatic_apply_allowed: true,
  });

  harness.button("Configure Buzz").props.onClick();
  config.resolve(pending);
  await drainPromises();
  harness.rerender();
  harness.button("Review Apply").props.onClick();
  intent.resolve({
    intent: "operation-token-must-not-render",
    action: "apply",
    revision: pending.revision,
    review: { changed_keys: ["BUZZ_DOMAIN"], impact_classes: ["relay_eligible"], high_impact: false },
  });
  await drainPromises();
  harness.rerender();

  assert.doesNotMatch(harness.textContent(), /operation-token-must-not-render/);
  harness.button("Cancel").props.onClick();
  harness.rerender();
  assert.equal(harness.requests.filter(function (item) { return item.url.endsWith("/config/apply"); }).length, 0);
}

async function confirmedApplySendsBoundIntentAndKeepsEditorMounted() {
  const config = deferred();
  const intent = deferred();
  const apply = deferred();
  const harness = createHarness([], [], [intent, apply], [], [config]);
  const pending = configFixture({
    pending: true,
    changed_keys: ["BUZZ_DOMAIN"],
    impact_classes: ["relay_eligible"],
    automatic_apply_allowed: true,
  });

  harness.button("Configure Buzz").props.onClick();
  config.resolve(pending);
  await drainPromises();
  harness.rerender();
  harness.button("Review Apply").props.onClick();
  intent.resolve({
    intent: "bound-operation-token",
    action: "apply",
    revision: pending.revision,
    review: { changed_keys: ["BUZZ_DOMAIN"], impact_classes: ["relay_eligible"], high_impact: false },
  });
  await drainPromises();
  harness.rerender();
  harness.button("Confirm Apply").props.onClick();
  harness.rerender();

  const request = harness.requests[harness.requests.length - 1];
  assert.equal(request.url, "/api/plugins/buzz-control/config/apply");
  assert.deepEqual(JSON.parse(request.options.body), {
    action: "apply",
    intent: "bound-operation-token",
    revision: pending.revision,
  });
  assert.equal(harness.button("Back to Buzz").props.disabled, true);
  harness.button("Back to Buzz").props.onClick();
  harness.rerender();
  assert.match(harness.textContent(), /Configure Buzz/);

  apply.resolve(configFixture({ pending: false, reconcile_result: "applied" }));
  await drainPromises();
  harness.rerender();
  assert.match(harness.textContent(), /Buzz configuration applied and verified/);
}

async function applyingConfigurationKeepsReviewModalOpenUntilSettled() {
  const config = deferred();
  const intent = deferred();
  const apply = deferred();
  const harness = createHarness([], [], [intent, apply], [], [config]);
  const pending = configFixture({
    pending: true,
    changed_keys: ["BUZZ_DOMAIN"],
    impact_classes: ["relay_eligible"],
    automatic_apply_allowed: true,
  });

  harness.button("Configure Buzz").props.onClick();
  config.resolve(pending);
  await drainPromises();
  harness.rerender();
  harness.button("Review Apply").props.onClick();
  intent.resolve({
    intent: "bound-operation-token",
    action: "apply",
    revision: pending.revision,
    review: { changed_keys: ["BUZZ_DOMAIN"], impact_classes: ["relay_eligible"], high_impact: false },
  });
  await drainPromises();
  harness.rerender();
  harness.button("Confirm Apply").props.onClick();
  harness.rerender();

  const modal = harness.nodeWithClassName("buzz-control__modal");
  let escapePrevented = false;
  modal.props.onKeyDown({
    key: "Escape",
    preventDefault: function () { escapePrevented = true; },
  });
  harness.button("Cancel").props.onClick();
  harness.rerender();

  assert.equal(escapePrevented, true);
  assert.equal(harness.button("Cancel").props.disabled, true);
  assert.match(harness.textContent(), /Applying and verifying/);
  assert.ok(harness.nodeWithClassName("buzz-control__modal"));

  apply.resolve(configFixture({ pending: false, reconcile_result: "applied" }));
  await drainPromises();
  harness.rerender();

  assert.equal(harness.nodeWithClassName("buzz-control__modal"), undefined);
  assert.match(harness.textContent(), /Buzz configuration applied and verified/);
}

async function backIsDisabledWhileSaveIsInFlight() {
  const config = deferred();
  const save = deferred();
  const harness = createHarness([], [], [save], [], [config]);

  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture());
  await drainPromises();
  harness.rerender();
  harness.input("Edit BUZZ_DOMAIN").props.onChange({ target: { value: "second.test" } });
  harness.rerender();
  harness.button("Save changes").props.onClick();
  harness.rerender();

  assert.equal(harness.button("Back to Buzz").props.disabled, true);
  harness.button("Back to Buzz").props.onClick();
  harness.rerender();
  assert.match(harness.textContent(), /Configure Buzz/);

  save.resolve(configFixture({ pending: true, wrote: true }));
  await drainPromises();
}

async function baselineMissingBlocksEditingAndOffersExplicitAdoption() {
  const config = deferred();
  const harness = createHarness([], [], [], [], [config]);

  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture({ baseline_state: "baseline_missing", pending: true }));
  await drainPromises();
  harness.rerender();

  assert.equal(harness.button("Save changes").props.disabled, true);
  assert.match(harness.textContent(), /Applied baseline required/);
  harness.button("Adopt current healthy configuration");
}

async function degradedOperationOffersRecoveryInsteadOfMoreMutation() {
  const config = deferred();
  const harness = createHarness([], [], [], [], [config]);

  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture({
    pending: true,
    changed_keys: ["BUZZ_DOMAIN"],
    impact_classes: ["relay_eligible"],
    automatic_apply_allowed: true,
    operation: { phase: "degraded", outcome: "rollback_unverified" },
  }));
  await drainPromises();
  harness.rerender();

  assert.match(harness.textContent(), /Configuration recovery required/);
  assert.equal(harness.button("Save changes").props.disabled, true);
  assert.equal(harness.button("Review Apply").props.disabled, true);
  assert.equal(harness.button("Restore last applied").props.disabled, true);
  harness.button("Verify and recover configuration");
}

async function dirtyBackRequiresConfirmation() {
  const config = deferred();
  const harness = createHarness([], [], [], [], [config]);

  harness.button("Configure Buzz").props.onClick();
  config.resolve(configFixture());
  await drainPromises();
  harness.rerender();
  harness.input("Edit BUZZ_DOMAIN").props.onChange({ target: { value: "dirty.test" } });
  harness.rerender();
  harness.button("Back to Buzz").props.onClick();

  assert.match(harness.confirmations[harness.confirmations.length - 1], /Discard your unsaved/i);
}

async function main() {
  await pollsHealthAndUpdatesAtDifferentIntervals();
  await successfulStatusPollClearsTransientError();
  await stalePollsCannotOverwriteUpdateResult();
  await manualRefreshLoadsBothResources();
  await stalePollsCannotOverwriteRefreshResult();
  await stoppedRelayUsesExplicitStartCopy();
  await successfulLatestCheckHidesHistoricalFailure();
  await failedLatestCheckShowsOnlyTheCurrentError();
  await baselineMissingReceiptOffersAdoptionGuidance();
  await opensProtectedEditorAndPausesDashboardPolling();
  await saveStagesOnlyManagedSettingsWithoutCallingApply();
  await cancellingApplyReviewNeverSubmitsRuntimeMutation();
  await confirmedApplySendsBoundIntentAndKeepsEditorMounted();
  await applyingConfigurationKeepsReviewModalOpenUntilSettled();
  await backIsDisabledWhileSaveIsInFlight();
  await baselineMissingBlocksEditingAndOffersExplicitAdoption();
  await degradedOperationOffersRecoveryInsteadOfMoreMutation();
  await dirtyBackRequiresConfirmation();
}

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
