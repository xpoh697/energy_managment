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
