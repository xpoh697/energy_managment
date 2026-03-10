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
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_MIN_SOC_BUY,
    CONF_DEDUCT_SETTINGS,
    CONF_POWER_LOAD_SENSORS,
    CONF_POWER_GEN_SENSORS,
    CONF_SALE_PV_NO_BAT_MAX_HOUR
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
        self.deduct_settings = config_data.get(CONF_DEDUCT_SETTINGS, {})
        self.all_sensors = self.consumption_sensors | self.generation_sensors | self.deduct_sensors
        
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
        self.sensor_last_values = {}
        
        self.daily_deduct_consumption = {s: 0.0 for s in self.deduct_sensors}
        
        self.update_listeners = []
        self._unsub_state = None
        self._unsub_time = None
        self._unsub_power_poll = None
        
        # Track historical power samples for 5-10 minute average smoothing
        self.power_history = []

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
            
        if "prices_buy" not in self.data:
            self.data["prices_buy"] = {}
        if "prices_sell" not in self.data:
            self.data["prices_sell"] = {}

    async def async_save(self):
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

    async def async_start(self):
        for entity_id in self.all_sensors:
            state_obj = self.hass.states.get(entity_id)
            val = _get_kwh_val(state_obj)
            if val is not None:
                self.sensor_last_values[entity_id] = val
                
        # Parse prices immediately on load
        for p_sensor in self.all_price_sensors:
            state_obj = self.hass.states.get(p_sensor)
            if state_obj:
                self._update_prices_from_sensor(p_sensor, state_obj)
                    
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
            self._poll_instant_power(datetime.now())

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
        
        # Protective logic:
        # If this is the absolute FIRST time we see a value, we just record it 
        # as the baseline and exit. DO NOT ADD it to delta.
        if old_val is None:
            self.sensor_last_values[entity_id] = new_val
            return
            
        delta = new_val - old_val
        
        if delta < 0:
            # The sensor reset its internal counter (e.g. daily/monthly reset on the device).
            # Usually the new delta is just the new value. BUT if new_val is massive out of nowhere,
            # that means the old_val was somehow 0 or broken.
            delta = new_val
            
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
            
        if self.current_consumption_base < 0:
            self.current_consumption_base = 0.0
        if self.current_consumption_total < 0:
            self.current_consumption_total = 0.0
            
        self._notify_update()

    async def _async_reset_hour(self, now):
        # Time is precisely the top of the new hour, meaning we need to save the PAST hour.
        past_hour = (now.hour - 1) % 24
        
        today_wd = now.weekday()
        
        # Append to history lists
        self.data["consumption_base"][str(past_hour)].append({"v": self.current_consumption_base, "wd": today_wd})
        self.data["consumption_total"][str(past_hour)].append({"v": self.current_consumption_total, "wd": today_wd})
        self.data["generation"][str(past_hour)].append({"v": self.current_generation, "wd": today_wd})
        
        # Trim history arrays to ensure we don't leak memory and only keep required `max_days`
        for h in range(24):
            sh = str(h)
            if len(self.data["consumption_base"][sh]) > self.max_days:
                self.data["consumption_base"][sh] = self.data["consumption_base"][sh][-self.max_days:]
            if len(self.data["consumption_total"][sh]) > self.max_days:
                self.data["consumption_total"][sh] = self.data["consumption_total"][sh][-self.max_days:]
            if len(self.data["generation"][sh]) > self.max_days:
                self.data["generation"][sh] = self.data["generation"][sh][-self.max_days:]
                
        # Save to internal filesystem
        await self.async_save()
        
        # Reset counters
        self.current_consumption_base = 0.0
        self.current_consumption_total = 0.0
        self.current_generation = 0.0
        
        # Reset daily deduct consumption at midnight
        if now.hour == 0:
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
                now = datetime.now()
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

    def get_average_profile(self, profile_type, days, day_type="all"):
        """Returns a dict with 24 keys ("0" to "23") representing average values.
        day_type: "all", "weekday", "weekend".
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
                    else:
                        v = float(str(item).replace(',', '.'))
                        wd = None
                        
                    if wd is not None:
                        if day_type == "weekday" and wd >= 5: continue
                        if day_type == "weekend" and wd < 5: continue
                        
                    valid_vals.append(v)
                except ValueError:
                    pass
                
            if valid_vals:
                profile[str(h)] = round(sum(valid_vals) / len(valid_vals), 3)
            else:
                profile[str(h)] = 0.0
        return profile

    def get_todays_profile(self, profile_type):
        """Returns the actual hourly profile for the current day up to the current hour."""
        now = datetime.now()
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
        val = self.settings.get(key, default)
        if val is None:
            return default
        if isinstance(default, float):
            try:
                return float(str(val).replace(',', '.'))
            except ValueError:
                return default
        return val

    async def async_set_setting(self, key, value):
        self.settings[key] = value
        self.data["settings"] = self.settings
        await self.store.async_save(self.data)
        self._notify_update()

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        now = datetime.now()
        cur_hour = now.hour
        
        # 1. Get Forecast Remaining
        forecast_val = 0.0
        if self.forecast_today_sensor:
            for fsensor in self.forecast_today_sensor:
                forecast_state = self.hass.states.get(fsensor)
                val = _get_kwh_val(forecast_state)
                if val is not None:
                    forecast_val += val
            self.data["temp_max_forecast"] = max(self.data.get("temp_max_forecast", 0.0), forecast_val)
                
        # Calculate Reliability Coefficient
        coeff = 1.0
        history = self.data.get("forecast_history", [])
        if history:
            tot_actual = sum(h["actual"] for h in history)
            tot_expected = sum(h["forecast"] for h in history)
            if tot_expected > 0.1:
                coeff = tot_actual / tot_expected
                coeff = max(0.2, min(coeff, 2.0)) # Clamp coefficient manually between 0.2 and 2.0
                
        forecast_val_adjusted = forecast_val * coeff
                
        # 2. Get Battery Energy Available
        batt_energy_val = 0.0
        if self.battery_soc_sensor and self.battery_capacity_sensor:
            soc_state = self.hass.states.get(self.battery_soc_sensor)
            cap_state = self.hass.states.get(self.battery_capacity_sensor)
            if soc_state and soc_state.state not in ("unknown", "unavailable") and cap_state and cap_state.state not in ("unknown", "unavailable"):
                try:
                    soc = float(str(soc_state.state).replace(',', '.'))
                    cap = float(str(cap_state.state).replace(',', '.'))
                    batt_energy_val = cap * (soc / 100.0)
                except ValueError:
                    pass
                    
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
            
        prof_gen_today = self.get_average_profile("generation", days_for_profile, "all")
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
                
            req_kwh = settings.get("required_kwh", 2.5)
            req_kw = settings.get("required_kw", 0.0)
            consumed = self.daily_deduct_consumption.get(sensor_id, 0.0)
            
            power_bottleneck = False
            gen_bottleneck = False
            is_free_price = cur_price_buy is not None and cur_price_buy <= 0.0
            
            if req_kw > 0.0:
                if available_budget < req_kw:
                    power_bottleneck = True
                if only_solar_free and not is_free_price:
                    if available_gen_kw < (req_kw * 0.6):
                        gen_bottleneck = True
            
            if req_kwh == 0:
                if available_budget > 0 and not power_bottleneck and not gen_bottleneck:
                    permissions[sensor_id] = True
                    if only_solar_free and not is_free_price and req_kw > 0.0:
                        available_gen_kw -= (req_kw * 0.6)
                    permissions_reasons[sensor_id] = f"Разрешено: Динамическая (Профиц. {round(available_budget, 2)} кВт*ч, Ген. доступно {round(available_gen_kw, 2)} кВт)"
                else:
                    permissions[sensor_id] = False
                    if gen_bottleneck:
                        permissions_reasons[sensor_id] = f"Блокировка: Ост. генерация {round(available_gen_kw, 2)} кВт < 60% от Мощности {req_kw} кВт"
                    elif power_bottleneck:
                        permissions_reasons[sensor_id] = f"Блокировка: Профицит {round(available_budget, 2)} кВт*ч < Мощность {req_kw} кВт"
                    else:
                        permissions_reasons[sensor_id] = "Блокировка: Нет профицита энергии"
                continue

            needed = req_kwh - consumed
            
            if needed <= 0:
                permissions[sensor_id] = True
                permissions_reasons[sensor_id] = "Разрешено: Дневная норма выполнена (или перерасход)"
            elif available_budget >= needed and not power_bottleneck and not gen_bottleneck:
                permissions[sensor_id] = True
                available_budget -= needed
                if only_solar_free and not is_free_price and req_kw > 0.0:
                    available_gen_kw -= (req_kw * 0.6)
                permissions_reasons[sensor_id] = f"Разрешено: Зарезервировано {round(needed, 2)} кВт*ч из профицита"
            else:
                permissions[sensor_id] = False
                if gen_bottleneck:
                    permissions_reasons[sensor_id] = f"Блокировка: Ост. генерация {round(available_gen_kw, 2)} кВт < 60% от Мощности {req_kw} кВт"
                elif power_bottleneck:
                    permissions_reasons[sensor_id] = f"Блокировка: Профицит {round(available_budget, 2)} кВт*ч < Мощность {req_kw} кВт"
                else:
                    permissions_reasons[sensor_id] = f"Блокировка: Не хватает энергии (нужно {round(needed, 2)} кВт*ч, доступно {round(available_budget, 2)} кВт*ч)"
                
                
        return {
            "initial_budget": initial_budget,
            "permissions": permissions,
            "permissions_reasons": permissions_reasons,
            "forecast_val": forecast_val_adjusted,
            "forecast_raw": forecast_val,
            "forecast_coefficient": coeff,
            "batt_energy_val": batt_energy_val,
            "expected_consumption": expected_consumption
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
        
        now = datetime.now()
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
            
        def safe_float(entity_id, default=0.0):
            if not entity_id: return default
            st = self.hass.states.get(entity_id)
            if not st or st.state in ("unknown", "unavailable"): return default
            try: return float(str(st.state).replace(',', '.'))
            except ValueError: return default
            
        if mode == "buy":
            tolerance = self.get_setting(CONF_PRICE_TOLERANCE, 0.0)
        else:
            tolerance = self.get_setting(CONF_PRICE_SELL_TOLERANCE, 0.0)
            
        max_power = self.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
        
        batt_cap = safe_float(self.battery_capacity_sensor, 0.0)
        batt_soc = safe_float(self.battery_soc_sensor, 0.0)
        
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
            limit_used = limit
            if negative_hours:
                # Carte blanche: we buy whenever price is negative, ignore windows
                target_hours = negative_hours
                target_price = min([all_prices[h] for h in negative_hours])
                carte_blanche = True
            else:
                peaks_today = get_peaks(window_today, False, limit, tolerance)
                peaks_tom = get_peaks(window_tomorrow, False, limit, tolerance)
                combined = peaks_today + peaks_tom
                if combined:
                    target_hours = [h for h, p in combined]
                    target_price = min(p for h, p in combined)

        else: # sell
            limit = self.get_setting(CONF_PRICE_SELL_LIMIT, -99.0)
            limit_used = limit
            if negative_hours and cur_hour in negative_hours:
                # If price is negative today, we PAY to sell to the grid. NEVER SELL.
                res["state"] = "price_limit_not_met"
                return res
            
            peaks_today = get_peaks(window_today, True, limit, tolerance)
            peaks_tom = get_peaks(window_tomorrow, True, limit, tolerance)
            
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

        # Update target_price to reflect only the upcoming peak block (so we don't show tomorrow's price while today's peak is still expected)
        future_hours = [h for h in target_hours if h >= cur_hour]
        if future_hours:
            upcoming_h = future_hours[0]
            if upcoming_h < 24:
                rel_hours = [h for h in future_hours if h < 24]
            else:
                rel_hours = [h for h in future_hours if h >= 24]
            if mode == "buy":
                target_price = min(all_prices[h] for h in rel_hours)
            else:
                target_price = max(all_prices[h] for h in rel_hours)

        # Filter out past hours ONLY from the final execution command, so we don't return past periods
        target_hours = future_hours

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
            forecast_today_val = None
            forecast_tom_val = None
            
            if self.forecast_today_sensor:
                val_sum = 0.0
                for fsensor in self.forecast_today_sensor:
                    st = self.hass.states.get(fsensor)
                    v = _get_kwh_val(st)
                    if v is not None: val_sum += v
                if val_sum > 0: forecast_today_val = val_sum
                
            if self.forecast_tomorrow_sensor:
                val_sum = 0.0
                for fsensor in self.forecast_tomorrow_sensor:
                    st = self.hass.states.get(fsensor)
                    v = _get_kwh_val(st)
                    if v is not None: val_sum += v
                if val_sum > 0: forecast_tom_val = val_sum
                
            hist_today_rem = sum(float(prof_gen.get(str(h), 0.0)) for h in range(cur_hour + 1, 24))
            hist_tom_total = sum(float(prof_gen.get(str(h), 0.0)) for h in range(0, 24))
            
            coeff_today = (forecast_today_val / hist_today_rem) if forecast_today_val is not None and hist_today_rem > 0 else 1.0
            coeff_tom = (forecast_tom_val / hist_tom_total) if forecast_tom_val is not None and hist_tom_total > 0 else 1.0
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
                        # Apply true CC/CV charge limit instead of magic +20.0%
                        if simulated_soc < 80.0:
                            accepted_power_kw = max_batt_power
                        elif simulated_soc < 90.0:
                            accepted_power_kw = max_batt_power * 0.5
                        elif simulated_soc < 95.0:
                            accepted_power_kw = max_batt_power * 0.25
                        else:
                            accepted_power_kw = max_batt_power * 0.1
                        
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

        res["limit_used"] = limit_used
        res["target_price"] = target_price

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
                    # We absolutely must keep `expected_night` energy in battery!
                    ai_soc_reserve = (expected_night / batt_cap) * 100.0
                    target_soc = max(base_target, ai_soc_reserve) # Users setting acts as absolute minimum floor
                else:
                    target_soc = base_target
                    
                target_soc = min(100.0, target_soc)
                
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
        now = datetime.now()
        
        min_soc = self.manager.get_setting(CONF_MIN_SOC_BUY, 10.0)
        
        batt_soc = 100.0
        batt_cap = 0.0
        if self.manager.battery_soc_sensor:
            st = self.manager.hass.states.get(self.manager.battery_soc_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_soc = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
        if self.manager.battery_capacity_sensor:
            st = self.manager.hass.states.get(self.manager.battery_capacity_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_cap = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
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
        
        # 1. Look up forecast today remaining
        forecast_today_val = None
        if self.manager.forecast_today_sensor:
            val_sum = 0.0
            for fsensor in self.manager.forecast_today_sensor:
                st = self.manager.hass.states.get(fsensor)
                v = _get_kwh_val(st)
                if v is not None: val_sum += v
            if val_sum > 0: forecast_today_val = val_sum
            
        # 2. Look up forecast tomorrow
        forecast_tom_val = None
        if self.manager.forecast_tomorrow_sensor:
            val_sum = 0.0
            for fsensor in self.manager.forecast_tomorrow_sensor:
                st = self.manager.hass.states.get(fsensor)
                v = _get_kwh_val(st)
                if v is not None: val_sum += v
            if val_sum > 0: forecast_tom_val = val_sum
            
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
            
            # If we are discharging
            if net_cons_kw > 0.0:
                soc_delta = (net_cons_kw / batt_cap) * 100.0
                simulated_soc -= soc_delta
            # If we are charging, apply CC/CV boundaries
            elif net_solar_kw > 0.0:
                max_batt_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                if simulated_soc < 80.0:
                    accepted_power_kw = max_batt_power             # CC Phase: Full power
                elif simulated_soc < 90.0:
                    accepted_power_kw = max_batt_power * 0.5       # CV Phase 1
                elif simulated_soc < 95.0:
                    accepted_power_kw = max_batt_power * 0.25      # CV Phase 2
                else:
                    accepted_power_kw = max_batt_power * 0.1       # Final trickle
                    
                actual_charge_kw = min(net_solar_kw, accepted_power_kw)
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
        now = datetime.now()
        
        batt_soc = 100.0
        batt_cap = 0.0
        if self.manager.battery_soc_sensor:
            st = self.manager.hass.states.get(self.manager.battery_soc_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_soc = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
        if self.manager.battery_capacity_sensor:
            st = self.manager.hass.states.get(self.manager.battery_capacity_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_cap = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
        if batt_cap <= 0.0:
            self._attr_extra_state_attributes = {"error": "Нет емкости батареи"}
            return None
            
        self._attr_extra_state_attributes = {
            "initial_soc": batt_soc,
            "battery_capacity": batt_cap
        }
            
        simulated_soc = batt_soc
        
        # 1. Look up forecast today remaining
        forecast_today_val = None
        if self.manager.forecast_today_sensor:
            val_sum = 0.0
            for fsensor in self.manager.forecast_today_sensor:
                st = self.manager.hass.states.get(fsensor)
                v = _get_kwh_val(st)
                if v is not None: val_sum += v
            if val_sum > 0: forecast_today_val = val_sum
            
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
            
            net_solar_kw = max(0.0, expected_gen - expected_cons)
            net_cons_kw = max(0.0, expected_cons - expected_gen)
            
            # If we are discharging
            if net_cons_kw > 0.0:
                soc_delta = (net_cons_kw / batt_cap) * 100.0
                simulated_soc -= soc_delta
            # If we are charging, apply CC/CV boundaries
            elif net_solar_kw > 0.0:
                max_batt_power = self.manager.get_setting(CONF_BATTERY_MAX_POWER, 5.0)
                if simulated_soc < 80.0:
                    accepted_power_kw = max_batt_power
                elif simulated_soc < 90.0:
                    accepted_power_kw = max_batt_power * 0.5
                elif simulated_soc < 95.0:
                    accepted_power_kw = max_batt_power * 0.25
                else:
                    accepted_power_kw = max_batt_power * 0.1
                    
                actual_charge_kw = min(net_solar_kw, accepted_power_kw)
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
        
        now = datetime.now()
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
        batt_soc = 100.0
        batt_cap = 0.0
        if self.manager.battery_soc_sensor:
            st = self.manager.hass.states.get(self.manager.battery_soc_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_soc = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
        if self.manager.battery_capacity_sensor:
            st = self.manager.hass.states.get(self.manager.battery_capacity_sensor)
            if st and st.state not in ("unknown", "unavailable"):
                try: batt_cap = float(str(st.state).replace(',', '.'))
                except ValueError: pass
                
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
                forecast_today_val = None
                if self.manager.forecast_today_sensor:
                    val_sum = 0.0
                    for fsensor in self.manager.forecast_today_sensor:
                        st = self.manager.hass.states.get(fsensor)
                        v = _get_kwh_val(st)
                        if v is not None: val_sum += v
                    if val_sum > 0: forecast_today_val = val_sum
                
                hist_today_rem = sum(float(prof_gen.get(str(h), 0.0)) for h in range(int(cur_hour) + 1, 24))
                coeff_today = (forecast_today_val / hist_today_rem) if forecast_today_val is not None and hist_today_rem > 0 else 1.0
                # ----------------------------
                
                tom_type = "weekend" if (now + timedelta(days=1)).weekday() >= 5 else "weekday"
                prof_cons_tom = self.manager.get_average_profile("consumption_total", self.manager.custom_period, tom_type)
                
                # --- Forecast Tomorrow Adjustments ---
                forecast_tom_val = None
                if self.manager.forecast_tomorrow_sensor:
                    val_sum = 0.0
                    for fsensor in self.manager.forecast_tomorrow_sensor:
                        st = self.manager.hass.states.get(fsensor)
                        v = _get_kwh_val(st)
                        if v is not None: val_sum += v
                    if val_sum > 0: forecast_tom_val = val_sum
                    
                hist_tom_total = sum(float(prof_gen.get(str(h), 0.0)) for h in range(0, 24))
                coeff_tom = (forecast_tom_val / hist_tom_total) if forecast_tom_val is not None and hist_tom_total > 0 else 1.0
                
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
                        
                    net_gen_kw = max(0.0, expected_gen - expected_cons)
                    
                    # Compute max accepted charge power based on simulated SOC (CC/CV phases)
                    if sim_soc < 80.0:
                        accepted_power_kw = max_batt_power             # CC Phase: Full power
                    elif sim_soc < 90.0:
                        accepted_power_kw = max_batt_power * 0.5       # CV Phase 1
                    elif sim_soc < 95.0:
                        accepted_power_kw = max_batt_power * 0.25      # CV Phase 2
                    else:
                        accepted_power_kw = max_batt_power * 0.1       # Final trickle
                        
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
                    # Calculate average load/gen over the recent history (~10 mins)
                    avg_load_kw = sum(x["load_kw"] for x in self.manager.power_history) / len(self.manager.power_history)
                    avg_gen_kw = sum(x["gen_kw"] for x in self.manager.power_history) / len(self.manager.power_history)
                    
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

    @property
    def extra_state_attributes(self):
        if not hasattr(self, "_attr_extra_state_attributes"):
            return {}
        return self._attr_extra_state_attributes

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
            "battery_energy_kwh": round(res["batt_energy_val"], 3),
            "expected_consumption_until_0800_kwh": round(res["expected_consumption"], 3)
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
        
        now = datetime.now()
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
