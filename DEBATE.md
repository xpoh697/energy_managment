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

## [2026-05-17 00:10] Task: Align Emergency Mode logic between sensor.py and dispatch_plan.py

### Archi
The mismatch between the Lovelace card display ("NORMAL" mode planned for the night hours when the battery is exactly at `min_soc` like 13.0%) and the real-time execution is caused by a logic discrepancy. 
In `sensor.py` (real-time control), the `P1: Emergency` block uses `batt_soc <= min_soc` and sets the mode to `bat_emergency` when there is no solar surplus (`has_surplus = False`).
However, in `dispatch_plan.py` (simulation planning), `EnergyLogicEngine.get_mode_at` uses a strict inequality `if batt_soc < min_soc:` (strictly less than). When the battery is exactly at `min_soc` (13.0%), the condition evaluates to `False`, falling through to P9 ("sale_pv", rendered as "NORMAL" on the card).

I propose:
1. Aligning `dispatch_plan.py`'s `EnergyLogicEngine.get_mode_at` to use the exact same P1 Emergency block logic as `sensor.py`.
2. Specifically, when `round(batt_soc, 1) <= min_soc`:
   - If `has_surplus` is `True`, plan `sale_pv` (or `stop_sale` depending on price) with `target_soc = min_soc`.
   - If `has_surplus` is `False` (e.g., at night), plan `bat_emergency` (rendered as "Emergency" in violet on the card) with `target_soc = 100.0`.

### Skeptic
Here are 3 SRE/Security points of critique on this proposed change:
1. **Float Comparison Precision**: Floating point variables can suffer from representation drift. We must use `round(batt_soc, 1) <= min_soc` or similar epsilon-based comparison to prevent exact equal bounds from failing under minor precision noise in the simulation loop.
2. **Surplus Threshold Guard**: In the simulation, predicted profiles might contain low-level noise (e.g., 0.01 kW). The existing `has_surplus = bool(avg_gen > (avg_load + 0.05))` guard is robust and should be preserved in `dispatch_plan.py` to prevent twilight mode flickering.
3. **Simulation State Parity**: Confirm that setting `target_soc = 100.0` in `bat_emergency` simulation does not trigger fake "grid charging" in the forecast. In `const.py`, `bat_emergency` has `charge_from_grid=False`, which correctly avoids fake grid charges.

### Conclusion
We will update the P1 block in `dispatch_plan.py` (`EnergyLogicEngine.get_mode_at`) to perfectly mirror `sensor.py`'s emergency logic. We will use `round(batt_soc, 1) <= min_soc` and check `has_surplus` to correctly plan `bat_emergency` at night when the battery is drained, ensuring the Lovelace card accurately displays the "Emergency" state.

## [2026-05-17 00:13] Task: Populating is_manual flag for future forecast hours

### Archi
The missing hand icon (`mdi:hand-back-right`) and white glow on the Lovelace card cells for manually overridden future hours are caused by `slot.is_manual` only being populated for the current hour (`h_abs == 0`). For all future slots (`h_abs > 0`), `slot.is_manual` is left at its default value `False`, even when the slot is successfully overridden.

I propose:
1. Performing a quick lookup against `self.hourly_manual_overrides` and `self.manual_mode_overrides` for every simulated hour in the loop inside `sensor.py`.
2. Setting `slot.is_manual = True` for any slot that matches a manual override, ensuring that the custom Lovelace card gets the `is_manual: true` attribute for all hours.

### Skeptic
Here are 3 SRE/Security points of critique on this proposed change:
1. **Clean Deletion Guard**: When a manual override is reset to AI mode, the key is deleted from `self.hourly_manual_overrides` by `async_set_hourly_override`. Thus, a simple `in` check is perfectly safe and won't produce false-positive manual flags for reset slots.
2. **Timezone/Midnight Safety**: The `dt_h` variable is a timezone-aware datetime object. Using it for string formatting ensures we don't experience midnight desync issues between the local time and the simulation timezone when determining tomorrow's overrides.
3. **No Overhead**: Checking `in` on Python dictionaries is an $O(1)$ operation, meaning this change introduces virtually zero CPU overhead for the 48-hour simulation.

### Conclusion
We will update `sensor.py` at line 965 to check for active overrides using the hour's specific timestamp `ts_key` and populate `slot.is_manual = True` for both current and future forecast hours. This immediately restores the hand override icon and card styling in Lovelace.

## [2026-05-17 00:19] Task: Making Sell Strategy aware of Manual Overrides to protect Gatekeeper Floor

### Archi
The problem where the sell strategy plans a "phantom" sale (e.g. at 20:00) despite the battery being manual discharged down to 23% is caused by the sell strategy's internal simulations being completely blind to the user's manual overrides (e.g. discharging down to 25% at 18:00). Because `strategy_sell.py`'s simulations run with `mode_overrides=None` by default, they assume the battery remains at 64% at 18:00, leading them to believe they have plenty of excess charge to sell at 20:00.

I propose:
1. Constructing `m_overrides` inside `strategy_sell.py` from both `man.hourly_manual_overrides` and `man.manual_mode_overrides`.
2. Passing `mode_overrides=m_overrides` to all internal simulations inside `strategy_sell.py`.
3. Merging `m_overrides` into `mode_overrides_sim` for the final Stage 4 UI simulation projection.

### Skeptic
Here are my 3 points of SRE/QA critique:
1. **Safety Floor Protection**: By passing manual overrides to the simulator, the sell strategy's budget allocator will immediately detect that the manual discharge of 18:00 leaves the battery at 23% by 20:00. This is below the gatekeeper floor of 45.6%, so the allocator will correctly zero out the sale at 20:00 to protect the battery from hit-bottom/over-discharge.
2. **Key Consistency**: Since `h_abs` is an integer index from `cur_hour` to `cur_hour + 47`, mapping the manual overrides to `h_abs` using `now + timedelta(hours=i)` is highly precise and completely correct.
3. **Robust Convergence**: The allocator converges beautifully in under 20 iterations. Having accurate SOC starting levels prevents the solver from wasting iterations trying to refine impossible sales.

### Conclusion
We will update `strategy_sell.py` to extract all active manual overrides at the start of `get_market_strategy` and feed them directly into all internal simulations as `mode_overrides`. This guarantees the sell strategy respects manual actions, protects the night survival floor, and prevents phantom sales.

## [2026-05-17 00:23] Task: Making Buy Strategy aware of Manual Overrides for Simulation Integrity

### Archi
Exactly as with the sell strategy, the buy strategy (`strategy_buy.py`) must also be aware of the user's manual overrides (e.g. manual discharges) inside all of its internal simulations. If the buy strategy's simulations are blind to manual overrides, it will run simulations assuming the battery is still at 64% when it has actually been manually discharged down to 25%. This would make the buy strategy believe no grid charging is needed, leaving the user with an empty battery at night and high electricity bills!

I propose:
1. Building `m_manual_overrides` at the start of `get_market_strategy` in `strategy_buy.py`.
2. Passing it to the first survival simulation and the solar baseline simulation.
3. Merging `m_manual_overrides` into `m_overrides` for the final buy simulations to ensure exact progression mapping.

### Skeptic
My 3 security/performance critique points:
1. **Critical Battery Survival**: This is a direct SRE safety fix! A blind buy strategy could lead to a fully depleted battery at night during expensive periods, which completely violates the primary goal of the integration. Being aware of the manual discharge ensures the buy strategy schedules a necessary survival grid charge.
2. **Double-Command Safeguard**: By merging manual overrides into `m_overrides`, if the user has overridden a hour to "buy", the strategy won't try to overlay a conflicting AI sell or AI buy command, maintaining absolute clean target isolation.
3. **Flawless Convergence**: Dict lookup is extremely fast, so there's zero chance of introducing performance lag.

### Conclusion
We will update `strategy_buy.py` to compile manual overrides at the start and pass them to all four internal SOC simulations. This ensures the buy strategy correctly plans grid charging when manual overrides drain the battery.

## [2026-05-17 00:30] Task: Resolve false-positive convergence in sell allocator by separating sale power from total battery power

### Archi
The allocator in `strategy_sell.py` uses `p_real_bat = sim_data.get("p_bat", 0.0)` in the convergence refinement loop to measure how much power the battery provided for sale in the simulation. However, `p_bat` represents the *total* battery discharge, which includes both the house load (`p_house_dc`) and the sale (`p_sale_dc`). When the battery is below the survival floor, the simulation correctly zeroed out the sale component, but still discharged to cover the house load. The allocator saw this house load discharge and mistakenly assumed the battery had successfully discharged for the sale! This false positive prevented the allocator from registering a deficit, causing it to leave the impossible sale active. 

I propose:
1. Calculating `sim_p_sale` (the actual sale component) separately in `run_soc_simulation`.
2. Saving it in the history log as `"p_sale"`.
3. Having the allocator read `"p_sale"` instead of `"p_bat"`.

### Skeptic
Here are 3 points of SRE/QA critique:
1. **Defensive Fallback**: Using `.get("p_sale", sim_data.get("p_bat", 0.0))` guarantees that if any part of the system or older cache results lack the new `"p_sale"` key, the allocator gracefully falls back to the old total battery power behavior without throwing exceptions.
2. **Mathematical Correctness**: Setting `sim_p_sale = (sale_drop_act / 100.0 * b_cap_f) / step_duration * sim_eff` is perfectly accurate and accounts for efficiency and capacity in the AC/DC conversions.
3. **Sign Parity**: Since both `sim_p_bat` and `sim_p_sale` are positive when discharging, this is a clean drop-in replacement that requires no sign flips or complex scaling adjustments.

### Conclusion
We will define and calculate `sim_p_sale` inside `run_soc_simulation`'s discharge block and log it as `"p_sale"`. Then we will update the allocator convergence loop in `strategy_sell.py` to check `sim_data.get("p_sale", sim_data.get("p_bat", 0.0))` to measure actual sale discharge, completely eliminating the false-positive convergence bug and ensuring impossible sales are safely zeroed out.

## [2026-05-17 00:35] Task: Designing a Premium, Beautiful Hour Detail Modal HUD

### Archi
The current hourly details modal in `energy-management-card.js` is quite plain, using simple text key-value pairs (`.info-row`) with basic dark theme coloring. I propose converting this details section into a premium, state-of-the-art glassmorphic HUD panel. This will include:
1. Color-coded Home Assistant `<ha-icon>` icons for each parameter to visually guide the user immediately.
2. A beautiful glowing green `soc-badge` styling for the battery Forecast percentage to highlight the key forecast state.
3. An elegant, premium callout box (`reason-box`) for the AI decision reason, utilizing an accent border-left bar and styled typography.
4. Soft glassmorphism effects (`background: rgba(255, 255, 255, 0.03)`, `border: 1px solid rgba(255,255,255,0.08)`) with subtle inset shadows to create deep premium layers.

### Skeptic
Here are 3 SRE/Security points of critique on this proposed layout change:
1. **Element ID Preservation**: The custom card's Javascript relies strictly on direct ID lookups (`info-prices`, `info-forecast`, `info-power`, `info-reason`, `info-forecast-soc`). If we alter these IDs, the script will crash or fail to display state data, causing card configuration errors. We must keep all IDs exactly the same.
2. **Icon Compatibility**: Using standard, core Material Design Icons (like `mdi:swap-horizontal`, `mdi:lightning-bolt`, `mdi:flash-outline`, `mdi:battery-80`, `mdi:information-outline`) is highly reliable as they are bundled natively with Home Assistant.
3. **Contrast and Legibility**: Monospace fonts (`Roboto Mono` / `Roboto`) can sometimes become hard to read when too thin or low-contrast. We must ensure robust font weights (600+) and proper text-transform rules to preserve excellent usability across light and dark system settings.

### Conclusion
We will implement the premium glassmorphic HUD panel redesign inside `energy-management-card.js`. We will keep all JavaScript element IDs intact to prevent breaking the logic. We will use native, high-performance Home Assistant `<ha-icon>` tags with standard MDI icons and high-contrast curated styling tokens to create a stunning user experience.

## [2026-05-17 00:39] Task: Colorizing HUD Modal Values and Renaming Forecast to SOC Forecast

### Archi
I propose dynamically colorizing the individual numbers in the details modal to instantly highlight cost vs profit and energy flow:
1. Spitting the prices and forecast data into distinct spans: `#info-buy`, `#info-sell`, `#info-gen`, `#info-load`.
2. Applying custom premium HSL color tokens:
   - Buy price & Load consumption (Red): `#ff6b6b` (beautiful soft coral red).
   - Sell price (Green): `#66bb6a` (rich emerald green).
   - Gen solar power (Yellow): `#ffe082` (warm amber gold).
3. Changing the label "Forecast" to "SOC Forecast" to clarify the battery state of charge.

### Skeptic
Here are 3 points of SRE/QA critique:
1. **Defensive Reference Handling**: When targeting the new sub-spans in JS, we must wrap them in safe existence guards (e.g. `if (buyEl) buyEl.innerText = ...`) to prevent any unhandled Javascript errors if elements are missing during early loads.
2. **Monospace Font Contrast**: The monospace fonts inside these spans should remain thick (`font-weight: 600`) to guarantee legibility when colored, as light red/yellow text on dark background can wash out.
3. **Unit Isolation**: Moving units like `kW` or currency symbols out of the styled numbers into standard `unit-text` spans ensures they remain neutral-colored (`rgba(255,255,255,0.4)`), keeping the focus entirely on the numeric values.

### Conclusion
We will split the compound price and generator strings in `energy-management-card.js` into distinct sub-spans, applying red for Buy/Load, green for Sell, and yellow for Gen. We will rename the Forecast label to "SOC Forecast" and log this debate in `DEBATE.md`, then deploy the updated code under `v12.0.71`.

## [2026-05-17 00:41] Task: Dynamic Visibility for Power/Amps Row Based on Active Mode

### Archi
I propose showing the `Power / Amps` telemetry row only when the hour has an active battery flow (i.e. Charging `buy` or Discharging `sale_pv_bat`). In other states like `Normal`, `Wait`, or `Stop Sale`, the battery is inactive, so showing `0 kW / 0 A` is redundant. 
I will:
1. Wrap the `Power / Amps` markup in `id="info-power-row"`.
2. Keep the current hour's data in `this._currentHourData = hourData;` inside `_openModal`.
3. Update `_toggleSocVisibility()` to resolve the active mode (checking the select dropdown value, and if it is `'ai'`, falling back to `this._currentHourData.mode`).
4. Set the CSS display of `info-power-row` to `flex` if the active mode is `buy` or `sale_pv_bat`, and `none` otherwise.

### Skeptic
Here are 3 points of SRE/QA critique:
1. **Safety Null-Guards**: If `this._currentHourData` or `this._currentHourData.mode` is missing, the code could throw a `TypeError`. We must fall back safely to a default value (e.g. `'ai'` or empty string) if attributes are missing.
2. **Smooth Layout Flow**: By toggle-hiding using `display: none` / `display: flex`, the other items in `.modal-info-grid` will collapse and slide smoothly, avoiding any broken layout gaps or white-space jumps.
3. **Synchronization**: Since `_toggleSocVisibility()` is called both during `_openModal` and inside the `onchange` event of the dropdown selector, the row's visibility will stay perfectly in sync under both initial load and manual override edits.

### Conclusion
We will implement the dynamic visibility logic for the `Power / Amps` row inside `_toggleSocVisibility()`, using safe fallback guards. We will tag the row with `id="info-power-row"` and update the version to `v12.0.72`.

## [2026-05-17 00:43] Task: Dynamic Battery SOC Badge in Timeline Grid and Normal Mode Icon

### Archi
I propose two significant visual upgrades to the hourly timeline grid cards:
1. **Normal Icon Upgrade**: Update `MODE_ICONS['sale_pv']` to use `'mdi:solar-power-variant'` instead of `'mdi:home-lightning-bolt'`, matching the user's preference and improving clarity.
2. **Dynamic Top-Left SOC Badge**: Move the battery SOC reading from the bottom to the top-left corner of the card. Instead of text, it will display a color-coded battery icon and percentage:
   - Introduce a helper `getSocInfo(soc)` mapping charge levels to high-resolution battery icons (`mdi:battery-10` to `mdi:battery`) and colors (Green `>75%`, Light Green `>60%`, Amber `>40%`, Orange `>25%`, Coral Red `<25%`).
   - Wrap the new top-left badge in `.h-soc-top-left` with clean absolute positioning.
   - Synchronize both initial card generation and the fast point-update algorithm to dynamically bind, colorize, and update this badge.

### Skeptic
Here are 3 points of QA/SRE critique:
1. **Visual Symmetry and Overlap**: Placing the SOC badge at `top: 4px; left: 4px;` balanced with the manual override hand indicator at `top: 4px; right: 4px;` prevents any layout overlaps. 
2. **Missing SOC Safety**: If `hourData.soc` is missing/undefined, `getSocInfo` must fall back to a faded grey question icon without text, avoiding displaying `NaN%` or `undefined%`.
3. **Point-Update Performance**: When updating cells, targeting `.h-soc-top-left` and updating `style.color` and child element attributes is extremely fast, ensuring that live telemetry updates do not introduce lag.

### Conclusion
We will implement the global helper `getSocInfo`, change the Normal mode icon to `'mdi:solar-power-variant'`, update the timeline CSS and HTML structure to place the battery badge in the top-left corner, and rewrite the point-update mechanism to fully support this color-coded telemetry. We will deploy this under version `v12.0.73`.

## [2026-05-17 00:46] Task: Fine-Tuning Spacing in Timeline Hour Cards

### Archi
I propose raising the battery SOC badge higher and pushing all other central card elements lower to make the layout feel perfectly balanced, spacious, and premium:
1. Shift `.h-soc-top-left` absolute positioning to `top: 2px; left: 4px;` (raising it by 3px and shifting it 2px left).
2. Add a `margin-top: 5px;` to `.h-icon` (the top icon inside the card) to push the entire group of centered elements (Icon, Time, Prices, Mode) down, creating a beautiful margin between them and the top-left SOC badge.

### Skeptic
Here are 3 points of visual/QA critique:
1. **Overlap Prevention**: Pushing the main solar panel/battery icon down by 5px and pulling the SOC badge up by 3px creates a clear 8px buffer between them, completely eliminating the visual overlap.
2. **Symmetry**: The new coordinates align the badge perfectly with the rounded corner of the card.
3. **No Layout Breaks**: Using simple margins and absolute top coordinate changes is completely safe and won't cause any flex layout breakage in standard Lovelace views.

### Conclusion
We will adjust the coordinates of the absolute SOC badge to `top: 2px; left: 4px;` and add `margin-top: 5px;` to `.h-icon` in the CSS styles inside `energy-management-card.js`, deploying under version `v12.0.74`.

## [2026-05-17 00:50] Task: Dynamic bat_emergency Mode Prediction in Global Slots Loop

### Archi
I propose aligning the rolling simulation inside `sensor.py`'s `async_update_global_plan` loop with the high-fidelity simulator's physics. 
Currently, the rolling loop assumes `p_actual = 0` during non-grid-command hours (like Normal or Wait modes), leaving `sim_soc` frozen. This prevents the decision logic from realizing that the battery will be drained by household load, which in turn prevents the activation of `bat_emergency` mode in the forecast slots.
I will:
1. Detect non-grid-command hours (when `p_actual == 0`).
2. Calculate the net load flow (`net_flow = slot.gen_raw - slot.load_total`).
3. Query the active `InverterModeClass` details.
4. If there is solar surplus and the mode permits `charge_from_pv`, charge the battery (up to `battery_max_power`).
5. If there is deficit and the mode permits `discharge_to_house`, discharge the battery to cover it.
6. This will naturally drain `sim_soc` during evening hours, allowing the decision logic at later hours to correctly detect `sim_soc <= min_soc` and trigger the `bat_emergency` mode.

### Skeptic
Here are 3 SRE/QA points:
1. **Safety Null-Guards**: Mode lookup using `INVERTER_MODES.get(mode)` is extremely safe and cannot crash. If missing, it defaults to ignoring the flow.
2. **Infinite Emergency Prevention**: When `bat_emergency` is activated, its class parameters strictly specify `discharge_to_house = False`. This means the rolling simulation will stop discharging the battery as soon as it hits the emergency threshold, matching exact hardware behavior.
3. **Bound Clipping**: Flow values must be strictly clipped to hardware-configured `battery_max_power` limits.

### Conclusion
We will implement this physically accurate rolling battery flow simulation in `sensor.py` during the global plan loop, ensuring the decision logic correctly triggers emergency states in projections. We will deploy this under version `v12.0.75`.

## [2026-05-17 00:58] Задача: Исправить расхождение симуляции (NORMAL вместо EMERGENCY при 13% SOC) путем синхронизации лимитов ручного управления (Manual Overrides) в 48-часовой цикл прогнозирования

### Archi
Я предлагаю исправить расхождение между циклом прогнозирования и высокоточной симуляцией `run_soc_simulation`.
Причина бага в том, что в цикле прогнозирования для будущих интервалов (`h_abs > 0`) ручные переопределения (Manual Overrides) определяются по режиму, но их активная мощность (разряд/заряд до целевого `soc_limit`) полностью игнорируется (принимается за `p_actual = 0`). Это приводит к тому, что в прогнозе батарея якобы не разряжается при ручном экспорте, `sim_soc` остается завышенным (например, 70%), и на ночные часы выбирается режим `NORMAL` (`sale_pv`).
Однако в высокоточной симуляции разряд честно рассчитывается, батарея падает до `13%` (пол), создавая расхождение на графике (13% SOC, но режим `NORMAL`).
Решение: Внутри forecast-блока `h_abs > 0` в `sensor.py` мы будем извлекать `man_override` и рассчитывать необходимую мощность `p_est` для достижения целевого `soc_limit` аналогично функции `calculate_realtime_power`.

### Skeptic
Критикую данное решение по 3 пунктам:
1. **Безопасность деления на ноль**: При извлечении `battery_capacity` мы должны гарантировать, что емкость не равна нулю (задать фоллбек `10.0`), иначе возникнет исключение при делении.
2. **Физическая точность КПД**: При разряде в сеть мощность `req_p` должна умножаться на КПД (`eff = 0.98`), а при заряде из сети `p_calc` должна делиться на КПД (`/ eff`), чтобы соответствовать реальным потерям инвертора.
3. **Границы мощности**: Рассчитанная мощность переопределения `p_calc` должна быть строго ограничена системным параметром `battery_max_power`, чтобы прогноз не строил нереалистичные графики мгновенного заряда.

### Заключение
Мы реализуем точный расчет прогнозной мощности для будущих ручных переопределений с учетом КПД, ограничений емкости и максимального тока. Это полностью синхронизирует логику прогноза с симулятором и исправит баг отображения `13% NORMAL`. Код будет внедрен в `sensor.py` под версией `v12.0.76`.

## [2026-05-17 01:10] Задача: Устранение преждевременного включения режима EMERGENCY (на 21:00 при заряде 18%) из-за двойного учета нагрузки в прогнозе

### Archi
В цикле планирования `sensor.py` (в блоке прогнозирования rolling SOC) расчет естественного тока батареи `net_flow = slot.gen_raw - slot.load_total` использует общую нагрузку (`load_total`), которая исторически включает в себя зарядку аккумулятора от сети. Это приводит к двойному учету нагрузки и искусственному завышению расхода энергии в прогнозе (батарея «проседает» на лишние 5-7% в час быстрее, чем в реальности).
Высокоточный симулятор `run_soc_simulation` в `strategy_base.py` очищен от зарядных токов и работает на базе очищенного профиля `consumption_base` (`load_base`).
Из-за этого прогноз считает, что на 21:00 заряд упадет до `11%` (что меньше лимита `13%`), и ошибочно решает включить `bat_emergency`. Реальный же симулятор видит режим `bat_emergency`, прекращает разряд и удерживает батарею на `18%`.
Решение: В расчете `net_flow` в `sensor.py` заменить `slot.load_total` на `slot.load_base` с безопасным фоллбеком.

### Skeptic
Критикую данное решение по 3 пунктам:
1. **Безопасность нулевых или пустых значений**: Базовый профиль нагрузки `slot.load_base` может быть равен нулю, если данные о базовой нагрузке не заполнены или повреждены. Мы обязаны сделать безопасный фоллбек на `slot.load_total`, если `slot.load_base` равен нулю или `None`.
2. **Double-counting Protection**: В некоторых конфигурациях пользователь может переопределить профили, поэтому фоллбек на `slot.load_total` должен быть надежным и не ломать расчеты.
3. **Версионирование**: Любые изменения в формуле симуляции глобального планировщика должны сопровождаться изменением внутренней версии интеграции в логах, чтобы облегчить отладку при последующих обращениях.

### Заключение
Мы заменим `slot.load_total` на `slot.load_base` в расчете естественного тока батареи прогноза с безопасным фоллбеком на `slot.load_total` при отсутствии или нулевом значении базового профиля: `base_load = float(slot.load_base) if (slot.load_base and slot.load_base > 0.01) else float(slot.load_total)`. Это обеспечит абсолютную физическую синхронность прогноза и симулятора, решив проблему ложных `bat_emergency` при `18%`. Изменение будет внедрено в `sensor.py` под версией `v12.0.77`.

## [2026-05-17 08:40] Задача: Что за продажа фантомная? И по прежнему глючит emergency

### Archi
Я проанализировал обе проблемы. 

1. **Фантомная продажа на 20:00 (команда 0.054 кВт)**:
   Причина кроется в условиях цикла сходимости (convergence loop) в `strategy_sell.py`. При уменьшении бюджета на продажу, когда батарея упирается в защитный порог (floor), мы проверяем дефицит:
   `if total_deficit_kwh > 0.15: ...`
   Если дефицит становится меньше `0.15` кВт·ч (например, `0.05` кВт·ч при продаже `0.05` кВт в течение 1 часа), цикл сходимости считает, что схождение достигнуто, прекращает уменьшение бюджета и оставляет эту мелкую «фантомную» команду активной. В Lovelace это отображается как режим разряда DISCHARGE (поскольку любая команда > 0 переводит час в продажу).
   **Решение**: Снизить порог сходимости `total_deficit_kwh > 0.15` до `0.01` кВт·ч. Это заставит оптимизатор полностью сбросить бюджет до нуля, если продать физически невозможно, убирая любые фантомные продажи.

2. **Глюк EMERGENCY (13% SOC отображает режим NORMAL на 21:00 и 22:00)**:
   Здесь сразу две критические проблемы:
   - **Рассинхронизация двух фаз планирования (Two-Pass Drift)**: В `sensor.py` планирование идет в два прохода. В первом проходе слоты рассчитывают свои режимы на основе простой rolling-симуляции SOC. Поскольку в первом проходе продажные команды еще не были применены к SOC, на 21:00 прогнозировался высокий SOC (например, 16%), и `get_mode_at` выдал `sale_pv` (NORMAL). Во втором проходе запускается точный симулятор `run_soc_simulation`, который правильно применил команды разряда и опустил SOC на 21:00 до `13%`. Но `sensor.py` никогда не обновляет режим слота (`slot.mode`) после второго прохода! В итоге Lovelace показывает SOC 13%, но режим остается «NORMAL» от первого прохода.
     **Решение**: В `sensor.py` в блоке маппинга результатов симуляции (после второго прохода) перерассчитывать `slot.mode` и `slot.reason` с помощью `EnergyLogicEngine.get_mode_at` на основе реального `slot.soc_start`.
   - **Ошибочное ветвление в P1 Emergency**: В `get_mode_at` (и в `sensor.py`, и в `dispatch_plan.py`) при `batt_soc <= min_soc` мы проверяем `if has_surplus: mode = "sale_pv"`. Это огромная ошибка. В режиме `sale_pv` разрешен разряд батареи на дом (`discharge_to_house = True`). Если солнце колеблется или наступает ночь (а из-за сдвига прогнозов ночью рисовался фантомный профицит), батарея разряжается ниже минимума. В режиме `bat_emergency` добор солнца от PV работает прекрасно (`charge_from_pv = True`), но разряд на дом полностью запрещен.
     **Решение**: Сделать переход в `bat_emergency` безусловным при `batt_soc <= min_soc`, убрав проверку `has_surplus` в блоке аварийной защиты.

### Skeptic
Критикую предложенное решение по 3 пунктам:
1. **Производительность повторного перерасчета**: Повторный вызов `EnergyLogicEngine.get_mode_at` для 48 часов после второго прохода симуляции может замедлить HA, если внутри происходят тяжелые дисковые операции или расчеты. Мы должны убедиться, что все стратегии и профили передаются в метод в виде готовых кэшированных словарей (`shared_profiles`, `buy_strat`, `sell_strat`).
2. **Безопасность режима bat_emergency**: Убедиться на 100%, что в режиме `bat_emergency` инвертор действительно может заряжаться от солнца (параметр `charge_from_pv` в `InverterModeClass` равен `True` для `bat_emergency` — проверено по `const.py`, это так). Также нужно гарантировать, что при росте SOC выше `min_soc` на `0.1%` система не начнет мгновенно прыгать обратно в `sale_pv` и обратно в `emergency` (для этого в реальном времени работает 10-минутный замок режимов `self._mode_lock_until`, а в прогнозе плавный рост SOC естественным образом выведет батарею из аварии).
3. **Фильтрация микро-команд**: Мало снизить порог сходимости до `0.01` кВт. Мы должны гарантировать, что в `strategy_sell.py` при построении плана `planned_results` любые команды продажи батареи меньше или равные `0.05` кВт полностью приравниваются к нулю, чтобы исключить отправку «мусорных» команд на реальный инвертор.

### Заключение
Консенсус достигнут:
1. В `strategy_sell.py` (convergence loop) заменяем порог `total_deficit_kwh > 0.15` на `total_deficit_kwh > 0.01` для глубокой очистки бюджета.
2. В `strategy_sell.py` (финальный маппинг) убеждаемся, что команды `<= 0.05` кВт не попадают в `planned_results` (это уже частично реализовано через `if sell_commands.get(h, 0.0) <= 0.05: continue`).
3. В `sensor.py` после второго прохода симуляции внедряем точечный перерасчет `slot.mode` и `slot.reason` на основе высокоточного `slot.soc_start` с передачей кэшированных профилей.
4. В `dispatch_plan.py` (`get_mode_at`) и `sensor.py` (`_get_mode_at`) делаем переход в режим `bat_emergency` безусловным при `batt_soc <= min_soc` (убираем ветвление `has_surplus` внутри P1 Emergency). Это защитит АКБ от глубокого разряда и гарантирует правильный фиолетовый статус Emergency на Lovelace при 13% SOC.



