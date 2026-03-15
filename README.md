# Energy Management Integration for Home Assistant

Energy Management is an intelligent Home Assistant integration designed to optimize energy usage, track consumption profiles, and distribute solar generation surpluses efficiently. It bridges the gap between solar generation forecasting, volatile market prices, and heavy domestic appliances.

## Core Features

### 1. Hourly Profiling & Historical Analysis
The core of the integration relies on creating a "statistical daily profile" of your home's energy consumption. Unlike standard counters, this integration builds an array of historical hourly loads.
- It automatically separates data into **Weekday** and **Weekend** profiles.
- It tracks **Occupancy** level for every hour, allowing the system to scale its consumption forecast if everyone is away.
- It separates **Total Consumption** from **Base Consumption** (total minus controllable loads).
- By calculating an N-day moving average, it accurately estimates exactly how much energy your house requires to survive the night.

### 2. Auto-Adjusting Solar Forecast & Inverter Efficiency
Cloud coverage forecasts (like Forecast.Solar) are often over-optimistic. The integration contains a "Confidence Algorithm":
- **Forecast Correction & Real-time Adaptivity**: It compares daily actual production vs predicted and calculates a reliability coefficient. As the day progresses, it dynamically weights real-time generation more heavily (the "blended coefficient"), ensuring the system reacts immediately to unexpectedly high solar irradiation.
- **Inverter Efficiency (КПД)**: If you provide an inverter loss sensor, the system calculates real DC↔AC conversion efficiency. This ensures that battery discharge and solar forecasts are adjusted for real-world thermal losses.

### 3. Smart Energy Budget (Surplus Calculator)
The integration generates an `Energy Budget` sensor. The budget formula is:
`Available Budget = (Adjusted Solar Forecast × КПД) + (Current Battery Energy × КПД) - (Expected Base Consumption × Occupancy Factor)`

**Note:** Starting from v3.2, the budget is calculated from the current minute **until 08:00 AM next morning**, and explicitly uses **Base Consumption** (total minus managed loads). This prevents double-counting and ensures your morning coffee is "pre-booked" in the battery before permitting a secondary boiler to run.

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
- **Battery Degradation**: Calculates the wear cost per kWh based on battery investment and rated cycles. Arbitrage is automatically blocked if the price difference is lower than the wear cost.

### 7. Anomaly Detector
Compares your house's instant power against the historical average for the current hour and day of the week. If consumption is significantly higher (e.g., 2.5x), it triggers an anomaly state.

### 8. Solar Curtailment Analysis
Track exactly how much free energy was lost because your battery was full and your home had no demand. This sensor provides actionable recommendations to improve your self-consumption ratio and ROI.

---

## Main Entities

| Entity | Description |
|--------|-------------|
| `Профили Потребления / Генерации` | Суммарные профили за неделю, месяц и год. |
| `Профицит энергии до утра` | Главный сенсор бюджета с атрибутами разрешений (`permissions`). |
| `Inverter Mode Command` | Основная команда для автоматизаций (buy, sale_pv, stop_sale, etc). |
| `Market Strategy (Buy / Sell)` | Детальные планы зарядки и продажи с графиками цен. |
| `Прогноз разряда батареи` | Предсказывает час разряда (например, "Сегодня в 23:00"). |
| `Прогноз заряда к закату` | Ожидаемый SOC к моменту ухода солнца. |
| `Окупаемость системы (ROI)` | Финансовый трекер системы. |
| `Детектор аномалий` | Сенсор отклонения от типичного профиля. |
| `Стоимость износа батареи` | Стоимость 1 кВт·ч оборота АКБ. |
| `Упущенная солнечная энергия` | Счетчик потерянной энергии из-за простоя PV. |
| `Время автономной работы` | Таймер "выживания" без сети (в часах/минутах). |

---

## Configuration

Installation is available via **HACS** or manual copy to `custom_components/energy_management`. Configuration is fully handled via the Home Assistant UI (Integrations page).

*Version: 1.3.4 (v3.2 core) | 2026*
