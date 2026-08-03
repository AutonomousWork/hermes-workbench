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

function createHarness(statusRequests, actionRequests) {
  const state = [];
  const refs = [];
  const intervals = [];
  const requests = [];
  let component;
  let effect;
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
      effect = callback;
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
        const queue = options && options.method === "POST"
          ? actionRequests
          : statusRequests;
        assert.ok(queue.length, "unexpected request: " + url);
        return queue.shift().promise;
      },
      hooks: hooks,
    },
    clearInterval: function () {},
    confirm: function () { return true; },
    setInterval: function (callback, milliseconds) {
      intervals.push({ callback: callback, milliseconds: milliseconds });
      return intervals.length;
    },
  };

  vm.runInNewContext(bundle, { window: window });
  assert.equal(typeof component, "function");
  const tree = component();

  function button(label) {
    const pending = [tree];
    while (pending.length) {
      const node = pending.pop();
      if (!node || typeof node !== "object") continue;
      if (node.type === "Button" && node.props.children.includes(label)) return node;
      pending.push.apply(pending, node.props && node.props.children || []);
    }
    throw new Error("button not found: " + label);
  }

  return {
    button: button,
    intervals: intervals,
    requests: requests,
    runEffect: function () { return effect(); },
    state: state,
  };
}

async function drainPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function successfulPollClearsTransientError() {
  const failedPoll = deferred();
  const successfulPoll = deferred();
  const harness = createHarness([failedPoll, successfulPoll], []);

  harness.runEffect();
  failedPoll.reject(new Error('503: {"detail":"temporarily offline"}'));
  await drainPromises();
  assert.equal(harness.state[3], "temporarily offline");

  harness.intervals[0].callback();
  successfulPoll.resolve({ healthy: true, source: "recovered poll" });
  await drainPromises();

  assert.equal(harness.state[0].source, "recovered poll");
  assert.equal(harness.state[3], null);
}

async function stalePollCannotOverwriteAction() {
  const stalePoll = deferred();
  const resumedPoll = deferred();
  const action = deferred();
  const harness = createHarness([stalePoll, resumedPoll], [action]);

  harness.runEffect();
  assert.equal(harness.intervals[0].milliseconds, 10000);
  assert.equal(harness.requests.length, 1);

  harness.button("Start").props.onClick();
  harness.intervals[0].callback();
  assert.equal(harness.requests.length, 2, "polling should pause during an action");

  action.resolve({ message: "Started.", status: { healthy: true, source: "action" } });
  await drainPromises();
  assert.equal(harness.state[0].source, "action");

  stalePoll.resolve({ healthy: false, source: "stale poll" });
  await drainPromises();
  assert.equal(harness.state[0].source, "action");

  harness.intervals[0].callback();
  resumedPoll.resolve({ healthy: true, source: "resumed poll" });
  await drainPromises();
  assert.equal(harness.state[0].source, "resumed poll");
}

async function manualRefreshStillWorks() {
  const refresh = deferred();
  const harness = createHarness([refresh], []);

  harness.button("Refresh status").props.onClick();
  refresh.resolve({ healthy: true, source: "manual refresh" });
  await drainPromises();

  assert.equal(harness.state[0].source, "manual refresh");
  assert.equal(harness.state[2], "Status refreshed.");
}

async function main() {
  await successfulPollClearsTransientError();
  await stalePollCannotOverwriteAction();
  await manualRefreshStillWorks();
}

main().catch(function (error) {
  console.error(error);
  process.exitCode = 1;
});
