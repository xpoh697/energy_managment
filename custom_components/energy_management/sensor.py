import logging
import json
import os
from typing import Any, cast
from datetime import datetime, timedelta
from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change, async_track_time_interval
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import callback
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from .const import *
from .strategy import StrategyEngine
from .utils import get_kwh_val, normalize_float, get_price_from_store

# Legacy aliases for safety during refactoring synchronization
_get_kwh_val = get_kwh_val
_normalize_float = normalize_float
_get_stored_price = get_price_from_store

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform."""
    manager = hass.data[DOMAIN][entry.entry_id]

    entities = []

    # We will create 3 defined periods and 1 custom
    periods = {
        "month": ("Месяц", 30),
    }

    config_data = {**entry.data, **entry.options}
    custom_period = config_data.get(CONF_CUSTOM_PERIOD, 14)
    periods["custom"] = (f"Кастом ({custom_period} дн.)", custom_period)

    manager.set_max_days(max(365, custom_period))

    has_consumption = bool(config_data.get(CONF_CONSUMPTION_SENSORS, []))
    has_generation = bool(config_data.get(CONF_GENERATION_SENSORS, []))

    if has_consumption:
        for key, (name_ru, days) in periods.items():
            entities.append(ProfileAveragedSensor(manager, "consumption", key, f"Профиль Потребления ({name_ru})", days))
        entities.append(LiveHourlySensor(manager, "consumption", "Текущее почасовое потребление"))
        entities.append(TodayProfileSensor(manager, "consumption", "Потребление за сегодня (Профиль)"))

        # Add the Smart Budget sensor using the custom period length as the profile baseline
        entities.append(EnergyBudgetSensor(manager, "Профицит энергии до утра", custom_period))

    if has_generation:
        for key, (name_ru, days) in periods.items():
            entities.append(ProfileAveragedSensor(manager, "generation", key, f"Профиль Генерации ({name_ru})", days))
        entities.append(LiveHourlySensor(manager, "generation", "Текущая почасовая генерация"))
        entities.append(TodayProfileSensor(manager, "generation", "Генерация за сегодня (Профиль)"))

    if manager.price_buy_sensors:
        entities.append(MarketStrategySensor(manager, "buy", "Market BUY Strategy (Charge)"))
    if manager.price_sell_sensors:
        entities.append(MarketStrategySensor(manager, "sell", "Market SELL Strategy (Discharge)"))

    entities.append(InverterOperationModeSensor(manager, "Inverter Mode Command"))
    entities.append(ConsumptionDeviationSensor(manager, "Отклонение потребления (бытовое)"))

    if has_generation:
        entities.append(BatteryEndOfDaySOCSensor(manager, "Прогноз заряда (ближайший)"))

    if config_data.get(CONF_BATTERY_SOC) and config_data.get(CONF_BATTERY_CAPACITY):
        entities.append(BatteryAutonomySensor(manager, "Время автономной работы"))


    # Combined Savings / revenue tracking sensor
    if has_consumption and config_data.get(CONF_PRICE_BUY):
        entities.append(SavingsSensor(manager, "total", "Экономия: Итоговая выгода"))

        if config_data.get(CONF_BATTERY_POWER):
            entities.append(EnergyBalanceSensor(manager, "Энергетический кошелёк (Сальдо)"))

    # Advanced Analysis Sensors
    if has_consumption:
        entities.append(AnomalyDetectionSensor(manager, "Детектор аномалий потребления"))

    if config_data.get(CONF_TOTAL_SYSTEM_COST):
        entities.append(PaybackSensor(manager, "Окупаемость системы (ROI)"))

    if config_data.get(CONF_BATTERY_COST):
        entities.append(BatteryDegradationSensor(manager, "Стоимость износа батареи"))

    if has_consumption and has_generation:
        entities.append(InstantPowerAveragedSensor(manager, "load"))
        entities.append(InstantPowerAveragedSensor(manager, "gen"))
        entities.append(SolarWasteSensor(manager, "Упущенная солнечная энергия"))

    async_add_entities(entities)



class EnergyProfileManager:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry

        config_data = {**entry.data, **entry.options}

        # Initialize internal storage handler for preserving profiles across restarts
        self.store = Store(hass, STORAGE_VERSION, f"energy_management_{entry.entry_id}")

        self.strategy_engine = StrategyEngine(self)

        self.consumption_sensors: set[str] = set(config_data.get(CONF_CONSUMPTION_SENSORS, []) or [])
        self.generation_sensors: set[str] = set(config_data.get(CONF_GENERATION_SENSORS, []) or [])
        self.deduct_sensors: set[str] = set(config_data.get(CONF_DEDUCT_SENSORS, []) or [])
        self.grid_import_sensors: set[str] = set(config_data.get(CONF_GRID_IMPORT_SENSORS, []) or [])
        self.grid_export_sensors: set[str] = set(config_data.get(CONF_GRID_EXPORT_SENSORS, []) or [])
        raw_deduct = config_data.get(CONF_DEDUCT_SETTINGS, {})
        self.deduct_settings: dict[str, Any] = raw_deduct if isinstance(raw_deduct, dict) else {}
        self.all_sensors: set[str] = self.consumption_sensors | self.generation_sensors | self.deduct_sensors | self.grid_import_sensors | self.grid_export_sensors

        self.power_load_sensors = config_data.get(CONF_POWER_LOAD_SENSORS, [])
        self.power_gen_sensors = config_data.get(CONF_POWER_GEN_SENSORS, [])
        if isinstance(self.power_load_sensors, str): self.power_load_sensors = [self.power_load_sensors]
        if isinstance(self.power_gen_sensors, str): self.power_gen_sensors = [self.power_gen_sensors]

        today_forecasts = config_data.get(CONF_FORECAST_TODAY_REMAINING, [])
        self.forecast_today_sensor = [today_forecasts] if isinstance(today_forecasts, str) else today_forecasts

        tomorrow_forecasts = config_data.get(CONF_FORECAST_TOMORROW, [])
        self.forecast_tomorrow_sensor = [tomorrow_forecasts] if isinstance(tomorrow_forecasts, str) else tomorrow_forecasts
        self.battery_soc_sensor = config_data.get(CONF_BATTERY_SOC)
        self.battery_capacity_sensor = config_data.get(CONF_BATTERY_CAPACITY)
        self.battery_power_sensor = config_data.get(CONF_BATTERY_POWER)

        # Presence / occupancy sensors (person.* or binary_sensor.*)
        presence_raw = config_data.get(CONF_PRESENCE_SENSORS, [])
        self.presence_sensors = [s.strip() for s in ([presence_raw] if isinstance(presence_raw, str) else (presence_raw or [])) if isinstance(s, str)]

        self.all_power_sensors: set[str] = set()
        if self.power_load_sensors: self.all_power_sensors.update(self.power_load_sensors)
        if self.power_gen_sensors: self.all_power_sensors.update(self.power_gen_sensors)
        if self.battery_power_sensor: self.all_power_sensors.add(self.battery_power_sensor)

        raw_deduct = config_data.get(CONF_DEDUCT_SETTINGS, {})
        self.deduct_settings = {}
        if isinstance(raw_deduct, dict):
            for s_id, s_conf in raw_deduct.items():
                # Clean nested power sensor IDs
                if isinstance(s_conf, dict) and CONF_POWER_SENSOR in s_conf:
                    if isinstance(s_conf[CONF_POWER_SENSOR], str):
                        p_s = s_conf[CONF_POWER_SENSOR].strip()
                        s_conf[CONF_POWER_SENSOR] = p_s
                        self.all_power_sensors.add(p_s)
                self.deduct_settings[s_id.strip()] = s_conf

        self.consumption_sensors = {s.strip() for s in self.consumption_sensors if isinstance(s, str)}
        self.generation_sensors = {s.strip() for s in self.generation_sensors if isinstance(s, str)}
        self.deduct_sensors = {s.strip() for s in self.deduct_sensors if isinstance(s, str)}
        self.grid_import_sensors = {s.strip() for s in self.grid_import_sensors if isinstance(s, str)}
        self.grid_export_sensors = {s.strip() for s in self.grid_export_sensors if isinstance(s, str)}
        
        if self.battery_soc_sensor and isinstance(self.battery_soc_sensor, str):
            self.battery_soc_sensor = self.battery_soc_sensor.strip()
        if self.battery_capacity_sensor and isinstance(self.battery_capacity_sensor, str):
            self.battery_capacity_sensor = self.battery_capacity_sensor.strip()
        if self.battery_power_sensor and isinstance(self.battery_power_sensor, str):
            self.battery_power_sensor = self.battery_power_sensor.strip()

        buy_p = config_data.get(CONF_PRICE_BUY)
        sell_p = config_data.get(CONF_PRICE_SELL)
        self.price_buy_sensors: list[str] = [str(buy_p)] if buy_p else []
        self.price_sell_sensors: list[str] = [str(sell_p)] if sell_p else []

        self.all_price_sensors: set[str] = set([s for s in (self.price_buy_sensors + self.price_sell_sensors) if s])

        self.max_days = 365
        self.custom_period = config_data.get(CONF_CUSTOM_PERIOD, 14)

        # Internal configuration from UI (Number/Switch defaults handled by platform)
        self.settings: dict[str, Any] = {}

        # Array to store history of consumption per hour. e.g. "13" -> [1.3, 1.2, 1.5...]
        self.data: dict[str, Any] = {}

        self.current_consumption_base = 0.0
        self.current_consumption_total = 0.0
        self.current_generation = 0.0
        self.current_grid_import = 0.0
        self.current_grid_export = 0.0
        self.current_hourly_deduct = 0.0  # Accumulator for all deduct sensors this hour
        self.sensor_last_values: dict[str, float] = {}

        self.daily_deduct_consumption: dict[str, float] = {s: 0.0 for s in self.deduct_sensors}

        self.update_listeners = []
        self._unsub_state = None
        self._unsub_time = None
        self._unsub_power_poll = None
        self._unsub_periodic_save = None

        # Inverter losses sensor (daily kWh counter that resets at midnight)
        losses_raw = config_data.get(CONF_INVERTER_LOSSES_SENSOR)
        self.inverter_losses_sensor = losses_raw if losses_raw else None
        self.current_losses = 0.0  # kWh accumulated this hour
        if self.inverter_losses_sensor:
            self.all_sensors = self.all_sensors | {str(self.inverter_losses_sensor)}
        if self.battery_power_sensor:
            self.all_sensors = self.all_sensors | {str(self.battery_power_sensor)}

        # Track historical power samples for 5-10 minute average smoothing
        self.power_history: list[dict[str, Any]] = []

        # Power sensor runtime tracking
        self.learned_standby_power: dict[str, float] = {}
        self.learned_real_power: dict[str, float] = {}
        self.learned_avg_cycle_power: dict[str, float] = {}
        self.learned_cycle_total_kwh: dict[str, float] = {}
        self.learned_avg_cycle_duration: dict[str, float] = {}  # In seconds
        self.cycle_start_time: dict[str, datetime] = {}
        self.cycle_actual_start_time: dict[str, datetime] = {}
        self.cycle_energy_start: dict[str, float] = {}
        self.last_known_power: dict[str, float] = {}
        # Sensors that need to re-establish a baseline on first read after restart
        # (prevents large accumulated deltas from being counted as generation/consumption)
        self._sensors_need_baseline: set = set()

        self.current_solar_waste_power = 0.0
        self.last_blended_coeff = 1.0
        self._profile_cache = {}

    def set_max_days(self, days):
        self.max_days = days

    async def async_load(self):
        stored = await self.store.async_load()
        if stored:
            self.data = stored
            # Retroactive cleanup for impossible data recorded prior to the 100kwh delta limits
            for ptype in ["consumption_base", "consumption_total", "generation"]:
                if ptype in self.data:
                    for h_key in self.data[ptype]:
                        clean_list = []
                        for item in self.data[ptype][h_key]:
                            try:
                                if isinstance(item, dict):
                                    val = float(str(item.get("v", 0.0)).replace(',', '.'))
                                else:
                                    val = float(str(item).replace(',', '.'))
                                if val <= 100.0:
                                    clean_list.append(item)
                            except ValueError:
                                pass
                        self.data[ptype][h_key] = clean_list

        self.settings = self.data.get("settings", {})
        self.learned_standby_power = self.data.get("learned_standby_power", {})
        self.learned_real_power = self.data.get("learned_real_power", {})
        self.learned_avg_cycle_power = self.data.get("learned_avg_cycle_power", {})
        self.learned_cycle_total_kwh = self.data.get("learned_cycle_total_kwh", {})
        self.learned_avg_cycle_duration = self.data.get("learned_avg_cycle_duration", {})
        
        # Restore cycle start times (handle ISO strings or missing)
        saved_starts = self.data.get("cycle_actual_start_time", {})
        for s_id, start_str in saved_starts.items():
            try:
                self.cycle_actual_start_time[s_id] = dt_util.parse_datetime(start_str)
            except:
                pass

        if "generation" not in self.data:
            self.data["generation"] = {str(i): [] for i in range(24)}
        if "consumption_total" not in self.data:
            self.data["consumption_total"] = {str(i): [] for i in range(24)}
        if "consumption_base" not in self.data:
            if "consumption" in self.data:
                self.data["consumption_base"] = self.data.pop("consumption")
            else:
                self.data["consumption_base"] = {str(i): [] for i in range(24)}

        if "forecast_history" not in self.data:
            self.data["forecast_history"] = []
        if "temp_daily_gen" not in self.data:
            self.data["temp_daily_gen"] = 0.0
        if "temp_max_forecast" not in self.data:
            self.data["temp_max_forecast"] = 0.0
        if "temp_daily_waste" not in self.data:
            self.data["temp_daily_waste"] = 0.0

        if "prices_sell" not in self.data:
            self.data["prices_sell"] = {}
        if "prices_buy" not in self.data:
            self.data["prices_buy"] = {}

        if "energy_balance_today_start" not in self.data:
            self.data["energy_balance_today_start"] = self.data.get("energy_balance", 0.0)

        if "savings" not in self.data:
            self.data["savings"] = {}  # {"YYYY-MM-DD": {"solar": x, "arbitrage": x, "sell": x}}

        self.sensor_last_values = self.data.get("sensor_last_values", {})
        # Mark ALL known sensors as needing a fresh baseline on first reading.
        # This prevents restart delta spikes when HA was offline while sensors accumulated data.
        self._sensors_need_baseline = set(self.sensor_last_values.keys())

        # Restore daily deduct consumption (how much each managed load already consumed today)
        saved_deduct = self.data.get("daily_deduct_consumption", {})
        for s in self.deduct_sensors:
            self.daily_deduct_consumption[s] = saved_deduct.get(s, 0.0)

        # Restore hourly accumulators (energy accumulated since the last hour-top save)
        accum = self.data.get("hourly_accumulators", {})
        self.current_consumption_total = accum.get("consumption_total", 0.0)
        self.current_generation = accum.get("generation", 0.0)
        self.current_grid_import = accum.get("grid_import", 0.0)
        self.current_grid_export = accum.get("grid_export", 0.0)
        self.current_losses = accum.get("losses", 0.0)
        self.current_hourly_deduct = accum.get("hourly_deduct", 0.0)

        # Recalculate base from total and deduct
        self.current_consumption_base = max(0.0, self.current_consumption_total - self.current_hourly_deduct)

    async def async_save(self):
        self.data["learned_standby_power"] = self.learned_standby_power
        self.data["learned_real_power"] = self.learned_real_power
        self.data["learned_avg_cycle_power"] = self.learned_avg_cycle_power
        self.data["learned_cycle_total_kwh"] = self.learned_cycle_total_kwh
        self.data["learned_avg_cycle_duration"] = self.learned_avg_cycle_duration
        self.data["cycle_actual_start_time"] = {
            s_id: dt.isoformat() for s_id, dt in self.cycle_actual_start_time.items()
        }
        self.data["sensor_last_values"] = self.sensor_last_values
        self.data["daily_deduct_consumption"] = dict(self.daily_deduct_consumption)
        self.data["hourly_accumulators"] = {
            "consumption_total": self.current_consumption_total,
            "generation": self.current_generation,
            "grid_import": self.current_grid_import,
            "grid_export": self.current_grid_export,
            "losses": self.current_losses,
            "hourly_deduct": self.current_hourly_deduct,
            "hourly_deduct": self.current_hourly_deduct
        }
        await self.store.async_save(self.data)

    def export_data(self, file_path):
        """Export internal data dict to a JSON file."""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to export data: {e}")
            return False

    def import_data(self, file_path):
        """Import internal data dict from a JSON file."""
        if not os.path.exists(file_path):
            return False

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                imported_data = json.load(f)

            # Basic validation to ensure we don't crash HA with garbled JSON
            if isinstance(imported_data, dict) and "consumption_base" in imported_data:
                self.data = imported_data
                self.settings = self.data.get("settings", {})

                # v2.1.4 - Hot Restore live values from imported data
                
                # 1. Daily Deduct Consumption (Managed loads)
                saved_deduct = self.data.get("daily_deduct_consumption", {})
                for s in self.deduct_sensors:
                    self.daily_deduct_consumption[s] = saved_deduct.get(s, 0.0)

                # 2. Hourly accumulators
                accum = self.data.get("hourly_accumulators", {})
                self.current_consumption_total = accum.get("consumption_total", 0.0)
                self.current_generation = accum.get("generation", 0.0)
                self.current_grid_import = accum.get("grid_import", 0.0)
                self.current_grid_export = accum.get("grid_export", 0.0)
                self.current_losses = accum.get("losses", 0.0)
                self.current_hourly_deduct = accum.get("hourly_deduct", 0.0)
                self.current_consumption_base = max(0.0, self.current_consumption_total - self.current_hourly_deduct)

                # 3. Learned values and baselines
                self.learned_standby_power = self.data.get("learned_standby_power", {})
                self.learned_real_power = self.data.get("learned_real_power", {})
                self.learned_avg_cycle_power = self.data.get("learned_avg_cycle_power", {})
                self.learned_cycle_total_kwh = self.data.get("learned_cycle_total_kwh", {})
                self.sensor_last_values = self.data.get("sensor_last_values", {})
                
                # 4. Notify all entities to refresh their state
                self._notify_update()

                return True
        except Exception as e:
            _LOGGER.error(f"Failed to import data: {e}")

        return False

    async def async_stop(self):
        """Cleanup all listeners and tasks."""
        if self._unsub_state:
            self._unsub_state()
        if self._unsub_time:
            self._unsub_time()
        if self._unsub_power_poll:
            self._unsub_power_poll()
        if self._unsub_periodic_save:
            self._unsub_periodic_save()

        self._unsub_state = None
        self._unsub_time = None
        self._unsub_power_poll = None
        self._unsub_periodic_save = None

    async def async_start(self):
        # Parse prices immediately on load
        for p_sensor in self.all_price_sensors:
            state_obj = self.hass.states.get(p_sensor)
            if state_obj:
                self._update_prices_from_sensor(p_sensor, state_obj)

        # Recover missed energy deltas between the last save (hour top) and now
        class MockEvent:
            def __init__(self, data):
                self.data = data

        for entity_id in self.all_sensors:
            state_obj = self.hass.states.get(entity_id)
            if state_obj:
                ev = MockEvent({"entity_id": entity_id, "new_state": state_obj})
                self._async_state_changed(ev)

        monitored_sensors = self.all_sensors | self.all_price_sensors | self.all_power_sensors
        if self.battery_soc_sensor: monitored_sensors.add(self.battery_soc_sensor)
        if self.battery_capacity_sensor: monitored_sensors.add(self.battery_capacity_sensor)
        if self.forecast_today_sensor: monitored_sensors.update(self.forecast_today_sensor)
        if self.forecast_tomorrow_sensor: monitored_sensors.update(self.forecast_tomorrow_sensor)

        self._unsub_state = async_track_state_change_event(
            self.hass, list(monitored_sensors), self._async_state_changed
        )
        # Trigger at exactly minute=0, second=0 every hour
        self._unsub_time = async_track_time_change(
            self.hass, self._async_reset_hour, minute=0, second=0
        )

        # Poll instant power every 1 minute for averaging
        if self.power_load_sensors or self.power_gen_sensors:
            self._unsub_power_poll = async_track_time_interval(
                self.hass, self._poll_instant_power, timedelta(minutes=1)
            )
            # Perform initial poll
            self._poll_instant_power(dt_util.now())

        # Periodic save to disk every 5 minutes to prevent data loss on frequent restarts
        self._unsub_periodic_save = async_track_time_interval(
            self.hass, self._async_periodic_save, timedelta(minutes=5)
        )

    @callback
    def _poll_instant_power(self, now):
        """Poll and save the current instantaneous power levels for averaging."""
        load_kw = 0.0
        gen_kw = 0.0

        if self.power_load_sensors:
            load_kw = sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_load_sensors) # sync_v2.1.3
        if self.power_gen_sensors:
            gen_kw = sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_gen_sensors)

        self.power_history.append({"time": now, "load_kw": float(load_kw), "gen_kw": float(gen_kw)})

        # --- Real-time Balance / Savings Account Logic ---
        # Logic: Increment/Decrement based on (Solar_to_Load + Battery_to_Load - Grid_to_Battery)
        # We need at least price and load power to calculate any savings.
        if self.price_buy_sensors and self.power_load_sensors:
            p_buy = self.get_price("buy", now.strftime("%Y-%m-%d"), now.hour) or 0.0
            p_sell = self.get_price("sell", now.strftime("%Y-%m-%d"), now.hour) or 0.0

            batt_p = 0.0
            if self.battery_power_sensor:
                st = self.hass.states.get(self.battery_power_sensor)
                batt_p = get_kwh_val(st) or 0.0 # _get_kwh_val handles W/kW conversion

            # Time delta in hours (polling is roughly 1 min)
            last_run = self.data.get("last_balance_poll_time")
            now_ts = now.timestamp()
            if last_run:
                dt_h = (now_ts - last_run) / 3600.0
                if 0 < dt_h < 0.2: # Guard against huge leaps
                    # 1. Solar to Load = energy we didn't buy because of PV
                    s_to_l = min(gen_kw, load_kw)

                    # 2. Battery to Load = energy we didn't buy because of Battery
                    # (only counts if it covers remaining load)
                    load_rem = max(0.0, load_kw - gen_kw)
                    b_to_l = min(max(0.0, batt_p), load_rem)

                    # 3. Grid to Battery = energy we bought specifically to fill the buffer
                    p_charge = max(0.0, -batt_p)
                    s_avail_for_batt = max(0.0, gen_kw - load_kw)
                    g_to_b = max(0.0, p_charge - s_avail_for_batt)

                    # 4. Grid Export = surplus energy we sold
                    grid_export_kw = max(0.0, gen_kw + batt_p - load_kw)

                    # Net saving power in kW for this moment
                    net_saving_kw = s_to_l + b_to_l - g_to_b

                    # Total incremental wallet change: savings from self-consumption + revenue from sales
                    step_delta = (net_saving_kw * p_buy * dt_h) + (grid_export_kw * p_sell * dt_h)

                    current_bal = self.data.get("energy_balance", 0.0)
                    self.data["energy_balance"] = round(current_bal + step_delta, 4)

            self.data["last_balance_poll_time"] = now_ts

        # Prune older than 10 minutes
        cutoff = now - timedelta(minutes=10)
        self.power_history = [x for x in self.power_history if x["time"] >= cutoff]

        # --- Power Learning & Cycle Tracking ---
        for sensor_id, settings in self.deduct_settings.items():
            if not isinstance(settings, dict): continue
            p_entity = settings.get(CONF_POWER_SENSOR)
            if not p_entity: continue

            p_state = self.hass.states.get(p_entity)
            if not p_state or p_state.state in ("unknown", "unavailable"): continue

            try:
                cur_p = float(str(p_state.state).replace(',', '.'))
                if p_state.attributes.get("unit_of_measurement") == "kW":
                    cur_p *= 1000.0
                self.last_known_power[sensor_id] = cur_p
            except ValueError: continue

            standby = self.learned_standby_power.get(sensor_id, 15.0)
            is_active = cur_p > (standby + 10.0)

            if is_active:
                # Still active -> push forward the "last seen active" time for grace period
                self.cycle_start_time[sensor_id] = now

                # If this is the start of a new cycle
                if sensor_id not in self.cycle_actual_start_time:
                    self.cycle_actual_start_time[sensor_id] = now
                    self.cycle_energy_start[sensor_id] = self.daily_deduct_consumption.get(sensor_id, 0.0)

                # Active Power Learning (EMA)
                old_real = float(self.learned_real_power.get(sensor_id, cur_p))
                if settings.get(CONF_IS_CYCLIC):
                    # For cyclic, we use a slow EMA as it might have different phases
                    self.learned_real_power[sensor_id] = round(old_real * 0.9 + float(cur_p) * 0.1, 1)
                else:
                    # For non-cyclic (Boiler, etc), we want the stable peak power observed
                    if float(cur_p) >= old_real:
                        # Instant jump to new higher peak
                        self.learned_real_power[sensor_id] = round(float(cur_p), 1)
                    else:
                        # Very slow decay if current is lower (to filter ramps and capture steady "on" state)
                        self.learned_real_power[sensor_id] = round(old_real * 0.98 + float(cur_p) * 0.02, 1)
            else:
                # Standby Power Learning (Slow EMA)
                if 0.1 < cur_p < (standby + 5.0):
                    old_s = float(self.learned_standby_power.get(sensor_id, cur_p))
                    self.learned_standby_power[sensor_id] = round(old_s * 0.95 + float(cur_p) * 0.05, 2)

                # If we just finished a cycle
                if sensor_id in self.cycle_actual_start_time:
                    duration = (now - self.cycle_actual_start_time[sensor_id]).total_seconds() / 3600.0
                    energy = self.daily_deduct_consumption.get(sensor_id, 0.0) - self.cycle_energy_start.get(sensor_id, 0.0)

                    if energy > 0.02 and duration > (1/60.0): # At least 20Wh and 1 minute
                        avg_p_w = (float(energy) * 1000.0) / float(duration)
                        if settings.get(CONF_IS_CYCLIC):
                            self.learned_real_power[sensor_id] = round(float(avg_p_w), 1)
                        # For non-cyclic, we DON'T overwrite with average, as average is skewed by thermostat OFF time
                        if settings.get(CONF_IS_CYCLIC):
                            self.learned_cycle_total_kwh[sensor_id] = round(float(energy), 3)
                            self.learned_avg_cycle_power[sensor_id] = round(float(avg_p_w), 1)
                            
                            # Update historical duration (EMA)
                            dur_secs = (now - self.cycle_actual_start_time[sensor_id]).total_seconds()
                            old_dur = float(self.learned_avg_cycle_duration.get(sensor_id, dur_secs))
                            self.learned_avg_cycle_duration[sensor_id] = round(old_dur * 0.7 + dur_secs * 0.3, 0)

                    self.cycle_actual_start_time.pop(sensor_id, None)
                    self.cycle_energy_start.pop(sensor_id, None)

        # --- Solar Waste Calculation ---
        if self.power_gen_sensors and self.generation_sensors:
            # We need potential power. We'll use the profile-based estimate.
            # (Calculation of blended_coeff is done in budget/strategy, but we'll use a snapshot here)
            prof_gen_today = self.get_average_profile("generation", self.custom_period, "all")
            cur_expected_gen = float(prof_gen_today.get(str(now.hour), 0.0))
            potential_kw = cur_expected_gen * self.last_blended_coeff

            soc, _, _ = self.get_battery_state()
            # Waste occurs if battery is near full and we generate less than the panels could potentially give
            if soc >= 97.0 and potential_kw > (gen_kw + 0.1):
                waste_kw = potential_kw - gen_kw
                self.current_solar_waste_power = round(float(waste_kw), 3)
                # Accumulate kWh (1 min sample)
                self.data["temp_daily_waste"] = self.data.get("temp_daily_waste", 0.0) + (waste_kw / 60.0)
            else:
                self.current_solar_waste_power = 0.0

        self._notify_update()

    @property
    def now(self):
        return dt_util.now()

    @property
    def day_type(self):
        return "weekend" if self.now.weekday() >= 5 else "weekday"

    @property
    def avg_load_kw(self):
        if not self.power_history:
            return 0.0
        val = sum(x["load_kw"] for x in self.power_history) / len(self.power_history)
        return round(float(val), 3)

    @property
    def avg_gen_kw(self):
        if not self.power_history:
            return 0.0
        val = sum(x["gen_kw"] for x in self.power_history) / len(self.power_history)
        return round(float(val), 3)

    async def _async_periodic_save(self, _now):
        """Periodically persist data to disk between hour-top resets."""
        await self.async_save()

    @callback
    def _async_state_changed(self, event):
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if entity_id in self.all_price_sensors:
            self._update_prices_from_sensor(entity_id, new_state)
            return

        # Handle power sensors (trigger re-calculation of current power and balance)
        if entity_id in self.all_power_sensors:
            self._poll_instant_power(dt_util.now())
            return

        # Handle energy sensors
        new_val = get_kwh_val(new_state)
        if new_val is None:
            return

        old_val = self.sensor_last_values.get(entity_id)
        is_restarting = entity_id in self._sensors_need_baseline
        self._sensors_need_baseline.discard(entity_id)

        # Protective logic:
        # On first read ever (new sensor), just establish a baseline.
        if old_val is None:
            self.sensor_last_values[entity_id] = new_val
            return

        delta = new_val - old_val

        if delta < 0:
            # The sensor reset its internal counter (e.g. daily/monthly reset on the device).
            # Usually the new delta is just the new value.
            delta = new_val

        # If it is the first read after restart, the delta might be large.
        if is_restarting and (delta <= 0 or delta > 50.0):
             self.sensor_last_values[entity_id] = new_val
             return

        if delta > 100.0:
            _LOGGER.warning("Energy Management: Ignored impossible delta of %s kWh for sensor %s. Baseline reset.", delta, entity_id)
            self.sensor_last_values[entity_id] = new_val
            return

        self.sensor_last_values[entity_id] = new_val
        if delta == 0:
            return

        # Update accumulators
        if entity_id in self.consumption_sensors:
            self.current_consumption_total += delta
        if entity_id in self.deduct_sensors:
            self.current_hourly_deduct += delta
            self.daily_deduct_consumption[entity_id] = self.daily_deduct_consumption.get(entity_id, 0.0) + delta
        if entity_id in self.generation_sensors:
            self.current_generation += delta
            self.data["temp_daily_gen"] = self.data.get("temp_daily_gen", 0.0) + delta
        if self.inverter_losses_sensor and entity_id == self.inverter_losses_sensor:
            if delta > 0:
                self.current_losses += delta
        if entity_id in self.grid_import_sensors:
            self.current_grid_import += delta
        if entity_id in self.grid_export_sensors:
            self.current_grid_export += delta

        # Consolidate base consumption: total meter minus all managed loads
        self.current_consumption_base = max(0.0, self.current_consumption_total - self.current_hourly_deduct)

        if self.current_consumption_base < 0:
            self.current_consumption_base = 0.0
        if self.current_consumption_total < 0:
            self.current_consumption_total = 0.0

        self._notify_update()

    async def _async_reset_hour(self, now):
        # v2.1.1 - Forced clean indentation
        self._profile_cache = {}
        past_hour = (now.hour - 1) % 24

        today_wd = now.weekday()

        # Track occupancy at snapshot time
        occ_count = self.get_current_occupancy()

        # Append to history lists (with occupancy tag)
        self.data["consumption_base"][str(past_hour)].append({"v": self.current_consumption_base, "wd": today_wd, "occ": occ_count})
        self.data["consumption_total"][str(past_hour)].append({"v": self.current_consumption_total, "wd": today_wd, "occ": occ_count})
        self.data["generation"][str(past_hour)].append({"v": self.current_generation, "wd": today_wd})

        # Store losses alongside generation for efficiency calculation
        if "losses" not in self.data:
            self.data["losses"] = {str(i): [] for i in range(24)}
        self.data["losses"][str(past_hour)].append({"v": self.current_losses, "gen": self.current_generation})
        if len(self.data["losses"][str(past_hour)]) > self.max_days:
            self.data["losses"][str(past_hour)] = self.data["losses"][str(past_hour)][-self.max_days:]

        # Trim history arrays to ensure we don't leak memory and only keep required `max_days`
        for h in range(24):
            sh = str(h)
            if len(self.data["consumption_base"][sh]) > self.max_days:
                self.data["consumption_base"][sh] = self.data["consumption_base"][sh][-self.max_days:]
            if len(self.data["consumption_total"][sh]) > self.max_days:
                self.data["consumption_total"][sh] = self.data["consumption_total"][sh][-self.max_days:]
            if len(self.data["generation"][sh]) > self.max_days:
                self.data["generation"][sh] = self.data["generation"][sh][-self.max_days:]

        # Save exact sensor limits at the top of the hour to disk for reboot recovery
        self.data["sensor_last_values"] = self.sensor_last_values

        # ── Hourly Savings Tracking ────────────────────────────────────────────
        try:
            if self.price_buy_sensors or self.price_sell_sensors:
                past_dt = now - timedelta(hours=1)
                past_date_str = past_dt.strftime("%Y-%m-%d")

                p_buy  = self.get_price("buy",  past_date_str, past_hour)
                p_sell = self.get_price("sell", past_date_str, past_hour)

                gen_h  = self.current_generation
                cons_h = self.current_consumption_total

                # Battery SOC delta across this hour
                batt_cap_h = self.get_sensor_float(self.battery_capacity_sensor, 0.0)
                soc_now    = self.get_sensor_float(self.battery_soc_sensor, 0.0)
                last_soc_v = self.data.get("last_soc_savings", soc_now)
                soc_delta  = soc_now - last_soc_v
                kwh_delta  = batt_cap_h * soc_delta / 100.0 if batt_cap_h > 0 else 0.0
                self.data["last_soc_savings"] = soc_now

                batt_charged    = max(0.0,  kwh_delta)
                batt_discharged = max(0.0, -kwh_delta)

                # ── Unified Savings Logic ───────────────────────────────────────────
                # Formula: (Consumption * p_buy) - (Grid_Buy * p_buy) + (Grid_Sell * p_sell)
                # This accounts for solar self-consumption, arbitrage, and sales in one go.

                # We need grid_buy_h and grid_sell_h.
                # If we have direct import/export sensors, use them.
                # Otherwise derive from mathematical balance (which can have errors due to КПД/SOC drift).
                if self.grid_import_sensors or self.grid_export_sensors:
                    h_buy_kwh = self.current_grid_import
                    h_sell_kwh = self.current_grid_export
                else:
                    grid_flow = cons_h + batt_charged - gen_h - batt_discharged
                    h_buy_kwh  = max(0.0,  grid_flow)
                    h_sell_kwh = max(0.0, -grid_flow)

                # 1. Total Benefit Component
                # Value of not having the system (Baseline)
                baseline_cost = cons_h * (p_buy or 0.0)
                # Actual cost now
                actual_net_cost = (h_buy_kwh * (p_buy or 0.0)) - (h_sell_kwh * (p_sell or 0.0))

                total_profit_h = round(baseline_cost - actual_net_cost, 4)

                # Persist to "total" category
                if "savings" not in self.data:
                    self.data["savings"] = {}
                day_entry = self.data["savings"].setdefault(
                    past_date_str, {"total": 0.0, "solar": 0.0, "arbitrage": 0.0, "sell": 0.0})

                day_entry["total"] = round(day_entry.get("total", 0.0) + total_profit_h, 4)

                # Also keep old components as breakdown (for attributes)
                solar_self = min(gen_h, cons_h)
                day_entry["solar"]     = round(day_entry.get("solar",     0.0) + (solar_self * (p_buy or 0.0)), 4)
                day_entry["sell"]      = round(day_entry.get("sell",      0.0) + (h_sell_kwh * (p_sell or 0.0)), 4)
                # Arbitrage is the remainder
                day_entry["arbitrage"] = round(day_entry["total"] - day_entry["solar"] - day_entry["sell"], 4)

                # Keep at most 400 days of savings
                if len(self.data["savings"]) > 400:
                    del self.data["savings"][sorted(self.data["savings"].keys())[0]]

                # Trim price stores to 60 days
                cutoff_dt = now - timedelta(days=60)
                cutoff_date = cutoff_dt.strftime("%Y-%m-%d")
                for p_store_kr in ["prices_buy", "prices_sell"]:
                    p_store = self.data.get(p_store_kr, {})
                    for d_str in list(p_store.keys()):
                        if d_str < cutoff_date:
                            del p_store[d_str]
        except Exception as e:
            _LOGGER.error("Energy Management: Error in hourly savings tracking: %s", e)

        # ── End savings tracking ───────────────────────────────────────────────

        # Reset counters BEFORE saving, so that the saved accumulators reflect
        # the NEW hour (zeroed out). This prevents double-counting if HA restarts:
        # the old hour's data is already committed to the profile history above.
        self.current_consumption_base = 0.0
        self.current_consumption_total = 0.0
        self.current_generation = 0.0
        self.current_grid_import = 0.0
        self.current_grid_export = 0.0
        self.current_losses = 0.0
        self.current_hourly_deduct = 0.0

        # Reset daily deduct consumption and daily balance start at midnight
        if now.hour == 0:
            self.data["last_reset_date"] = now.strftime("%Y-%m-%d")
            # Clear managed loads daily counters
            for s in self.daily_deduct_consumption:
                self.daily_deduct_consumption[s] = 0.0
            
            # Record current balance as start-of-day baseline for the "Energy Wallet"
            self.data["energy_balance_today_start"] = self.data.get("energy_balance", 0.0)

            # Forecast history rolling update
            actual = self.data.get("temp_daily_gen", 0.0)
            expected = self.data.get("temp_max_forecast", 0.0)

            if expected > 0.1 or actual > 0.1:
                if "forecast_history" not in self.data:
                    self.data["forecast_history"] = [] # No daily reset needed anymore as we rely on get_todays_profile logic
                                                       # which evaluates hours 0-23
                self.data["forecast_history"].append({
                    "actual": round(actual, 3),
                    "forecast": round(expected, 3),
                    "date": now.strftime("%Y-%m-%d")
                })
                # Keep up to configured max days of history for the coefficient
                custom_period = self.entry.data.get(CONF_CUSTOM_PERIOD, 14)
                if len(self.data["forecast_history"]) > custom_period:
                    self.data["forecast_history"] = self.data["forecast_history"][-custom_period:]

            # Reset day temps
            self.data["temp_daily_gen"] = 0.0
            self.data["temp_daily_cons_total"] = 0.0
            self.data["temp_max_forecast"] = 0.0
            self.data["temp_daily_waste"] = 0.0



        # Prune historical prices to keep storage file small
        # We keep only yesterday, today, and any future forecasts
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        for p_key in ["prices_buy", "prices_sell"]:
            if p_key in self.data:
                store = self.data[p_key]
                to_delete = [d for d in store.keys() if d < yesterday_str]
                for d in to_delete:
                    del store[d]

        # Save to internal filesystem AFTER all resets
        await self.async_save()

        self._notify_update()

    def register_listener(self, update_cb):
        self.update_listeners.append(update_cb)

    def _notify_update(self):
        for cb in self.update_listeners:
            cb()

    async def async_set_setting(self, key, value):
        self.settings[key] = value
        self.data["settings"] = self.settings
        await self.async_save()
        self._notify_update()

    def get_active_managed_loads_power(self, hour_offset=0):
        """Calculate total power of currently active managed loads for simulation."""
        now = dt_util.now()
        active_load_kw = 0.0
        for s_id, s_settings in self.get_setting(CONF_DEDUCT_SETTINGS, {}).items():
            if self._is_currently_pulling_power(s_id):
                is_cyclic = s_settings.get(CONF_IS_CYCLIC, False)
                if is_cyclic and s_id in self.learned_avg_cycle_power:
                    p_kw = self.learned_avg_cycle_power[s_id] / 1000.0
                else:
                    p_kw = self.learned_real_power.get(s_id, s_settings.get("required_kw", 0.0) * 1000.0) / 1000.0

                req_kwh = s_settings.get("required_kwh", 0.0)
                if req_kwh > 0:
                    remaining = max(0.0, req_kwh - self.daily_deduct_consumption.get(s_id, 0.0))
                    if p_kw > 0 and (hour_offset + 1) <= (remaining / p_kw):
                        active_load_kw += p_kw
                elif is_cyclic and s_id in self.cycle_actual_start_time:
                    # Estimate based on learned duration
                    start_time = self.cycle_actual_start_time[s_id]
                    avg_dur = self.learned_avg_cycle_duration.get(s_id, 3600.0) # default 1h
                    predicted_end = start_time + timedelta(seconds=avg_dur)
                    if (now + timedelta(hours=hour_offset)) < predicted_end:
                        active_load_kw += p_kw
                elif hour_offset == 0:
                    active_load_kw += p_kw
        return active_load_kw

    def _update_prices_from_sensor(self, entity_id, state_obj):
        if not state_obj:
            return

        res = {}
        # Parse arrays in attributes (NordPool, ENTSO-E, etc common formats)
        for attr in ["price_today", "prices_today", "prices", "data", "raw_today", "price_tomorrow", "prices_tomorrow", "raw_tomorrow"]:
            arr = state_obj.attributes.get(attr)
            if isinstance(arr, list):
                for item in arr:
                    if isinstance(item, dict):
                        start_str = item.get("start") or item.get("start_time") or item.get("time") or item.get("datetime")
                        price_val = item.get("price")
                        if price_val is None:
                            price_val = item.get("value")
                        if price_val is None:
                            price_val = item.get("total")

                        if start_str and price_val is not None:
                            try:
                                if "T" in str(start_str):
                                    p_date = str(start_str).split("T")[0]
                                    p_time = str(start_str).split("T")[1][:2]
                                    hour = str(int(p_time))
                                    if p_date not in res:
                                        res[p_date] = {}
                                    res[p_date][hour] = float(price_val)
                            except Exception:
                                pass

        # Fallback to current continuous state if no arrays exist
        if not res:
            try:
                val = float(state_obj.state)
                now = dt_util.now()
                d_str = now.strftime("%Y-%m-%d")
                h_str = str(now.hour)
                res = {d_str: {h_str: val}}
            except ValueError:
                pass

        # Merge into caching dictionary
        if res:
            if entity_id in self.price_buy_sensors:
                target = self.data["prices_buy"]
            elif entity_id in self.price_sell_sensors:
                target = self.data["prices_sell"]
            else:
                return

            for p_date, hours in res.items():
                if p_date not in target:
                    target[p_date] = {}
                for h, price in hours.items():
                    target[p_date][h] = price

    def get_total_savings(self):
        """Calculate cumulative savings since tracking began."""
        savings = self.data.get("savings", {})
        total = 0.0
        for day in savings.values():
            if isinstance(day, dict):
                total += day.get("total", 0.0)
        return total

    def get_battery_degradation_cost(self) -> float:
        """Cost of battery wear per kWh."""
        return self.strategy_engine.get_battery_degradation_cost()

    def get_expected_consumption(self):
        """Helper to get the expected consumption value for the current hour."""
        now = dt_util.now()
        day_type = "weekend" if now.weekday() >= 5 else "weekday"
        prof = self.get_average_profile("consumption_total", self.custom_period, day_type)
        return float(prof.get(str(now.hour), 0.0))

    def get_average_profile(self, profile_type, days, day_type="all", occupancy_filter=None):
        """Returns a dict with 24 keys ("0" to "23") representing average values.
        day_type: "all", "weekday", "weekend".
        occupancy_filter: None (no filter), "home" (occ > 0), "away" (occ == 0).
        """
        cache_key = (profile_type, days, day_type, occupancy_filter)
        if cache_key in self._profile_cache:
            return self._profile_cache[cache_key]

        profile = {}
        for h in range(24):
            sh = str(h)
            h_data = self.data.get(profile_type, {})
            history = h_data.get(sh, [])
            relevant = history[-days:] if days > 0 else history
            valid_vals = []

            for item in relevant:
                try:
                    if isinstance(item, dict):
                        v = normalize_float(item.get("v", 0.0))
                        wd = item.get("wd")
                        occ = item.get("occ")
                    else:
                        v = normalize_float(item)
                        wd = None
                        occ = None

                    if wd is not None:
                        if day_type == "weekday" and wd >= 5: continue
                        if day_type == "weekend" and wd < 5: continue

                    # Filter by occupancy if requested
                    if occupancy_filter is not None and occ is not None:
                        if occupancy_filter == "home" and occ == 0: continue
                        if occupancy_filter == "away" and occ > 0: continue

                    valid_vals.append(v)
                except Exception:
                    pass

            if valid_vals:
                profile[str(h)] = round(sum(valid_vals) / len(valid_vals), 3)
            else:
                profile[str(h)] = 0.0

        self._profile_cache[cache_key] = profile
        return profile

    def get_expected_so_far(self, profile_type, days=None, day_type=None):
        """Returns expected accumulated value from midnight to current minute."""
        now = self.now
        days = days or self.custom_period
        day_type = day_type or self.day_type

        prof = self.get_average_profile(profile_type, days, day_type)
        cur_hour = now.hour

        expected_full_hours = sum(float(prof.get(str(h), 0.0)) for h in range(cur_hour))
        fraction = now.minute / 60.0
        expected_current_hour = float(prof.get(str(cur_hour), 0.0)) * fraction

        return expected_full_hours + expected_current_hour

    def get_expected_remaining(self, profile_type, days=None, day_type=None):
        """Returns expected accumulated value from current minute to end of day (23:59)."""
        now = dt_util.now()
        days = days or self.custom_period
        day_type = day_type or self.day_type

        prof = self.get_average_profile(profile_type, days, day_type)
        cur_hour = now.hour

        fraction_left = 1.0 - (now.minute / 60.0)
        expected_current_hour = float(prof.get(str(cur_hour), 0.0)) * fraction_left
        expected_remaining_hours = sum(float(prof.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))

        return expected_current_hour + expected_remaining_hours

    def get_expected_night(self, profile_type, days=None, day_type=None, until_hour=8):
        """Returns expected accumulated value from 00:00 to until_hour (usually morning)."""
        days = days or self.custom_period
        day_type = day_type or self.day_type
        prof = self.get_average_profile(profile_type, days, day_type)
        return sum(float(prof.get(str(h), 0.0)) for h in range(0, until_hour))

    def get_total_so_far(self, profile_type):
        """Returns actual accumulated value for today so far (past hours + current)."""
        prof = self.get_todays_profile(profile_type)
        return sum(prof.values())

    def get_expected_for_day(self, profile_type, days=None, day_type=None):
        """Returns total expected value for the entire day (24h)."""
        days = days or self.custom_period
        day_type = day_type or self.day_type
        prof = self.get_average_profile(profile_type, days, day_type)
        return sum(float(v) for v in prof.values())

    def get_current_occupancy(self):
        """Returns number of persons/entities currently 'home'.

        Supported entity types:
        - person.*:          state == 'home' → counts as 1 person
        - binary_sensor.*:  state == 'on'   → counts as 1 person
        - zone.*:           state is numeric count (e.g. zone.home returns '2') → used directly
        """
        if not self.presence_sensors:
            return -1  # -1 means "occupancy tracking not configured"
        count = 0
        for entity_id in self.presence_sensors:
            state = self.hass.states.get(entity_id)
            if not state or state.state in ("unknown", "unavailable"):
                continue
            # zone.* — state is the number of people in the zone
            if entity_id.startswith("zone."):
                try:
                    count += int(float(state.state))
                except (ValueError, TypeError):
                    pass
            # person.* or binary_sensor.*
            elif state.state in ("home", "on"):
                count += 1
        return count

    def get_occupancy_coefficient(self, hour=None):
        """Returns a multiplier (0.0-1.5+) to scale consumption forecast based on current occupancy.

        Compares average consumption when home vs away from stored history.
        If nobody is home, returns a ratio < 1.0 (typically 0.3-0.7).
        If occupancy tracking is not configured, returns 1.0.
        """
        if not self.presence_sensors:
            return 1.0

        current_occ = self.get_current_occupancy()
        if current_occ < 0:
            return 1.0

        # Calculate average consumption for home vs away from historical data
        days = self.custom_period

        hours_to_check = range(24) if hour is None else [hour]
        home_total = 0.0
        home_count = 0
        away_total = 0.0
        away_count = 0

        for h in hours_to_check:
            sh = str(h)
            history = self.data.get("consumption_total", {}).get(sh, [])
            relevant = history[-days:] if days > 0 else history
            for item in relevant:
                if not isinstance(item, dict):
                    continue
                v = normalize_float(item.get("v", 0.0))
                occ = item.get("occ")
                if occ is None:
                    continue  # Legacy data without occupancy tag
                if occ > 0:
                    home_total += v
                    home_count += 1
                else:
                    away_total += v
                    away_count += 1

        # Not enough data to distinguish — return 1.0
        if home_count < 5 or away_count < 3:
            return 1.0

        avg_home = home_total / home_count
        avg_away = away_total / away_count

        if avg_home <= 0.01:
            return 1.0

        # If nobody is home right now, return the away/home ratio
        if current_occ == 0:
            return max(0.1, min(1.0, avg_away / avg_home))

        # Everyone is home — no adjustment needed
        return 1.0

    def get_efficiency_coefficient(self):
        """Calculates historical inverter/system efficiency."""
        return self.strategy_engine.get_efficiency_coefficient()

    def get_todays_profile(self, profile_type):
        """Returns the actual hourly profile for the current day up to the current hour."""
        now = dt_util.now()
        cur_hour = now.hour
        res = {}
        for h in range(24):
            sh = str(h)
            if h < cur_hour:
                history = self.data[profile_type][sh]
                if history:
                    last_record = history[-1]
                    val = normalize_float(last_record.get("v") if isinstance(last_record, dict) else last_record)
                    res[sh] = round(val, 3)
                else:
                    res[sh] = 0.0
            elif h == cur_hour:
                if profile_type == "consumption_base": res[sh] = round(self.current_consumption_base, 3)
                elif profile_type == "consumption_total": res[sh] = round(self.current_consumption_total, 3)
                elif profile_type == "generation": res[sh] = round(self.current_generation, 3)
                else: res[sh] = 0.0
            else:
                res[sh] = 0.0
        return res

    def run_investment_simulation(self, extra_batt_kwh=0.0, pv_multiplier=1.0):
        """Simulate last 30 days with modified system specs."""
        return self.strategy_engine.run_investment_simulation(extra_batt_kwh, pv_multiplier)

    def get_setting(self, key, default=None):
        """Get setting from internal storage or config entry."""
        # 1. Try internal storage (persisted across reinstalls/reboots)
        val = self.settings.get(key)

        # 2. Try entry options (from Options Flow)
        if val is None:
            val = self.entry.options.get(key)

        # 3. Try entry data (from initial config)
        if val is None:
            val = self.entry.data.get(key)

        if val is None:
            return default

        if isinstance(default, float):
            try: return float(val)
            except Exception: return default
        return val

    def get_price(self, mode, date_str, hour):
        """Standardized price fetching from data store."""
        store = self.data.get(f"prices_{mode}", {})
        return get_price_from_store(store, date_str, hour)

    def _is_currently_pulling_power(self, sensor_id: str) -> bool:
        """Return True if the device currently has an active cycle (pulling power above standby)."""
        settings = self.deduct_settings.get(sensor_id, {})
        p_sensor = settings.get(CONF_POWER_SENSOR) if isinstance(settings, dict) else None
        
        standby = self.learned_standby_power.get(sensor_id, 15.0)
        
        if not p_sensor:
            # No power sensor configured — rely on cycle_start_time (manual start/stop logic)
            return sensor_id in self.cycle_start_time
            
        p_state = self.hass.states.get(p_sensor)
        if not p_state or p_state.state in ("unknown", "unavailable"):
            # If sensor is dead, but it was running recently, assume it's still running
            # OR if we have an active persistent cycle
            return (sensor_id in self.cycle_start_time) or (sensor_id in self.cycle_actual_start_time)

        try:
            cur_p = normalize_float(p_state.state)
            if p_state.attributes.get("unit_of_measurement") == "kW":
                cur_p *= 1000.0
        except Exception:
            return (sensor_id in self.cycle_start_time) or (sensor_id in self.cycle_actual_start_time)

        # Update last known power for UI consistency if we're here
        self.last_known_power[sensor_id] = cur_p

        return cur_p > (standby + 10.0)

    def get_sensor_float(self, entity_id, default=0.0):
        """Read a float value from a sensor entity. Handles strings, lists, and comma decimals."""
        if not entity_id:
            return default

        # Handle if passed as a list
        if isinstance(entity_id, list):
            if not entity_id: return default
            entity_id = entity_id[0]

        st = self.hass.states.get(str(entity_id))
        if not st or st.state in ("unknown", "unavailable", "None"):
            return default

        try:
            return normalize_float(st.state)
        except Exception:
            return default

    def get_battery_state(self, soc_default=0.0):
        """Read battery SOC, capacity, and calculate stored energy."""
        soc = self.get_sensor_float(self.battery_soc_sensor, soc_default)
        cap = self.get_sensor_float(self.battery_capacity_sensor, 0.0)
        energy = cap * (soc / 100.0) if cap > 0 else 0.0
        return soc, cap, energy

    def get_forecast_value(self, sensor_list):
        """Sum forecast values from a list of sensor entity IDs. Returns None if no data."""
        if not sensor_list:
            return None
        val_sum = 0.0
        for fsensor in sensor_list:
            st = self.hass.states.get(fsensor)
            v = get_kwh_val(st)
            if v is not None:
                val_sum += v
        return val_sum if val_sum > 0 else None

    @staticmethod
    def get_cc_cv_ratio(soc):
        """Calculate CC/CV charge acceptance ratio."""
        return StrategyEngine.get_cc_cv_ratio(soc)

    def get_gen_forecast_coefficient(self, forecast_value, prof_gen, hour_start, hour_end):
        """Calculate scaling coefficient."""
        return self.strategy_engine.get_gen_forecast_coefficient(forecast_value, prof_gen, hour_start, hour_end)

    def get_market_strategy(self, mode="buy"):
        """Complex market strategy solver."""
        return self.strategy_engine.get_market_strategy(mode)

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        """Analyze current day state and return permissions for heavy loads."""
        return self.strategy_engine.get_budget_and_permissions(days_for_profile, skip_strategy_check)

    def run_soc_simulation(self, start_soc, sim_hours_abs, start_time=None, charge_commands=None):
        """Universal SOC simulation engine."""
        now = start_time or dt_util.now()
        return self.strategy_engine.run_soc_simulation(start_soc, sim_hours_abs, now, charge_commands)

class EnergyBaseSensor(SensorEntity):
    """Base class for Energy Management sensors to reduce boilerplate."""
    def __init__(self, manager, name, unique_id_prefix):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_{unique_id_prefix}"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        """Register listener for manager updates."""
        self.manager.register_listener(self.async_write_ha_state)

class ProfileAveragedSensor(EnergyBaseSensor):
    def __init__(self, manager, ptype, period_key, name, days):
        super().__init__(manager, name, f"{ptype}_{period_key}")
        self.ptype = ptype
        self.period_key = period_key
        self.days = days
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:chart-bell-curve-cumulative"

    @property
    def native_value(self):
        # We define the basic state as the "Total Average Daily Energy"
        if self.ptype == "consumption":
            profile = self.manager.get_average_profile("consumption_base", self.days)
        else:
            profile = self.manager.get_average_profile("generation", self.days)
        return round(sum(profile.values()), 3)

    @property
    def extra_state_attributes(self):
        if self.ptype == "consumption":
            base_profile = self.manager.get_average_profile("consumption_base", self.days)
            base_profile_weekday = self.manager.get_average_profile("consumption_base", self.days, "weekday")
            base_profile_weekend = self.manager.get_average_profile("consumption_base", self.days, "weekend")
            total_profile = self.manager.get_average_profile("consumption_total", self.days)
            return {
                "base_profile": base_profile,
                "base_profile_weekday": base_profile_weekday,
                "base_profile_weekend": base_profile_weekend,
                "total_profile": total_profile,
                "total_daily_average": round(sum(total_profile.values()), 3)
            }
        else:
            profile = self.manager.get_average_profile("generation", self.days, "all")
            return {
                "profile": profile
            }



class BatteryEndOfDaySOCSensor(SensorEntity):
    """Predicts battery SOC at the next major event (sunset or sunrise)."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_translation_key = "battery_end_of_day_soc"
        self._attr_unique_id = f"{manager.entry.entry_id}_battery_end_of_day_soc"
        self._attr_icon = "mdi:battery-arrow-up"
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        now = dt_util.now()
        batt_soc, batt_cap, _ = self.manager.get_battery_state(soc_default=100.0)
        eff_coeff = self.manager.get_efficiency_coefficient()

        if batt_cap <= 0.0:
            sensor_name = self.manager.battery_capacity_sensor or "Не задан"
            self._attr_extra_state_attributes = {
                "error": "Нет емкости батареи",
                "debug_sensor": str(sensor_name)
            }
            return None

        prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")

        # Detect sunrise/sunset hours based on history
        sunrise_hour = 6
        sunset_hour = 20
        found_sun = False
        for h in range(24):
            if float(prof_gen.get(str(h), 0.0)) > 0.05:
                if not found_sun:
                    sunrise_hour = h
                    found_sun = True
                sunset_hour = h

        # Current actual status can override profile if it's currently sunny.
        # This helps during seasonal transitions (spring/autumn) or exceptionally clear days.
        avg_gen = self.manager.avg_gen_kw
        is_gen_active = avg_gen > 0.05

        # We consider it "day" if we are within productive hours OR we currently have generation.
        # We include sunset_hour in the inclusive range because the productive period usually ends
        # AT THE END of that hour.
        is_day = (sunrise_hour <= now.hour <= sunset_hour) or is_gen_active

        if is_day:
            # If we are in "overtime" (sunny but profile says night), predict until end of this hour or profile sunset
            actual_sunset_h = max(sunset_hour, now.hour)
            target_hour = (actual_sunset_h + 1) % 24
            target_label = "К закату"
            self._attr_icon = "mdi:battery-arrow-up"
            # Include current hour in simulation for partial-hour accuracy
            sim_hours = list(range(now.hour, actual_sunset_h + 1))
        else:
            target_hour = sunrise_hour
            target_label = "К восходу"
            self._attr_icon = "mdi:battery-arrow-down"
            # Night simulation till sunrise (including remainder of current hour)
            if now.hour > sunset_hour:
                sim_hours = list(range(now.hour, 24)) + list(range(0, sunrise_hour))
            else:
                sim_hours = list(range(now.hour, sunrise_hour))

        # 1. Run Unified Simulation Engine
        simulated_soc, charge_log = self.manager.run_soc_simulation(batt_soc, sim_hours, now)

        f_raw = self.manager.get_forecast_value(self.manager.forecast_today_sensor)
        coeff = getattr(self.manager, "last_blended_coeff", 1.0)
        f_val = f_raw * coeff if f_raw is not None else 0.0

        self._attr_extra_state_attributes = {
            "prediction_target": target_label,
            "target_hour": f"{target_hour:02d}:00",
            "current_soc_pct": round(batt_soc, 1),
            "forecast_income_remaining_kwh": round(f_val, 2),
            "forecast_raw_kwh": round(f_raw or 0.0, 2),
            "forecast_coefficient_blended": round(coeff, 3),
            "efficiency_coefficient": round(eff_coeff, 3),
            "simulation_log": charge_log
        }
        return round(simulated_soc, 1)

    @property
    def extra_state_attributes(self):
        if not hasattr(self, "_attr_extra_state_attributes"):
            return {}
        return self._attr_extra_state_attributes


class ConsumptionDeviationSensor(EnergyBaseSensor):
    """Compares current base consumption against historical profile (weekday/weekend aware)."""
    def __init__(self, manager, name):
        super().__init__(manager, name, "consumption_deviation")
        self._attr_icon = "mdi:gauge"
        self._attr_native_unit_of_measurement = "%"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self):
        now = dt_util.now()
        cur_hour = now.hour

        # 1. Get Actual Base Today (synchronized with Profile sensor)
        today_total_prof = self.manager.get_todays_profile("consumption_total")
        total_actual = sum(today_total_prof.values())

        # Deduct managed loads (daily accumulators)
        deduct_sum = sum(self.manager.daily_deduct_consumption.get(s, 0.0) for s in self.manager.deduct_settings)
        actual_base = max(0.0, total_actual - deduct_sum)

        # 2. Get Expected Base So Far
        day_type = "weekend" if now.weekday() >= 5 else "weekday"
        prof_base = self.manager.get_average_profile("consumption_base", self.manager.custom_period, day_type)

        # We compare up to the current hour (inclusive, but current hour is partial)
        expected_full_hours = sum(float(prof_base.get(str(h), 0.0)) for h in range(cur_hour))
        # Add fractional part of current hour
        fraction = now.minute / 60.0
        expected_current_hour = float(prof_base.get(str(cur_hour), 0.0)) * fraction

        expected_so_far = expected_full_hours + expected_current_hour

        if expected_so_far < 0.1:
            return 0.0

        deviation = ((actual_base / expected_so_far) - 1.0) * 100.0

        self._attr_extra_state_attributes = {
            "actual_base_kwh": round(actual_base, 3),
            "expected_base_kwh": round(expected_so_far, 3),
            "managed_loads_kwh": round(deduct_sum, 3),
            "day_type": day_type,
            "status": "accumulating" if actual_base < 0.1 else "active"
        }

        return round(deviation, 1) if abs(deviation) < 1000 else 0.0


class InverterOperationModeSensor(SensorEntity):
    """Outputs the specific inverter command state based on logic."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_inverter_mode"
        self._attr_icon = "mdi:state-machine"
        self._state = "sale_pv"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        try:
            return self._calculate_mode()
        except Exception as e:
            self._attr_extra_state_attributes = {"error": f"Ошибка вычислений: {str(e)}"}
            return "sale_pv"

    def _calculate_mode(self):
        mode = "sale_pv" # default

        now = dt_util.now()
        cur_hour = str(now.hour)
        today_str = now.strftime("%Y-%m-%d")

        price_sell_limit = self.manager.get_setting(CONF_PRICE_SELL_LIMIT, -99.0)
        try:
            from .const import CONF_PRICE_STOP_SELL, CONF_PRICE_SELL_ONLY_PV, CONF_SALE_PV_NO_BAT_MAX_HOUR
            price_stop_sell = self.manager.get_setting(CONF_PRICE_STOP_SELL, 0.0)
            price_sell_only_pv = self.manager.get_setting(CONF_PRICE_SELL_ONLY_PV, 999.0)
            sale_pv_no_bat_max_hour = self.manager.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0)
        except ImportError:
            price_stop_sell = 0.0
            price_sell_only_pv = 999.0
            sale_pv_no_bat_max_hour = 13.0

        min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)

        cur_price = self.manager.get_price("sell", today_str, now.hour)

        # Strategy
        sell_strategy = self.manager.get_market_strategy("sell")
        is_selling_active = sell_strategy.get("state") == "active"

        buy_strategy = self.manager.get_market_strategy("buy")
        is_buying_active = buy_strategy.get("state") == "active"

        # SOC
        batt_soc, batt_cap, _ = self.manager.get_battery_state(soc_default=100.0)

        # Prep for peak
        target_hours = sell_strategy.get("active_hours", [])
        peak_start_hour = None
        for h in sorted(target_hours):
            if h > int(cur_hour):
                peak_start_hour = h
                break

        is_preparing_for_peak = False
        bms_debug = {"status": "Not evaluated"}

        if batt_cap <= 0:
            bms_debug = {"status": "Не задана емкость батареи"}
        else:
            try: from .const import CONF_TARGET_SOC_SELL
            except ImportError: CONF_TARGET_SOC_SELL = "target_soc_sell"
            target_soc = self.manager.get_setting(CONF_TARGET_SOC_SELL, 100.0)

            if batt_soc >= target_soc:
                bms_debug = {
                    "status": "Батарея уже заряжена до целевого уровня",
                    "target_soc": target_soc,
                    "current_soc": batt_soc
                }
            else:
                # Use standard simulation engine
                end_h = peak_start_hour if peak_start_hour is not None else (int(now.hour) + 24)
                sim_range = list(range(int(now.hour), end_h))
                sim_range = [h for h in sim_range if h < 48]
                
                sim_soc, sim_log = self.manager.strategy_engine.run_soc_simulation(batt_soc, sim_range, now)
                
                # Determine when target reached for surplus/delay logic
                target_reached_at = None
                for i, h_abs in enumerate(sim_range):
                    is_tom = (h_abs >= 24)
                    h_key = f"{h_abs % 24:0>2}:59" + (" (Завтра)" if is_tom else "")
                    if sim_log.get(h_key, 0.0) >= (target_soc - 0.5): # 0.5% tolerance
                        target_reached_at = i + 1
                        break
                
                hours_available = len(sim_range)
                total_hours_needed = target_reached_at if target_reached_at is not None else hours_available

                # If after the simulation timeline we failed to reach the target (allowing 1% tolerance)
                late_start_recommended = False
                if sim_soc < (target_soc - 1.0) and peak_start_hour is not None:
                    is_preparing_for_peak = True
                elif sim_soc >= (target_soc - 1.0) and peak_start_hour is not None:
                    # We have enough time! Calculate how much extra time we have.
                    hours_surplus = hours_available - total_hours_needed
                    # If we have surplus hours AND it's not the cheapest time to charge, we can delay
                    if hours_surplus > 0:
                        latest_start_hour = peak_start_hour - total_hours_needed
                        if int(now.hour) < latest_start_hour:
                            late_start_recommended = True

                if peak_start_hour is None:
                    sim_status = "Прогноз заряда до конца дня (пиков нет)"
                elif is_preparing_for_peak:
                    sim_status = "Внимание: Подготовка к Пику"
                elif late_start_recommended:
                    sim_status = "Зарядка отложена (ждем дешевых часов)"
                    is_preparing_for_peak = False # Don't block battery usage yet
                else:
                    sim_status = "Штатный заряд к пику"
                    is_preparing_for_peak = True # We are in the critical charging window, block discharging!

                bms_debug = {
                    "status": sim_status,
                    "target_soc": target_soc,
                    "hours_available": hours_available,
                    "final_simulated_soc": round(sim_soc, 2),
                    "success": not is_preparing_for_peak if peak_start_hour is not None else True,
                    "log": sim_log
                }

        day_type = "weekend" if now.weekday() >= 5 else "weekday"
        prof_cons = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type)
        prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")

        formatted_peak = self.manager.strategy_engine._format_h(peak_start_hour)

        self._attr_extra_state_attributes = {
            "is_preparing_for_peak": is_preparing_for_peak,
            "next_peak_start_hour": formatted_peak
        }

        # State Machine Logic
        mode = "sale_pv" # Default
        reason = "Значения по умолчанию (нет особых условий рынка или заряда)"

        if batt_soc <= min_soc:
            mode = "bat_emergency"
            reason = f"Заряд батареи ({round(batt_soc, 1)}%) <= Критического минимума ({min_soc}%)"
        elif is_buying_active:
            mode = "buy"
            reason = f"Активна стратегия ПОКУПКИ (Смотри сенсор Market BUY Strategy)"
        elif is_selling_active:
            mode = "sale_pv_bat"
            reason = f"Активна стратегия ПРОДАЖИ (Смотри сенсор Market SELL Strategy)"
        elif cur_price is not None and cur_price < price_stop_sell:
            mode = "stop_sale"
            reason = f"Текущая цена ({cur_price}) < Порога блокировки продажи ({price_stop_sell})"
        elif cur_price is not None and cur_price >= price_sell_only_pv and not is_preparing_for_peak:
            if int(cur_hour) < sale_pv_no_bat_max_hour:
                instant_ok = True
                if self.manager.power_history:
                    # Use properties for average load/gen over the recent history (~10 mins)
                    avg_load_kw = self.manager.avg_load_kw
                    avg_gen_kw = self.manager.avg_gen_kw

                    if getattr(self.manager, "power_load_sensors", []) and getattr(self.manager, "power_gen_sensors", []):
                        if avg_gen_kw <= avg_load_kw + 0.1: # Require at least 100W of actual surplus average to flip to sell mode
                            instant_ok = False
                    elif getattr(self.manager, "power_gen_sensors", []):
                        if avg_gen_kw < 0.1:
                            instant_ok = False
                else:
                    # Fallback if no history yet
                    if getattr(self.manager, "power_load_sensors", []) and getattr(self.manager, "power_gen_sensors", []):
                        load_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_load_sensors)
                        gen_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
                        if gen_kw <= load_kw + 0.1:
                            instant_ok = False
                    elif getattr(self.manager, "power_gen_sensors", []):
                        gen_kw = sum((get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
                        if gen_kw < 0.1:
                            instant_ok = False

                # Also check profile. We only want to sell if historically this hour yields more than we consume
                h_mod = str(int(cur_hour) % 24)
                c_kwh = float(prof_cons.get(h_mod, 0.0)) if 'prof_cons' in locals() else 0.0
                g_kwh = float(prof_gen.get(h_mod, 0.0)) if 'prof_gen' in locals() else 0.0

                if instant_ok and g_kwh >= c_kwh:
                    mode = "sale_pv_no_bat"
                    reason = f"Текущая цена ({cur_price}) >= Порога продажи PV ({price_sell_only_pv}) и есть профицит Солнца"
                elif instant_ok:
                    reason = f"Цена ок, но исторически нет профицита Солнца в этот час. Ждем"
                else:
                    reason = f"Цена ок, но моментально генерация не превышает потребление"
            else:
                 reason = f"Блокировка sale_pv_no_bat: ограничение по времени (до {sale_pv_no_bat_max_hour}:00)"

        if mode == "buy":
            self._attr_extra_state_attributes["charge_target_soc"] = buy_strategy.get("charge_target_soc", 100.0)
            self._attr_extra_state_attributes["charge_reason"] = buy_strategy.get("charge_reason", "price")

        debug_info = f" [Buy:{is_buying_active}, Sell:{is_selling_active}, P:{cur_price}, Stop:{price_stop_sell}]"
        self._attr_extra_state_attributes["mode_reason"] = reason + debug_info
        self._attr_extra_state_attributes["bms_status"] = bms_debug

        return mode

class InstantPowerAveragedSensor(SensorEntity):
    """Displays the averaged instantaneous power (W/kW sensors) over the last 10 minutes."""
    _attr_has_entity_name = True
    def __init__(self, manager, ptype):
        self.manager = manager
        self.ptype = ptype
        self._attr_translation_key = f"avg_power_{ptype}"
        self._attr_unique_id = f"{manager.entry.entry_id}_avg_power_{ptype}"
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:chart-bell-curve"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        if self.ptype == "load":
            return self.manager.avg_load_kw
        return self.manager.avg_gen_kw

    @property
    def extra_state_attributes(self):
        return {
            "samples_count": len(self.manager.power_history),
            "window_minutes": 10
        }

class LiveHourlySensor(RestoreEntity, SensorEntity):
    """Keeps the original live hourly behavior, for reference and diagnostics."""
    def __init__(self, manager, ptype, name):
        self.manager = manager
        self.ptype = ptype
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_live_{ptype}"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_icon = "mdi:lightning-bolt"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # Restore logic to ensure we don't lose the current hour progress unexpectedly
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            val = normalize_float(last_state.state)
            # Recover into manager if it hasn't accumulated anything since restart
            if self.ptype == "consumption" and self.manager.current_consumption_base == 0:
                self.manager.current_consumption_base = val
                self.manager.current_consumption_total = val # We can only guess total from base if restored like this
            if self.ptype == "generation" and self.manager.current_generation == 0:
                self.manager.current_generation = val
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        if self.ptype == "consumption":
            return round(self.manager.current_consumption_base, 3)
        return round(self.manager.current_generation, 3)

    @property
    def extra_state_attributes(self):
        if self.ptype == "consumption":
            return {
                "total_consumption": round(self.manager.current_consumption_total, 3)
            }
        return {}

class TodayProfileSensor(SensorEntity):
    """Shows the actual accumulated hourly profile for the current day."""
    def __init__(self, manager, ptype, name):
        self.manager = manager
        self.ptype = ptype
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_today_{ptype}"
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:chart-timeline-variant"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        query_type = "consumption_base" if self.ptype == "consumption" else self.ptype
        profile = self.manager.get_todays_profile(query_type)
        return round(sum(profile.values()), 3)

    @property
    def extra_state_attributes(self):
        query_type = "consumption_base" if self.ptype == "consumption" else self.ptype
        profile = self.manager.get_todays_profile(query_type)

        if self.ptype == "consumption":
            total_profile = self.manager.get_todays_profile("consumption_total")
            return {
                "base_profile": profile,
                "total_profile": total_profile,
                "total_daily_sum": round(sum(total_profile.values()), 3)
            }
        return {
            "profile": profile
        }

class EnergyBudgetSensor(SensorEntity):
    """Calculates if there is expected energy surplus until tomorrow morning (08:00)."""
    def __init__(self, manager, name, days_for_profile):
        self.manager = manager
        self.days_for_profile = days_for_profile
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_energy_budget"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:scale-balance"
        self._state = 0.0
        self._attrs = {}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        self._calculate()
        return round(self._state, 3)

    @property
    def extra_state_attributes(self):
        return self._attrs

    def _calculate(self):
        try:
            res = self.manager.get_budget_and_permissions(self.days_for_profile)
            if not isinstance(res, dict):
                res = {}

            def _sr(v, default=0.0):
                """Safe round."""
                try:
                    return round(float(v if v is not None else default), 3)
                except (TypeError, ValueError):
                    return round(float(default), 3)

            self._state = float(res.get("initial_budget", 0.0) or 0.0)
            self._attrs = {
                "permissions": res.get("permissions", {}),
                "permissions_reasons": res.get("permissions_reasons", {}),
                "forecast_remaining_kwh": _sr(res.get("forecast_val")),
                "forecast_raw_kwh": _sr(res.get("forecast_raw")),
                "forecast_coefficient_blended": _sr(res.get("forecast_coefficient", 1.0), 1.0),
                "forecast_coefficient_history": _sr(res.get("forecast_hist_coefficient", 1.0), 1.0),
                "forecast_coefficient_today": _sr(res.get("forecast_today_coefficient", 1.0), 1.0),
                "battery_energy_kwh": _sr(res.get("batt_energy_val")),
                "expected_consumption_kwh": _sr(res.get("expected_consumption")),
                "occupancy_coefficient": _sr(res.get("occupancy_coefficient", 1.0), 1.0),
                "occupancy_persons_home": self.manager.get_current_occupancy() if self.manager.presence_sensors else "N/A",
                "efficiency_coefficient": _sr(res.get("efficiency_coefficient", 1.0), 1.0),
                "debug_actual_today": _sr(res.get("debug_actual_today")),
                "debug_expected_today_total": _sr(res.get("debug_expected_today_total")),
                "debug_expected_today_so_far": _sr(res.get("debug_expected_today_so_far")),
                "debug_fraction_so_far": _sr(res.get("debug_fraction_so_far")),
            }
        except Exception as e:
            _LOGGER.error("Error calculating EnergyBudgetSensor: %s", e)
            self._state = 0.0
            self._attrs = {"error": str(e)}

class MarketStrategySensor(SensorEntity):
    def __init__(self, manager, mode, name):
        self.manager = manager
        self.mode = mode
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_market_strategy_{mode}"
        self._state = "idle"
        self._attrs = {}
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_translation_key = "market_strategy"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    @property
    def native_value(self):
        res = self.manager.get_market_strategy(self.mode)
        return res["state"]

    @property
    def extra_state_attributes(self):
        res = self.manager.get_market_strategy(self.mode)

        now = dt_util.now()
        cur_hour = now.hour

        def safe_round(val):
            return round(normalize_float(val), 3)

        today_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res["today_prices"].items(), key=lambda item: int(item[0])) if int(k) >= cur_hour}
        tom_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res["tomorrow_prices"].items(), key=lambda item: int(item[0]))}

        # Determine the user-friendly mode string
        current_mode = "Ожидание"
        state = res.get("state", "idle")
        active_hours = res.get("active_hours", [])

        if state == "active":
            rec_p = res.get("recommended_power_kw", 0.0)
            if self.mode == "buy":
                reason = res.get("charge_reason", "price")
                if rec_p <= 0:
                    current_mode = "Ожидание (Заряжено)"
                elif reason == "survival":
                    current_mode = "Зарядка (Экстренно)"
                else:
                    current_mode = "Зарядка (Дешевая цена)"
            else:
                if rec_p <= 0:
                    current_mode = "Ожидание (Пусто)"
                else:
                    current_mode = "Активная продажа"
        elif state == "preparing_arbitrage":
            current_mode = "Ожидание арбитража"
        elif state in ["price_limit_not_met", "unprofitable_arbitrage"] or not active_hours:
            current_mode = "Нет ценового окна"
        elif state == "idle":
            if self.mode == "buy" and res.get("charge_reason") == "survival":
                current_mode = "Ожидание (Экстренно)"
            elif self.mode == "sell":
                if "Арбитраж" in res.get("arbitrage_decision", ""):
                    current_mode = "Ожидание (Арбитраж)"
                else:
                    current_mode = "Ожидание (Пик цены)"
            else:
                current_mode = "Ожидание"

        attrs = {
            "analyzed_window": res.get("analyzed_window", "Неизвестно"),
            "double_cycle_opportunity": res.get("multi_cycle", "Не предвидится"),
            "active_hours": res.get("active_hours_formatted", ""),
            "active_periods": res.get("active_periods", ""),
            "target_price": round(float(res.get("target_price", 0.0) or 0.0), 3),
            "limit_used": round(float(res.get("limit_used", 0.0) or 0.0), 3),
            "recommended_power_kw": res.get("recommended_power_kw", 0.0),
            "current_mode": current_mode,
            "arbitrage_decision": res.get("arbitrage_decision", "Нет данных"),
            "prices_today": today_fmt,
            "prices_tomorrow": tom_fmt
        }

        if self.mode == "sell":
            attrs.update({
                "projected_soc_at_sale_start": res.get("sell_simulation", {}).get("projected_soc_at_start_pct", 0.0),
                "projected_soc_after_sale": res.get("sell_simulation", {}).get("projected_soc_after_sale_pct", 0.0),
                "projected_soc_morning": res.get("sell_simulation", {}).get("projected_soc_morning_pct", 0.0),
                "arbitrage_buyback_power": res.get("arbitrage_buyback", {}).get("power_kw", 0.0),
                "arbitrage_buyback_note": res.get("arbitrage_buyback", {}).get("note", ""),
                "arbitrage_available_kwh": res.get("arbitrage_buyback", {}).get("available_kwh", 0.0),
                "arbitrage_reserve_kwh": res.get("arbitrage_buyback", {}).get("reserve_kwh", 0.0),
                "arbitrage_energy_to_wait_kwh": res.get("arbitrage_buyback", {}).get("energy_to_wait_kwh", 0.0),
            })
        else: # buy
            attrs.update({
                "projected_soc_at_buy_start": res.get("buy_simulation", {}).get("projected_soc_at_start_pct", 0.0),
                "projected_soc_at_buy_end": res.get("buy_simulation", {}).get("projected_soc_at_end_pct", 0.0),
            })

        return attrs

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)




class SavingsSensor(SensorEntity):
    """Tracks financial savings / revenue from solar, arbitrage, or grid sales."""

    _CATEGORY_META = {
        "solar":     ("mdi:solar-panel",      "Самопотребление солнечной энергии: стоимость кВт·ч, которые не пришлось покупать у сети."),
        "arbitrage": ("mdi:swap-horizontal",   "Ценовой арбитраж: разница между пиковой ценой продажи и ценой дешёвой закупки."),
        "sell":      ("mdi:cash-plus",          "Выручка от продажи электроэнергии в сеть."),
    }

    def __init__(self, manager, category, name):
        self.manager  = manager
        self.category = category
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_savings_{category}"
        icon, _ = self._CATEGORY_META.get(category, ("mdi:cash", ""))
        self._attr_icon = icon
        # Unit will be set in async_added_to_hass from hass.config.currency
        self._attr_native_unit_of_measurement = "EUR"  # fallback until HA sets it
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        # Use the currency configured in HA Settings → System → General
        try:
            currency = self.hass.config.currency
            if currency:
                self._attr_native_unit_of_measurement = currency
        except Exception:
            pass  # keep EUR fallback
        self.manager.register_listener(self.async_write_ha_state)

    def _get_summary(self):
        now = dt_util.now()
        savings = self.manager.data.get("savings", {})
        cat = self.category

        def _day(d_str):
            return savings.get(d_str, {}).get(cat, 0.0)

        today_str     = now.strftime("%Y-%m-%d")
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        today_val     = _day(today_str)
        yesterday_val = _day(yesterday_str)
        last7   = sum(_day((now - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(7))
        last30  = sum(_day((now - timedelta(days=i)).strftime("%Y-%m-%d")) for i in range(30))

        this_month_pfx = now.strftime("%Y-%m")
        last_month_dt  = now.replace(day=1) - timedelta(days=1)
        last_month_pfx = last_month_dt.strftime("%Y-%m")

        this_month  = sum(v.get(cat, 0.0) for d, v in savings.items() if d.startswith(this_month_pfx))
        last_month  = sum(v.get(cat, 0.0) for d, v in savings.items() if d.startswith(last_month_pfx))

        monthly = {}
        for d, v in savings.items():
            m = d[:7]
            monthly[m] = round(monthly.get(m, 0.0) + v.get(cat, 0.0), 4)

        daily = {}
        for i in range(30):
            d_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            val = _day(d_str)
            if val > 0 or d_str == today_str:
                daily[d_str] = round(val, 4)

        return {
            "today":          round(today_val,     4),
            "yesterday":      round(yesterday_val, 4),
            "last_7_days":    round(last7,   4),
            "last_30_days":   round(last30,  4),
            "this_month":     round(this_month, 4),
            "last_month":     round(last_month, 4),
            "monthly_totals": {k: round(v, 2) for k, v in sorted(monthly.items())[-13:]},
            "daily_history":  dict(sorted(daily.items())),
        }

    @property
    def native_value(self):
        return round(self._get_summary().get("last_30_days", 0.0), 2)

    @property
    def extra_state_attributes(self):
        s = self._get_summary()
        _, description = self._CATEGORY_META.get(self.category, ("mdi:currency-eur", ""))
        attrs = {
            "description":    description,
            "today":          s["today"],
            "yesterday":      s["yesterday"],
            "last_7_days":    s["last_7_days"],
            "this_month":     s["this_month"],
            "last_month":     s["last_month"],
            "monthly_totals": s["monthly_totals"],
            "daily_history":  s["daily_history"],
        }

        # If this is the unified sensor, show the component breakdown for today/yesterday
        if self.category == "total":
            now = dt_util.now()
            today_str = now.strftime("%Y-%m-%d")
            yest_str  = (now - timedelta(days=1)).strftime("%Y-%m-%d")

            savings_store = self.manager.data.get("savings", {})
            t_data = savings_store.get(today_str, {})
            y_data = savings_store.get(yest_str,  {})

            attrs.update({
                "solar_benefit_today":     round(t_data.get("solar", 0.0), 4),
                "arbitrage_benefit_today": round(t_data.get("arbitrage", 0.0), 4),
                "sell_benefit_today":      round(t_data.get("sell", 0.0), 4),

                "solar_benefit_yesterday":     round(y_data.get("solar", 0.0), 4),
                "arbitrage_benefit_yesterday": round(y_data.get("arbitrage", 0.0), 4),
                "sell_benefit_yesterday":      round(y_data.get("sell", 0.0), 4),
            })

        return attrs

class EnergyBalanceSensor(SensorEntity):
    """Real-time financial balance tracking (Saldo)."""

    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_energy_balance"
        self._attr_icon = "mdi:wallet-outline"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )
        self._currency = "EUR"

    async def async_added_to_hass(self):
        try:
            self._currency = self.hass.config.currency
            self._attr_native_unit_of_measurement = self._currency
        except Exception:
            self._attr_native_unit_of_measurement = "EUR"
        self.manager.register_listener(self.async_write_ha_state)

    def _get_balance_summary(self):
        now = dt_util.now()
        savings_store = self.manager.data.get("savings", {})
        total_balance = self.manager.data.get("energy_balance", 0.0)
        today_start_v = self.manager.data.get("energy_balance_today_start", total_balance)
        
        # Real-time today balance
        today_val = total_balance - today_start_v
        
        def _get_hist(days):
            val = 0.0
            for i in range(1, days + 1): # Skip today as we use real-time today_val
                d_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                val += savings_store.get(d_str, {}).get("total", 0.0)
            return val

        yesterday_val = _get_hist(1)
        last_7_days   = _get_hist(7) + today_val
        last_30_days  = _get_hist(30) + today_val
        
        this_month_pfx = now.strftime("%Y-%m")
        this_month_val = sum(v.get("total", 0.0) for d, v in savings_store.items() if d.startswith(this_month_pfx))
        # Adjust this_month if it already included an older 'today' hourly snapshot (rare edge case)
        # but usually it's correct enough.

        return {
            "today":      round(today_val, 2),
            "yesterday":  round(yesterday_val, 2),
            "week":       round(last_7_days, 2),
            "month":      round(last_30_days, 2),
            "lifetime":   round(total_balance, 2),
        }

    @property
    def native_value(self):
        return self._get_balance_summary()["today"]

    @property
    def extra_state_attributes(self):
        s = self._get_balance_summary()
        return {
            "last_update": datetime.fromtimestamp(self.manager.data.get("last_balance_poll_time", 0)).isoformat() if self.manager.data.get("last_balance_poll_time") else None,
            "yesterday": s["yesterday"],
            "last_7_days": s["week"],
            "last_30_days": s["month"],
            "lifetime_all_time": s["lifetime"],
            "formula": "Savings(Solar+Battery)*Price_Buy + Export*Price_Sell - GridCharge*Price_Buy",
        }

class AnomalyDetectionSensor(SensorEntity):
    """Detects unusual consumption spikes compared to average profile."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_translation_key = "anomaly_detection"
        self._attr_unique_id = f"{manager.entry.unique_id}_anomaly_detector"
        self._attr_icon = "mdi:alert-decagram-outline"
        self._attr_native_unit_of_measurement = "score"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        expected = self.manager.get_expected_consumption()

        # Get actual power (kW)
        actual_kw = 0.0
        if self.manager.power_history:
            # Last minute average
            actual_kw = self.manager.power_history[-1]["load_kw"]

        if expected <= 0.05 or actual_kw <= 0.05:
            return 1.0 # Normal

        score = actual_kw / expected
        return round(score, 2)

    @property
    def extra_state_attributes(self):
        expected = self.manager.get_expected_consumption()
        actual_kw = self.manager.power_history[-1]["load_kw"] if self.manager.power_history else 0.0
        threshold = self.manager.get_setting(CONF_ANOMALY_THRESHOLD, 2.0)

        status = "normal"
        if actual_kw / expected > threshold if expected > 0.05 else False:
            status = "anomaly_high_consumption"
            self._attr_icon = "mdi:alert-decagram"
        else:
            self._attr_icon = "mdi:alert-decagram-outline"

        return {
            "status": status,
            "expected_kw": round(expected, 3),
            "actual_kw": round(actual_kw, 3),
            "threshold_multiplier": threshold,
            "anomaly_detected": actual_kw / expected > threshold if expected > 0.05 else False
        }

class PaybackSensor(SensorEntity):
    """Calculates ROI and Payback progress."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._currency = "EUR"
        self._attr_translation_key = "payback"
        self._attr_unique_id = f"{manager.entry.entry_id}_roi_payback"
        self._attr_icon = "mdi:finance"
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        try:
            currency = self.hass.config.currency
            self._currency = currency or "EUR"
        except Exception:
            self._currency = "EUR"
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        total_cost = self.manager.get_setting(CONF_TOTAL_SYSTEM_COST, 0.0)
        if total_cost <= 0: return None

        total_saved = self.manager.get_total_savings()
        roi = (total_saved / total_cost) * 100.0
        return round(roi, 2)

    @property
    def extra_state_attributes(self):
        total_cost = self.manager.get_setting(CONF_TOTAL_SYSTEM_COST, 0.0)
        total_saved = self.manager.get_total_savings()
        remaining = max(0.0, total_cost - total_saved)

        # Estimate days remaining
        savings_store = self.manager.data.get("savings", {})
        now = dt_util.now()
        savings_30d = 0.0
        days_found = 0
        for d, v in savings_store.items():
            try:
                dt_d = dt_util.parse_datetime(d + "T12:00:00Z") # Midday to avoid edge cases
                if dt_d and (now - dt_d).days <= 30:
                    if isinstance(v, dict): # Safety guard
                        savings_30d += v.get("total", 0.0)
                        days_found += 1
            except Exception:
                continue

        avg_daily = savings_30d / days_found if days_found > 0 else 0.0

        days_rem = int(remaining / avg_daily) if avg_daily > 0 else 9999
        payback_date = (dt_util.now() + timedelta(days=days_rem)).strftime("%Y-%m-%d") if avg_daily > 0 else "never"

        # ── Investment AI Analysis ──
        # Double the current battery capacity to see the impact
        _, batt_cap, _ = self.manager.get_battery_state()
        sim_batt_double = self.manager.run_investment_simulation(extra_batt_kwh=batt_cap)
        extra_monthly = sim_batt_double['monthly_estimate']

        payback_years_upgrade = "N/A"
        roi_upgrade = 0.0
        try:
            battery_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
            if battery_cost > 0 and extra_monthly > 0:
                payback_years_upgrade = round(float(battery_cost / (extra_monthly * 12)), 2)
                roi_upgrade = round(float(((extra_monthly * 12) / battery_cost) * 100), 1)
        except Exception:
            pass

        return {
            "total_investment": f"{total_cost} {self._currency}",
            "cumulative_savings": f"{round(float(total_saved or 0.0), 2)} {self._currency}",
            "remaining_amount": f"{round(float(remaining or 0.0), 2)} {self._currency}",
            "average_daily_saving": f"{round(float(avg_daily or 0.0), 2)} {self._currency}",
            "estimated_payback_days": days_rem if total_cost > 0 else "N/A",
            "estimated_payback_date": payback_date if total_cost > 0 else "N/A",
            "simulation_days": sim_batt_double.get("days_simulated", 0),
            "upgrade_batt_cap_kwh": round(float(batt_cap or 0.0), 2),
            "upgrade_batt_cost": f"{battery_cost} {self._currency}",
            "upgrade_potential_benefit": f"+{extra_monthly} {self._currency}/мес",
            "upgrade_payback_years": payback_years_upgrade,
            "upgrade_roi_annual": f"{roi_upgrade}%"
        }

class BatteryDegradationSensor(SensorEntity):
    """Shows the cost of 1kWh battery throughput in terms of wear."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_translation_key = "battery_degradation"
        self._attr_unique_id = f"{manager.entry.entry_id}_battery_degradation_cost"
        self._attr_icon = "mdi:battery-alert"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        try:
            self._attr_native_unit_of_measurement = f"{self.hass.config.currency}/kWh"
        except Exception:
            self._attr_native_unit_of_measurement = "/kWh"
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        # We show the ARBITRAGE threshold cost (1x degradation covers the full cycle)
        return round(self.manager.get_battery_degradation_cost(), 4)

    @property
    def extra_state_attributes(self):
        cost_per_kwh = self.manager.get_battery_degradation_cost()
        min_p = self.manager.get_setting(CONF_ARBITRAGE_MIN_PROFIT, 0.0)
        threshold = min_p if min_p >= cost_per_kwh else (2 * cost_per_kwh)

        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)

        return {
            "wear_cost_per_kwh_cycle": round(cost_per_kwh, 4),
            "arbitrage_profit_threshold": round(threshold, 4),
            "battery_investment": batt_cost,
            "rated_cycles": cycles,
            "note": "arbitrage_note"
        }

class SolarWasteSensor(SensorEntity):
    """Tracks lost solar energy (curtailment) when battery is full."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_translation_key = "solar_waste"
        self._attr_unique_id = f"{manager.entry.entry_id}_solar_waste"
        self._attr_icon = "mdi:solar-power-variant-outline"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        return round(self.manager.data.get("temp_daily_waste", 0.0), 3)

    @property
    def extra_state_attributes(self):
        # Calculate possible daily revenue loss if we sell it at current price
        prices_sell = self.manager.data.get("prices_sell", {})
        now = dt_util.now()
        today_str = now.strftime("%Y-%m-%d")
        cur_hour_int = now.hour
        cur_hour = str(cur_hour_int)

        cur_price = 0.0
        if today_str in prices_sell and cur_hour in prices_sell[today_str]:
            try:
                cur_price = float(str(prices_sell[today_str][cur_hour]).replace(',', '.'))
            except ValueError: pass

        waste_kwh = self.manager.data.get("temp_daily_waste", 0.0)

        # Recommendation logic
        rec = "Система сбалансирована"
        if self.manager.current_solar_waste_power > 0.5:
            rec = f"Теряется {self.manager.current_solar_waste_power} кВт. Рекомендуем включить мощную нагрузку!"
        elif waste_kwh > 2.0:
            rec = "Значительные потери за день. Рассмотрите возможность увеличения емкости АКБ."

        # Estimate potential power now
        prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
        prof_val = float(prof_gen.get(cur_hour, 0.0))
        coeff = getattr(self.manager, "last_blended_coeff", 1.0)
        # Reality check: potential cannot be less than actual generation
        potential_kw = round(max(prof_val * coeff, self.manager.avg_gen_kw), 3)

        return {
            "current_waste_kw": self.manager.current_solar_waste_power,
            "lost_potential_revenue": round(waste_kwh * cur_price, 2),
            "recommendation": rec,
            "potential_power_kw": potential_kw
        }


class BatteryAutonomySensor(SensorEntity):
    """Calculates how long the battery will last at current load."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_translation_key = "battery_autonomy"
        self._attr_unique_id = f"{manager.entry.entry_id}_battery_autonomy"
        self._attr_icon = "mdi:timer-sand"
        self._attr_native_unit_of_measurement = "h"
        self._attr_device_class = SensorDeviceClass.DURATION
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    async def async_added_to_hass(self):
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        soc, cap, energy_dc = self.manager.get_battery_state()
        eff = self.manager.get_efficiency_coefficient()

        # Energy available at AC side
        energy_ac = energy_dc * eff

        # Use 10-minute average load for stability, fallback to instant if history empty
        load_kw = self.manager.avg_load_kw
        if load_kw <= 0.005:
            # Check instant power if average is 0
            load_kw = sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.manager.power_load_sensors)

        if load_kw <= 0.005:
            return 99.0 # Effectively infinity for the sensor state

        hours = energy_ac / load_kw
        return round(float(hours), 2)

    @property
    def extra_state_attributes(self):
        soc, cap, energy_dc = self.manager.get_battery_state()
        eff = self.manager.get_efficiency_coefficient()
        load_kw = self.manager.avg_load_kw

        # 1. Total Autonomy (to 0%)
        total_hours = (energy_dc * eff) / load_kw if load_kw > 0.005 else 99.0

        # 2. Survival Autonomy (to min_soc_buy)
        min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
        reserve_energy_dc = (min_soc / 100.0) * cap
        usable_energy_dc = max(0.0, energy_dc - reserve_energy_dc)
        survival_hours = (usable_energy_dc * eff) / load_kw if load_kw > 0.005 else 99.0

        def format_time(h):
            if h >= 99: return "Бесконечно"
            total_min = int(h * 60)
            hh, mm = divmod(total_min, 60)
            if hh > 48: return "> 48ч"
            return f"{hh}ч {mm}мин"

        return {
            "autonomy_to_empty": format_time(total_hours),
            "autonomy_to_reserve": format_time(survival_hours),
            "current_load_avg_kw": round(float(load_kw), 3),
            "usable_energy_ac_kwh": round(float(energy_dc * eff), 3),
            "reserve_soc_target": min_soc
        }


