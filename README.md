# Energy Management Integration for Home Assistant

Energy Management (ex-Energy Profile) is a localized, intelligent Home Assistant integration designed to optimize energy usage, track consumption profiles, and distribute solar generation surpluses efficiently. It bridges the gap between dumb solar generation forecasting, volatile market prices, and heavy domestic appliances.

## Core Features

### 1. Hourly Profiling & Machine Learning Alternative
The core of the integration relies on creating a "statistical daily profile" of your home's energy consumption. Unlike standard HA Energy Dashboard counters which just increment, this integration builds an array of historical hourly load `[00:00 - 23:59]`. 
It automatically separates data into **Weekday** and **Weekend** profiles, allowing for highly accurate predictions based on different user habits. 
It separates your **Total Consumption** from your **Base Consumption** (total minus controllable loads, like your boiler or EV).
By calculating an N-day moving average of your **Base Consumption** (matched dynamically against tomorrow's Day of the Week), the integration can predictably estimate exactly how much energy your house requires to survive the night until the sun rises at 08:00 AM.

### 2. Auto-Adjusting Solar Forecast (Reliability Coefficient)
Cloud coverage forecasts (like Solcast) are notoriously unreliable. The integration contains a "Confidence Algorithm":
- It records exactly how much energy your panels produced today versus how much the cloud API promised.
- At midnight, it calculates a `Forecast Correction Coefficient` based on the past 14 days of historical accuracy.
- *Example*: If Solcast always overestimates by 15%, the integration will lower its trust in tomorrow's forecast, preventing your boiler from draining your home battery based on false solar promises.

### 3. Smart Energy Budget (Surplus Calculator)
The integration generates an `EnergyBudgetSensor` (The primary brain output).
The Budget formula calculates:
`Available Budget = (Adjusted Solar Forecast + Current Battery SoC Energy) - Expected Base Night Consumption`

If the budget is positive (>0), the house has a confirmed surplus of energy that will otherwise be exported to the grid or wasted.

### 4. Hierarchical Load Permissions
You configure "Managed Loads" (Deduct Sensors). These are large appliances that can be paused or started dynamically. The integration generates a `Binary Sensor` (Permission Switch) for each load based on:
- **Priority**: A queueing system (Priority 1 gets energy before Priority 2).
- **Daily Quota (kWh)**: How much energy the device needs today. Once satisfied, permission is revoked.
  - Setting this to `0` configures the device as an "Infinite Sink" (e.g. it turns on whenever there is ANY surplus energy available).
- **Power Rating / Bottleneck Protection (kW)**: The integration analyzes instant power deltas over the last 15 minutes. It will REVOKE permission if your solar panels are currently generating 1kW, but your priority 1 boiler demands 2kW. This protects your home battery from severe discharge cycles.

### 5. Universal Market Price Extractor
The integration accepts Market / Exchange Price sensors (Buy/Sell) to enable active Energy Arbitrage.
The inner Universal Extractor supports 99% of cloud vendor formats (Nordpool, ENTSO-E, Octopuss, YAML-Lists, etc.), pulling 24h-48h hour arrays from attributes like `price_today` and injecting them into a unified local SQLite index. This prevents routing failures even if the external price API goes down for hours.

### 6. Battery Survival Mode & Depletion Forecast
It features a built-in time machine simulation that reads your battery SOC, solar forecast, and historical consumption. It projects your battery level up to +48 hours into the future, hour by hour. If the battery is predicted to hit a critical threshold (`Min Survival SOC`) before a scheduled cheap charging window, the AI automatically bypasses your Buy Price Limits and identifies a "Bridge Window" (the cheapest available contiguous hour) to inject a survival charge and prevent grid reliance during peak prices.

### 7. Financial Analytics & ROI Tracking
The integration now includes a dedicated financial suite to track your return on investment:
- **Solar Economy**: Calculates savings from direct self-consumption.
- **Price Arbitrage**: Evaluates profit from shifting loads/charging to cheap hours, using a weighted-average future price projection (taking battery efficiency into account).
- **Sell Revenue**: Tracks gross income from electricity exports.
- **Dynamic Currency Support**: Automatically inherits currency settings and symbols from your Home Assistant global configuration.

### 8. Inverter Operation State Machine
A master orchestration sensor (`Inverter Mode Command`) outputs explicit action states based on a strict priority ladder:
1. `bat_emergency`: Absolute priority. Battery is drained below `Min Survival SOC`. Forces a charge from the Grid/PV.
2. `stop_sale`: Price of energy crashed below `Stop Sell Threshold`. Halts export to grid.
3. `sale_pv_bat`: Currently inside an optimal `Market SELL Strategy` peak window. Full discharge of PV + Battery allowed.
4. `sale_pv_no_bat`: Current price crossed the `Sell PV Only Threshold`. Export PV only, save battery. Also hard-blocks all managed loads to maximize export profit.
5. `sale_pv`: Default operation. PV primarily feeds the house and charges battery, minor surplus goes to the grid.

## Generated Entities
Once configured, the integration automatically produces the following main entities:

1. `sensor.average_hourly_base_consumption_<N>_days` *(with weekday/weekend attributes)*
2. `sensor.average_hourly_total_consumption_<N>_days` *(with weekday/weekend attributes)*
3. `sensor.average_hourly_generation_<N>_days` *(with weekday/weekend attributes)*
4. `sensor.energy_management_budget`
5. `binary_sensor.permission_<device_name>` (xN for every Managed Load)
6. `sensor.market_strategy_buy` & `sensor.market_strategy_sell` (Active AI Load balancing targets)
7. `sensor.inverter_mode_command` (Master state machine for automations)
8. `sensor.battery_depletion_forecast` (String prediction of the exact limit hit hour)
9. `sensor.savings_solar_generation`, `sensor.savings_price_arbitrage`, `sensor.savings_sell_revenue` (Financial metrics)

## Configuration UI Entities
You don't need to create any Helpers (`input_number`, `input_boolean`) yourself! The integration automatically generates them for you to tweak directly on your dashboard:
- **Price Limits**: `number.buy_price_limit`, `number.sell_price_limit`, `number.stop_sell_threshold`, and `number.price_sell_only_pv`
- **Tolerances**: `number.buy_price_tolerance` and `number.sell_price_tolerance` (Expands cheap windows outwards)
- **Battery Target/Survival SOC**: `number.target_soc_buy`, `number.target_soc_sell`, and `number.min_survival_soc`
- **Battery Max Power**: `number.battery_max_power`
- **Smart AI Toggles**: `switch.smart_charge_ai` and `switch.smart_sell_ai`

## Price Integration Details
*(For full details on supported external price attributes, refer to the old README_PRICES.md which is fully summarized here).*
Supported Attributes for Array Extraction: `price_today`, `prices_today`, `prices`, `data`, `raw_today`, `price_tomorrow`, `prices_tomorrow`, `raw_tomorrow`.

## Future Development Scope
*(Currently Implemented: State machine, separated AI charging targets, battery forecasting. Next: Add multi-inverter logic and additional managed load controllers).*
