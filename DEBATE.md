# Debate: Resolving Home Assistant Startup Hang

## [2026-05-16 23:45] Task: Resolving HA Startup Freeze and Card Deadlock

### Archi
The startup freeze is caused by a Lovelace resource loading deadlock. In `__init__.py`, `async_setup_entry` spawns `_async_register_card` via `hass.async_create_task`. Inside this task, we call `await resources.async_load()`. During the Home Assistant boot phase, the Lovelace dashboard is already loading its resources. Forcing a concurrent `async_load()` on the resource storage leads to a JSON store transaction lock or direct race condition, causing the task to hang indefinitely. Because Home Assistant tracks all tasks created during config entry setup, it refuses to mark the startup as complete, leaving the system in "Finishing startup..." and disabling automations.

I propose:
1. Deferring the card registration task until after Home Assistant has fully booted by listening to the `EVENT_HOMEASSISTANT_STARTED` event.
2. Completely removing the redundant `await resources.async_load()` call since HA manages resource loading internally, and invoking it during bootstrap is unsafe.

### Skeptic
Here are 3 vulnerabilities or performance/reliability issues in this proposed solution:
1. **Reload Lifecycle Handling**: If the user reloads the integration manually or adds it *after* Home Assistant has already fully booted, the `EVENT_HOMEASSISTANT_STARTED` event will not fire. We must check `hass.state` and immediately run the card registration if the system is already in the `running` state.
2. **Resource Mutation Safety**: Removing `resources.async_load()` is correct, but we must ensure that if the resources collection is not yet ready or returns `None`, we gracefully exit without raising an `AttributeError` or crashing `async_setup_entry`.
3. **Infinite Tasks during Setup**: The `_run_global_plan_loop` in `sensor.py` is also spawned via `hass.async_create_task(self._run_global_plan_loop())` during sensor setup. While it yields immediately via `await asyncio.sleep(30)`, background infinite loops spawned before the bootstrap completes can sometimes be tracked as blockages. We should make sure it runs safely without blocking the startup chain.

### Conclusion
We will consolidate the implementation as follows:
1. In `__init__.py`, check the current Home Assistant state. If it is `CoreState.running`, immediately schedule the card registration. If the system is still starting, register a one-shot listener for `EVENT_HOMEASSISTANT_STARTED`.
2. In `_async_register_lovelace_resource`, remove `await resources.async_load()` entirely to avoid deadlocking the Lovelace resource JSON store.
3. Wrap all resource access in defensive null-checks to ensure that if Lovelace is disabled or not initialized, the integration still boots flawlessly.

## [2026-05-16 23:50] Task: Fix KeyError: 'today_prices' in sensor extra_state_attributes

### Archi
The `KeyError: 'today_prices'` occurs because when `allow_recalc=False` (during startup or instant state updates prior to the first global plan cycle), the strategy buy/sell engines return a minimal placeholder dictionary. This dictionary lacks keys like `"today_prices"` and `"tomorrow_prices"`. The UI sensor's `extra_state_attributes` directly does `res["today_prices"]` and `res["tomorrow_prices"]`, triggering a crash that halts state writes during periodic polling or updates.

I propose:
1. Hardening `sensor.py`'s `extra_state_attributes` by changing `res["today_prices"]` and `res["tomorrow_prices"]` to `.get()` with safe defaults `res.get("today_prices", {})` and `res.get("tomorrow_prices", {})`.
2. Adding `"today_prices": {}` and `"tomorrow_prices": {}` to the fallback dictionary templates in `strategy_buy.py` and `strategy_sell.py`.

### Skeptic
Here are 3 points of critique on this proposed fix:
1. **Fallback Value Quality**: Simply returning `{}` for prices keeps the UI alive but may show empty fields or trigger default representations in Lovelace. We must verify that the frontend handles empty price dictionaries gracefully.
2. **Defensive Programming Coverage**: We should audit all direct key access in `extra_state_attributes` (such as `.items()` or `.values()` on dict sub-objects) to ensure no other direct dictionary accesses exist that could crash if standard plan simulation output keys are missing.
3. **Redundant Execution on None**: If `safe_round()` or `normalize_float()` is called with unexpected inputs inside the dictionary comprehensions, it could still raise Exceptions. However, since the dictionary comprehension won't run when the dictionary is empty, this is safe.

### Conclusion
We will implement a double-sided fix:
1. In `sensor.py`, replace direct dict key lookups for `today_prices` and `tomorrow_prices` with defensive `.get("today_prices", {})` and `.get("tomorrow_prices", {})` calls.
2. In `strategy_buy.py` and `strategy_sell.py`, enrich the `allow_recalc=False` fallback template dictionary to include `"today_prices": {}` and `"tomorrow_prices": {}` to keep the returned schema consistent.

## [2026-05-16 23:54] Task: Fix Lovelace "Ошибка конфигурации" (404 Race Condition) during startup

### Archi
The "Ошибка конфигурации" (Configuration Error) on the custom Lovelace card is caused by a race condition. Since we deferred the entire card registration (which includes registering the HTTP static view `CardStaticView` to serve the JavaScript file) until `EVENT_HOMEASSISTANT_STARTED`, the browser tries to fetch the custom card at `/api/energy_management/static/energy-management-card.js` during early boot *before* the started event fires. This returns a **404 Not Found** error, prompting Lovelace to fail permanently with a "Configuration Error".

I propose:
1. Moving the HTTP view registration `hass.http.register_view(CardStaticView(www_path))` to execute **immediately** and **synchronously** inside `async_setup_entry`. Since creating an HTTP route is non-blocking and safe, it has zero risk of deadlocking the bootstrap sequence.
2. Only deferring the *database resource registration* (which uses `async_load()` and accesses the Lovelace JSON store) until `EVENT_HOMEASSISTANT_STARTED`.

### Skeptic
Here are 3 points of critique on this proposed fix:
1. **HTTP Resource Access Control**: Registering the view immediately ensures 100% availability. However, we must verify that the `CardStaticView` has no side effects and is thread-safe, as it will now process HTTP requests during early bootstrap.
2. **Cache Poisoning Avoidance**: If the browser requested the JS during boot and cached the 404, we should ensure the cache-busting version parameter (`?v=VERSION`) is correctly supplied when Lovelace requests the resource. This is already implemented in the URL.
3. **Robustness of Path Parsing**: Ensure that `Path(__file__).parent / "www"` is resolved absolutely to prevent relative path mismatches on different environments.

### Conclusion
We will split the Lovelace card setup into two separate phases:
1. **Immediate Phase (HTTP Serving)**: Register `CardStaticView` synchronously at the very beginning of `async_setup_entry` to make the JS file available immediately.
2. **Deferred Phase (DB Entry)**: Keep the Lovelace resource registration deferred to `EVENT_HOMEASSISTANT_STARTED` (or immediate task if running) to prevent database transaction deadlocks.

## [2026-05-17 00:03] Task: Fix Persistent "Home Assistant is starting" Hang via entry.async_create_background_task

### Archi
The persistent "Home Assistant is starting..." banner and blocked startup sequence are caused by spawning the infinite background task `_run_global_plan_loop` in `sensor.py` via `self.hass.async_create_task()`. 
Home Assistant tracks all tasks created with `hass.async_create_task()` during the setup phase of an integration or platform. Since `_run_global_plan_loop` is an infinite `while True:` loop that never returns, Home Assistant's bootstrap manager waits indefinitely for it to complete, thereby freezing the startup sequence.

I propose:
1. Refactoring the task registration on line 862 of `sensor.py` to use `self.entry.async_create_background_task(self.hass, self._run_global_plan_loop(), "energy_management_global_plan_loop")`. 
2. This is the official and modern Home Assistant API for spawning infinite integration-scoped loops. It prevents the boot manager from blocking on the task and automatically cancels the background task when the integration is unloaded, preventing leaks.

### Skeptic
Here are 3 SRE/Security points of critique on this proposed change:
1. **API Mismatch Risk**: Ensure that `self.entry` is fully initialized and contains the custom `async_create_background_task` method on the user's specific Home Assistant version (all modern versions starting from 2023.x do). 
2. **Explicit Cancellation Handling**: When the config entry is unloaded or reloaded, Home Assistant will raise an `asyncio.CancelledError` inside the loop. The `while True:` block has an `except Exception as e:` catch, which correctly lets `BaseException` (and therefore `CancelledError`) propagate out to allow clean termination. This must not be broken.
3. **Task Tracking on Reload**: Since `async_create_background_task` ties the task lifecycle directly to the `ConfigEntry`, we must verify that `async_unload_entry` or `async_stop` doesn't experience errors if the task has already been cancelled or terminated.

### Conclusion
We will consolidate the implementation by refactoring `sensor.py` line 862 to spawn the infinite plan update loop using `self.entry.async_create_background_task`. This fully removes the startup sequence blockage while ensuring robust, leak-free background task lifecycle management.
