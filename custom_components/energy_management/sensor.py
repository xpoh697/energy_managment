import logging
import json
import os
from datetime import datetime, timedelta
from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change, async_track_time_interval
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import callback
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from .const import (
    DOMAIN,
    CONF_CONSUMPTION_SENSORS,
    CONF_GENERATION_SENSORS,
    CONF_DEDUCT_SENSORS,
    CONF_CUSTOM_PERIOD,
    CONF_FORECAST_TODAY_REMAINING,
    CONF_FORECAST_TOMORROW,
    CONF_BATTERY_SOC,
    CONF_BATTERY_CAPACITY,
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    CONF_PRICE_BUY_LIMIT,
    CONF_PRICE_SELL_LIMIT,
    CONF_PRICE_STOP_SELL,
    CONF_PRICE_SELL_ONLY_PV,
    CONF_PRICE_TOLERANCE,
    CONF_PRICE_SELL_TOLERANCE,
    CONF_BATTERY_MAX_POWER,
    CONF_TARGET_SOC_BUY,
    CONF_TARGET_SOC_SELL,
    CONF_MIN_SOC_BUY,
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_DEDUCT_SETTINGS,
    CONF_POWER_LOAD_SENSORS,
    CONF_POWER_GEN_SENSORS,
    CONF_SALE_PV_NO_BAT_MAX_HOUR,
    CONF_PRESENCE_SENSORS,
    CONF_INVERTER_LOSSES_SENSOR,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GRID_EXPORT_SENSORS,
    CONF_TOTAL_SYSTEM_COST,
    CONF_BATTERY_COST,
    CONF_BATTERY_RATED_CYCLES,
    CONF_ANOMALY_THRESHOLD,
    CONF_POWER_SENSOR,
    CONF_ACTIVE_HOLD_TIME,
    CONF_IS_CYCLIC,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the sensor platform."""
    manager = hass.data[DOMAIN][entry.entry_id]
    
    entities = []
    
    # We will create 3 defined periods and 1 custom
    periods = {
        "week": ("Неделя", 7),
        "month": ("Месяц", 30),
        "year": ("Год", 365),
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
    entities.append(BatteryDepletionTimeSensor(manager, "Battery Depletion Forecast"))
    
    if has_generation:
        entities.append(BatteryEndOfDaySOCSensor(manager, "Прогноз заряда к закату"))
    

    # Combined Savings / revenue tracking sensor
    if has_consumption and config_data.get(CONF_PRICE_BUY):
        entities.append(SavingsSensor(manager, "total", "Экономия: Итоговая выгода"))

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

    async_add_entities(entities)

def _get_kwh_val(state_obj):
    """Normalize state value to kWh."""
    if not state_obj or state_obj.state in ("unknown", "unavailable"):
        return None
    try:
        val = float(str(state_obj.state).replace(',', '.'))
    except ValueError:
        return None
        
    unit = state_obj.attributes.get("unit_of_measurement")
    if unit in ("Wh", "W"):
        return val / 1000.0
    elif unit in ("MWh", "MW"):
        return val * 1000.0
    return val

class EnergyProfileManager:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        
        config_data = {**entry.data, **entry.options}
        
        # Initialize internal storage handler for preserving profiles across restarts
        self.store = Store(hass, STORAGE_VERSION, f"energy_management_{entry.entry_id}")
        
        self.consumption_sensors = set(config_data.get(CONF_CONSUMPTION_SENSORS, []))
        self.generation_sensors = set(config_data.get(CONF_GENERATION_SENSORS, []))
        self.deduct_sensors = set(config_data.get(CONF_DEDUCT_SENSORS, []))
        self.grid_import_sensors = set(config_data.get(CONF_GRID_IMPORT_SENSORS, []))
        self.grid_export_sensors = set(config_data.get(CONF_GRID_EXPORT_SENSORS, []))
        self.deduct_settings = config_data.get(CONF_DEDUCT_SETTINGS, {})
        self.all_sensors = self.consumption_sensors | self.generation_sensors | self.deduct_sensors | self.grid_import_sensors | self.grid_export_sensors
        
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
        
        # Presence / occupancy sensors (person.* or binary_sensor.*)
        presence_raw = config_data.get(CONF_PRESENCE_SENSORS, [])
        self.presence_sensors = [presence_raw] if isinstance(presence_raw, str) else (presence_raw or [])
        
        self.price_buy_sensors = [config_data.get(CONF_PRICE_BUY)] if config_data.get(CONF_PRICE_BUY) else []
        self.price_sell_sensors = [config_data.get(CONF_PRICE_SELL)] if config_data.get(CONF_PRICE_SELL) else []
        
        self.all_price_sensors = set([s for s in self.price_buy_sensors + self.price_sell_sensors if s])
        
        self.max_days = 365
        self.custom_period = config_data.get(CONF_CUSTOM_PERIOD, 14)
        
        # Internal configuration from UI (Number/Switch defaults handled by platform)
        self.settings = {}
        
        # Array to store history of consumption per hour. e.g. "13" -> [1.3, 1.2, 1.5...]
        self.data = {}
        
        self.current_consumption_base = 0.0
        self.current_consumption_total = 0.0
        self.current_generation = 0.0
        self.current_grid_import = 0.0
        self.current_grid_export = 0.0
        self.current_hourly_deduct = 0.0  # Accumulator for all deduct sensors this hour
        self.sensor_last_values = {}
        
        self.daily_deduct_consumption = {s: 0.0 for s in self.deduct_sensors}
        
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
            self.all_sensors = self.all_sensors | {self.inverter_losses_sensor}
        
        # Track historical power samples for 5-10 minute average smoothing
        self.power_history = []
        
        # Power sensor runtime tracking
        self.learned_standby_power = {}   # sensor_id -> watts
        self.learned_real_power = {}      # sensor_id -> watts
        self.learned_avg_cycle_power = {} # sensor_id -> watts (smoothed over entire cycle)
        self.learned_cycle_total_kwh = {} # sensor_id -> kWh total per cycle
        self.cycle_start_time = {}        # sensor_id -> timestamp (when it was LAST seen active)
        self.cycle_actual_start_time = {} # sensor_id -> timestamp (when current cycle REALLY started)
        self.cycle_energy_start = {}      # sensor_id -> kWh consumed at cycle start
        self.last_known_power = {}        # sensor_id -> watts (for fallback)
        # Sensors that need to re-establish a baseline on first read after restart
        # (prevents large accumulated deltas from being counted as generation/consumption)
        self._sensors_need_baseline: set = set()

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
            
        if "prices_sell" not in self.data:
            self.data["prices_sell"] = {}

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
        self.data["sensor_last_values"] = self.sensor_last_values
        self.data["daily_deduct_consumption"] = dict(self.daily_deduct_consumption)
        self.data["hourly_accumulators"] = {
            "consumption_total": self.current_consumption_total,
            "generation": self.current_generation,
            "grid_import": self.current_grid_import,
            "grid_export": self.current_grid_export,
            "losses": self.current_losses,
            "hourly_deduct": self.current_hourly_deduct,
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
                    
        self._unsub_state = async_track_state_change_event(
            self.hass, list(self.all_sensors | self.all_price_sensors), self._async_state_changed
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
            self._poll_instant_power(dt_util.utcnow())

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
            load_kw = sum((_get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_load_sensors)
        if self.power_gen_sensors:
            gen_kw = sum((_get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_gen_sensors)
            
        self.power_history.append({"time": now, "load_kw": load_kw, "gen_kw": gen_kw})
        
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
                old_real = self.learned_real_power.get(sensor_id, cur_p)
                self.learned_real_power[sensor_id] = round(old_real * 0.9 + cur_p * 0.1, 1)
            else:
                # Standby Power Learning (Slow EMA)
                if 0.1 < cur_p < (standby + 5.0):
                    old_s = self.learned_standby_power.get(sensor_id, cur_p)
                    self.learned_standby_power[sensor_id] = round(old_s * 0.95 + cur_p * 0.05, 2)
                    
                # If we just finished a cycle
                if sensor_id in self.cycle_actual_start_time:
                    duration = (now - self.cycle_actual_start_time[sensor_id]).total_seconds() / 3600.0
                    energy = self.daily_deduct_consumption.get(sensor_id, 0.0) - self.cycle_energy_start.get(sensor_id, 0.0)
                    
                    if energy > 0.02 and duration > (1/60.0): # At least 20Wh and 1 minute
                        avg_p_w = (energy * 1000.0) / duration
                        self.learned_real_power[sensor_id] = round(avg_p_w, 1)
                        if settings.get(CONF_IS_CYCLIC):
                            self.learned_cycle_total_kwh[sensor_id] = round(energy, 3)
                            self.learned_avg_cycle_power[sensor_id] = round(avg_p_w, 1)
                    
                    self.cycle_actual_start_time.pop(sensor_id, None)
                    self.cycle_energy_start.pop(sensor_id, None)

        self._notify_update()

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
        
        # Handle prices
        if entity_id in self.all_price_sensors:
            self._update_prices_from_sensor(entity_id, new_state)
            return

        # Handle energy sensors
        new_val = _get_kwh_val(new_state)
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

        # If it is the first read after restart, the delta might be large (accumulated while HA was down).
        # We count it towards the DAILY TOTAL and HOURLY ACCUMULATORS (for budget, forecast, and savings)
        # but NOT towards the HOURLY PROFILE HISTORY to avoid massive visual spikes in the charts.
        if is_restarting:
            if delta > 0 and delta < 50.0:
                if entity_id in self.generation_sensors:
                    self.data["temp_daily_gen"] = self.data.get("temp_daily_gen", 0.0) + delta
                    self.current_generation += delta
                if entity_id in self.consumption_sensors:
                    self.current_consumption_base += delta
                    self.current_consumption_total += delta
                if entity_id in self.deduct_sensors:
                    if entity_id not in self.daily_deduct_consumption:
                        self.daily_deduct_consumption[entity_id] = 0.0
                    self.daily_deduct_consumption[entity_id] += delta
                    # Deduct sensors subtract from base consumption
                    self.current_consumption_base -= delta
                if entity_id in self.grid_import_sensors:
                    self.current_grid_import += delta
                if entity_id in self.grid_export_sensors:
                    self.current_grid_export += delta
                if self.inverter_losses_sensor and entity_id == self.inverter_losses_sensor:
                    self.current_losses += delta
            self.sensor_last_values[entity_id] = new_val
            return
            
        if delta > 100.0:
            # If the calculated delta is impossible for a 1-minute HA tick (100 kWh = 6000 kW power),
            # this means the sensor gave us a garbage reading before, and now returned to normal.
            # E.g. device disconnected, HA got 0, device reconnected, HA got 18000. 
            # We MUST reset the baseline, but we MUST NOT process this delta.
            _LOGGER.warning("Energy Management: Ignored impossible delta of %s kWh for sensor %s. Baseline reset.", delta, entity_id)
            self.sensor_last_values[entity_id] = new_val
            return

        self.sensor_last_values[entity_id] = new_val
        if delta == 0:
            return
            
        if entity_id in self.consumption_sensors:
            self.current_consumption_base += delta
            self.current_consumption_total += delta
        if entity_id in self.generation_sensors:
            self.current_generation += delta
            self.data["temp_daily_gen"] = self.data.get("temp_daily_gen", 0.0) + delta
        if entity_id in self.deduct_sensors:
            self.current_consumption_base -= delta
            if entity_id not in self.daily_deduct_consumption:
                self.daily_deduct_consumption[entity_id] = 0.0
            self.daily_deduct_consumption[entity_id] += delta
        if self.inverter_losses_sensor and entity_id == self.inverter_losses_sensor:
            # This sensor is a daily counter — it resets at midnight.
            # delta may be negative at midnight reset, which we skip.
            if delta > 0:
                self.current_losses += delta
        if entity_id in self.grid_import_sensors:
            self.current_grid_import += delta
        if entity_id in self.grid_export_sensors:
            self.current_grid_export += delta
            
        if self.current_consumption_base < 0:
            self.current_consumption_base = 0.0
        if self.current_consumption_total < 0:
            self.current_consumption_total = 0.0
            
        self._notify_update()

    async def _async_reset_hour(self, now):
        # Time is precisely the top of the new hour, meaning we need to save the PAST hour.
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
        if self.price_buy_sensors or self.price_sell_sensors:
            past_dt = now - timedelta(hours=1)
            past_date_str = past_dt.strftime("%Y-%m-%d")

            def _get_stored_price(store, date_str, hour):
                try:
                    v = store.get(date_str, {}).get(str(hour))
                    return float(str(v).replace(",", ".")) if v is not None else None
                except (ValueError, TypeError):
                    return None

            p_buy  = _get_stored_price(self.data.get("prices_buy",  {}), past_date_str, past_hour)
            p_sell = _get_stored_price(self.data.get("prices_sell", {}), past_date_str, past_hour)

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

            # Keep at most 400 days
            if len(self.data["savings"]) > 400:
                del self.data["savings"][sorted(self.data["savings"].keys())[0]]
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
        
        # Reset daily deduct consumption at midnight
        if now.hour == 0:
            self.data["last_reset_date"] = now.strftime("%Y-%m-%d")
            for s in self.daily_deduct_consumption:
                self.daily_deduct_consumption[s] = 0.0
                
            # Forecast history rolling update
            actual = self.data.get("temp_daily_gen", 0.0)
            expected = self.data.get("temp_max_forecast", 0.0)
            
            if expected > 0.1 or actual > 0.1:
                if "forecast_history" not in self.data:
                    self.data["forecast_history"] = []
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
            self.data["temp_max_forecast"] = 0.0

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
        # This ensures that saved accumulators = 0, saved daily_deduct is fresh,
        # and sensor_last_values reflect the latest readings at the hour boundary.
        await self.async_save()
        
        self._notify_update()

    def register_listener(self, update_cb):
        self.update_listeners.append(update_cb)
        
    def _notify_update(self):
        for cb in self.update_listeners:
            cb()

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
            total += day.get("total", 0.0)
        return total

    def get_battery_degradation_cost(self):
        """Cost of battery wear per kWh (Cycle Cost)."""
        batt_cost = self.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        
        # Get capacity from sensor if available, otherwise fallback to 10kWh
        _, cap, _ = self.get_battery_state()
        if cap <= 0:
            cap = 10.0
        
        if cycles <= 0 or batt_cost <= 0:
            return 0.0
            
        # Cost per 1 kWh of throughput (Charge + Discharge wear)
        return batt_cost / (cycles * cap)

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
        profile = {}
        for h in range(24):
            sh = str(h)
            history = self.data[profile_type][sh]
            relevant = history[-days:] if days > 0 else history
            valid_vals = []
            
            for item in relevant:
                try:
                    if isinstance(item, dict):
                        v = float(str(item.get("v", 0.0)).replace(',', '.'))
                        wd = item.get("wd")
                        occ = item.get("occ")
                    else:
                        v = float(str(item).replace(',', '.'))
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
                except ValueError:
                    pass
                
            if valid_vals:
                profile[str(h)] = round(sum(valid_vals) / len(valid_vals), 3)
            else:
                profile[str(h)] = 0.0
        return profile

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
                v = float(str(item.get("v", 0.0)).replace(',', '.'))
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
        """Calculate inverter efficiency coefficient from historical losses data.
        
        Returns a multiplier in [0.70, 1.0] representing the fraction of energy
        that actually makes it through the inverter (1 - losses/generation).
        
        If no losses sensor is configured, returns 1.0 (no correction applied).
        Requires at least 5 hourly samples with non-zero generation to activate.
        """
        if not self.inverter_losses_sensor:
            return 1.0
        
        days = self.custom_period
        total_gen = 0.0
        total_losses = 0.0
        sample_count = 0
        
        losses_data = self.data.get("losses", {})
        for h in range(24):
            records = losses_data.get(str(h), [])
            relevant = records[-days:] if days > 0 else records
            for rec in relevant:
                if not isinstance(rec, dict):
                    continue
                gen = float(str(rec.get("gen", 0.0)).replace(",", "."))
                loss = float(str(rec.get("v", 0.0)).replace(",", "."))
                if gen > 0.01:   # Only count hours with real generation
                    total_gen += gen
                    total_losses += loss
                    sample_count += 1
        
        if sample_count < 5 or total_gen < 0.1:
            return 1.0
        
        efficiency = (total_gen - total_losses) / total_gen
        # Clamp: don't let it go below 0.70 (30% losses is physically impossible for a good inverter)
        return max(0.70, min(1.0, efficiency))

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
                    try:
                        if isinstance(last_record, dict):
                            res[sh] = round(float(str(last_record.get("v", 0.0)).replace(',', '.')), 3)
                        else:
                            res[sh] = round(float(str(last_record).replace(',', '.')), 3)
                    except ValueError:
                        res[sh] = 0.0
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
            try:
                # Handle both numeric and string values from UI
                return float(str(val).replace(',', '.'))
            except (ValueError, TypeError):
                return default
        return val

    async def async_set_setting(self, key, value):
        self.settings[key] = value
        self.data["settings"] = self.settings
        await self.async_save()
        self._notify_update()

    def _is_currently_pulling_power(self, sensor_id: str) -> bool:
        """Return True if the device currently has an active cycle (pulling power above standby)."""
        if sensor_id not in self.cycle_start_time:
            return False
        settings = self.deduct_settings.get(sensor_id, {})
        p_sensor = settings.get(CONF_POWER_SENSOR) if isinstance(settings, dict) else None
        if not p_sensor:
            # No power sensor configured — assume active if in cycle_start_time
            return True
        p_state = self.hass.states.get(p_sensor)
        if not p_state or p_state.state in ("unknown", "unavailable"):
            return True  # Keep as active if sensor unavailable (use last known state)
        try:
            cur_p = float(str(p_state.state).replace(',', '.'))
            if p_state.attributes.get("unit_of_measurement") == "kW":
                cur_p *= 1000.0
        except ValueError:
            return True
        standby = self.learned_standby_power.get(sensor_id, 15.0)
        return cur_p > (standby + 10.0)

    def get_sensor_float(self, entity_id, default=0.0):
        """Read a float value from a sensor entity."""
        if not entity_id:
            return default
        st = self.hass.states.get(entity_id)
        if not st or st.state in ("unknown", "unavailable"):
            return default
        try:
            return float(str(st.state).replace(',', '.'))
        except ValueError:
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
            v = _get_kwh_val(st)
            if v is not None:
                val_sum += v
        return val_sum if val_sum > 0 else None

    @staticmethod
    def get_cc_cv_ratio(soc):
        """Calculate CC/CV charge acceptance ratio based on battery SOC.
        
        Returns a value between 0.02 and 1.0 representing the fraction
        of max charge power the battery can accept.
        """
        if soc <= 85.0:
            ratio = 1.0
        elif soc <= 92.0:
            ratio = 1.0 - ((soc - 85.0) / 7.0) * 0.3
        elif soc <= 97.0:
            ratio = 0.7 - ((soc - 92.0) / 5.0) * 0.5
        elif soc <= 98.0:
            ratio = 0.2 - ((soc - 97.0) / 1.0) * 0.1
        else:
            ratio = 0.1 - ((soc - 98.0) / 2.0) * 0.08
        return max(0.02, min(1.0, ratio))

    def get_gen_forecast_coefficient(self, forecast_value, prof_gen, hour_start, hour_end):
        """Calculate scaling coefficient between forecast and historical generation."""
        hist_sum = sum(float(prof_gen.get(str(h), 0.0)) for h in range(hour_start, hour_end))
        if forecast_value is not None and hist_sum > 0:
            return forecast_value / hist_sum
        return 1.0

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        now = dt_util.now()
        cur_hour = now.hour
        
        # 1. Get Forecast Remaining
        forecast_raw_today = self.get_forecast_value(self.forecast_today_sensor)
        forecast_val = forecast_raw_today if forecast_raw_today is not None else 0.0
        if self.forecast_today_sensor:
            self.data["temp_max_forecast"] = max(self.data.get("temp_max_forecast", 0.0), forecast_val)
                
        # Calculate Historical Reliability Coefficient
        hist_coeff = 1.0
        history = self.data.get("forecast_history", [])
        if history:
            tot_actual = sum(h["actual"] for h in history)
            tot_expected = sum(h["forecast"] for h in history)
            if tot_expected > 0.1:
                hist_coeff = tot_actual / tot_expected
                hist_coeff = max(0.2, min(hist_coeff, 2.0)) # Clamp coefficient manually between 0.2 and 2.0
                
        # Calculate Intra-day Dynamic Coefficient (Blended)
        prof_gen_today = self.get_average_profile("generation", days_for_profile, "all")
        total_hist_gen = sum(float(prof_gen_today.get(str(h), 0.0)) for h in range(24))
        
        fraction_so_far = 0.0
        if total_hist_gen > 0.1:
            hist_gen_so_far = sum(float(prof_gen_today.get(str(h), 0.0)) for h in range(cur_hour))
            fraction_so_far = hist_gen_so_far / total_hist_gen
            
        # Today's actual is taken from the daily accumulator (resilient to restart gaps)
        actual_today = self.data.get("temp_daily_gen", 0.0)
        expected_today_total = self.data.get("temp_max_forecast", 0.0)
        expected_today_so_far = expected_today_total * fraction_so_far
        
        today_coeff = hist_coeff
        if expected_today_so_far > 0.1:
            today_coeff = actual_today / expected_today_so_far
            today_coeff = max(0.2, min(today_coeff, 2.0))
            
        blended_coeff = (today_coeff * fraction_so_far) + (hist_coeff * (1.0 - fraction_so_far))
                
        forecast_val_adjusted = forecast_val * blended_coeff
                
        # 2. Get Battery Energy Available
        _, _, batt_energy_val = self.get_battery_state()
                    
        # 3. Expected profile until 08AM
        today_type = "weekend" if now.weekday() >= 5 else "weekday"
        tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
        
        prof_today = self.get_average_profile("consumption_base", days_for_profile, today_type)
        prof_tom = self.get_average_profile("consumption_base", days_for_profile, tom_type)
        
        expected_consumption = 0.0
        
        for h in range(cur_hour, 24):
            expected_consumption += prof_today.get(str(h), 0.0)
        for h in range(0, 8):
            expected_consumption += prof_tom.get(str(h), 0.0)
        
        # Apply occupancy coefficient to scale consumption when nobody is home
        occ_coeff = self.get_occupancy_coefficient()
        expected_consumption *= occ_coeff
        
        # Apply inverter efficiency coefficient to both forecast and battery energy
        eff_coeff = self.get_efficiency_coefficient()
        forecast_val_adjusted *= eff_coeff
        batt_energy_val *= eff_coeff
            
        initial_budget = (forecast_val_adjusted + batt_energy_val) - expected_consumption
        available_budget = initial_budget
        
        # 3.5 Calculate Instant Available Power (kW) based on recent deltas
        # We estimate instant power (kW) by looking at generation vs consumption. 
        # (This is a rough heuristic based on accumulation, but it's effective for catching if we are dragging the battery down *right now*)
        current_instant_surplus_kw = 999.0 # Assume enough if no generators
        if self.generation_sensors:
            # Let's derive a quick 'instant surplus' metric from the current hourly accumulator state. 
            # OR better yet, let's keep it simple: Has generation exceeded consumption since the last tick?
            # It's hard to get true "instant" kW from kWh delta without timing the ticks exactly, 
            # but if we just rely on battery discharging vs charging we'd need battery power sensor.
            # Instead, we'll try to use a simple "is generation > consumption" check 
            # as a safety factor if the user requested kW limits.
            pass
            
        # For a truly robust instant kW check without polling external sensors, 
        # we can calculate it dynamically if we track the time of the last update.
        # But to keep it bulletproof: we'll add an expected power check vs budget.
        
        # 4. Market and Solar state for "Only Solar or Free" constraint
        today_str = now.strftime("%Y-%m-%d")
        
        prices_store_buy = self.data.get("prices_buy", {})
        cur_price_buy = None
        if today_str in prices_store_buy and str(cur_hour) in prices_store_buy[today_str]:
            try:
                cur_price_buy = float(str(prices_store_buy[today_str][str(cur_hour)]).replace(',', '.'))
            except ValueError:
                cur_price_buy = None
                
        prices_store_sell = self.data.get("prices_sell", {})
        cur_price_sell = None
        if today_str in prices_store_sell and str(cur_hour) in prices_store_sell[today_str]:
            try:
                cur_price_sell = float(str(prices_store_sell[today_str][str(cur_hour)]).replace(',', '.'))
            except ValueError:
                cur_price_sell = None
            
        # prof_gen_today is already calculated above for the coefficient blending
        cur_expected_gen = float(prof_gen_today.get(str(cur_hour), 0.0))
        sun_state = self.hass.states.get("sun.sun")
        is_sun_up = sun_state and sun_state.state == "above_horizon"
        
        is_solar_or_free = False
        if cur_price_buy is not None and cur_price_buy <= 0.0:
            is_solar_or_free = True
        elif cur_expected_gen > 0.1 or is_sun_up:
            # We consider it "solar time" if historically we generate > 100Wh this hour, or the sun is simply up.
            is_solar_or_free = True
            
        sell_only_pv_threshold = self.get_setting(CONF_PRICE_SELL_ONLY_PV, 999.0)
        is_export_peak = False
        if cur_price_sell is not None and cur_price_sell >= sell_only_pv_threshold:
            is_export_peak = True
        elif not skip_strategy_check:
            sell_stat = self.get_market_strategy("sell")
            if sell_stat.get("state") == "active":
                is_export_peak = True
        
        # 5. Filter and sort permissions
        permissions = {}
        permissions_reasons = {}
        sorted_sensors = sorted(self.deduct_settings.items(), key=lambda item: item[1].get("priority", 999))
        
        avg_gen_kw = 0.0
        if self.power_history:
            avg_gen_kw = sum(x["gen_kw"] for x in self.power_history) / len(self.power_history)

        available_gen_kw = avg_gen_kw
        available_power_kw = avg_gen_kw
        initial_power_kw = avg_gen_kw

        # Pass 1: Pre-detect and commit resources for active cycles (Grace Period)
        committed_sensors = {}
        
        for sensor_id, settings in sorted_sensors:
            # We need to perform the same cycle/grace detection as in the main loop
            p_sensor = settings.get(CONF_POWER_SENSOR)
            hold_time_min = settings.get(CONF_ACTIVE_HOLD_TIME, 15)
            
            cur_p_watts = 0.0
            if p_sensor:
                p_state = self.hass.states.get(p_sensor)
                if p_state and p_state.state not in ("unknown", "unavailable"):
                    try:
                        cur_p_watts = float(str(p_state.state).replace(',', '.'))
                        if p_state.attributes.get("unit_of_measurement") == "kW":
                            cur_p_watts *= 1000.0
                    except ValueError: pass

            standby_threshold = self.learned_standby_power.get(sensor_id, 15.0)
            is_currently_pulling_power = cur_p_watts > (standby_threshold + 10.0)
            
            is_cyclic = settings.get(CONF_IS_CYCLIC, False)
            curr_cycle_kwh = 0.0
            if sensor_id in self.cycle_energy_start and p_sensor:
                st = self.hass.states.get(p_sensor)
                if st and st.state not in ("unknown", "unavailable"):
                    try: curr_cycle_kwh = max(0.0, float(st.state) - self.cycle_energy_start[sensor_id])
                    except ValueError: pass

            is_in_grace_period = False
            if sensor_id in self.cycle_start_time:
                diff = (now - self.cycle_start_time[sensor_id]).total_seconds()
                if diff < (float(hold_time_min) * 60):
                    is_in_grace_period = True
                    if is_cyclic and sensor_id in self.learned_cycle_total_kwh:
                        if curr_cycle_kwh >= (self.learned_cycle_total_kwh[sensor_id] * 1.05):
                            is_in_grace_period = False

            if is_in_grace_period:
                req_kw = self.learned_real_power.get(sensor_id, settings.get("required_kw", 0.0) * 1000.0) / 1000.0
                active_kw = (cur_p_watts / 1000.0) if cur_p_watts > 10.0 else req_kw
                
                # Commit resources
                available_power_kw -= active_kw
                available_gen_kw -= active_kw
                available_budget -= active_kw
                
                committed_sensors[sensor_id] = {
                    "is_in_grace_period": True,
                    "active_kw": active_kw,
                    "cur_p_watts": cur_p_watts
                }

        # Pass 2: Evaluate candidate loads in priority order
        for sensor_id, settings in sorted_sensors:
            if is_export_peak:
                permissions[sensor_id] = False
                permissions_reasons[sensor_id] = "Блокировка: Дорогой час (Выгоднее продавать в сеть PV / Батарею)"
                continue
                
            only_solar_free = settings.get("only_solar_or_negative_price", False)
            if only_solar_free and not is_solar_or_free:
                permissions[sensor_id] = False
                permissions_reasons[sensor_id] = "Блокировка: Ограничение 'Только от солнца или Цена <= 0'"
                continue
                
            # If already committed in Pass 1
            if sensor_id in committed_sensors:
                permissions[sensor_id] = True
                permissions_reasons[sensor_id] = "Разрешено: Удержание активного цикла (Grace Period)"
                continue

            # Full evaluation for all other (idle or non-grace) sensors
            p_sensor = settings.get(CONF_POWER_SENSOR)
            hold_time_min = settings.get(CONF_ACTIVE_HOLD_TIME, 15)
            
            cur_p_watts = 0.0
            if p_sensor:
                p_state = self.hass.states.get(p_sensor)
                if p_state and p_state.state not in ("unknown", "unavailable"):
                    try:
                        cur_p_watts = float(str(p_state.state).replace(',', '.'))
                        if p_state.attributes.get("unit_of_measurement") == "kW":
                            cur_p_watts *= 1000.0
                        self.last_known_power[sensor_id] = cur_p_watts
                    except ValueError:
                        cur_p_watts = self.last_known_power.get(sensor_id, 0.0)
                else:
                    cur_p_watts = self.last_known_power.get(sensor_id, 0.0)

            req_kwh = settings.get("required_kwh", 2.5)
            req_kw = self.learned_real_power.get(sensor_id, settings.get("required_kw", 0.0) * 1000.0) / 1000.0
            consumed = self.daily_deduct_consumption.get(sensor_id, 0.0)
            
            # is_idle = NOT in active cycle (it's a candidate to START)
            is_currently_pulling_now = self._is_currently_pulling_power(sensor_id)
            is_idle = not is_currently_pulling_now
            
            power_bottleneck = False
            gen_bottleneck = False
            is_free_price = cur_price_buy is not None and cur_price_buy <= 0.0

            if req_kw > 0.0:
                if available_power_kw < req_kw:
                    power_bottleneck = True
                if only_solar_free and not is_free_price:
                    if available_gen_kw < (req_kw * 0.6):
                        gen_bottleneck = True
            elif initial_power_kw > 0.5 and available_power_kw < 0:
                power_bottleneck = True

            if req_kwh == 0:
                # Dynamic load logic
                if available_budget > 0 and not power_bottleneck and not gen_bottleneck:
                    permissions[sensor_id] = True
                    if req_kw > 0.0:
                        available_power_kw -= req_kw
                        if only_solar_free and not is_free_price:
                            available_gen_kw -= (float(req_kw) * 0.6)
                    
                    b_val = round(max(0.0, float(available_budget)), 2)
                    g_val = round(max(0.0, float(available_power_kw)), 2)
                    permissions_reasons[sensor_id] = f"Разрешено: Динамическая (Профицит {b_val} кВт*ч, Доступно {g_val} кВт)"
                else:
                    permissions[sensor_id] = False
                    g_val = round(float(available_power_kw), 2)
                    g_gen = round(float(available_gen_kw), 2)
                    if gen_bottleneck:
                        permissions_reasons[sensor_id] = f"Блокировка: Доступная генер. {g_gen} кВт < 60% от {req_kw} кВт"
                    elif power_bottleneck:
                        permissions_reasons[sensor_id] = f"Блокировка: Доступно {g_val} кВт < Мощность {req_kw} кВт"
                    elif available_budget <= 0:
                        permissions_reasons[sensor_id] = f"Блокировка: Нет профицита энергии ({round(float(available_budget), 2)} кВт*ч)"
                    else:
                        permissions_reasons[sensor_id] = "Блокировка: Ограничения по мощности"
                continue

            needed = req_kwh - consumed
            if needed <= 0:
                permissions[sensor_id] = True
                permissions_reasons[sensor_id] = "Разрешено: Дневная норма выполнена (или перерасход)"
            elif available_budget >= needed and not power_bottleneck and not gen_bottleneck:
                permissions[sensor_id] = True
                available_budget -= float(needed)
                if only_solar_free and not is_free_price and req_kw > 0.0:
                    available_gen_kw -= (float(req_kw) * 0.6)
                
                n_val = round(float(needed), 2)
                permissions_reasons[sensor_id] = f"Разрешено: Зарезервировано {n_val} кВт*ч из профицита"
            else:
                permissions[sensor_id] = False
                if gen_bottleneck:
                    permissions_reasons[sensor_id] = f"Блокировка: Доступная генерация {round(float(available_gen_kw), 2)} кВт < 60% от {req_kw} кВт"
                elif power_bottleneck:
                    permissions_reasons[sensor_id] = f"Блокировка: Доступно {round(float(available_power_kw), 2)} кВт < Мощность {req_kw} кВт"
                else:
                    permissions_reasons[sensor_id] = f"Блокировка: Не хватает энергии (нужно {round(float(needed), 2)} кВт*ч, доступно {round(float(available_budget), 2)} кВт*ч)"
                
                
        return {
            "initial_budget": initial_budget,
            "permissions": permissions,
            "permissions_reasons": permissions_reasons,
            "forecast_val": forecast_val_adjusted,
            "forecast_raw": forecast_val,
            "forecast_coefficient": blended_coeff,
            "forecast_hist_coefficient": hist_coeff,
            "forecast_today_coefficient": today_coeff,
            "batt_energy_val": batt_energy_val,
            "expected_consumption": expected_consumption,
            "debug_actual_today": actual_today,
            "debug_expected_today_total": expected_today_total,
            "debug_expected_today_so_far": expected_today_so_far,
            "debug_fraction_so_far": fraction_so_far,
            "occupancy_coefficient": occ_coeff,
            "efficiency_coefficient": eff_coeff
        }
        
    def get_market_strategy(self, mode="buy"):
        res = {
            "state": "idle",
            "active_hours": [],
            "active_periods": "",
            "recommended_power_kw": 0.0,
            "target_price": 0.0,
            "limit_used": 0.0,
            "today_prices": {},
            "tomorrow_prices": {},
            "multi_cycle": "Не предвидится"
        }
        
        now = dt_util.now()
        cur_hour = now.hour
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        
        prices_store = self.data.get(f"prices_{mode}", {})
        today_prices = prices_store.get(today_str, {})
        tomorrow_prices = prices_store.get(tomorrow_str, {})
        
        res["today_prices"] = today_prices
        res["tomorrow_prices"] = tomorrow_prices
        
        if not today_prices:
            return res
            
        if mode == "buy":
            tolerance = self.get_setting(CONF_PRICE_TOLERANCE, 0.0)
        else:
            tolerance = self.get_setting(CONF_PRICE_SELL_TOLERANCE, 0.0)
            
        max_power = self.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        
        batt_soc, batt_cap, _ = self.get_battery_state()
        
        # Unify today and tomorrow prices into a 48h timeline for FULL window evaluation
        all_prices = {}
        for h, p in today_prices.items():
            try: all_prices[int(h)] = float(str(p).replace(',', '.'))
            except ValueError: all_prices[int(h)] = 0.0
        for h, p in tomorrow_prices.items():
            try: all_prices[int(h) + 24] = float(str(p).replace(',', '.'))
            except ValueError: all_prices[int(h) + 24] = 0.0
            
        negative_hours = [h for h, p in all_prices.items() if p < 0 and h >= cur_hour]

        # Evaluate the entire 48-hour horizon continuously without blind spots
        active_window = (0, 47)
        res["analyzed_window"] = "Сегодня 00:00 - Завтра 23:59"
                
        target_hours = []
        target_price = 0.0
        limit_used = 0.0
        carte_blanche = False

        def get_peaks(window, is_sell, limit, tol):
            if not window: return []
            if is_sell:
                best_p = max(window.values())
                if best_p >= limit:
                    return [(h, p) for h, p in window.items() if p >= limit and p >= (best_p - tol)]
            else:
                best_p = min(window.values())
                if best_p <= limit:
                    return [(h, p) for h, p in window.items() if p <= limit and p <= (best_p + tol)]
            return []

        window_today = {h: p for h, p in all_prices.items() if cur_hour <= h < 24}
        window_tomorrow = {h: p for h, p in all_prices.items() if 24 <= h <= 47}

        if mode == "buy":
            limit = self.get_setting(CONF_PRICE_BUY_LIMIT, 99.0)
            res["limit_used"] = limit
            if negative_hours:
                # Carte blanche: we buy whenever price is negative, ignore windows
                target_hours = negative_hours
                target_price = min([all_prices[h] for h in negative_hours])
                res["target_price"] = target_price
                carte_blanche = True
            else:
                peaks_today = get_peaks(window_today, False, limit, tolerance)
                peaks_tom = get_peaks(window_tomorrow, False, limit, tolerance)
                combined = peaks_today + peaks_tom
                if combined:
                    target_hours = [h for h, p in combined]
                    target_price = min(p for h, p in combined)
                    res["target_price"] = target_price
                    res["limit_used"] = limit

        else: # sell
            limit = self.get_setting(CONF_PRICE_SELL_LIMIT, -99.0)
            res["limit_used"] = limit
            if negative_hours and cur_hour in negative_hours:
                # If price is negative today, we PAY to sell to the grid. NEVER SELL.
                res["state"] = "price_limit_not_met"
                return res
            
            # Profitability check against degradation cost
            deg_cost = self.get_battery_degradation_cost()
            eff = self.get_efficiency_coefficient()
            # Requirement: (SellPrice - BuyPrice) * Efficiency > 2 * deg_cost
            # We don't always know the exact buy price we used, but we can assume the Buy Limit or current min price 
            min_buy_limit = self.get_setting(CONF_PRICE_BUY_LIMIT, 99.0)
            
            # Filter peaks that aren't actually profitable
            def is_profitable(price):
                if deg_cost <= 0: return True
                # (Price - BuyLimit) * Eff > wear_of_cycle
                profit_per_kwh = (price - min_buy_limit) * eff
                return profit_per_kwh > (2 * deg_cost)

            raw_peaks_today = get_peaks(window_today, True, limit, tolerance)
            raw_peaks_tom = get_peaks(window_tomorrow, True, limit, tolerance)
            
            if not raw_peaks_today and not raw_peaks_tom:
                res["state"] = "price_limit_not_met"
                return res

            peaks_today = [(h, p) for h, p in raw_peaks_today if is_profitable(p)]
            peaks_tom = [(h, p) for h, p in raw_peaks_tom if is_profitable(p)]
            
            if not peaks_today and not peaks_tom:
                res["state"] = "unprofitable_arbitrage"
                res["multi_cycle"] = "Деградация АКБ > Выгоды"
                return res

            if peaks_today and peaks_tom:
                # We have peaks on both days. Check if we can recharge between them.
                max_h_today = max(h for h, p in peaks_today)
                min_h_tom = min(h for h, p in peaks_tom)
                
                buy_limit = self.get_setting(CONF_PRICE_BUY_LIMIT, 99.0)
                can_recharge = False
                for h in range(max_h_today + 1, min_h_tom):
                    if all_prices.get(h, 99.0) <= buy_limit:
                        can_recharge = True
                        res["multi_cycle"] = "Благоприятно (Дешевая сеть ночью)"
                        break
                    if 8 <= (h % 24) <= 16:
                        # Ensure there's actually a decent forecast for solar generation!
                        fsensors = self.forecast_tomorrow_sensor
                        if fsensors:
                            if isinstance(fsensors, str): fsensors = [fsensors]
                            val_sum = 0.0
                            for fsensor in fsensors:
                                st = self.hass.states.get(fsensor)
                                v = _get_kwh_val(st)
                                if v is not None: val_sum += v
                            if val_sum > 3.0: # threshold of 3kWh expected solar energy 
                                can_recharge = True
                                res["multi_cycle"] = "Благоприятно (Ожидается солнце)"
                                break
                        else:
                            # If no forecast sensors are configured, fallback to pure daylight hours
                            can_recharge = True
                            res["multi_cycle"] = "Благоприятно (Световой день)"
                            break
                        
                if can_recharge:
                    combined = peaks_today + peaks_tom
                    target_hours = [h for h, p in combined]
                    target_price = max(p for h, p in combined)
                else:
                    res["multi_cycle"] = "Неблагоприятно (Нет условий для дозарядки)"
                    max_today_p = max(p for h, p in peaks_today)
                    max_tom_p = max(p for h, p in peaks_tom)
                    if max_today_p >= max_tom_p:
                        target_hours = [h for h, p in peaks_today]
                        target_price = max_today_p
                    else:
                        target_hours = [h for h, p in peaks_tom]
                        target_price = max_tom_p
            elif peaks_today:
                target_hours = [h for h, p in peaks_today]
                target_price = max(p for h, p in peaks_today)
            elif peaks_tom:
                target_hours = [h for h, p in peaks_tom]
                target_price = max(p for h, p in peaks_tom)
            
            res["target_price"] = target_price

        # Filter out past hours ONLY from the final execution command, so we don't return past periods
        target_hours = [h for h in target_hours if h >= cur_hour]

        # Survival Logic (Bridge the gap if battery risks hitting min_soc before next charge)
        # Note: This deliberately ignores the Buy Price Limit, safely prioritizing survival over price rules!
        if mode == "buy" and batt_cap > 0 and self.get_setting(CONF_DYNAMIC_SOC_BUY, True) and active_window:
            min_soc = self.get_setting(CONF_MIN_SOC_BUY, 10.0)
            
            today_type = "weekend" if now.weekday() >= 5 else "weekday"
            tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
            prof_today = self.get_average_profile("consumption_total", self.custom_period, today_type)
            prof_tom = self.get_average_profile("consumption_total", self.custom_period, tom_type)
            
            prof_gen = self.get_average_profile("generation", self.custom_period, "all")
            
            # --- Forecast Solar Adjustments ---
            forecast_today_val = self.get_forecast_value(self.forecast_today_sensor)
            forecast_tom_val = self.get_forecast_value(self.forecast_tomorrow_sensor)
            
            coeff_today = self.get_gen_forecast_coefficient(forecast_today_val, prof_gen, cur_hour + 1, 24)
            coeff_tom = self.get_gen_forecast_coefficient(forecast_tom_val, prof_gen, 0, 24)
            # ----------------------------------
            
            natural_hours = set(target_hours)
            survival_hours = set(target_hours)
            
            while True:
                added_bridge = False
                simulated_soc = batt_soc
                min_sim_soc_in_run = 100.0
                cur_hour_end_soc = None
                
                max_batt_power = self.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                
                for h in range(cur_hour, active_window[1] + 1):
                    if h in survival_hours:
                        # Apply CC/CV charge limit
                        accepted_power_kw = max_batt_power * self.get_cc_cv_ratio(simulated_soc)
                        
                        soc_gained = (accepted_power_kw / batt_cap) * 100.0 if batt_cap > 0 else 0.0
                        simulated_soc = min(100.0, simulated_soc + soc_gained)
                    else:
                        h_mod = h % 24
                        h_str = str(h_mod)
                        if h < 24:
                            cons_kwh = float(prof_today.get(h_str, 0.0))
                            gen_kwh = float(prof_gen.get(h_str, 0.0)) * coeff_today
                        else:
                            cons_kwh = float(prof_tom.get(h_str, 0.0))
                            gen_kwh = float(prof_gen.get(h_str, 0.0)) * coeff_tom
                            
                        net_kwh = max(0.0, cons_kwh - gen_kwh)
                        soc_drop = (net_kwh / batt_cap) * 100.0 if batt_cap > 0 else 0.0
                        simulated_soc -= soc_drop
                    
                    if h == cur_hour:
                        cur_hour_end_soc = simulated_soc
                        
                    min_sim_soc_in_run = min(min_sim_soc_in_run, simulated_soc)
                    
                    if simulated_soc < min_soc:
                        # Find cheapest legal hour between now and `h`
                        search_space = [sh for sh in range(cur_hour, h + 1) if sh not in survival_hours and sh in all_prices]
                        if search_space:
                            cheapest_bridge = min(search_space, key=lambda sh: all_prices[sh])
                            survival_hours.add(cheapest_bridge)
                            added_bridge = True
                            break
                        else:
                            break
                            
                if not added_bridge:
                    if cur_hour in survival_hours and cur_hour not in natural_hours:
                        res["charge_reason"] = "survival"
                        excess = min_sim_soc_in_run - min_soc
                        if excess > 0 and cur_hour_end_soc is not None:
                            exact_target = max(batt_soc, cur_hour_end_soc - excess)
                            res["charge_target_soc"] = round(exact_target, 1)
                        else:
                            res["charge_target_soc"] = round(cur_hour_end_soc, 1) if cur_hour_end_soc else 100.0
                    else:
                        res["charge_reason"] = "price"
                        res["charge_target_soc"] = 100.0
                    break
                    
            target_hours = list(survival_hours)

        # Final attribute population after all logic (including survival bridge)
        res["limit_used"] = limit
        
        future_active = [h for h in target_hours if h >= cur_hour]
        if future_active:
            upcoming_h = future_active[0]
            if upcoming_h < 24:
                rel_hours = [h for h in future_active if h < 24]
            else:
                rel_hours = [h for h in future_active if h >= 24]
            
            p_list = [all_prices.get(h, 0.0) for h in rel_hours]
            if p_list:
                if mode == "buy":
                    res["target_price"] = min(p_list)
                else:
                    res["target_price"] = max(p_list)

        if not target_hours:
            res["state"] = "price_limit_not_met"
            return res
            
        target_hours_sorted = sorted(target_hours)
        found_periods = []
        
        def _format_period(s, e):
            s_d = "Завтра " if s >= 24 else ""
            e_d = "Завтра " if e >= 24 else ""
            return f"{s_d}{s % 24:02d}:00 - {e_d}{e % 24:02d}:59"
            
        if target_hours_sorted:
            start = target_hours_sorted[0]
            prev = target_hours_sorted[0]
            for h in target_hours_sorted[1:]:
                if h == prev + 1:
                    prev = h
                else:
                    found_periods.append(_format_period(start, prev))
                    start = h
                    prev = h
            found_periods.append(_format_period(start, prev))
            
        def _format_hour_simple(h):
            d = "Завтра " if h >= 24 else "Сегодня "
            return f"{d}{h % 24:02d}:00"
            
        res["active_hours"] = [h for h in target_hours_sorted]
        res["active_hours_formatted"] = ", ".join([_format_hour_simple(h) for h in target_hours_sorted])
        res["active_periods"] = ", ".join(found_periods)
            
        # SOC Target & Power Calculation
        hours_count = len(target_hours)
        power_needed = 0.0
        
        if batt_cap > 0:
            if mode == "buy":
                base_target = self.get_setting(CONF_TARGET_SOC_BUY, 100.0)
                if carte_blanche:
                    target_soc = 100.0 # Force max charge when you get paid to do it
                elif self.get_setting(CONF_DYNAMIC_SOC_BUY, True):
                    # Smart AI calculation
                    budget_data = self.get_budget_and_permissions(self.custom_period)
                    expected_night = budget_data.get("expected_consumption", 0.0)
                    forecast = budget_data.get("forecast_val", 0.0)
                    
                    tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
                    total_avg = sum(self.get_average_profile("consumption_total", self.custom_period, tom_type).values())
                    day_need = max(0.0, total_avg - expected_night)
                    tomorrow_need = max(0.0, day_need - forecast)
                    total_need = expected_night + tomorrow_need
                    
                    ai_soc = (total_need / batt_cap) * 100.0
                    target_soc = min(base_target, ai_soc) # User setting acts as max ceiling
                else:
                    target_soc = base_target
                    
                target_soc = min(100.0, target_soc)
                
                charge_plan = {}
                sim_soc_plan = batt_soc
                nh_set = natural_hours if 'natural_hours' in locals() else set(target_hours_sorted)
                prof_today = self.get_average_profile("consumption_total", self.custom_period, "all")
                min_soc = self.get_setting(CONF_MIN_SOC_BUY, 10.0)
                
                final_active_hours = []
                
                for h in target_hours_sorted:
                    if h < cur_hour:
                        continue
                        
                    is_surv = h not in nh_set
                    
                    if is_surv:
                        gap_cons = 0.0
                        for f_h in range(h, h + 24):
                            if f_h > h and f_h in target_hours_sorted:
                                break
                            
                            c_kwh = float(prof_today.get(str(f_h % 24), 1.0)) if f_h < 24 else float(prof_today.get(str(f_h % 24), 1.0)) # Simplified, using prof_today for both days
                            g_kwh = float(prof_gen.get(str(f_h % 24), 0.0)) if 'prof_gen' in locals() else 0.0
                            cf = coeff_today if 'coeff_today' in locals() and f_h < 24 else (coeff_tom if 'coeff_tom' in locals() else 1.0)
                            
                            gap_cons += max(0.0, c_kwh - (g_kwh * cf))
                            
                        s_need = gap_cons + (batt_cap * 0.05)
                        
                        # Suppress Survival Bridge if currently producing generous solar surplus
                        cancel_surv = False
                        if h == cur_hour and getattr(self, "power_load_sensors", []) and getattr(self, "power_gen_sensors", []):
                            load_kw = sum((_get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_load_sensors)
                            gen_kw = sum((_get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_gen_sensors)
                            surplus_kw = gen_kw - load_kw
                            if surplus_kw > 0.3:
                                cancel_surv = True
                        
                        s_targ = min(target_soc, min_soc + ((s_need / batt_cap) * 100.0))
                        
                        if s_targ > sim_soc_plan and not cancel_surv:
                            e_req = batt_cap * ((s_targ - sim_soc_plan) / 100.0)
                            p = min(max_power, e_req)
                        else:
                            p = 0.0
                            
                        if p > 0.0:
                            charge_plan[_format_hour_simple(h)] = {"Режим": "Мост (Выживание)", "Мощность": round(p, 2)}
                            final_active_hours.append(h)
                        
                        if h == cur_hour:
                            power_needed = p
                            res["charge_reason"] = "survival_bridge" if p > 0 else "idle"
                            
                        sim_soc_plan = min(100.0, sim_soc_plan + (p / batt_cap * 100.0))
                    else:
                        rem_n = [x for x in nh_set if x >= h]
                        n_count = len(rem_n) if rem_n else 1
                        
                        if target_soc > sim_soc_plan:
                            e_req = batt_cap * ((target_soc - sim_soc_plan) / 100.0)
                            p = min(max_power, e_req / n_count)
                        else:
                            p = 0.0
                            
                        if p > 0.0:
                            charge_plan[_format_hour_simple(h)] = {"Режим": "Штатный (Дешевая цена)", "Мощность": round(p, 2)}
                            final_active_hours.append(h)
                        
                        if h == cur_hour:
                            power_needed = p
                            res["charge_reason"] = "price" if p > 0 else "idle"
                            
                        sim_soc_plan = min(100.0, sim_soc_plan + (p / batt_cap * 100.0))
                        
                    # Project SOC drop for current hour and subsequent idle hours
                    if h < target_hours_sorted[-1]:
                        next_h = [x for x in target_hours_sorted if x > h][0]
                        drop_kwh = 0.0
                        for drop_h in range(h, next_h):
                            c_kwh = float(prof_today.get(str(drop_h % 24), 1.0))
                            g_kwh = float(prof_gen.get(str(drop_h % 24), 0.0)) if 'prof_gen' in locals() else 0.0
                            cf = coeff_today if 'coeff_today' in locals() and drop_h < 24 else (coeff_tom if 'coeff_tom' in locals() else 1.0)
                            drop_kwh += max(0.0, c_kwh - (g_kwh * cf))
                            
                        sim_soc_plan = max(0.0, sim_soc_plan - (drop_kwh / batt_cap * 100.0))
                            
                res["charge_plan"] = charge_plan
                if cur_hour not in target_hours_sorted or power_needed <= 0:
                    res["charge_reason"] = "idle"
                    
                target_hours = final_active_hours
                
                # Recompile strings purely based on non-zero hours to keep UI clean
                found_periods = []
                if final_active_hours:
                    start = final_active_hours[0]
                    prev = final_active_hours[0]
                    for curr_h in final_active_hours[1:]:
                        if curr_h == prev + 1:
                            prev = curr_h
                        else:
                            found_periods.append(_format_period(start, prev))
                            start = curr_h
                            prev = curr_h
                    found_periods.append(_format_period(start, prev))
                    
                res["active_hours"] = final_active_hours
                res["active_hours_formatted"] = ", ".join([_format_hour_simple(x) for x in final_active_hours])
                res["active_periods"] = ", ".join(found_periods)
            else: # mode == "sell"
                base_target = self.get_setting(CONF_TARGET_SOC_SELL, 20.0)
                if self.get_setting(CONF_DYNAMIC_SOC_SELL, True):
                    # Smart AI calculation
                    budget_data = self.get_budget_and_permissions(self.custom_period, skip_strategy_check=True)
                    expected_night = budget_data.get("expected_consumption", 0.0)
                    eff_coeff = budget_data.get("efficiency_coefficient", 1.0)
                    min_soc_reserve = self.get_setting(CONF_MIN_SOC_BUY, 10.0)

                    # Correction 1: account for inverter losses.
                    # expected_night is AC-side consumption. To deliver that from the battery
                    # we need more raw DC energy: expected_night / eff_coeff.
                    # Only apply when eff_coeff < 1 (i.e. a losses sensor is configured).
                    expected_night_from_batt = expected_night / eff_coeff if eff_coeff > 0 else expected_night

                    # Correction 2: add min_soc as a hard non-reducible reserve ON TOP of the
                    # consumption reserve (they are independent: one is energy needed, the
                    # other is the absolute floor the battery must never go below).
                    ai_soc_reserve = (expected_night_from_batt / batt_cap) * 100.0 + min_soc_reserve

                    # User's CONF_TARGET_SOC_SELL acts as absolute minimum floor only
                    target_soc = max(base_target, ai_soc_reserve)
                else:
                    target_soc = base_target

                target_soc = min(100.0, target_soc)

                # Store debug info for sell mode
                if self.get_setting(CONF_DYNAMIC_SOC_SELL, True) and batt_cap > 0:
                    res["sell_target_soc_debug"] = {
                        "expected_consumption_kwh": round(expected_night, 3),
                        "efficiency_coefficient": round(eff_coeff, 3),
                        "expected_from_battery_kwh": round(expected_night_from_batt, 3),
                        "min_soc_reserve_pct": round(min_soc_reserve, 1),
                        "ai_soc_reserve_pct": round(ai_soc_reserve, 1),
                        "base_target_soc_pct": round(base_target, 1),
                        "final_target_soc_pct": round(target_soc, 1),
                        "current_soc_pct": round(batt_soc, 1),
                        "battery_capacity_kwh": round(batt_cap, 2),
                    }

                if batt_soc > target_soc:
                    energy_available = batt_cap * ((batt_soc - target_soc) / 100.0)
                    power_needed = energy_available / hours_count if hours_count > 0 else 0.0

        if max_power > 0 and power_needed > max_power:
            power_needed = max_power

        res["recommended_power_kw"] = round(power_needed, 3)

        if round(power_needed, 3) <= 0.0:
            res["state"] = "idle"
        else:
            res["state"] = "active" if cur_hour in target_hours else "idle"

        return res

class ProfileAveragedSensor(SensorEntity):
    """Sensor exposing Total Average as state and 24-hours array in attributes."""
    def __init__(self, manager, ptype, period_key, name, days):
        self.manager = manager
        self.ptype = ptype
        self.period_key = period_key
        self.days = days
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_{ptype}_{period_key}"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_icon = "mdi:chart-bell-curve-cumulative"
        
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

class BatteryDepletionTimeSensor(SensorEntity):
    """Predicts when the battery will hit the min_soc limit."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_battery_depletion"
        self._attr_icon = "mdi:battery-clock"
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
        
        min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
        
        batt_soc, batt_cap, _ = self.manager.get_battery_state(soc_default=100.0)
                
        if batt_cap <= 0.0:
            self._attr_extra_state_attributes = {}
            return "Нет батареи"
            
        if batt_soc <= min_soc:
            self._attr_extra_state_attributes = {}
            return "Уже разряжена"
            
        self._attr_extra_state_attributes = {
            "initial_soc": batt_soc,
            "battery_capacity": batt_cap,
            "min_soc_target": min_soc
        }
            
        simulated_soc = batt_soc
        
        # Inverter efficiency: discharging (DC->AC) loses energy too
        eff_coeff = self.manager.get_efficiency_coefficient()
        
        # 1. Look up forecast today remaining
        forecast_today_val = self.manager.get_forecast_value(self.manager.forecast_today_sensor)
            
        # 2. Look up forecast tomorrow
        forecast_tom_val = self.manager.get_forecast_value(self.manager.forecast_tomorrow_sensor)
            
        # Pre-calculate historical sums
        today_type = "weekend" if now.weekday() >= 5 else "weekday"
        tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
        
        prof_gen_today = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
        prof_gen_tom = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
        
        hist_today_rem = sum(float(prof_gen_today.get(str(h), 0.0)) for h in range(now.hour + 1, 24))
        hist_tom_total = sum(float(prof_gen_tom.get(str(h), 0.0)) for h in range(0, 24))
        
        # Simulate over next 48 hours to find the hour it dips below min_soc
        for hour_offset in range(1, 49):
            sim_time = now + timedelta(hours=hour_offset)
            h_str = str(sim_time.hour)
            
            day_type = "weekend" if sim_time.weekday() >= 5 else "weekday"
            
            prof_cons = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type)
            expected_cons = float(prof_cons.get(h_str, 0.0))
            
            prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
            hist_hour_gen = float(prof_gen.get(h_str, 0.0))
            
            expected_gen = hist_hour_gen
            if sim_time.date() == now.date() and forecast_today_val is not None:
                if hist_today_rem > 0:
                    expected_gen = (hist_hour_gen / hist_today_rem) * forecast_today_val
                else:
                    expected_gen = 0.0
            elif sim_time.date() == (now + timedelta(days=1)).date() and forecast_tom_val is not None:
                if hist_tom_total > 0:
                    expected_gen = (hist_hour_gen / hist_tom_total) * forecast_tom_val
                else:
                    expected_gen = 0.0
            
            net_solar_kw = max(0.0, expected_gen - expected_cons)
            net_cons_kw = max(0.0, expected_cons - expected_gen)
            
            # If we are discharging — need more battery energy to deliver net_cons to AC loads
            if net_cons_kw > 0.0:
                soc_delta = ((net_cons_kw / eff_coeff) / batt_cap) * 100.0
                simulated_soc -= soc_delta
            # If we are charging — not all solar makes it into battery due to AC->DC losses
            elif net_solar_kw > 0.0:
                max_batt_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                accepted_power_kw = max_batt_power * EnergyProfileManager.get_cc_cv_ratio(simulated_soc)
                    
                actual_charge_kw = min(net_solar_kw * eff_coeff, accepted_power_kw)
                soc_gained = (actual_charge_kw / batt_cap) * 100.0
                simulated_soc = min(100.0, simulated_soc + soc_gained)

            if simulated_soc <= min_soc:
                self._attr_extra_state_attributes["hours_to_depletion"] = hour_offset
                if sim_time.date() == now.date():
                    return f"Сегодня в {sim_time.hour:02d}:00"
                elif sim_time.date() == (now + timedelta(days=1)).date():
                    return f"Завтра в {sim_time.hour:02d}:00"
                else:
                    return f"Послезавтра в {sim_time.hour:02d}:00"
                    
        self._attr_extra_state_attributes["final_simulated_soc_48h"] = round(simulated_soc, 1)
        return "> 48 часов"

    @property
    def extra_state_attributes(self):
        if not hasattr(self, "_attr_extra_state_attributes"):
            return {}
        return self._attr_extra_state_attributes


class BatteryEndOfDaySOCSensor(SensorEntity):
    """Predicts battery SOC at the end of today's charging cycle (sunset)."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
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
                
        if batt_cap <= 0.0:
            self._attr_extra_state_attributes = {"error": "Нет емкости батареи"}
            return None
            
        self._attr_extra_state_attributes = {
            "initial_soc": batt_soc,
            "battery_capacity": batt_cap
        }
            
        simulated_soc = batt_soc
        
        # Inverter efficiency: AC<->DC conversion losses in both directions
        eff_coeff = self.manager.get_efficiency_coefficient()
        
        # 1. Look up forecast today remaining
        forecast_today_val = self.manager.get_forecast_value(self.manager.forecast_today_sensor)
            
        # Get generation profile to find the last hour of generation today
        prof_gen_today = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
        
        # Determine the sunset hour (last hour with significant expected generation > 0.05 kWh)
        sunset_hour = 23
        for h in range(23, -1, -1):
            if float(prof_gen_today.get(str(h), 0.0)) > 0.05:
                sunset_hour = h
                break
                
        if now.hour >= sunset_hour:
            self._attr_extra_state_attributes["status"] = "Генерация завершена"
            self._attr_extra_state_attributes["sunset_hour"] = sunset_hour
            return round(simulated_soc, 1)

        hist_today_rem = sum(float(prof_gen_today.get(str(h), 0.0)) for h in range(now.hour + 1, 24))
        day_type = "weekend" if now.weekday() >= 5 else "weekday"
        prof_cons = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type)
        
        charge_log = {}

        # Simulate until the sunset hour
        for h_step in range(now.hour + 1, sunset_hour + 1):
            h_str = str(h_step)
            
            expected_cons = float(prof_cons.get(h_str, 0.0))
            hist_hour_gen = float(prof_gen_today.get(h_str, 0.0))
            
            expected_gen = hist_hour_gen
            if forecast_today_val is not None:
                if hist_today_rem > 0:
                    expected_gen = (hist_hour_gen / hist_today_rem) * forecast_today_val
                else:
                    expected_gen = 0.0
            
            # --- Dynamic: Add Active Loads to Simulation ---
            active_load_add_kw = 0.0
            for s_id, settings in self.manager.get_setting(CONF_DEDUCT_SETTINGS, {}).items():
                # Check if device is active NOW
                is_pulling = self.manager._is_currently_pulling_power(s_id)
                
                # If active, we simulate its real power impact
                if is_pulling:
                    # Decide which power to use for simulation
                    is_cyclic = settings.get(CONF_IS_CYCLIC, False)
                    if is_cyclic and s_id in self.manager.learned_avg_cycle_power:
                        p_kw = self.manager.learned_avg_cycle_power[s_id] / 1000.0
                    else:
                        p_kw = self.manager.learned_real_power.get(s_id, settings.get("required_kw", 0.0) * 1000.0) / 1000.0
                    
                    # For devices with daily norm (required_kwh > 0)
                    req_kwh = settings.get("required_kwh", 0.0)
                    if req_kwh > 0:
                        consumed = self.manager.daily_deduct_consumption.get(s_id, 0.0)
                        remaining_kwh = max(0.0, req_kwh - consumed)
                        
                        # How many hours from 'now' this device will continue to run?
                        if p_kw > 0:
                            hours_to_finish = remaining_kwh / p_kw
                            # If we are within the simulated window for this device
                            if (h_step - now.hour) <= hours_to_finish:
                                active_load_add_kw += p_kw
                    else:
                        # For devices without norm (e.g. Boiler), add power only to current/immediate simulation steps
                        # We don't know when it stops, so we are conservative
                        if h_step == (now.hour + 1):
                            active_load_add_kw += p_kw
            
            expected_cons += active_load_add_kw
            # -----------------------------------------------

            net_solar_kw = max(0.0, expected_gen - expected_cons)
            net_cons_kw = max(0.0, expected_cons - expected_gen)
            
            # If we are discharging — need more battery energy to deliver net_cons to AC loads
            if net_cons_kw > 0.0:
                soc_delta = ((net_cons_kw / eff_coeff) / batt_cap) * 100.0
                simulated_soc -= soc_delta
            # If we are charging — AC->DC conversion reduces what enters the battery
            elif net_solar_kw > 0.0:
                max_batt_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                accepted_power_kw = max_batt_power * EnergyProfileManager.get_cc_cv_ratio(simulated_soc)
                    
                actual_charge_kw = min(net_solar_kw * eff_coeff, accepted_power_kw)
                soc_gained = (actual_charge_kw / batt_cap) * 100.0
                simulated_soc = min(100.0, simulated_soc + soc_gained)
                
            # Prevent going below 0 in simulation
            simulated_soc = max(0.0, simulated_soc)
            
            charge_log[f"{h_step:02d}:00"] = round(simulated_soc, 1)

        self._attr_extra_state_attributes["sunset_hour"] = f"{sunset_hour:02d}:00"
        self._attr_extra_state_attributes["expected_remaining_gen"] = round(forecast_today_val if forecast_today_val is not None else hist_today_rem, 2)
        self._attr_extra_state_attributes["hourly_simulation"] = charge_log

        return round(simulated_soc, 1)

    @property
    def extra_state_attributes(self):
        if not hasattr(self, "_attr_extra_state_attributes"):
            return {}
        return self._attr_extra_state_attributes


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
        
        # Prices
        cur_price = None
        prices_store = self.manager.data.get("prices_sell", {})
        if today_str in prices_store and cur_hour in prices_store[today_str]:
            try:
                cur_price = float(str(prices_store[today_str][cur_hour]).replace(',', '.'))
            except ValueError:
                cur_price = None
            
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
                day_type = "weekend" if now.weekday() >= 5 else "weekday"
                prof_cons = self.manager.get_average_profile("consumption_total", self.manager.custom_period, day_type)
                prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
                
                # --- Forecast Adjustments ---
                forecast_today_val = self.manager.get_forecast_value(self.manager.forecast_today_sensor)
                coeff_today = self.manager.get_gen_forecast_coefficient(forecast_today_val, prof_gen, int(cur_hour) + 1, 24)
                # ----------------------------
                
                tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
                prof_cons_tom = self.manager.get_average_profile("consumption_total", self.manager.custom_period, tom_type)
                
                # --- Forecast Tomorrow Adjustments ---
                forecast_tom_val = self.manager.get_forecast_value(self.manager.forecast_tomorrow_sensor)
                coeff_tom = self.manager.get_gen_forecast_coefficient(forecast_tom_val, prof_gen, 0, 24)
                
                max_batt_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                
                sim_soc = batt_soc
                hours_available = (peak_start_hour - int(cur_hour)) if peak_start_hour is not None else (48 - int(cur_hour))
                charge_log = []
                
                # Simulating CC/CV Charging behavior hour by hour
                for h_offset in range(hours_available):
                    h_step = int(cur_hour) + h_offset
                    if h_step >= 48:
                        break
                        
                    h_mod = h_step % 24
                    
                    if h_step < 24:
                        expected_cons = float(prof_cons.get(str(h_mod), 0.0))
                        expected_gen = float(prof_gen.get(str(h_mod), 0.0)) * coeff_today
                    else:
                        expected_cons = float(prof_cons_tom.get(str(h_mod), 0.0))
                        expected_gen = float(prof_gen.get(str(h_mod), 0.0)) * coeff_tom
                        
                    # --- Dynamic: Add Active Loads to BMS Peak Simulation ---
                    active_load_kw = 0.0
                    for s_id, s_settings in self.manager.get_setting(CONF_DEDUCT_SETTINGS, {}).items():
                        if self.manager._is_currently_pulling_power(s_id):
                            is_cyclic = s_settings.get(CONF_IS_CYCLIC, False)
                            if is_cyclic and s_id in self.manager.learned_avg_cycle_power:
                                p_kw = self.manager.learned_avg_cycle_power[s_id] / 1000.0
                            else:
                                p_kw = self.manager.learned_real_power.get(s_id, s_settings.get("required_kw", 0.0) * 1000.0) / 1000.0
                            
                            req_kwh = s_settings.get("required_kwh", 0.0)
                            if req_kwh > 0:
                                remaining = max(0.0, req_kwh - self.manager.daily_deduct_consumption.get(s_id, 0.0))
                                if p_kw > 0 and (h_offset + 1) <= (remaining / p_kw):
                                    active_load_kw += p_kw
                            elif h_offset == 0:
                                # For boiler-type loads, only count in current hour of simulation
                                active_load_kw += p_kw
                    
                    expected_cons += active_load_kw
                    # --------------------------------------------------------

                    net_gen_kw = max(0.0, expected_gen - expected_cons)
                    
                    # Compute max accepted charge power based on simulated SOC (CC/CV phases)
                    accepted_power_kw = max_batt_power * EnergyProfileManager.get_cc_cv_ratio(sim_soc)
                        
                    # How much energy can we physically push in this hour?
                    actual_charge_kw = min(net_gen_kw, accepted_power_kw)
                    
                    soc_gained = (actual_charge_kw / batt_cap) * 100.0 if batt_cap > 0 else 0
                    
                    old_soc = sim_soc
                    sim_soc = min(target_soc, sim_soc + soc_gained)
                    
                    charge_log.append({
                        "hour": h_step,
                        "net_solar_kw": round(net_gen_kw, 2),
                        "bms_limit_kw": round(accepted_power_kw, 2),
                        "actual_charge_kw": round(actual_charge_kw, 2),
                        "soc_start": round(old_soc, 1),
                        "soc_end": round(sim_soc, 1)
                    })
                    
                    if sim_soc >= target_soc:
                        break # Reached target early!
                        
                # If after the simulation timeline we failed to reach the target (allowing 1% tolerance)
                late_start_recommended = False
                if sim_soc < (target_soc - 1.0) and peak_start_hour is not None:
                    is_preparing_for_peak = True
                elif sim_soc >= (target_soc - 1.0) and peak_start_hour is not None:
                    # We have enough time! Calculate how much extra time we have.
                    total_hours_needed = len(charge_log)
                    hours_surplus = hours_available - total_hours_needed
                    # If we have surplus hours AND it's not the cheapest time to charge, we can delay
                    # For a simple heuristic: delay gathering charge until `total_hours_needed` before the peak
                    if hours_surplus > 0:
                        latest_start_hour = peak_start_hour - total_hours_needed
                        if int(cur_hour) < latest_start_hour:
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
                    "log": charge_log
                }
                    
        formatted_peak = "Нет пика сегодня"
        if peak_start_hour is not None:
            if peak_start_hour >= 24:
                formatted_peak = f"Завтра {peak_start_hour - 24:02d}:00"
            else:
                formatted_peak = f"Сегодня {peak_start_hour:02d}:00"
                
        self._attr_extra_state_attributes = {
            "is_preparing_for_peak": is_preparing_for_peak,
            "next_peak_start_hour": formatted_peak,
            "bms_forecast": bms_debug
        }
                    
        # State Machine Logic
        mode = "sale_pv" # Default
        reason = "Значения по умолчанию (нет особых условий рынка или заряда)"
        
        if batt_soc <= min_soc:
            mode = "bat_emergency"
            reason = f"Заряд батареи ({round(batt_soc, 1)}%) <= Критического минимума ({min_soc}%)"
        elif cur_price is not None and cur_price < price_stop_sell:
            mode = "stop_sale"
            reason = f"Текущая цена ({cur_price}) < Порога блокировки продажи ({price_stop_sell})"
        elif is_buying_active:
            mode = "buy"
            reason = f"Активна стратегия ПОКУПКИ (Смотри сенсор Market BUY Strategy)"
        elif is_selling_active:
            mode = "sale_pv_bat"
            reason = f"Активна стратегия ПРОДАЖИ (Смотри сенсор Market SELL Strategy)"
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
                        load_kw = sum((_get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_load_sensors)
                        gen_kw = sum((_get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
                        if gen_kw <= load_kw + 0.1:
                            instant_ok = False
                    elif getattr(self.manager, "power_gen_sensors", []):
                        gen_kw = sum((_get_kwh_val(self.manager.hass.states.get(s)) or 0.0) for s in self.manager.power_gen_sensors)
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
            
        self._attr_extra_state_attributes["mode_reason"] = reason

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
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            try:
                val = float(str(last_state.state).replace(',', '.'))
                # Recover into manager if it hasn't accumulated anything since restart
                if self.ptype == "consumption" and self.manager.current_consumption_base == 0:
                    self.manager.current_consumption_base = val
                    self.manager.current_consumption_total = val # We can only guess total from base if restored like this
                if self.ptype == "generation" and self.manager.current_generation == 0:
                    self.manager.current_generation = val
            except ValueError:
                pass
                
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
        res = self.manager.get_budget_and_permissions(self.days_for_profile)
        self._state = res["initial_budget"]
        self._attrs = {
            "permissions": res.get("permissions", {}),
            "permissions_reasons": res.get("permissions_reasons", {}),
            "forecast_remaining_kwh": round(res["forecast_val"], 3),
            "forecast_raw_kwh": round(res.get("forecast_raw", 0.0), 3),
            "forecast_correction_coefficient": round(res.get("forecast_coefficient", 1.0), 3),
            "forecast_hist_coefficient": round(res.get("forecast_hist_coefficient", 1.0), 3),
            "forecast_today_coefficient": round(res.get("forecast_today_coefficient", 1.0), 3),
            "battery_energy_kwh": round(res["batt_energy_val"], 3),
            "expected_consumption_until_0800_kwh": round(res["expected_consumption"], 3),
            "debug_actual_today": round(res.get("debug_actual_today", 0), 3),
            "debug_expected_today_total": round(res.get("debug_expected_today_total", 0), 3),
            "debug_expected_today_so_far": round(res.get("debug_expected_today_so_far", 0), 3),
            "debug_fraction_so_far": round(res.get("debug_fraction_so_far", 0), 3),
            "occupancy_coefficient": round(res.get("occupancy_coefficient", 1.0), 3),
            "occupancy_persons_home": self.manager.get_current_occupancy() if self.manager.presence_sensors else "N/A",
            "efficiency_coefficient": round(res.get("efficiency_coefficient", 1.0), 3)
        }

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
            try: return round(float(str(val).replace(',', '.')), 3)
            except ValueError: return 0.0
            
        today_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res["today_prices"].items(), key=lambda item: int(item[0])) if int(k) >= cur_hour}
        tom_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res["tomorrow_prices"].items(), key=lambda item: int(item[0]))}
        
        return {
            "analyzed_window": res.get("analyzed_window", "Неизвестно"),
            "double_cycle_opportunity": res.get("multi_cycle", "Не предвидится"),
            "active_hours": res.get("active_hours_formatted", ""),
            "active_periods": res.get("active_periods", ""),
            "target_price": round(res["target_price"], 3),
            "limit_used": round(res["limit_used"], 3),
            "recommended_power_kw": res["recommended_power_kw"],
            "current_mode": res.get("charge_reason", "Ожидание"),
            "charge_plan": res.get("charge_plan", {}),
            "prices_today": today_fmt,
            "prices_tomorrow": tom_fmt
        }

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
                    savings_30d += v.get("total", 0.0)
                    days_found += 1
            except Exception:
                continue
                
        avg_daily = savings_30d / days_found if days_found > 0 else 0.0
        
        days_rem = int(remaining / avg_daily) if avg_daily > 0 else 9999
        payback_date = (dt_util.now() + timedelta(days=days_rem)).strftime("%Y-%m-%d") if avg_daily > 0 else "never"

        return {
            "total_investment": f"{total_cost} {self._currency}",
            "cumulative_savings": f"{round(total_saved, 2)} {self._currency}",
            "remaining_amount": f"{round(remaining, 2)} {self._currency}",
            "average_daily_saving": f"{round(avg_daily, 2)} {self._currency}",
            "estimated_payback_days": days_rem if total_cost > 0 else "N/A",
            "estimated_payback_date": payback_date if total_cost > 0 else "N/A"
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
        # We show the ARBITRAGE threshold cost (2x degradation because 1 cycle = charge + discharge)
        return round(self.manager.get_battery_degradation_cost() * 2, 4)

    @property
    def extra_state_attributes(self):
        cost_per_kwh = self.manager.get_battery_degradation_cost()
        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)
        
        return {
            "wear_cost_per_kwh_throughput": round(cost_per_kwh, 4),
            "arbitrage_profit_threshold": round(cost_per_kwh * 2, 4),
            "battery_investment": batt_cost,
            "rated_cycles": cycles,
            "note": "arbitrage_note"
        }


