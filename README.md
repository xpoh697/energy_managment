# Energy Management Integration for Home Assistant

Energy Management is an intelligent Home Assistant integration designed to optimize energy usage, track consumption profiles, and distribute solar generation surpluses efficiently. It bridges the gap between solar generation forecasting, volatile market prices, and heavy domestic appliances.

## Core Features

### 1. Hourly Profiling & Historical Analysis
The core of the integration relies on creating a "statistical daily profile" of your home's energy consumption. Unlike standard counters, this integration builds an array of historical hourly loads.
- It automatically separates data into **7-Day Profiles** (Monday to Sunday), accounting for unique usage patterns each day.
- It tracks **Occupancy** level for every hour, allowing the system to scale its consumption forecast if everyone is away.
- It integrates with **Solcast** for precise hourly solar distribution curves instead of simple averaging.
- It separates **Total Consumption** from **Base Consumption** (total minus controllable loads).
- By calculating an N-day moving average (with accelerated 7-day learning for transition seasons), it accurately estimates house requirements.

### 2. Auto-Adjusting Solar Forecast & Inverter Efficiency
Cloud coverage forecasts (like Forecast.Solar) are often over-optimistic. The integration contains a "Confidence Algorithm":
- **Forecast Correction & Real-time Adaptivity**: It compares daily actual production vs predicted and calculates a reliability coefficient. As the day progresses, it dynamically weights real-time generation more heavily (the "blended coefficient"), ensuring the system reacts immediately to unexpectedly high solar irradiation.
- **Inverter Efficiency (КПД)**: If you provide an inverter loss sensor, the system calculates real DC\u2194AC conversion efficiency. This ensures that battery discharge and solar forecasts are adjusted for real-world thermal losses.

### 3. Smart Energy Budget (Surplus Calculator)
The integration generates an `Energy Budget` sensor. The budget formula is:
`Available Budget = (Adjusted Solar Forecast \u00d7 \u041a\u041f\u0414) + (Current Battery Energy \u00d7 \u041a\u041f\u0414) - (Expected Base Consumption \u00d7 Occupancy Factor)`

**Note:** Starting from v5.2, the budget is calculated from the current minute **until 08:00 AM next morning**, and explicitly uses **Base Consumption** (total minus managed loads). This prevents double-counting and ensures your morning coffee is "pre-booked" in the battery before permitting a secondary boiler to run.

### 4. Hierarchical Load Permissions
You configure "Managed Loads" (Boilers, EV Chargers, etc.) with:
- **Priority**: A queueing system (Priority 1 gets energy first).
- **Daily Quota (kWh)**: How much energy the device needs today.
- **Bottleneck Protection (kW)**: Permission is revoked if your priority 1 load demands more power than the current solar surplus (preventing battery drain).
- **Cyclic Learning**: For washing machines, the AI learns the average cycle power and automatically releases the "reservation" once the quota is hit.

### 5. Market Strategy & Energy Arbitrage
The integration parses Buy/Sell prices (Nordpool, ENTSO-E, etc.) 48 hours ahead.
- **Smart Charge**: Identifies the cheapest windows to charge the battery.
- **Survival Bridge**: If the battery is predicted to die before the next cheap window, the system automatically finds the best "emergency" hour to top up.
- **Continuous BMS Simulation**: Instead of fixed charging, it simulates a **CC/CV charging curve** to accurately predict when the battery will be full.

### 6. Financial Analytics & ROI Tracking
- **Savings Tracker**: Separate tracking for Solar Self-consumption, Price Arbitrage profit, and Sale Revenue.
- **ROI / Payback Sensor**: Tracks total system investment vs. accumulated savings, providing an estimated payback date.
- **Battery Degradation**: Calculates the wear cost per kWh. Arbitrage gain is calculated as `SalePrice * efficiency - BuyPrice - DegradationCost`. Arbitrage is automatically blocked if the net profit is lower than the required threshold.

### 7. Anomaly Detector
Compares your house's instant power against the historical average for the current hour and day of the week. If consumption is significantly higher (e.g., 2.5x), it triggers an anomaly state.

### 8. Solar Curtailment Analysis
Track exactly how much free energy was lost because your battery was full and your home had no demand. This sensor provides actionable recommendations to improve your self-consumption ratio and ROI.

---

## Main Entities

| Entity | Description |
|--------|-------------|
| `\u041f\u0440\u043e\u0444\u0438\u043b\u0438 \u041f\u043e\u0442\u0440\u0435\u0431\u043b\u0435\u043d\u0438\u044f / \u0413\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438` | \u0421\u0443\u043c\u043c\u0430\u0440\u043d\u044b\u0435 \u043f\u0440\u043e\u0444\u0438\u043b\u0438 \u0437\u0430 \u043d\u0435\u0434\u0435\u043b\u044e, \u043c\u0435\u0441\u044f\u0446 \u0438 \u0433\u043e\u0434. |
| `\u041f\u0440\u043e\u0444\u0438\u0446\u0438\u0442 \u044d\u043d\u0435\u0440\u0433\u0438\u0438 \u0434\u043e \u0443\u0442\u0440\u0430` | \u0413\u043b\u0430\u0432\u043d\u044b\u0439 \u0441\u0435\u043d\u0441\u043e\u0440 \u0431\u044e\u0434\u0436\u0435\u0442\u0430 \u0441 \u0430\u0442\u0440\u0438\u0431\u0443\u0442\u0430\u043c\u0438 \u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438\u0439 (`permissions`). |
| `Inverter Mode Command` | \u041e\u0441\u043d\u043e\u0432\u043d\u0430\u044f \u043a\u043e\u043c\u0430\u043d\u0434\u0430 \u0434\u043b\u044f \u0430\u0432\u0442\u043e\u043c\u0430\u0442\u0438\u0437\u0430\u0446\u0438\u0439 (buy, sale_pv, stop_sale, etc). |
| `Market Strategy (Buy / Sell)` | \u0414\u0435\u0442\u0430\u043b\u044c\u043d\u044b\u0435 \u043f\u043b\u0430\u043d\u044b \u0437\u0430\u0440\u044f\u0434\u043a\u0438 \u0438 \u043f\u0440\u043e\u0434\u0430\u0436\u0438 \u0441 \u0433\u0440\u0430\u0444\u0438\u043a\u0430\u043c\u0438 \u0446\u0435\u043d. |
| `\u041f\u0440\u043e\u0433\u043d\u043e\u0437 \u0440\u0430\u0437\u0440\u044f\u0434\u0430 \u0431\u0430\u0442\u0430\u0440\u0435\u0438` | \u041f\u0440\u0435\u0434\u0441\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u0442 \u0447\u0430\u0441 \u0440\u0430\u0437\u0440\u044f\u0434\u0430 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440, \"\u0421\u0435\u0433\u043e\u0434\u043d\u044f \u0432 23:00\"). |
| `\u041f\u0440\u043e\u0433\u043d\u043e\u0437 \u0437\u0430\u0440\u044f\u0434\u0430 \u043a \u0437\u0430\u043a\u0430\u0442\u0443` | \u041e\u0436\u0438\u0434\u0430\u0435\u043c\u044b\u0439 SOC \u043a \u043c\u043e\u043c\u0435\u043d\u0442\u0443 \u0443\u0445\u043e\u0434\u0430 \u0441\u043e\u043b\u043d\u0446\u0430. |
| `\u041e\u043a\u0443\u043f\u0430\u0435\u043c\u043e\u0441\u0442\u044c \u0441\u0438\u0441\u0442\u0435\u043c\u044b (ROI)` | \u0424\u0438\u043d\u0430\u043d\u0441\u043e\u0432\u044b\u0439 \u0442\u0440\u0435\u043a\u0435\u0440 \u0441\u0438\u0441\u0442\u0435\u043c\u044b. |
| `\u0414\u0435\u0442\u0435\u043a\u0442\u043e\u0440 \u0430\u043d\u043e\u043c\u0430\u043b\u0438\u0439` | \u0421\u0435\u043d\u0441\u043e\u0440 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0438\u044f \u043e\u0442 \u0442\u0438\u043f\u0438\u0447\u043d\u043e\u0433\u043e \u043f\u0440\u043e\u0444\u0438\u043b\u044f. |
| `\u0421\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c \u0438\u0437\u043d\u043e\u0441\u0430 \u0431\u0430\u0442\u0430\u0440\u0435\u0438` | \u0421\u0442\u043e\u0438\u043c\u043e\u0441\u0442\u044c 1 \u043a\u0412\u0442\u00b7\u0447 \u043e\u0431\u043e\u0440\u043e\u0442\u0430 \u0410\u041a\u0411. |
| `\u0423\u043f\u0443\u0449\u0435\u043d\u043d\u0430\u044f \u0441\u043e\u043b\u043d\u0435\u0447\u043d\u0430\u044f \u044d\u043d\u0435\u0440\u0433\u0438\u044f` | \u0421\u0447\u0435\u0442\u0447\u0438\u043a \u043f\u043e\u0442\u0435\u0440\u044f\u043d\u043d\u043e\u0439 \u044d\u043d\u0435\u0440\u0433\u0438\u0438 \u0438\u0437-\u0437\u0430 \u043f\u0440\u043e\u0441\u0442\u043e\u044f PV. |
| `\u0412\u0440\u0435\u043c\u044f \u0430\u0432\u0442\u043e\u043d\u043e\u043c\u043d\u043e\u0439 \u0440\u0430\u0431\u043e\u0442\u044b` | \u0422\u0430\u0439\u043c\u0435\u0440 \"\u0432\u044b\u0436\u0438\u0432\u0430\u043d\u0438\u044f\" \u0431\u0435\u0437 \u0441\u0435\u0442\u0438 (\u0432 \u0447\u0430\u0441\u0430\u0445/\u043c\u0438\u043d\u0443\u0442\u0430\u0445). |

---

## Configuration

Installation is available via **HACS** or manual copy to `custom_components/energy_management`. Configuration is fully handled via the Home Assistant UI (Integrations page).

*Version: 1.4.0 (v5.2 core) | March 2026*
