import logging
import asyncio
import time
import json
import os
from typing import Any, cast, List, Tuple, Dict, Optional
import statistics
from datetime import datetime, timedelta
from homeassistant.components.sensor import SensorEntity, SensorStateClass, SensorDeviceClass
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_change, async_track_time_interval
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import callback, HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from .const import (
    DOMAIN,
    CONF_GRID_POWER,
    CONF_BATTERY_POWER,
    CONF_BATTERY_SOC,
    CONF_BATTERY_CAPACITY,
    CONF_CONSUMPTION_SENSORS,
    CONF_GENERATION_SENSORS,
    CONF_PRESENCE_SENSORS,
    CONF_DEDUCT_SENSORS,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GRID_EXPORT_SENSORS,
    CONF_DEDUCT_SETTINGS,
    CONF_POWER_LOAD_SENSORS,
    CONF_POWER_GEN_SENSORS,
    CONF_FORECAST_TODAY_REMAINING,
    CONF_FORECAST_TODAY_HOURLY,
    CONF_FORECAST_TOMORROW,
    CONF_CUSTOM_PERIOD,
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    CONF_MIN_SOC_BAT,
    CONF_AI_CHARGE_LIMIT,
    CONF_AI_DISCHARGE_LIMIT,
    CONF_BATTERY_MAX_POWER,
    CONF_ACTIVE_SENSOR,
    CONF_TOTAL_SYSTEM_COST,
    CONF_BATTERY_COST,
    CONF_INVERTER_LOSSES_SENSOR,
    CONF_PRICE_BUY_LIMIT,
    CONF_PRICE_SELL_LIMIT,
    CONF_ARBITRAGE_PROFIT_THRESHOLD,
    CONF_BATTERY_RATED_CYCLES,
    CONF_ANOMALY_THRESHOLD,
    CONF_POWER_SENSOR,
    CONF_IS_CYCLIC,
    CONF_ACTIVE_HOLD_TIME,
    CONF_ONLY_SOLAR,
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_SOC_BUFFER,
    VERSION
)
from .const import CONF_BATTERY_VOLTAGE
from .const import INVERTER_MODES
from .strategy import StrategyEngine
from .strategy_dp import DPPlanner
from .utils import get_kwh_val, normalize_float, get_price_from_store, round_f
from .dispatch_plan import DispatchPlan, GlobalSlot, EnergyLogicEngine

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
        entities.append(TodayProfileSensor(manager, "consumption", "Профиль потребления сегодня"))

        # Add the Smart Budget sensor using the custom period length as the profile baseline
        entities.append(EnergyBudgetSensor(manager, "Энергетический прогноз (Выживание)", custom_period))
        entities.append(PotentialExportTodaySensor(manager))

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

    if getattr(manager, 'price_buy_sensors', []):
        entities.append(UniversalPriceSensor(manager, "buy", "Buy Price 48h"))
    if getattr(manager, 'price_sell_sensors', []):
        entities.append(UniversalPriceSensor(manager, "sell", "Sell Price 48h"))

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

    # DP Advisor (Shadow Mode)
    entities.append(EnergyDPAdviceSensor(manager, "DP Advice"))

    if config_data.get(CONF_BATTERY_COST):
        entities.append(BatteryDegradationSensor(manager, "Стоимость износа батареи"))

    if has_consumption and has_generation:
        entities.append(InstantPowerAveragedSensor(manager, "load"))
        entities.append(InstantPowerAveragedSensor(manager, "gen"))
        entities.append(SolarWasteSensor(manager, "Упущенная солнечная энергия"))
        entities.append(BMSLearnedProfileSensor(manager))

    # Real-time Grid Interaction
    entities.append(GridBalanceSensor(manager, "Текущий баланс сети"))

    async_add_entities(entities)



class EnergyProfileManager:
    hass: HomeAssistant
    entry: ConfigEntry
    store: Store
    strategy_engine: 'StrategyEngine'
    
    consumption_sensors: set[str]
    generation_sensors: set[str]
    deduct_sensors: set[str]
    grid_import_sensors: set[str]
    grid_export_sensors: set[str]
    all_sensors: set[str]
    
    power_load_sensors: List[str]
    power_gen_sensors: List[str]
    forecast_today_sensor: List[str]
    forecast_tomorrow_sensor: List[str]
    
    battery_soc_sensor: Optional[str]
    battery_capacity_sensor: Optional[str]
    battery_power_sensor: Optional[str]
    grid_power_sensor: Optional[str]
    
    presence_sensors: List[str]
    all_power_sensors: set[str]
    all_active_sensors: set[str]
    all_price_sensors: set[str]
    
    deduct_settings: Dict[str, Any]
    settings: Dict[str, Any]
    sensor_last_values: Dict[str, float]
    daily_deduct_consumption: Dict[str, float]
    update_listeners: List[Any]
    
    learned_standby_power: Dict[str, float]
    learned_real_power: Dict[str, float]
    learned_avg_cycle_power: Dict[str, float]
    learned_cycle_total_kwh: Dict[str, float]
    learned_avg_cycle_duration: Dict[str, float]
    cycle_start_time: Dict[str, datetime]
    cycle_actual_start_time: Dict[str, datetime]
    cycle_energy_start: Dict[str, float]
    last_known_power: Dict[str, float]
    _sensors_need_baseline: set[str]
    
    inverter_losses_sensor: Optional[str]
    current_losses: float
    current_consumption_base: float
    current_consumption_total: float
    current_generation: float
    current_grid_import: float
    current_grid_export: float
    current_hourly_deduct: float
    bms_learned_profile: Dict[int, float]
    current_inverter_mode: str
    
    _unsub_state: Any
    _unsub_time: Any
    _unsub_power_poll: Any
    _unsub_periodic_save: Any
    def get_sunrise_hour(self):
        """Find the first hour of solar generation from the profile (4:00 - 12:00)."""
        prof = self.get_average_profile("generation", 14, "all")
        for h in range(4, 12):
            if float(prof.get(str(h), 0.0)) > 0.05:
                return h
        return 6 # Default fallback

    def get_sunset_hour(self):
        """Find the last hour of solar generation from the profile (14:00 - 22:00)."""
        prof = self.get_average_profile("generation", 14, "all")
        for h in range(22, 14, -1):
            if float(prof.get(str(h), 0.0)) > 0.05:
                return h + 1
        return 19 # Default fallback

    @property
    def now(self) -> datetime:
        """Centralized time source."""
        return dt_util.now()

    def translate_dp_mode(self, dp_mode: str) -> str:
        """Переводит внутренние коды режима DP в сущности управления HA с учетом маппинга пользователя."""
        if not isinstance(dp_mode, str):
            dp_mode = str(dp_mode) if dp_mode is not None else "IDLE"
            
        if dp_mode in ["buy", "sale_pv", "sale_pv_bat", "sale_pv_no_bat", "stop_sale", "no_pv_sale_no_bat", "bat_emergency"]:
            return dp_mode

        if dp_mode == "GRID_CHG":
            val = self.get_setting("dp_map_grid_chg", self.get_setting("dp_map_charge", "buy"))
        elif dp_mode == "PAID_IMP":
            val = self.get_setting("dp_map_paid_imp", self.get_setting("dp_map_charge", "buy"))
        elif dp_mode == "DIS":
            val = self.get_setting("dp_map_dis", self.get_setting("dp_map_discharge", "sale_pv_bat"))
        elif dp_mode == "PV_CHG":
            val = self.get_setting("dp_map_pv_chg", self.get_setting("dp_map_solar", "sale_pv"))
        elif dp_mode == "SOL":
            val = self.get_setting("dp_map_sol", self.get_setting("dp_map_solar", "sale_pv"))
        elif dp_mode == "SELF_CON":
            val = self.get_setting("dp_map_self_con", self.get_setting("dp_map_self_consume", "sale_pv"))
        elif dp_mode == "GRID":
            val = self.get_setting("dp_map_grid_mode", self.get_setting("dp_map_grid", "sale_pv"))
        elif dp_mode == "IDLE":
            val = self.get_setting("dp_map_idle", self.get_setting("dp_map_grid", "sale_pv"))
        else:
            val = "sale_pv"
            
        # v12.1.3: Absolute safety fallback if val somehow evaluates to 0.0 or "0.0"
        if val in [0.0, 0, "0.0", "0", "None", None, ""]:
            if dp_mode in ["GRID_CHG", "PAID_IMP"]: val = "buy"
            elif dp_mode == "DIS": val = "sale_pv_bat"
            elif dp_mode in ["PV_CHG", "SOL"]: val = "sale_pv"
            elif dp_mode == "SELF_CON": val = "stop_sale"
            elif dp_mode in ["GRID", "IDLE"]: val = "no_pv_sale_no_bat"
            else: val = "sale_pv"
            
        return str(val).strip()

    @property
    def is_weekend(self) -> bool:
        """Determines if today is a weekend day (Sat/Sun) or holiday."""
        return self.day_type >= 5

    @property
    def avg_load_kw(self) -> float:
        """Retrieve smoothed load power (last 10m)."""
        if not self.power_history:
            if self.power_load_sensors:
                return float(sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_load_sensors))
            return 0.0
        return sum(s.get("load_kw", 0.0) for s in self.power_history) / len(self.power_history)

    @property
    def avg_gen_kw(self) -> float:
        """Retrieve smoothed generation power (last 10m)."""
        if not self.power_history:
            if self.power_gen_sensors:
                return float(sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_gen_sensors))
            return 0.0
        return sum(s.get("gen_kw", 0.0) for s in self.power_history) / len(self.power_history)

    @property
    def avg_base_load_kw(self) -> float:
        """Retrieve smoothed BASE load power (Total - Managed)."""
        tot = self.avg_load_kw
        man_kw = 0.0
        for s_id, settings in self.deduct_settings.items():
            if not isinstance(settings, dict): continue
            power_sensor = settings.get("power_sensor")
            if power_sensor:
                st = self.hass.states.get(power_sensor)
                man_kw += (get_kwh_val(st) or 0.0)
        return max(0.0, tot - man_kw)

    @property
    def avg_load_5m_kw(self) -> float:
        """Retrieve smoothed load power (last 5m)."""
        if not self.power_history:
            return self.avg_load_kw
        cutoff = self.now - timedelta(minutes=5)
        samples = [s.get("load_kw", 0.0) for s in self.power_history if s["time"] >= cutoff]
        if not samples: return self.avg_load_kw
        return sum(samples) / len(samples)

    @property
    def avg_gen_5m_kw(self) -> float:
        """Retrieve smoothed generation power (last 5m)."""
        if not self.power_history:
            return self.avg_gen_kw
        cutoff = self.now - timedelta(minutes=5)
        samples = [s.get("gen_kw", 0.0) for s in self.power_history if s["time"] >= cutoff]
        if not samples: return self.avg_gen_kw
        return sum(samples) / len(samples)
    
    data: Dict[str, Any]
    max_days: int
    custom_period: int
    
    price_buy_sensors: List[str]
    price_sell_sensors: List[str]
    
    last_blended_coeff: float
    current_solar_waste_power: float
    power_history: List[Dict[str, Any]]
    
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry

        config_data = {**entry.data, **entry.options}

        # Initialize internal storage handler for preserving profiles across restarts
        self.store = Store(hass, STORAGE_VERSION, f"energy_management_{entry.entry_id}")

        self.strategy_engine = StrategyEngine(self)

        self.consumption_sensors = set(cast(list, config_data.get(CONF_CONSUMPTION_SENSORS, [])))
        self.generation_sensors = set(cast(list, config_data.get(CONF_GENERATION_SENSORS, [])))
        self.deduct_sensors = set(cast(list, config_data.get(CONF_DEDUCT_SENSORS, [])))
        self.grid_import_sensors = set(cast(list, config_data.get(CONF_GRID_IMPORT_SENSORS, [])))
        self.grid_export_sensors = set(cast(list, config_data.get(CONF_GRID_EXPORT_SENSORS, [])))
        raw_deduct = cast(dict, config_data.get(CONF_DEDUCT_SETTINGS, {}))
        self.deduct_settings = raw_deduct if isinstance(raw_deduct, dict) else {}
        self.all_sensors = self.consumption_sensors | self.generation_sensors | self.deduct_sensors | self.grid_import_sensors | self.grid_export_sensors

        raw_load = config_data.get(CONF_POWER_LOAD_SENSORS, [])
        self.power_load_sensors = [str(raw_load)] if isinstance(raw_load, str) else cast(List[str], raw_load or [])
        raw_gen = config_data.get(CONF_POWER_GEN_SENSORS, [])
        self.power_gen_sensors = [str(raw_gen)] if isinstance(raw_gen, str) else cast(List[str], raw_gen or [])

        today_forecasts = config_data.get(CONF_FORECAST_TODAY_REMAINING) or config_data.get("forecast_today") or []
        self.forecast_today_sensor = [str(today_forecasts).strip()] if isinstance(today_forecasts, str) else [str(s).strip() for s in (today_forecasts or []) if s]

        # Use local import as safety fallback for mysterious NameError in some HA environments
        from .const import CONF_FORECAST_TODAY_HOURLY
        
        today_hourly = config_data.get(CONF_FORECAST_TODAY_HOURLY) or config_data.get("forecast_today_hourly") or []
        self.forecast_today_hourly_sensor = [str(today_hourly).strip()] if isinstance(today_hourly, str) else [str(s).strip() for s in (today_hourly or []) if s]

        tomorrow_forecasts = config_data.get(CONF_FORECAST_TOMORROW) or config_data.get("forecast_tomorrow") or []
        self.forecast_tomorrow_sensor = [str(tomorrow_forecasts).strip()] if isinstance(tomorrow_forecasts, str) else [str(s).strip() for s in (tomorrow_forecasts or []) if s]
        
        raw_soc = config_data.get(CONF_BATTERY_SOC)
        if isinstance(raw_soc, list): raw_soc = raw_soc[0] if raw_soc else None
        self.battery_soc_sensor = str(raw_soc).strip() if raw_soc else None
        
        raw_cap = config_data.get(CONF_BATTERY_CAPACITY)
        if isinstance(raw_cap, list): raw_cap = raw_cap[0] if raw_cap else None
        self.battery_capacity_sensor = str(raw_cap).strip() if raw_cap else None
        
        raw_bat_p = config_data.get(CONF_BATTERY_POWER)
        if isinstance(raw_bat_p, list): raw_bat_p = raw_bat_p[0] if raw_bat_p else None
        self.battery_power_sensor = str(raw_bat_p).strip() if raw_bat_p else None
        
        raw_grid_p = config_data.get(CONF_GRID_POWER)
        if isinstance(raw_grid_p, list): raw_grid_p = raw_grid_p[0] if raw_grid_p else None
        self.grid_power_sensor = str(raw_grid_p).strip() if raw_grid_p else None

        raw_p_buy = config_data.get(CONF_PRICE_BUY)
        if isinstance(raw_p_buy, list): raw_p_buy = raw_p_buy[0] if raw_p_buy else None
        self.price_buy_sensor = str(raw_p_buy).strip() if raw_p_buy else None
        
        raw_p_sell = config_data.get(CONF_PRICE_SELL)
        if isinstance(raw_p_sell, list): raw_p_sell = raw_p_sell[0] if raw_p_sell else None
        self.price_sell_sensor = str(raw_p_sell).strip() if raw_p_sell else None

        # v11.1.35: Using hardcoded string if constant import fails in end-user environment
        raw_bat_v = config_data.get("battery_voltage")
        if isinstance(raw_bat_v, list): raw_bat_v = raw_bat_v[0] if raw_bat_v else None
        self.battery_voltage_sensor = str(raw_bat_v) if raw_bat_v else None

        # Presence / occupancy sensors (person.* or binary_sensor.*)
        presence_raw = config_data.get(CONF_PRESENCE_SENSORS, [])
        if isinstance(presence_raw, str):
            presence_list = [presence_raw]
        else:
            presence_list = cast(List[Any], presence_raw or [])
        self.presence_sensors = [str(s).strip() for s in presence_list if s]

        self.all_power_sensors = set()
        if isinstance(self.power_load_sensors, list):
            for s in self.power_load_sensors:
                if s: self.all_power_sensors.add(str(s))
        if isinstance(self.power_gen_sensors, list):
            for s in self.power_gen_sensors:
                if s: self.all_power_sensors.add(str(s))
        if self.battery_power_sensor is not None:
            self.all_power_sensors.add(str(self.battery_power_sensor))
        if self.grid_power_sensor is not None:
            self.all_power_sensors.add(str(self.grid_power_sensor))
        
        if self.battery_voltage_sensor:
            self.all_sensors = self.all_sensors | {str(self.battery_voltage_sensor)}
        
        # Adaptive BMS Model: "SOC" -> Max Charge Power (kW)
        self.bms_learned_profile = {}
        self.current_inverter_mode = "sale_pv"
        self.config_error = None
        # v11.9.331: Mode overrides map {abs_hour -> mode_name} for simulation engine
        self.planned_mode_overrides = {}
        self.manual_mode_overrides = {}
        self.hourly_manual_overrides = {}
        self._last_override_hour = -1
        
        # v11.9.453: Manual mode anchoring for stable power commands
        self._manual_anchor_hour = -1
        self._manual_anchor_target_soc = -1.0
        self._manual_anchor_power = 0.0
        self._manual_anchor_amps = 0.0
        self._last_logged_mode = None

        self.all_active_sensors = set()
        raw_deduct_2 = config_data.get(CONF_DEDUCT_SETTINGS, {})
        if isinstance(raw_deduct_2, dict):
            for s_id, s_conf in raw_deduct_2.items():
                if not isinstance(s_conf, dict): continue
                if s_conf.get(CONF_POWER_SENSOR):
                    p_s = str(s_conf[CONF_POWER_SENSOR]).strip()
                    s_conf[CONF_POWER_SENSOR] = p_s
                    self.all_power_sensors.add(p_s)
                if s_conf.get(CONF_ACTIVE_SENSOR):
                    a_s = str(s_conf[CONF_ACTIVE_SENSOR]).strip()
                    s_conf[CONF_ACTIVE_SENSOR] = a_s
                    self.all_active_sensors.add(a_s)
                self.deduct_settings[str(s_id).strip()] = s_conf

        self.consumption_sensors = {str(s).strip() for s in self.consumption_sensors if s}
        self.generation_sensors = {str(s).strip() for s in self.generation_sensors if s}
        self.deduct_sensors = {str(s).strip() for s in self.deduct_sensors if s}
        self.grid_import_sensors = {str(s).strip() for s in self.grid_import_sensors if s}
        self.grid_export_sensors = {str(s).strip() for s in self.grid_export_sensors if s}
        
        if self.battery_soc_sensor:
            self.battery_soc_sensor = str(self.battery_soc_sensor).strip()
        if self.battery_capacity_sensor:
            self.battery_capacity_sensor = str(self.battery_capacity_sensor).strip()
        if self.battery_power_sensor:
            self.battery_power_sensor = str(self.battery_power_sensor).strip()
        if self.grid_power_sensor:
            self.grid_power_sensor = str(self.grid_power_sensor).strip()
        if self.battery_voltage_sensor:
            self.battery_voltage_sensor = str(self.battery_voltage_sensor).strip()

        buy_p = config_data.get(CONF_PRICE_BUY)
        sell_p = config_data.get(CONF_PRICE_SELL)
        if isinstance(buy_p, list): self.price_buy_sensors = [str(s) for s in buy_p if s]
        else: self.price_buy_sensors = [str(buy_p)] if buy_p and isinstance(buy_p, (str, int, float)) else []
        
        if isinstance(sell_p, list): self.price_sell_sensors = [str(s) for s in sell_p if s]
        else: self.price_sell_sensors = [str(sell_p)] if sell_p and isinstance(sell_p, (str, int, float)) else []

        self.all_price_sensors = set([s for s in (self.price_buy_sensors + self.price_sell_sensors) if s])

        self.max_days = 365
        raw_period = config_data.get(CONF_CUSTOM_PERIOD, 14)
        try:
            self.custom_period = int(float(str(raw_period)))
        except (ValueError, TypeError):
            self.custom_period = 14

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
        self.inverter_losses_sensor = str(losses_raw) if losses_raw and isinstance(losses_raw, (str, int, float)) else None
        self.current_losses = 0.0  # kWh accumulated this hour
        if self.inverter_losses_sensor:
            self.all_sensors = self.all_sensors | {str(self.inverter_losses_sensor)}
        if self.battery_power_sensor:
            self.all_sensors = self.all_sensors | {str(self.battery_power_sensor)}

        # Track historical power samples for 5-10 minute average smoothing
        self.power_history = []

        # Power sensor runtime tracking
        self.learned_standby_power = {}
        self.learned_real_power = {}
        self.learned_avg_cycle_power = {}
        self.learned_cycle_total_kwh = {}
        self.learned_avg_cycle_duration = {}  # In seconds
        self.cycle_start_time = {}
        self.cycle_actual_start_time = {}
        self.cycle_energy_start = {}
        self.last_known_power = {}
        # Sensors that need to re-establish a baseline on first read after restart
        # (prevents large accumulated deltas from being counted as generation/consumption)
        self._sensors_need_baseline = set()

        self.current_solar_waste_power = 0.0
        self.last_blended_coeff = 1.0
        self._profile_cache = {}
        
        self.fixed_strategy_data = {
            "buy": {"id": -1, "power": 0.0, "target_soc": 0.0},
            "sell": {"id": -1, "power": 0.0, "target_soc": 0.0}
        }
        
        # v12.0.0: Global Dispatch Plan
        self.global_plan = None
        self.heuristic_hourly_data = {}
        self.dp_hourly_data = {}

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
        
        # Migrate legacy settings for AI SOC limits and arbitrage
        if "target_soc_buy" in self.settings:
            self.settings[CONF_AI_CHARGE_LIMIT] = self.settings.pop("target_soc_buy")
        if "target_soc_sell" in self.settings:
            self.settings[CONF_AI_DISCHARGE_LIMIT] = self.settings.pop("target_soc_sell")
        if "arbitrage_min_profit" in self.settings:
            self.settings[CONF_ARBITRAGE_PROFIT_THRESHOLD] = self.settings.pop("arbitrage_min_profit")
            
        self.learned_standby_power = self.data.get("learned_standby_power", {})
        self.learned_real_power = self.data.get("learned_real_power", {})
        self.learned_avg_cycle_power = self.data.get("learned_avg_cycle_power", {})
        self.learned_cycle_total_kwh = self.data.get("learned_cycle_total_kwh", {})
        self.learned_avg_cycle_duration = self.data.get("learned_avg_cycle_duration", {})
        self.hourly_manual_overrides = self.data.get("hourly_manual_overrides", {})
        
        # Restore BMS learned profile safely
        bms_raw = self.data.get("bms_learned_profile", {})
        self.bms_learned_profile = {}
        if isinstance(bms_raw, dict):
            for k, v in bms_raw.items():
                try:
                    k_int = int(float(str(k)))
                    self.bms_learned_profile[k_int] = float(v)
                except (ValueError, TypeError):
                    continue
            
            # One-time cleanup for monotonicity (ensures the profile is physically sound)
            if self.bms_learned_profile:
                socs = sorted(self.bms_learned_profile.keys())
                for i in range(len(socs) - 2, -1, -1):
                    s_low = socs[i]
                    s_high = socs[i+1]
                    if self.bms_learned_profile[s_low] < self.bms_learned_profile[s_high]:
                        self.bms_learned_profile[s_low] = self.bms_learned_profile[s_high]
        
        # Restore cycle start times (handle ISO strings or missing)
        saved_starts = self.data.get("cycle_actual_start_time", {})
        for s_id, start_str in saved_starts.items():
            try:
                self.cycle_actual_start_time[s_id] = dt_util.parse_datetime(start_str)
            except:
                pass

        saved_last_active = self.data.get("cycle_start_time", {})
        for s_id, start_str in saved_last_active.items():
            try:
                self.cycle_start_time[s_id] = dt_util.parse_datetime(start_str)
            except:
                pass

        saved_energy_start = self.data.get("cycle_energy_start", {})
        for s_id, val in saved_energy_start.items():
            try:
                self.cycle_energy_start[s_id] = float(val)
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
            val = saved_deduct.get(s, 0.0)
            # v11.1.3 - Clean poisoned data (>20kWh per day for a single appliance is likely a bug)
            if val > 20.0:
                _LOGGER.warning("Energy Management: Purging poisoned daily deduct value (%s) for %s", val, s)
                val = 0.0
            self.daily_deduct_consumption[s] = val

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
        self.data["cycle_start_time"] = {
            s_id: dt.isoformat() for s_id, dt in self.cycle_start_time.items()
        }
        self.data["cycle_energy_start"] = {
            s_id: val for s_id, val in self.cycle_energy_start.items()
        }
        self.data["bms_learned_profile"] = self.bms_learned_profile
        self.data["sensor_last_values"] = self.sensor_last_values
        self.data["hourly_manual_overrides"] = self.hourly_manual_overrides
        self.data["daily_deduct_consumption"] = dict(self.daily_deduct_consumption)
        self.data["hourly_accumulators"] = {
            "consumption_total": self.current_consumption_total,
            "generation": self.current_generation,
            "grid_import": self.current_grid_import,
            "grid_export": self.current_grid_export,
            "losses": self.current_losses,
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

    def async_set_manual_override(self, mode: str):
        """Set manual override for the current hour (legacy buttons)."""
        now_h = self.now.hour
        if mode == "ai_mode":
            self.manual_mode_overrides = {}
            _LOGGER.info("[Manual Override] Cleared all overrides. AI mode restored.")
        else:
            self.manual_mode_overrides[now_h] = mode
            self._last_override_hour = now_h
            _LOGGER.warning("[Manual Override] Forced mode: %s for hour %s:00", mode, now_h)
        self._notify_update(force_strategy_recalc=True)
        self.hass.async_create_task(self.async_update_global_plan())

    async def async_set_hourly_override(self, timestamp: str, mode: str, soc_limit: float):
        """Set a manual override for a specific hour (modal window)."""
        if not self.hourly_manual_overrides:
            self.hourly_manual_overrides = {}
            
        # v11.9.503: Normalize timestamp to ensure frontend-backend sync (YYYY-MM-DD HH:00)
        try:
            # If it's already a clean string, parse and re-format to be safe
            if " " in timestamp:
                dt_p = dt_util.parse_datetime(timestamp.replace(" ", "T"))
                if dt_p:
                    timestamp = dt_p.strftime("%Y-%m-%d %H:00")
        except Exception as e:
            _LOGGER.error(f"[Manual Override] Timestamp normalization failed for '{timestamp}': {e}")

        if mode == "ai":
            if timestamp in self.hourly_manual_overrides:
                del self.hourly_manual_overrides[timestamp]
                _LOGGER.info(f"[Manual Override] Cleared for {timestamp}")
        else:
            self.hourly_manual_overrides[timestamp] = {
                "mode": mode,
                "soc_limit": soc_limit,
                "created_at": datetime.now().isoformat()
            }
            _LOGGER.warning(f"[Manual Override] Set for {timestamp}: {mode} (SOC {soc_limit}%)")
        
        # Persistent storage
        self.data["hourly_manual_overrides"] = self.hourly_manual_overrides
        await self.store.async_save(self.data)
        self._notify_update(force_strategy_recalc=True)
        self.hass.async_create_task(self.async_update_global_plan())

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
        self.log_to_file("DIAG: async_start called")
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

        monitored_sensors = self.all_sensors | self.all_price_sensors | self.all_power_sensors | self.all_active_sensors
        if isinstance(self.battery_soc_sensor, str): monitored_sensors.add(self.battery_soc_sensor)
        if isinstance(self.battery_capacity_sensor, str): monitored_sensors.add(self.battery_capacity_sensor)
        if isinstance(self.forecast_today_sensor, list): monitored_sensors.update([str(s) for s in self.forecast_today_sensor if s])
        if isinstance(self.forecast_tomorrow_sensor, list): monitored_sensors.update([str(s) for s in self.forecast_tomorrow_sensor if s])

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
        
        # v12.0.0: Global Plan refresh (Dedicated background task)
        self.entry.async_create_background_task(self.hass, self._run_global_plan_loop(), "energy_management_global_plan_loop")
        
    async def _run_global_plan_loop(self):
        """Reliable background loop for Global Plan updates."""
        self.log_to_file("DIAG: Global Plan loop started")
        
        # Initial delay to let HA states populate without blocking other setups
        await asyncio.sleep(30)
        
        while True:
            try:
                await self.async_update_global_plan()
            except Exception as e:
                import traceback
                self.log_to_file(f"DIAG: Global Plan loop iteration failed: {e}\n{traceback.format_exc()}")
            
            await asyncio.sleep(60)

    async def _calculate_heuristic_plan(
        self, now, batt_soc, buy_strat, sell_strat, scale_today, scale_tomorrow,
        prof_gen, prof_cons, prof_cons_base, shared_profiles, sim_range, eff
    ) -> DispatchPlan:
        """Потокобезопасный асинхронный расчет эвристического плана с отдачей event loop."""
        slots = []
        charge_cmds = {}
        sell_cmds = {}
        sim_soc = batt_soc
        b_cap = float(self.get_setting("battery_capacity", 10.0) or 10.0)
        max_batt_p = float(self.get_setting("battery_max_power", 3.0) or 3.0)

        for h_abs in range(48):
            await asyncio.sleep(0.005)  # Yield to event loop to keep UI smooth
            dt_h = (now + timedelta(hours=h_abs)).replace(minute=0, second=0, microsecond=0)
            h_rel = str(dt_h.hour)
            today_str = dt_h.strftime("%Y-%m-%d")
            
            raw_gen = float(normalize_float(prof_gen.get(h_rel, 0.0)))
            scaled_gen = raw_gen * scale_today if dt_h.date() == now.date() else (raw_gen * scale_tomorrow if dt_h.date() == (now.date() + timedelta(days=1)) else raw_gen)

            slot = GlobalSlot(
                hour_abs=h_abs,
                dt_iso=dt_h.isoformat(),
                price_buy=self.get_price("buy", today_str, dt_h.hour) or 0.0,
                price_sell=self.get_price("sell", today_str, dt_h.hour) or 0.0,
                gen_raw=scaled_gen,
                load_total=float(normalize_float(prof_cons.get(h_rel, 0.0))),
                load_base=float(normalize_float(prof_cons_base.get(h_rel, 0.0)))
            )
            
            mode, reason, is_buy, is_sell, t_soc = EnergyLogicEngine.get_mode_at(
                dt_now=dt_h,
                batt_soc=sim_soc,
                manager=self,
                is_forecast=(h_abs > 0),
                abs_hour=(now.hour + h_abs),
                profiles=shared_profiles,
                buy_strategy=buy_strat,
                sell_strategy=sell_strat
            )
            
            slot.mode = mode
            slot.reason = reason
            slot.target_soc = t_soc
            
            ts_key = dt_h.strftime("%Y-%m-%d %H:00")
            is_manual_hour = ts_key in self.hourly_manual_overrides
            is_legacy_manual = (dt_h.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and dt_h.hour in self.manual_mode_overrides)
            if is_manual_hour or is_legacy_manual:
                slot.is_manual = True
            
            if h_abs == 0:
                search_prefix = now.strftime("%Y-%m-%d %H")
                h_override = None
                for k, v in self.hourly_manual_overrides.items():
                    if k.startswith(search_prefix):
                        h_override = v
                        break
                p_real, t_soc, c_amps = EnergyLogicEngine.calculate_realtime_power(
                    mode=mode,
                    now=now,
                    batt_soc=batt_soc,
                    manager=self,
                    buy_strategy=buy_strat,
                    sell_strategy=sell_strat,
                    h_override=h_override
                )
                slot.power_ac = p_real
                slot.target_soc = t_soc
                slot.charge_amps = c_amps
                if h_override: slot.is_manual = True
                p_actual = p_real if mode == "buy" else -p_real if mode == "sale_pv_bat" else 0.0
            else:
                p_est = 0.0
                t_soc_est = sim_soc
                man_override = self.hourly_manual_overrides.get(ts_key)
                if man_override:
                    t_soc_est = float(man_override.get("soc_limit", sim_soc))
                    _m_mode = man_override.get("mode")
                    if _m_mode == "buy" and sim_soc < (t_soc_est - 0.05):
                        p_calc = (max(0.0, t_soc_est - sim_soc) / 100.0 * b_cap) / eff
                        p_est = min(max_batt_p, round(p_calc, 2))
                    elif _m_mode == "sale_pv_bat" and sim_soc > (t_soc_est + 0.2):
                        req_p = (max(0.0, sim_soc - t_soc_est) / 100.0 * b_cap) * eff
                        p_est = -min(max_batt_p, round(req_p, 2))
                else:
                    if mode == "buy":
                        p_est = buy_strat.get("raw_commands", {}).get(now.hour + h_abs, 0.0)
                        t_soc_est = buy_strat.get("planned_power_per_h", {}).get(f"{(now.hour + h_abs)%24:02d}:00", {}).get("soc", sim_soc)
                    elif mode == "sale_pv_bat":
                        p_est = -sell_strat.get("raw_commands", {}).get(now.hour + h_abs, 0.0)
                        t_soc_est = sell_strat.get("planned_power_per_h", {}).get(f"{(now.hour + h_abs)%24:02d}:00", {}).get("soc", sim_soc)
                
                slot.power_ac = abs(p_est)
                slot.target_soc = t_soc_est
                v_nom = self.get_sensor_float(self.battery_voltage_sensor) or 52.0
                slot.charge_amps = round((slot.power_ac * 1000.0) / max(10.0, v_nom), 1)
                p_actual = p_est

            if abs(p_actual) < 0.001:
                base_load = float(slot.load_base) if (slot.load_base and slot.load_base > 0.01) else float(slot.load_total)
                net_flow = slot.gen_raw - base_load
                _h_mode_cls = INVERTER_MODES.get(mode)
                if _h_mode_cls:
                    if net_flow > 0 and _h_mode_cls.charge_from_pv:
                        p_actual = min(net_flow, max_batt_p)
                    elif net_flow < 0 and _h_mode_cls.discharge_to_house:
                        p_actual = max(net_flow, -max_batt_p)

            delta_kwh = p_actual * (eff if p_actual > 0 else (1.0/eff))
            sim_soc = max(0.0, min(100.0, sim_soc + (delta_kwh / b_cap * 100.0)))
            slot.soc_end = sim_soc
            
            if mode == "buy": charge_cmds[now.hour + h_abs] = slot.power_ac
            elif mode == "sale_pv_bat": sell_cmds[now.hour + h_abs] = slot.power_ac
            slots.append(slot)

        # High-Fidelity Simulator Pass
        all_cmds = {}
        for h, p in charge_cmds.items(): all_cmds[h] = p
        for h, p in sell_cmds.items(): all_cmds[h] = -p
        m_overrides = { (now.hour + i): s.mode for i, s in enumerate(slots) }
        
        _, sim_log, _ = self.strategy_engine.run_soc_simulation(
            start_soc=batt_soc,
            sim_range=sim_range,
            now=now,
            commands=all_cmds,
            mode_overrides=m_overrides,
            dynamic_floors=sell_strat.get("floors_sliding", {})
        )
        
        for i, slot in enumerate(slots):
            h_abs_sim = now.hour + i
            sim_data = sim_log.get(h_abs_sim, {})
            if sim_data:
                slot.soc_start = sim_data.get("soc_start", 0.0)
                slot.soc_end = sim_data.get("soc_end", 0.0)
                slot.net_p_bat = sim_data.get("net_p_bat", 0.0)
                
                # Re-evaluate logic with simulated SOC
                new_mode, new_reason, _, _, new_t_soc = EnergyLogicEngine.get_mode_at(
                    dt_now=datetime.fromisoformat(slot.dt_iso),
                    batt_soc=slot.soc_start,
                    manager=self,
                    is_forecast=(i > 0),
                    abs_hour=h_abs_sim,
                    profiles=shared_profiles,
                    buy_strategy=buy_strat,
                    sell_strategy=sell_strat
                )
                slot.mode = new_mode
                slot.reason = new_reason
                slot.target_soc = new_t_soc
            
            if i == 0:
                slot.buy_debug = buy_strat
                slot.sell_debug = sell_strat
                
        return DispatchPlan(slots)

    async def _calculate_dp_plan(
        self, now, batt_soc, scale_today, scale_tomorrow,
        prof_gen, prof_cons, prof_cons_base,
        prof_gen_tomorrow, prof_cons_tomorrow, prof_cons_base_tomorrow,
        sim_range, eff
    ) -> DispatchPlan:
        """Потокобезопасный асинхронный расчет DP-плана на основе кэшированных советов."""
        slots = []
        charge_cmds = {}
        sell_cmds = {}
        dp_floors = {}
        dp_ceilings = {}
        sim_soc = batt_soc
        
        dp_advice = getattr(self, "dp_advice_stable", {})
        plan_by_ts = dp_advice.get("plan_by_timestamp", {}) if isinstance(dp_advice, dict) else {}
        
        v_nom = self.get_sensor_float(self.battery_voltage_sensor) or 52.0
        b_cap = float(self.get_setting("battery_capacity", 10.0) or 10.0)
        max_batt_p = float(self.get_setting("battery_max_power", 3.0) or 3.0)
        
        for h_abs in range(48):
            await asyncio.sleep(0.005)  # Yield to event loop to keep UI smooth
            dt_h = (now + timedelta(hours=h_abs)).replace(minute=0, second=0, microsecond=0)
            h_rel = str(dt_h.hour)
            today_str = dt_h.strftime("%Y-%m-%d")
            
            is_today = (dt_h.date() == now.date())
            if is_today:
                raw_gen = float(normalize_float(prof_gen.get(h_rel, 0.0)))
                scaled_gen = raw_gen * scale_today
                load_val = float(normalize_float(prof_cons.get(h_rel, 0.0)))
                load_base_val = float(normalize_float(prof_cons.get(h_rel, 0.0)))
            else:
                scaled_gen = float(normalize_float(prof_gen_tomorrow.get(h_rel, 0.0)))
                load_val = float(normalize_float(prof_cons_tomorrow.get(h_rel, 0.0)))
                load_base_val = float(normalize_float(prof_cons_tomorrow.get(h_rel, 0.0)))

            slot = GlobalSlot(
                hour_abs=h_abs,
                dt_iso=dt_h.isoformat(),
                price_buy=self.get_price("buy", today_str, dt_h.hour) or 0.0,
                price_sell=self.get_price("sell", today_str, dt_h.hour) or 0.0,
                gen_raw=scaled_gen,
                load_total=load_val,
                load_base=load_base_val
            )
            if h_abs == 0:
                slot.avg_gen = float(normalize_float(self.avg_gen_kw))
                slot.avg_load = float(normalize_float(self.avg_load_kw))
            
            ts_key = dt_h.strftime("%Y-%m-%d %H:00")
            adv = plan_by_ts.get(ts_key, {})
            
            if adv:
                dp_mode_raw = adv.get("mode", "IDLE")
                # v12.3.0: Rule to replace SOL with PV_CHG if current hour load > generation
                if h_abs == 0 and dp_mode_raw == "SOL":
                    if slot.avg_load > slot.avg_gen:
                        dp_mode_raw = "PV_CHG"

                dp_mode = self.translate_dp_mode(dp_mode_raw)
                power = adv.get("power_kw", 0.0)
                soc_limit = adv.get("target_soc", 10.0)
                reason = f"DP Optimizer ({dp_mode_raw})"
            else:
                # Startup/Error Safe Fallback
                dp_mode_raw = "IDLE"
                dp_mode = "sale_pv"
                power = 0.0
                soc_limit = 100.0
                reason = "DP Optimizer (Waiting...)"
                
            slot.mode = dp_mode
            slot.reason = reason
            slot.target_soc = soc_limit
            slot.power_ac = power
            slot.charge_amps = round((power * 1000.0) / max(10.0, v_nom), 1)
            
            # Apply unified manual overrides to DP plan too!
            man_override = self.hourly_manual_overrides.get(ts_key)
            is_legacy_manual = (dt_h.strftime("%Y-%m-%d") == now.strftime("%Y-%m-%d") and dt_h.hour in self.manual_mode_overrides)
            
            if man_override or is_legacy_manual:
                slot.is_manual = True
                if man_override:
                    slot.mode = man_override.get("mode", dp_mode)
                    slot.target_soc = float(man_override.get("soc_limit", soc_limit))
                else:
                    legacy_override = self.manual_mode_overrides.get(dt_h.hour)
                    slot.mode = legacy_override
                    slot.target_soc = 100.0 if legacy_override == "buy" else 10.0
                
                if h_abs == 0:
                    search_prefix = now.strftime("%Y-%m-%d %H")
                    h_override = None
                    for k, v in self.hourly_manual_overrides.items():
                        if k.startswith(search_prefix):
                            h_override = v
                            break
                    p_real, t_soc, c_amps = EnergyLogicEngine.calculate_realtime_power(
                        mode=slot.mode,
                        now=now,
                        batt_soc=batt_soc,
                        manager=self,
                        buy_strategy={},
                        sell_strategy={},
                        h_override=h_override
                    )
                    slot.power_ac = p_real
                    slot.target_soc = t_soc
                    slot.charge_amps = c_amps
                    p_actual = p_real if slot.mode == "buy" else -p_real if slot.mode == "sale_pv_bat" else 0.0
                else:
                    p_est = 0.0
                    t_soc_est = slot.target_soc
                    if slot.mode == "buy" and sim_soc < (t_soc_est - 0.05):
                        p_calc = (max(0.0, t_soc_est - sim_soc) / 100.0 * b_cap) / eff
                        p_est = min(max_batt_p, round(p_calc, 2))
                    elif slot.mode == "sale_pv_bat" and sim_soc > (t_soc_est + 0.2):
                        req_p = (max(0.0, sim_soc - t_soc_est) / 100.0 * b_cap) * eff
                        p_est = -min(max_batt_p, round(req_p, 2))
                    slot.power_ac = abs(p_est)
                    slot.charge_amps = round((slot.power_ac * 1000.0) / max(10.0, v_nom), 1)
                    p_actual = p_est
            else:
                p_actual = power if slot.mode == "buy" else -power if slot.mode == "sale_pv_bat" else 0.0
                
            if abs(p_actual) < 0.001:
                base_load = float(slot.load_base) if (slot.load_base and slot.load_base > 0.01) else float(slot.load_total)
                net_flow = slot.gen_raw - base_load
                _h_mode_cls = INVERTER_MODES.get(slot.mode)
                if _h_mode_cls:
                    if net_flow > 0 and _h_mode_cls.charge_from_pv:
                        p_actual = min(net_flow, max_batt_p)
                    elif net_flow < 0 and _h_mode_cls.discharge_to_house:
                        p_actual = max(net_flow, -max_batt_p)
                        
            delta_kwh = p_actual * (eff if p_actual > 0 else (1.0/eff))
            sim_soc = max(0.0, min(100.0, sim_soc + (delta_kwh / b_cap * 100.0)))
            slot.soc_end = sim_soc
            
            if slot.mode == "buy": charge_cmds[now.hour + h_abs] = slot.power_ac
            elif slot.mode == "sale_pv_bat": sell_cmds[now.hour + h_abs] = slot.power_ac
            
            h_abs_sim = int(now.hour + h_abs)
            if slot.mode == "sale_pv_bat":
                dp_floors[h_abs_sim] = slot.target_soc
            elif slot.mode == "buy":
                dp_ceilings[h_abs_sim] = slot.target_soc

            slots.append(slot)
            
        # High-Fidelity Simulation Pass for DP
        all_cmds = {}
        for h, p in charge_cmds.items(): all_cmds[h] = p
        for h, p in sell_cmds.items(): all_cmds[h] = -p
        m_overrides = { (now.hour + i): s.mode for i, s in enumerate(slots) }
        
        _, sim_log, _ = self.strategy_engine.run_soc_simulation(
            start_soc=batt_soc,
            sim_range=sim_range,
            now=now,
            commands=all_cmds,
            mode_overrides=m_overrides,
            dynamic_floors=dp_floors,
            dynamic_ceilings=dp_ceilings
        )
        
        for i, slot in enumerate(slots):
            h_abs_sim = now.hour + i
            sim_data = sim_log.get(h_abs_sim, {})
            if sim_data:
                slot.soc_start = batt_soc if i == 0 else slots[i-1].target_soc
                slot.soc_end = slot.target_soc
                slot.net_p_bat = sim_data.get("net_p_bat", 0.0)
                
        return DispatchPlan(slots)

    async def async_update_global_plan(self, force_strategy_recalc=True):
        """
        Main Orchestrator for the Global Dispatch Plan (v12.0).
        Calculates both plans (Heuristic & DP) independently and caches them.
        """
        self.log_to_file("DIAG: async_update_global_plan started")
        try:
            now = self.now
            self.strategy_engine.clear_cache()
            await asyncio.sleep(0)
            
            batt_soc, b_cap, _ = self.get_battery_state()
            batt_soc = float(batt_soc)
            
            if b_cap <= 0.1:
                if self.config_error != "Error: Missing Capacity":
                    _LOGGER.error("[ConfigError] CRITICAL: Battery Capacity is NOT SET or 0.0! Calculations STOPPED.")
                self.config_error = "Error: Missing Capacity"
                return

            self.config_error = None

            buy_strat = self.get_market_strategy("buy") or {}
            await asyncio.sleep(0.01)
            
            sell_strat = self.get_market_strategy("sell") or {}
            await asyncio.sleep(0.01)
            
            prof_gen = self.get_predicted_profile("generation")
            prof_cons = self.get_predicted_profile("consumption_total")
            prof_cons_base = self.get_predicted_profile("consumption_base")
            
            prof_gen_tomorrow = self.get_predicted_profile_tomorrow("generation")
            prof_cons_tomorrow = self.get_predicted_profile_tomorrow("consumption_total")
            prof_cons_base_tomorrow = self.get_predicted_profile_tomorrow("consumption_base")
            
            # Forecast Scaling parameters
            now_h = now.hour
            f_today_val = self.get_forecast_value(self.forecast_today_sensor)
            f_tomorrow_val = self.get_forecast_value(self.forecast_tomorrow_sensor)
            
            hist_today_rem = sum(float(normalize_float(prof_gen.get(str(h), 0.0))) for h in range(now_h, 24))
            scale_today = float(f_today_val / hist_today_rem) if (f_today_val is not None and hist_today_rem > 0.1) else 1.0
            
            # Scale tomorrow based on the unique tomorrow generation profile!
            hist_tomorrow = sum(float(normalize_float(prof_gen_tomorrow.get(str(h), 0.0))) for h in range(24))
            scale_tomorrow = float(f_tomorrow_val / hist_tomorrow) if (f_tomorrow_val is not None and hist_tomorrow > 0.1) else 1.0
            
            shared_profiles = {"gen": prof_gen, "cons": prof_cons, "cons_base": prof_cons_base}
            sim_range = list(range(now.hour, now.hour + 48))
            eff = 0.98

            # --- Independent Calculations for both strategies ---
            heuristic_plan = await self._calculate_heuristic_plan(
                now, batt_soc, buy_strat, sell_strat, scale_today, scale_tomorrow,
                prof_gen, prof_cons, prof_cons_base, shared_profiles, sim_range, eff
            )
            
            dp_plan = await self._calculate_dp_plan(
                now, batt_soc, scale_today, scale_tomorrow,
                prof_gen, prof_cons, prof_cons_base,
                prof_gen_tomorrow, prof_cons_tomorrow, prof_cons_base_tomorrow,
                sim_range, eff
            )
            
            # Cache the plans in EnergyProfileManager for the UI sensor attributes
            self.heuristic_hourly_data = heuristic_plan.to_hourly_data_attr()
            self.dp_hourly_data = dp_plan.to_hourly_data_attr()
            
            # --- Dynamic routing based on the 'use_dp' switch ---
            use_dp = self.get_setting("use_dp", False)
            if use_dp:
                self.global_plan = dp_plan
                self.log_to_file("DIAG: Active Global Plan set to Dynamic Programming (DP)")
            else:
                self.global_plan = heuristic_plan
                self.log_to_file("DIAG: Active Global Plan set to Heuristic")

            # Log inverter mode change if it differs from the last logged mode
            self.log_mode_change_to_file()

            # --- Write target option to inverter select entity if configured ---
            try:
                slot0_mode = self.global_plan.get_slot(0).mode if self.global_plan else None
                if slot0_mode:
                    inverter_select = self.get_setting("inverter_modes_select_entity")
                    if inverter_select:
                        option_val = None
                        if slot0_mode == "buy":
                            option_val = self.get_setting("dp_map_charge", "buy")
                        elif slot0_mode == "sale_pv_bat":
                            option_val = self.get_setting("dp_map_discharge", "sale_pv_bat")
                        elif slot0_mode == "sale_pv":
                            option_val = self.get_setting("dp_map_solar", "sale_pv")
                        elif slot0_mode == "stop_sale":
                            option_val = self.get_setting("dp_map_self_consume", "stop_sale")
                        elif slot0_mode == "no_pv_sale_no_bat":
                            option_val = self.get_setting("dp_map_grid", "no_pv_sale_no_bat")
                        
                        if option_val:
                            self.log_to_file(f"DIAG: Mapped mode {slot0_mode} -> {option_val}. Writing to inverter select is DISABLED by user request.")
            except Exception as e_write:
                _LOGGER.error("Error logging mode recommendation: %s", e_write)
                self.log_to_file(f"DIAG: Error logging mode: {e_write}")

            _LOGGER.info("[Global Plan] Successfully updated 48h dispatch registry with dual-planning support.")
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            self.log_to_file(f"[Global Plan] Update failed: {e}\n{error_details}")

    @callback
    def _poll_instant_power(self, now):
        """Poll and save the current instantaneous power levels for averaging."""
        is_neg_price = False
        load_kw = 0.0
        gen_kw = 0.0
        batt_p = 0.0

        if self.power_load_sensors:
            load_kw = sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_load_sensors)
        if self.power_gen_sensors:
            gen_kw = sum((get_kwh_val(self.hass.states.get(s)) or 0.0) for s in self.power_gen_sensors)
        
        if self.battery_power_sensor:
            batt_p = get_kwh_val(self.hass.states.get(self.battery_power_sensor)) or 0.0

        grid_p = 0.0
        if self.grid_power_sensor:
            raw_grid = get_kwh_val(self.hass.states.get(self.grid_power_sensor)) or 0.0
            # User sensor: + import, - export. 
            # Our internal convention: + export, - import.
            grid_p = -float(raw_grid)

        # Export calculation: prioritizing real grid sensor if available.
        # Component convention: Export (selling) is positive, Import (buying) is negative.
        real_export = max(0.0, float(grid_p))
        calc_export = max(0.0, float(gen_kw) + float(batt_p) - float(load_kw))

        self.power_history.append({
            "time": now, 
            "load_kw": float(load_kw), 
            "gen_kw": float(gen_kw),
            "batt_kw": float(batt_p),
            "grid_kw": float(grid_p),
            "export_kw": real_export if self.grid_power_sensor else calc_export
        })

        self._update_bms_learned_profile(now)

        # --- Real-time Balance / Savings Account Logic ---
        # v11.1.87: Fail-safe initialization to prevent NameError on startup
        record_grid_imp = 0.0
        record_grid_exp = 0.0
        
        # Logic: Increment/Decrement based on (Solar_to_Load + Battery_to_Load - Grid_to_Battery)
        # We need at least price and load power to calculate any savings.
        # We need prices and at least some source of power data (load, grid, or battery)
        if self.price_buy_sensors:
            p_buy = self.get_price("buy", now.strftime("%Y-%m-%d"), now.hour) or 0.0
            p_sell = self.get_price("sell", now.strftime("%Y-%m-%d"), now.hour) or 0.0
            is_neg_price = bool(p_buy <= 0)

            batt_p = 0.0
            if self.battery_power_sensor:
                st = self.hass.states.get(self.battery_power_sensor)
                batt_p = get_kwh_val(st) or 0.0 # _get_kwh_val handles W/kW conversion

            # Time delta in hours (polling is roughly 1 min)
            last_run = self.data.get("last_balance_poll_time")
            now_ts = now.timestamp()
            if last_run:
                dt_h = (now_ts - last_run) / 3600.0
                if 0 < dt_h < 24.0:
                    # 1. Solar to Load = energy we didn't buy because of PV
                    s_to_l = min(gen_kw, load_kw)

                    # 2. Battery to Load = energy we didn't buy because of Battery
                    load_rem = max(0.0, load_kw - gen_kw)
                    b_to_l = min(max(0.0, batt_p), load_rem)

                    # v11.1.88 - Restore Grid Flow Logic
                    if self.grid_power_sensor:
                        record_grid_exp = max(0.0, grid_p)
                        record_grid_imp = max(0.0, -grid_p)
                    else:
                        # Fallback for derived grid flow
                        p_charge = max(0.0, -batt_p)
                        s_avail_for_batt = max(0.0, gen_kw - load_kw)
                        record_grid_imp = max(0.0, load_kw + p_charge - s_to_l - b_to_l - s_avail_for_batt)
                        record_grid_exp = max(0.0, gen_kw + batt_p - load_kw)

                    # v11.1.91 - Perfected Economic Logic (Avoided Cost Model)
                    try:
                        # 1. Self-Consumption Gain (PV and Battery used for House)
                        # All energy used at home is valued at p_buy (money saved)
                        self_consumption_gain = (s_to_l + b_to_l) * (p_buy or 0.0)
                        
                        # 2. Grid Meter Revenue/Cost (Real Export/Import)
                        grid_revenue = record_grid_exp * (p_sell or 0.0)
                        grid_cost = record_grid_imp * (p_buy or 0.0)
                        
                        # 3. Amortization (Real resource cost)
                        deg_each_kwh = float(self.get_battery_degradation_cost() or 0.0)
                        battery_wear_total = abs(batt_p) * deg_each_kwh
                        
                        # Total Step Profit/Loss
                        if 0 < dt_h < 24.0:
                            step_delta = (self_consumption_gain + grid_revenue - grid_cost - battery_wear_total) * dt_h
                            
                            current_bal = self.data.get("energy_balance", 0.0)
                            self.data["energy_balance"] = round_f(current_bal + step_delta, 6)
                        else:
                            step_delta = 0.0
                        
                        # Store diagnostic snapshot
                        self.data["wallet_debug"] = {
                            "p_buy": p_buy, "p_sell": p_sell,
                            "grid_imp": round_f(record_grid_imp, 3), 
                            "grid_exp": round_f(record_grid_exp, 3),
                            "dt_h": dt_h, "step": step_delta,
                            "self_consume_gain": round_f(self_consumption_gain * dt_h, 4),
                            "wear_cost": round_f(battery_wear_total * dt_h, 4)
                        }
                    except Exception as e:
                        _LOGGER.error("Energy Management: Wallet calculation error: %s", e)
                   
                    is_neg_price = bool(p_buy is not None and p_buy <= 0)
                    if is_neg_price and step_delta > 0:
                        pass

            # v11.1.94 - Correct anchor update level (OUTSIDE if last_run)
            self.data["last_balance_poll_time"] = now_ts

        # Prune older than 10 minutes
        cutoff = now - timedelta(minutes=10)
        self.power_history = [x for x in self.power_history if x["time"] >= cutoff]

        # --- Power Learning & Cycle Tracking ---
        for sensor_id, settings in self.deduct_settings.items():
            if not isinstance(settings, dict): continue

            # 1. Determine activity state
            is_active = False
            cur_p = None
            
            # Check binary active sensor first if configured
            active_entity = settings.get(CONF_ACTIVE_SENSOR)
            if active_entity:
                st_active = self.hass.states.get(active_entity)
                if st_active:
                    is_active = st_active.state in ("on", "true", "active")

            # Check power sensor (for learning and as fallback for activity)
            p_entity = settings.get(CONF_POWER_SENSOR)
            if p_entity:
                p_state = self.hass.states.get(p_entity)
                if p_state and p_state.state not in ("unknown", "unavailable"):
                    try:
                        cur_p = float(str(p_state.state).replace(',', '.'))
                        if p_state.attributes.get("unit_of_measurement") == "kW":
                            cur_p *= 1000.0
                        self.last_known_power[sensor_id] = cur_p
                        
                        # Fallback to power-based detection if no dedicated active sensor
                        if not active_entity:
                            standby = self.learned_standby_power.get(sensor_id, 15.0)
                            is_active = cur_p > (standby + 10.0)
                    except ValueError:
                        cur_p = None

            if is_active:
                # Still active -> push forward the "last seen active" time for grace period
                last_active_before = self.cycle_start_time.get(sensor_id)
                self.cycle_start_time[sensor_id] = now

                # If this is the start of a new cycle OR there was a significant gap (e.g. > 1h)
                # which means the previous cycle wasn't closed properly (e.g. sensor dropout or restart)
                is_new_cycle = sensor_id not in self.cycle_actual_start_time
                if not is_new_cycle and last_active_before:
                    # v4.9 - Dynamic gap detection. If gap > hold_min * 2, assume it's a new cycle.
                    # This prevents merging cycles after a long HA restart or a machine pause.
                    hold_min = int(settings.get(CONF_ACTIVE_HOLD_TIME, 5))
                    gap_limit = max(600, hold_min * 120) # At least 10 min or 2x hold_min
                    gap = (now - last_active_before).total_seconds()
                    if gap > gap_limit: 
                        is_new_cycle = True

                if is_new_cycle:
                    self.cycle_actual_start_time[sensor_id] = now
                    self.cycle_energy_start[sensor_id] = self.daily_deduct_consumption.get(sensor_id, 0.0)

                # Active Power Learning (ONLY if we have a real power sensor)
                if cur_p is not None:
                    old_real = float(self.learned_real_power.get(sensor_id, cur_p))
                    if settings.get(CONF_IS_CYCLIC):
                        self.learned_real_power[sensor_id] = round_f(old_real * 0.9 + float(cur_p) * 0.1, 1)
                    else:
                        if float(cur_p) >= old_real:
                            self.learned_real_power[sensor_id] = round_f(float(cur_p), 1)
                        else:
                            self.learned_real_power[sensor_id] = round_f(old_real * 0.98 + float(cur_p) * 0.02, 1)
                elif not p_entity:
                    # If active by sensor but NO power sensor, 
                    # use config_kw as fallback for UI display
                    config_kw = float(settings.get("required_kw", 0.0)) * 1000.0
                    if config_kw > 0:
                        self.last_known_power[sensor_id] = config_kw
            else:
                # Standby Power Learning (Only if power sensor is idle)
                if cur_p is not None:
                    standby = self.learned_standby_power.get(sensor_id, 15.0)
                    if 0.1 < cur_p < (standby + 5.0):
                        old_s = float(self.learned_standby_power.get(sensor_id, cur_p))
                        self.learned_standby_power[sensor_id] = round_f(old_s * 0.95 + float(cur_p) * 0.05, 2)

                # If we just finished a cycle
                if sensor_id in self.cycle_actual_start_time:
                    # v4.4 - Improved Cycle termination with grace period
                    # We only terminate if the device hasn't been seen active for some time
                    # cycle_start_time stores the "last seen active" timestamp
                    last_active = self.cycle_start_time.get(sensor_id)
                    
                    # Use configurable hold time from settings
                    hold_min = int(settings.get(CONF_ACTIVE_HOLD_TIME, 5))
                    grace_timeout = now - timedelta(minutes=hold_min)
                    
                    # Robustness fallback: if last_active is missing (e.g. after update/settings change)
                    # use actual start time to allow termination if it's already old enough.
                    if not last_active:
                        last_active = self.cycle_actual_start_time.get(sensor_id)
                    
                    if last_active and last_active < grace_timeout:
                        duration = (last_active - self.cycle_actual_start_time[sensor_id]).total_seconds() / 3600.0
                        energy = self.daily_deduct_consumption.get(sensor_id, 0.0) - self.cycle_energy_start.get(sensor_id, 0.0)

                        if energy > 0.02 and duration > (1/60.0): # At least 20Wh and 1 minute
                            avg_p_w = (float(energy) * 1000.0) / float(duration)
                            if settings.get(CONF_IS_CYCLIC):
                                # Use EMA (Exponential Moving Average) to smooth learning
                                # This prevents wild jumps in predictions due to one unusual cycle.
                                
                                # 1. Learned Real Power (used for availability forecasts)
                                old_rp = float(self.learned_real_power.get(sensor_id, avg_p_w))
                                self.learned_real_power[sensor_id] = round_f(old_rp * 0.7 + avg_p_w * 0.3, 1)
                                
                                # 2. Learned Cycle Total kWh
                                old_kwh = float(self.learned_cycle_total_kwh.get(sensor_id, energy))
                                self.learned_cycle_total_kwh[sensor_id] = round_f(old_kwh * 0.7 + energy * 0.3, 3)
                                
                                # 3. Learned Avg Cycle Power (used for UI display)
                                old_ap = float(self.learned_avg_cycle_power.get(sensor_id, avg_p_w))
                                self.learned_avg_cycle_power[sensor_id] = round_f(old_ap * 0.7 + avg_p_w * 0.3, 1)
                                
                                # 4. Update historical duration (EMA)
                                dur_secs = (last_active - self.cycle_actual_start_time[sensor_id]).total_seconds()
                                old_dur = float(self.learned_avg_cycle_duration.get(sensor_id, dur_secs))
                                self.learned_avg_cycle_duration[sensor_id] = round_f(old_dur * 0.7 + dur_secs * 0.3, 0)

                        self.cycle_actual_start_time.pop(sensor_id, None)
                        self.cycle_energy_start.pop(sensor_id, None)

        # --- Solar Waste Calculation ---
        if self.power_gen_sensors and self.generation_sensors:
            # Use today's forecast distributed by profile (Solcast/Forecast.solar aware)
            # instead of just historical averages, because forecast knows about clouds.
            f_today = float(self.get_forecast_value(self.forecast_today_sensor) or 0.0)
            prof_gen_today = self.get_average_profile("generation", self.custom_period, "all")
            cur_hour = now.hour
            
            # Cumulative hist gen from now until 23:59
            hist_rem = sum(float(prof_gen_today.get(str(h), 0.0)) for h in range(cur_hour, 24))
            cur_hist = float(prof_gen_today.get(str(cur_hour), 0.0))
            
            # Potential for this hour based on TODAY'S weather forecast
            if hist_rem > 0.1:
                potential_kw = float(f_today * (cur_hist / hist_rem))
            else:
                potential_kw = 0.0
            
            soc, _, _ = self.get_battery_state()
            soc_f = float(soc) if soc is not None else 0.0
            current_gen = float(max(0.0, gen_kw))
            
            # Ensure potential doesn't drop below actual if we are doing better than forecast
            potential_kw = float(max(potential_kw, current_gen))
            
            # Waste occurs if: 
            # 1. Inverter is in 'stop_sale' mode (explicitly refusing to sell)
            # 2. Battery is full (>= 95%) 
            # 3. We are NOT exporting (throttled/limited)
            # 4. We are NOT importing (House load is fully covered by PV)
            # v11.9.331: Use InverterModeClass to determine if PV may be curtailed.
            # Any mode with curtail_pv=True is a candidate for solar waste tracking.
            _cur_mode_name = getattr(self, "current_inverter_mode", "")
            _cur_mode_cls = INVERTER_MODES.get(_cur_mode_name)
            is_pv_capped = (
                _cur_mode_cls is not None and
                _cur_mode_cls.curtail_pv and
                soc_f >= _cur_mode_cls.calibration_limit_soc
            )
            is_exporting = float(grid_p) > 0.1 if self.grid_power_sensor else False
            is_importing = float(grid_p) < -0.1 if self.grid_power_sensor else False
            
            if is_pv_capped and not is_exporting and not is_importing and potential_kw > (current_gen + 0.1):
                waste_kw = float(max(0.0, potential_kw - current_gen))
                # Sanity check: cap waste at 20kW
                waste_kw = float(min(waste_kw, 20.0))
                
                self.current_solar_waste_power = round_f(float(waste_kw), 3)
                # Accumulate kWh (1 min sample)
                step_waste = float(waste_kw / 60.0)
                self.data["temp_daily_waste"] = float(self.data.get("temp_daily_waste", 0.0) + step_waste)
            else:
                self.current_solar_waste_power = 0.0

        self._notify_update()

    @property
    def now(self):
        return dt_util.now()

    @property
    def day_type(self):
        """Returns the current day type index (0-6). 
        If binary_sensor.workday is 'off' (holiday), it may return 6 (Sunday) if configured.
        """
        now = self.now
        wd = now.weekday()
        
        # Holiday awareness (Optional)
        # If today is a holiday, we might want to treat it as a Sunday (6) for profiles
        if self.entry.data.get("holiday_as_weekend", True):
            workday_sensor = self.entry.data.get("workday_sensor")
            if workday_sensor:
                st = self.hass.states.get(workday_sensor)
                if st and st.state == "off":
                    return 6 # Sunday
        
        return wd

    @property
    def avg_load_kw(self):
        if not self.power_history:
            return 0.0
        val = sum(float(x.get("load_kw") or 0.0) for x in self.power_history) / len(self.power_history)
        return round_f(float(val), 3)

    @property
    def avg_gen_kw(self):
        if not self.power_history:
            return 0.0
        val = sum(float(x.get("gen_kw") or 0.0) for x in self.power_history) / len(self.power_history)
        return round_f(float(val), 3)

    @property
    def avg_batt_kw(self):
        """Average battery power (positive=discharging, negative=charging)."""
        if not self.power_history:
            return 0.0
        val = sum(float(x.get("batt_kw") or 0.0) for x in self.power_history) / len(self.power_history)
        return round_f(float(val), 3)

    @property
    def avg_export_kw(self):
        """Average export to grid power."""
        if not self.power_history:
            return 0.0
        val = sum(x.get("export_kw", 0.0) for x in self.power_history) / len(self.power_history)
        return round_f(float(val), 3)

    async def _async_periodic_save(self, _now):
        """Periodically persist data to disk between hour-top resets."""
        await self.async_save()

    @callback
    def _async_state_changed(self, event):
        entity_id = event.data.get("entity_id")
        new_state = event.data.get("new_state")

        if entity_id in self.all_price_sensors:
            self._update_prices_from_sensor(entity_id, new_state)
            # v11.9.560: Force immediate recalculation when prices update
            self._notify_update(force_strategy_recalc=True)
            return

        # Handle power sensors (trigger re-calculation of current power and balance)
        if entity_id in self.all_power_sensors:
            self._poll_instant_power(dt_util.now())
            return

        # v11.9.560: Force immediate recalculation when forecasts update
        is_f = False
        if isinstance(self.forecast_today_sensor, list) and entity_id in self.forecast_today_sensor: is_f = True
        if isinstance(self.forecast_tomorrow_sensor, list) and entity_id in self.forecast_tomorrow_sensor: is_f = True
        if is_f:
            self._notify_update(force_strategy_recalc=True)
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
            # v11.1.3 - Improved reset logic. 
            # If a sensor drops (e.g. transient 0 on reconnect), DON'T treat new total as delta.
            # Just reset baseline and wait for next real increment.
            self.sensor_last_values[entity_id] = new_val
            return

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
            # v11.1.3 - Spike protection for managed loads
            if delta > 2.0:
                _LOGGER.warning("Energy Management: Ignored suspicious jump of %s kWh for managed load %s", delta, entity_id)
                return
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

        # v11.1.19 - Incremental Base Accumulation:
        # Instead of raw subtraction (which fails if Meter lags behind the Plug),
        # we 'grow' the base energy at the historical rate when a load is ON.
        
        # 1. Calculate the 'floor' (expected progress based on historical profile)
        avg_prof = self.get_average_profile("consumption_base", 14, "all")
        hour_key = str(dt_util.now().hour)
        expected_total_h = float(avg_prof.get(hour_key, 0.4))
        
        raw_base = self.current_consumption_total - self.current_hourly_deduct
        
        # 2. Update rule
        if self.current_hourly_deduct > 0.05:
            # Managed load active: Use incremental synthesis
            # delta (in kWh) from previous measure is what the meter saw.
            # But if Meter < Managed, we use the historical average power (kW) multiplied by time.
            # For simplicity, we just ensure it's at least as high as the pro-rated historical profile.
            progress = dt_util.now().minute / 60.0
            expected_so_far = expected_total_h * progress
            self.current_consumption_base = max(self.current_consumption_base, expected_so_far, raw_base)
        else:
            # Trust the meter when no loads are active
            self.current_consumption_base = max(self.current_consumption_base, raw_base)

        if self.current_consumption_base < 0:
            self.current_consumption_base = 0.0
        if self.current_consumption_total < 0:
            self.current_consumption_total = 0.0

        self._notify_update()

    async def _async_reset_hour(self, now):
        # v11.3.2 - Reset hourly anchors for strategy fixing
        self.fixed_strategy_data = {
            "buy": {"id": -1, "power": 0.0, "target_soc": 0.0, "charge_amps": 0.0},
            "sell": {"id": -1, "power": 0.0, "target_soc": 0.0, "charge_amps": 0.0}
        }
        self._profile_cache = {}
        past_hour = (now.hour - 1) % 24

        today_wd = now.weekday()

        # Track occupancy at snapshot time
        occ_count, _ = self.get_current_occupancy()

        # Capture current forecast for the past hour to track accuracy
        f_dist = self.get_forecast_hourly_distribution(self.forecast_today_hourly_sensor)
        f_val = float(f_dist.get(str(past_hour), 0.0))

        # v11.1.23 - Typical base consumption for the healing logic
        avg_cons_prof = self.get_average_profile("consumption_base", 14, self.day_type)
        avg_cons_val = float(avg_cons_prof.get(str(past_hour), 0.5))

        # Check for solar curtailment (clipping due to full battery or negative price)
        # v11.1.21 - Expanded: also detect economic curtailment (negative price purchase)
        # v11.1.25 - Robust Healing Trigger
        p_buy = self.get_price("buy", now.strftime("%Y-%m-%d"), past_hour)
        
        cur_mode = getattr(self, "current_inverter_mode", "")
        b_soc, _, _ = self.get_battery_state()
        
        # v11.9.331: Use InverterModeClass to determine curtailment instead of hardcoded mode names.
        # curtail_pv=True means the mode POTENTIALLY limits generation.
        # calibration_limit_soc defines the SOC threshold above which the limitation actually kicks in.
        # Example: stop_sale curtails only when SOC >= 90%, no_pv_sale_no_bat always curtails (limit=0.0).
        _mode_cls = INVERTER_MODES.get(cur_mode)
        _is_mode_curtailed = (
            _mode_cls is not None and
            _mode_cls.curtail_pv and
            b_soc >= _mode_cls.calibration_limit_soc
        )
        is_curtailed = bool(_is_mode_curtailed or (p_buy is not None and p_buy <= 0))
        
        # v11.1.21 - Solar "Healing" logic: if curtailed, record forecast instead of actual zero/low gen
        gen_to_record = self.current_generation
        if is_curtailed and f_val > (gen_to_record + 0.1):
            _LOGGER.info("Energy Management: Healing solar profile for hour %s. Recording forecast %s instead of actual %s (Mode: %s)", past_hour, f_val, gen_to_record, "Economy" if (p_buy is not None and p_buy <= 0) else "BMS")
            gen_to_record = f_val

        # We heal if price is negative/near-zero (<= 0.05) OR if the strategy explicitly forced a buy.
        is_buy_mode = getattr(self, "current_inverter_mode", "") == "buy"
        is_cheap_energy = bool(p_buy is not None and p_buy <= 0.05)
        should_heal = is_cheap_energy or is_buy_mode

        record_base = self.current_consumption_base
        record_total = self.current_consumption_total
        healed_flag = 0

        # Check if this hour has a manual override
        is_manual = str(past_hour) in self.hourly_manual_overrides

        if should_heal:
             _LOGGER.info("Energy Management: Healing consumption profile for hour %s (Price: %s, Mode: %s). Recording average %s instead of actual %s", past_hour, p_buy, "buy" if is_buy_mode else "cheap", avg_cons_val, record_base)
             record_base = avg_cons_val
             record_total = avg_cons_val + self.current_hourly_deduct
             healed_flag = 1
            
        # Append to history lists (with occupancy tag and forecast snapshot)
        self.data["consumption_base"][str(past_hour)].append({"v": record_base, "wd": today_wd, "occ": occ_count, "h": healed_flag})
        self.data["consumption_total"][str(past_hour)].append({"v": record_total, "wd": today_wd, "occ": occ_count, "h": healed_flag})
        self.data["generation"][str(past_hour)].append({"v": gen_to_record, "f": f_val, "wd": today_wd, "c": 1 if is_curtailed else 0})

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

                # v11.1.24 - New Integrated ROI Model (System Benefit)
                solar_self = min(gen_h, cons_h)
                batt_to_load = batt_discharged
                # Portion of charging that came from GRID (not from surplus solar)
                grid_to_batt = max(0.0, batt_charged - max(0.0, gen_h - cons_h))
                # v11.1.95 - Use raw p_buy (no floor) to match wallet Avoided Cost model
                # Negative prices: self-consumption = loss, grid import = gain
                p_buy_eff = float(p_buy or 0.0)

                # Total profit = Solar Savings + Battery Savings + Sales - Grid Charge Cost
                total_profit_h = (solar_self * p_buy_eff) + (batt_to_load * p_buy_eff) + (h_sell_kwh * (p_sell or 0.0)) - (grid_to_batt * (p_buy or 0.0))
                total_profit_h = round_f(total_profit_h, 4)

                # Persist to "total" category
                if "savings" not in self.data:
                    self.data["savings"] = {}
                day_entry = self.data["savings"].setdefault(
                    past_date_str, {"total": 0.0, "solar": 0.0, "arbitrage": 0.0, "sell": 0.0})

                day_entry["total"] = round_f(day_entry.get("total", 0.0) + total_profit_h, 4)

                # Breakdown for attributes
                day_entry["solar"]     = round_f(day_entry.get("solar",     0.0) + (solar_self * p_buy_eff), 4)
                day_entry["sell"]      = round_f(day_entry.get("sell",      0.0) + (h_sell_kwh * (p_sell or 0.0)), 4)
                # Arbitrage captures battery savings and charging profit
                day_entry["arbitrage"] = round_f(day_entry["total"] - day_entry["solar"] - day_entry["sell"], 4)

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
                # v4.5 - Support for midnight-crossing cycles
                # If we are in the middle of a cycle, preserve what we've already counted today
                # by setting the cycle start energy to a negative offset.
                if s in self.cycle_energy_start:
                    yesterday_acc = self.daily_deduct_consumption.get(s, 0.0) - self.cycle_energy_start.get(s, 0.0)
                    self.cycle_energy_start[s] = -yesterday_acc
                
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
                    "actual": round_f(actual, 3),
                    "forecast": round_f(expected, 3),
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

    def _notify_update(self, force_strategy_recalc=False):
        if force_strategy_recalc:
            self.strategy_engine.clear_cache()
        for cb in self.update_listeners:
            cb()

    async def async_set_setting(self, key, value):
        self.settings[key] = value
        self.data["settings"] = self.settings
        await self.async_save()
        self._notify_update(force_strategy_recalc=True)

    def get_managed_load_stats(self, s_id):
        """Returns (expected_kw, remaining_kwh, is_cyclic, is_running). Single source of truth for Strategy."""
        settings = self.deduct_settings.get(s_id, {})
        if not isinstance(settings, dict):
            return 0.0, 0.0, False, False

        is_cyclic = settings.get(CONF_IS_CYCLIC, False)
        is_running = s_id in self.cycle_actual_start_time
        
        # Predicted kW (peak or reached)
        # We prefer learned_avg_cycle_power as it contains the real "working" power even when idle
        learn_w = float(self.learned_avg_cycle_power.get(s_id, 0.0))
        if learn_w < 100:
             learn_w = float(self.learned_real_power.get(s_id, 0.0))
        
        config_kw = float(settings.get("required_kw", 0.0))
        expected_kw = max(config_kw, (learn_w / 1000.0) if learn_w > 100 else 0.0)
        
        # Remaining energy for today
        req_kwh = float(settings.get("required_kwh", 0.0))
        consumed = float(self.daily_deduct_consumption.get(s_id, 0.0))
        remaining_kwh = max(0.0, req_kwh - consumed)

        return expected_kw, remaining_kwh, is_cyclic, is_running

    def get_active_managed_loads_power(self, hour_offset=0):
        """Calculate total power of currently active managed loads for simulation (legacy helper)."""
        active_load_kw = 0.0
        for s_id in self.deduct_settings:
            p_kw, rem_kwh, is_cyclic, is_running = self.get_managed_load_stats(s_id)
            
            if is_running:
                if rem_kwh > 0:
                    # If limited by energy, check if it will finish soon
                    if p_kw > 0 and (hour_offset + 1) <= (rem_kwh / p_kw):
                        active_load_kw += p_kw
                else: 
                    # If no energy limit (0), count as active until cycle ends
                    active_load_kw += p_kw
            elif not is_cyclic and hour_offset == 0:
                # Persistent loads reserve power in current budget
                active_load_kw += p_kw
                
        return active_load_kw

    def _update_prices_from_sensor(self, entity_id, state_obj):
        if not state_obj:
            return

        res = {}
        # Parse arrays in attributes (NordPool, ENTSO-E, Solcast, etc common formats)
        # v7.8.1 - Expanded with Solcast-specific attributes
        search_attrs = [
            "price_today", "prices_today", "prices", "data", "raw_today", 
            "price_tomorrow", "prices_tomorrow", "raw_tomorrow",
            "forecast", "hourly", "detailedForecast", "forecast_today", "forecast_tomorrow"
        ]
        for attr in search_attrs:
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
                                hour = None
                                if "T" in str(start_str):
                                    p_date = str(start_str).split("T")[0]
                                    p_time = str(start_str).split("T")[1][:2]
                                    hour = str(int(p_time))
                                elif " " in str(start_str):
                                    # Fallback for "YYYY-MM-DD HH:MM:SS"
                                    p_date = str(start_str).split(" ")[0]
                                    p_time = str(start_str).split(" ")[1][:2]
                                    hour = str(int(p_time))
                                
                                if hour is not None:
                                    if p_date not in res:
                                        res[p_date] = {}
                                    res[p_date][hour] = float(price_val)
                            except Exception:
                                pass
                        
                        # Support for Solcast's specific keys if not found above
                        # (period_start, pv_estimate / pv_estimate10 / pv_estimate90)
                        p_start = item.get("period_start") or item.get("period")
                        pv_est = item.get("pv_estimate") or item.get("pv_estimate10")
                        if p_start and pv_est is not None:
                            try:
                                p_date = str(p_start).split("T")[0] if "T" in str(p_start) else str(p_start).split(" ")[0]
                                p_time = str(p_start).split("T")[1][:2] if "T" in str(p_start) else str(p_start).split(" ")[1][:2]
                                hour = str(int(p_time))
                                if p_date not in res: res[p_date] = {}
                                res[p_date][hour] = float(pv_est)
                            except Exception: pass

        # Fallback to current continuous state if no arrays exist
        # v7.8.2 - Avoid "flat solar" bug. If it's a solar/generation sensor, a flat line is always wrong.
        e_id = str(state_obj.entity_id).lower()
        is_generation = "solar" in e_id or "gen" in e_id or "pv" in e_id
        
        if not res and not is_generation:
            try:
                val = float(state_obj.state)
                now = dt_util.now()
                # Populate 24h for today and 24h for tomorrow if it's a fixed price sensor
                for d_off in [0, 1]:
                    d_str = (now + timedelta(days=d_off)).strftime("%Y-%m-%d")
                    res[d_str] = {str(h): val for h in range(24)}
            except ValueError:
                pass
        elif not res and is_generation:
             # For solar sensors without internal distribution, we assume 0 for night/future 
             # and avoid generating a dangerous 24h flat line.
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

    def log_to_file(self, message: str):
        """Persistent logging for diagnostics (Non-blocking v12.0.27)."""
        def _write_sync():
            try:
                log_file = self.hass.config.path("energy_management.log")
                timestamp = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
                full_msg = f"{timestamp} {message}\n"
                
                # Rotation: Check size (max 5MB)
                import os
                if os.path.exists(log_file) and os.path.getsize(log_file) > 5 * 1024 * 1024:
                    old_log = log_file + ".old"
                    if os.path.exists(old_log): os.remove(old_log)
                    os.rename(log_file, old_log)
                    
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(full_msg)
            except: pass

        if hasattr(self, "hass") and self.hass:
            self.hass.async_add_executor_job(_write_sync)
        else:
            _write_sync()

    def log_mode_change_to_file(self):
        """Log inverter mode changes with all parameters to em_mode_change.log."""
        try:
            slot0 = self.global_plan.get_slot(0) if self.global_plan else None
            if not slot0: return
            
            current_mode = slot0.mode
            # Only log if the mode has changed from the last logged mode
            if self._last_logged_mode == current_mode:
                return
                
            self._last_logged_mode = current_mode
            
            soc = 0.0
            try:
                soc_val, _, _ = self.get_battery_state()
                if soc_val is not None:
                    soc = float(soc_val)
            except: pass
            
            use_dp = self.get_setting("use_dp", False)
            strategy = "DP" if use_dp else "Heuristic"
            
            power = round(slot0.power_ac, 2)
            amps = round(slot0.charge_amps, 1)
            target_soc = round(slot0.target_soc, 1)
            gen_hour = round(slot0.gen_raw, 2)
            load_hour = round(slot0.load_total, 2)
            
            avg_gen_5m = 0.0
            try:
                avg_gen_5m = round(self.avg_gen_5m_kw, 2)
            except: pass
            
            avg_load_5m = 0.0
            try:
                avg_load_5m = round(self.avg_load_5m_kw, 2)
            except: pass
            
            log_line = (
                f"MODE_CHANGE: Mode -> [{current_mode}] | Strategy: {strategy} | "
                f"Power: {power} kW | Amps: {amps} A | SOC: {soc}% | Target SOC: {target_soc}% | "
                f"Gen Hour: {gen_hour} kWh | Load Hour: {load_hour} kWh | "
                f"Avg Gen 5m: {avg_gen_5m} kW | Avg Load 5m: {avg_load_5m} kW | "
                f"Reason: {slot0.reason}"
            )
            
            def _write_mode_sync():
                try:
                    log_file = self.hass.config.path("em_mode_change.log")
                    timestamp = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
                    full_msg = f"{timestamp} {log_line}\n"
                    
                    # Rotation: Check size (max 5MB)
                    import os
                    if os.path.exists(log_file) and os.path.getsize(log_file) > 5 * 1024 * 1024:
                        old_log = log_file + ".old"
                        if os.path.exists(old_log): os.remove(old_log)
                        os.rename(log_file, old_log)
                        
                    with open(log_file, "a", encoding="utf-8") as f:
                        f.write(full_msg)
                except: pass
                
            if hasattr(self, "hass") and self.hass:
                self.hass.async_add_executor_job(_write_mode_sync)
            else:
                _write_mode_sync()
        except Exception as e:
            _LOGGER.error("Error in log_mode_change_to_file: %s", e)


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
            
            # v5.2 - Dynamic Period Adaptability
            # If no period is passed, use self.custom_period (default 14)
            # Transition periods (spring/autumn) might use shorter windows (e.g. 7)
            eff_days = days if days is not None else self.custom_period
            relevant = history[-eff_days:] if eff_days > 0 else history
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
                        # Support legacy "weekday"/"weekend" AND specific day 0-6
                        if day_type == "weekday" and wd >= 5: continue
                        if day_type == "weekend" and wd < 5: continue
                        
                        if isinstance(day_type, int) or (isinstance(day_type, str) and day_type.isdigit()):
                            try:
                                if int(wd) != int(day_type): continue
                            except (ValueError, TypeError): pass

                    # Filter by occupancy if requested
                    if occupancy_filter is not None and occ is not None:
                        if occupancy_filter == "home" and occ == 0: continue
                        if occupancy_filter == "away" and occ > 0: continue

                    valid_vals.append(v)
                except Exception:
                    pass

            if valid_vals:
                # v7.9.9 - MEDIAN Filter (Maximum Robustness)
                # Instead of a mean with outliers, we use a median to find the "Most Likely" base load.
                if len(valid_vals) >= 3:
                     profile[str(h)] = round_f(float(statistics.median(valid_vals)), 3)
                else:
                     # Fallback to mean for very small datasets
                     profile[str(h)] = round_f(sum(valid_vals) / len(valid_vals), 3)
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
            return -1, []
        count = 0
        active_sensors = []
        for entity_id in self.presence_sensors:
            state = self.hass.states.get(entity_id)
            if not state or state.state in ("unknown", "unavailable"):
                continue
            
            val = 0
            # zone.* — state is the number of people in the zone
            if entity_id.startswith("zone."):
                try:
                    val = int(float(state.state))
                except (ValueError, TypeError):
                    pass
            # person.* or binary_sensor.*
            elif state.state in ("home", "on"):
                val = 1
            
            if val > 0:
                count += val
                active_sensors.append(f"{entity_id}={state.state}")
                
        return count, active_sensors

    def get_occupancy_coefficient(self, hour=None):
        """Returns a multiplier (0.0-1.5+) to scale consumption forecast based on current occupancy.

        Compares average consumption when home vs away from stored history.
        If nobody is home, returns a ratio < 1.0 (typically 0.3-0.7).
        If occupancy tracking is not configured, returns 1.0.
        """
        if not self.presence_sensors:
            return 1.0, 0, 0, -1, [], 0.0, 0.0

        current_occ, active_sensors = self.get_current_occupancy()
        if current_occ < 0:
            return 1.0, 0, 0, current_occ, active_sensors, 0.0, 0.0

        # Calculate average consumption for home vs away from historical data
        days = self.custom_period

        hours_to_check = range(24) if hour is None else [hour]
        home_total = 0.0
        home_count = 0
        away_total = 0.0
        away_count = 0

        for h in hours_to_check:
            sh = str(h)
            history = self.data.get("consumption_base", {}).get(sh, [])
            relevant = history[-days:] if days > 0 else history
            for item in relevant:
                if not isinstance(item, dict):
                    continue
                v = normalize_float(item.get("v", 0.0))
                occ = item.get("occ")
                # v11.1.102 - Robust type handling for legacy/corrupted tuple records
                if isinstance(occ, (list, tuple)):
                    occ = occ[0] if occ else 0

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
            return 1.0, home_count, away_count, current_occ, active_sensors, avg_home, avg_away

        avg_home = home_total / home_count
        avg_away = away_total / away_count

        if avg_home <= 0.01:
            return 1.0, home_count, away_count, current_occ, active_sensors, avg_home, avg_away

        # If nobody is home right now, return the away/home ratio
        if current_occ == 0:
            return max(0.1, min(1.0, avg_away / avg_home)), home_count, away_count, current_occ, active_sensors, avg_home, avg_away

        # Everyone is home — no adjustment needed
        return 1.0, home_count, away_count, current_occ, active_sensors, avg_home, avg_away

    def get_efficiency_coefficient(self):
        """Calculates historical inverter/system efficiency."""
        return self.strategy_engine.get_efficiency_coefficient()

    def get_predicted_profile(self, profile_type):
        """Returns a 24h profile combining today's actual data with historical averages for future hours."""
        now = dt_util.now()
        cur_hour = now.hour
        res = {}
        for h in range(24):
            sh = str(h)
            if h < cur_hour:
                # Use today's actual data
                history = self.data.get(profile_type, {}).get(sh, [])
                if history:
                    last_record = history[-1]
                    val = normalize_float(last_record.get("v") if isinstance(last_record, dict) else last_record)
                    res[sh] = round_f(val, 3)
                else:
                    res[sh] = 0.0
            elif h == cur_hour:
                # Use current real-time value (v11.6.568: Use Power (kW) instead of Energy (kWh))
                if profile_type == "consumption_base": 
                    res[sh] = round_f(float(getattr(self, "avg_base_load_kw", 0.5)), 3)
                elif profile_type == "consumption_total": 
                    res[sh] = round_f(float(self.avg_load_kw), 3)
                elif profile_type == "generation": 
                    res[sh] = round_f(float(self.avg_gen_kw), 3)
                else: res[sh] = 0.0
            else:
                # v11.9.69: Future: Use smart forecast scaled to match total remaining forecast
                val = 0.0
                if profile_type == "generation":
                    dist = self.get_forecast_hourly_distribution(self.forecast_today_hourly_sensor)
                    if dist:
                        # Calculate current sum of smart forecast
                        smart_sum = sum(float(v) for h_str, v in dist.items() if int(h_str) >= cur_hour)
                        # Get real total remaining from sensor state
                        real_rem = self.get_forecast_value(self.forecast_today_sensor) or 0.0
                        
                        if smart_sum > 0.1:
                            scale = real_rem / smart_sum
                            val = float(dist.get(str(h), 0.0)) * scale
                        else:
                            val = 0.0 # No forecast left
                    else:
                        # Fallback to history
                        avg_prof = self.get_average_profile(profile_type, self.custom_period, self.day_type)
                        val = float(normalize_float(avg_prof.get(sh, 0.0)))
                else:
                    avg_prof = self.get_average_profile(profile_type, self.custom_period, self.day_type)
                    val = float(normalize_float(avg_prof.get(sh, 0.0)))
                
                res[sh] = round_f(val, 3)
        return res

    def get_predicted_profile_tomorrow(self, profile_type):
        """Returns tomorrow's 24h predicted profile (specific to tomorrow's day of week)."""
        now = dt_util.now()
        tomorrow = now + timedelta(days=1)
        tom_weekday = tomorrow.weekday()  # Integer 0-6
        tom_str = tomorrow.strftime("%Y-%m-%d")
        res = {}
        
        for h in range(24):
            sh = str(h)
            val = 0.0
            if profile_type == "generation":
                dist = self.get_forecast_hourly_distribution(self.forecast_tomorrow_sensor, target_date_str=tom_str) if self.forecast_tomorrow_sensor else {}
                if dist:
                    smart_sum = sum(float(v) for h_str, v in dist.items())
                    real_rem = self.get_forecast_value(self.forecast_tomorrow_sensor) or 0.0
                    if smart_sum > 0.1 and real_rem > 0.1:
                        scale = real_rem / smart_sum
                        val = float(dist.get(sh, 0.0)) * scale
                    else:
                        # Fallback to weekday history if Solcast tomorrow total or sum is 0
                        avg_prof = self.get_average_profile(profile_type, self.custom_period, tom_weekday)
                        val = float(normalize_float(avg_prof.get(sh, 0.0)))
                else:
                    avg_prof = self.get_average_profile(profile_type, self.custom_period, tom_weekday)
                    val = float(normalize_float(avg_prof.get(sh, 0.0)))
            else:
                avg_prof = self.get_average_profile(profile_type, self.custom_period, tom_weekday)
                val = float(normalize_float(avg_prof.get(sh, 0.0)))
                
            res[sh] = round_f(val, 3)
            
        return res

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
                    res[sh] = round_f(val, 3)
                else:
                    res[sh] = 0.0
            elif h == cur_hour:
                if profile_type == "consumption_base": res[sh] = round_f(self.current_consumption_base, 3)
                elif profile_type == "consumption_total": res[sh] = round_f(self.current_consumption_total, 3)
                elif profile_type == "generation": res[sh] = round_f(self.current_generation, 3)
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
        source = "internal_storage"

        # 2. Try entry options (from Options Flow)
        if val is None:
            val = self.entry.options.get(key)
            source = "config_options"

        # 3. Try entry data (from initial config)
        if val is None:
            val = self.entry.data.get(key)
            source = "config_data"

        if val is None:
            # v11.9.556: Trace default fallback
            # _LOGGER.debug(f"[SettingTrace] {key} fallback to default: {default}")
            return default

        # v11.9.556: Trace successful pull for critical settings
        if key in [CONF_BATTERY_MAX_POWER, CONF_AI_DISCHARGE_LIMIT]:
            _LOGGER.debug(f"[SettingTrace] {key} = {val} (Source: {source})")

        if isinstance(val, str):
            val_stripped = val.strip()
            # Fast CPU-friendly numeric string validation
            if val_stripped and set(val_stripped) <= set("0123456789.,-"):
                try:
                    float(val_stripped.replace(',', '.'))
                    val = normalize_float(val_stripped)
                except ValueError:
                    pass

        if isinstance(default, float):
            try:
                return float(val)
            except Exception as e:
                _LOGGER.warning(f"[ConfigError] Failed to parse setting '{key}' (Value: '{val}') as float. Using default: {default}. Error: {e}")
                return default
        return val

    def get_price(self, mode, date_str, hour):
        """Standardized price fetching from data store."""
        store = self.data.get(f"prices_{mode}", {})
        return get_price_from_store(store, date_str, hour)

    def _is_currently_pulling_power(self, sensor_id: str) -> bool:
        """Return True if the device currently has an active cycle (pulling power above standby)."""
        settings = self.deduct_settings.get(sensor_id, {})
        if not isinstance(settings, dict): return False

        # 1. Official 'Active' sensor (Binary Sensor) takes precedence
        active_sensor = settings.get(CONF_ACTIVE_SENSOR)
        if active_sensor:
            st = self.hass.states.get(active_sensor)
            if st:
                if st.state in ("on", "true", "active"):
                    return True
                if st.state in ("off", "false", "inactive"):
                    return False
        
        # 2. Traditional power-based detection (Fallback)
        p_sensor = settings.get(CONF_POWER_SENSOR)
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

        eid_str = str(entity_id)
        # Try direct numeric conversion first (for fixed values in config)
        try:
            return float(eid_str.replace(",", "."))
        except ValueError:
            pass

        st = self.hass.states.get(eid_str)
        _LOGGER.warning(f"[SENSOR_TRACE] entity_id='{eid_str}', state='{st.state if st else 'None'}'")
        
        if not st or st.state in ("unknown", "unavailable", "None"):
            return default

        try:
            val = normalize_float(st.state)
            return val
        except Exception:
            return default

    def get_battery_state(self, soc_default=0.0):
        """Read battery SOC, capacity, and calculate stored energy with glitch protection."""
        st = self.hass.states.get(self.battery_soc_sensor) if self.battery_soc_sensor else None
        soc = soc_default
        
        # Initialize last_valid_soc/cap if not exists
        if not hasattr(self, "_last_valid_soc"):
            self._last_valid_soc = None
        if not hasattr(self, "_last_valid_cap"):
            self._last_valid_cap = None

        if st:
            try:
                raw_state = str(st.state).replace(',', '.')
                if raw_state not in ['unavailable', 'unknown', 'none', '']:
                    soc = float(raw_state)
                else:
                    soc = None
            except (ValueError, TypeError):
                soc = None
            
            if soc is None:
                # Check attributes if state is non-numeric
                soc_attr = st.attributes.get("soc") or st.attributes.get("battery_level") or st.attributes.get("battery")
                if soc_attr is not None:
                    soc = float(normalize_float(soc_attr))
            
            # Glitch protection: If we get 0.0 but had a much higher value recently, ignore the 0.0
            if (soc is None or soc <= 0.0) and self._last_valid_soc is not None and self._last_valid_soc > 1.0:
                _LOGGER.warning(f"Battery SOC glitch detected: {soc}% (last valid: {self._last_valid_soc}%). Using last valid.")
                soc = self._last_valid_soc
            
            if soc is not None and soc > 0.0:
                self._last_valid_soc = soc
        
        # Final fallback
        if soc is None:
            _LOGGER.warning(f"[ConfigError] Battery SOC sensor '{self.battery_soc_sensor}' returned None. Using fallback: {soc_default}%")
            soc = soc_default
        
        cap = self.get_sensor_float(self.battery_capacity_sensor, 0.0)
        
        # Initialize last_valid_cap if not exists
        if not hasattr(self, "_last_valid_cap"):
            self._last_valid_cap = None

        # Capacity Glitch Protection
        if cap <= 0.1:
            if self._last_valid_cap is not None:
                _LOGGER.warning(f"Battery Capacity glitch detected: {cap} kWh. Using last valid: {self._last_valid_cap} kWh")
                cap = self._last_valid_cap
            elif "last_known_battery_capacity" in self.data:
                cap = float(self.data["last_known_battery_capacity"])
                self._last_valid_cap = cap
                _LOGGER.warning(f"Battery Capacity sensor is unavailable/0.0. Restored last known from persistent storage: {cap} kWh")
        
        if cap > 0.1:
            self._last_valid_cap = cap
            if self.data.get("last_known_battery_capacity") != cap:
                self.data["last_known_battery_capacity"] = cap
                try:
                    self.hass.async_create_task(self.store.async_save(self.data))
                except Exception as ex:
                    _LOGGER.debug(f"Failed to auto-save battery capacity: {ex}")
            
        if cap <= 0.1:
            from .const import CONF_BATTERY_CAPACITY
            raw_setting = self.get_setting(CONF_BATTERY_CAPACITY)
            cap = self.get_setting(CONF_BATTERY_CAPACITY, 0.0)
            if cap <= 0.1:
                 _LOGGER.error("[ConfigError] CRITICAL: Battery Capacity is NOT SET! Cannot calculate energy.")
                 cap = 0.0
            
        energy = cap * (soc / 100.0) if cap > 0 else 0.0
        return float(soc), float(cap), energy

    def get_forecast_hourly(self, ptype="generation") -> Dict[int, float]:
        """Retrieve hourly forecast map {abs_hour: kw} for the next 48h (Solcast/Templates)."""
        res = {}
        now = self.now
        
        sensors = []
        if ptype == "generation":
            sensors = getattr(self, "forecast_today_hourly_sensor", [])
        
        if not sensors:
            return res
            
        for s_id in sensors:
            state = self.hass.states.get(s_id)
            if not state: continue
            
            # v11.9.715: Support for Solcast-style 'forecast' attribute
            forecast_list = state.attributes.get("forecast") or state.attributes.get("hourly")
            if not forecast_list or not isinstance(forecast_list, list):
                continue
                
            for item in forecast_list:
                if not isinstance(item, dict): continue
                try:
                    # Item can be {datetime: ..., pv_estimate: ...} or {dt: ..., value: ...}
                    dt_str = item.get("period_start") or item.get("datetime") or item.get("dt")
                    if not dt_str: continue
                    
                    dt_p = dt_util.parse_datetime(str(dt_str))
                    if not dt_p: continue
                    
                    # Normalize to local time for hour matching
                    dt_local = dt_util.as_local(dt_p)
                    
                    # Calculate absolute hour offset from now
                    # (Used as key in simulation loop)
                    delta = dt_local.replace(minute=0, second=0, microsecond=0) - now.replace(minute=0, second=0, microsecond=0)
                    h_abs = now.hour + int(delta.total_seconds() / 3600)
                    
                    if 0 <= h_abs < 72: # 3-day horizon
                        val = float(item.get("pv_estimate") or item.get("value") or item.get("gen_kw") or 0.0)
                        # Sum up if multiple sensors provide data for the same hour
                        res[h_abs] = res.get(h_abs, 0.0) + val
                except (ValueError, TypeError, Exception):
                    continue
        return res

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

    def get_forecast_hourly_distribution(self, sensor_list, target_date_str=None):
        """
        Parses Solcast 'Analysis' attributes to get hourly distribution for a specific day.
        Returns a dict {hour: value} normalized or raw.
        [Diag v5.2.1-fix-indent-persistent-411]
        """
        if not sensor_list:
            return {}
        if isinstance(sensor_list, str):
            sensor_list = [sensor_list]
            
        res = {str(h): 0.0 for h in range(24)}
        found_data = False
        
        if target_date_str is None:
            target_date_str = self.now.strftime("%Y-%m-%d")

        for fsensor in sensor_list:
            st = self.hass.states.get(fsensor)
            if not st: continue
            
            items_processed = 0
            # 1. Check for Solcast detailedForecast (Priority)
            intervals = st.attributes.get("detailedForecast") or st.attributes.get("detailed_forecast")
            
            if not intervals:
                # 2. Check top level intervals
                intervals = st.attributes.get("intervals")
            
            if not intervals:
                # 3. Other Solcast specialized keys
                intervals = st.attributes.get("forecast_today") or st.attributes.get("forecast_total") or st.attributes.get("forecast_tomorrow")
            
            if not intervals:
                # 4. Fallback: Forecast.Solar uses 'forecast' or 'hourly'
                intervals = (st.attributes.get("forecast") or st.attributes.get("hourly"))
            
            # If we have attributes but no intervals, skip
            if not isinstance(intervals, list): 
                continue
            
            for item in intervals:
                if not isinstance(item, dict): continue
                
                items_processed += 1
                
                try:
                    # Solcast uses 'period_start', Forecast.Solar might use 'datetime' or 'time'
                    p_start = item.get("period_start") or item.get("datetime") or item.get("time")
                    if not p_start: continue
                    
                    try:
                        # Handle both strings and native datetime objects
                        if isinstance(p_start, datetime):
                            dt_val = p_start
                        else:
                            dt_val = dt_util.parse_datetime(str(p_start))
                        
                        if not dt_val:
                            # Manual string split fallback
                            p_str = str(p_start)
                            d_part = p_str.split("T")[0].split(" ")[0]
                            if d_part != target_date_str: continue
                            h_idx = int(p_str.split("T" if "T" in p_str else " ")[1][:2])
                        else:
                            # Use Home Assistant's local time if available
                            dt_local = dt_util.as_local(dt_val)
                            if dt_local.strftime("%Y-%m-%d") != target_date_str: continue
                            h_idx = dt_local.hour
                            
                        # Value field (Aggressive search)
                        val = 0.0
                        v_keys = ["pv_estimate", "estimate", "spread_kwh", "pv_estimate10", "estimate10", "value", "amount", "kwh", "energy", "pv"]
                        for k in v_keys:
                            if k in item:
                                val = item[k]
                                break
                        
                        # Solcast detailedForecast uses 30-minute intervals. 
                        # Summing two 30-min kW values gives 2x actual kWh, so we multiply by 0.5.
                        res[str(h_idx)] += float(val or 0.0) * 0.5
                        found_data = True
                    except (ValueError, IndexError, TypeError):
                        continue
                except Exception:
                    continue
                    
        return {k: round(float(v), 2) for k, v in res.items()} if found_data else {}

    @staticmethod
    def get_cc_cv_ratio(soc):
        """Calculate CC/CV charge acceptance ratio."""
        return StrategyEngine.get_cc_cv_ratio(soc)

    def get_gen_forecast_coefficient(self, forecast_value, prof_gen, hour_start, hour_end):
        """Calculate scaling coefficient."""
        return self.strategy_engine.get_gen_forecast_coefficient(forecast_value, prof_gen, hour_start, hour_end)

    def get_market_strategy(self, mode="buy", allow_recalc=True):
        """Complex market strategy solver."""
        res = self.strategy_engine.get_market_strategy(mode, allow_recalc=allow_recalc)
        
        # v11.3.2: Capture Power/SOC/Amps anchor at the start of the hour or mode entry
        now = dt_util.now()
        cur_hour = now.hour
        # v11.6.521: Safe Anchor Permission.
        # Prevent anchoring zeros during initial calculation or when no strategy is planned (none).
        # We only anchor if there is a real strategic reason AND a valid state.
        c_reason = res.get("charge_reason", "none")
        if c_reason == "none" or res.get("state") == "idle":
             return res

        is_active = True # Any reason other than 'none' is an active strategic intent
        
        # Unified key for the current hour and mode
        hour_key = f"{cur_hour}_{mode}"
        stored = self.fixed_strategy_data.get(mode, {"hour_key": ""})
        
        if is_active:
            p_val_new = float(res.get("recommended_power_kw", 0.0))
            t_soc_new = float(res.get("target_soc", 0.0))
            stored_p = float(stored.get("power", 0.0))
            
            # v11.6.516: High-Water Mark Anchor (Revised).
            # Update the anchor if:
            # 1. It's a new hour.
            # 2. Strategy suggests MORE power than currently locked (budget increase).
            # If power drops, we stay at the previously locked maximum to avoid jitter.
            if stored.get("hour_key") != hour_key or p_val_new > (stored_p + 0.05):
                c_amps = 0.0
                if self.battery_voltage_sensor:
                    v_val = self.get_sensor_float(self.battery_voltage_sensor)
                    if v_val and v_val > 0.1 and p_val_new > 0.01:
                        c_amps = round_f((p_val_new * 1000.0) / v_val, 2)
                
                self.fixed_strategy_data[mode] = {
                    "id": 1,
                    "hour_key": hour_key,
                    "power": p_val_new,
                    "target_soc": t_soc_new,
                    "charge_amps": c_amps
                }
        else:
            # Wipe anchor if no longer active
            self.fixed_strategy_data[mode] = {"id": -1, "hour_key": "", "power": 0.0, "target_soc": 0.0, "charge_amps": 0.0}
            
        return res

    def get_battery_charge_limit_kw(self, soc):
        """Returns the maximum possible charge power (kW) for a given SOC.
        Uses learned BMS profile if available, otherwise falls back to theoretical CC/CV model.
        """
        soc_int = int(round_f(soc, 0))
        
        # 1. Exact match in learned profile
        if soc_int in self.bms_learned_profile:
            return self.bms_learned_profile[soc_int]
        
        # 2. Heuristic: Interpolate between known points or use boundaries
        known_socs = sorted(self.bms_learned_profile.keys())
        if known_socs:
            if soc_int < known_socs[0]:
                return self.bms_learned_profile[known_socs[0]]
            if soc_int > known_socs[-1]:
                return self.bms_learned_profile[known_socs[-1]]
            
            # Interpolation (linear)
            for i in range(len(known_socs) - 1):
                s1, s2 = known_socs[i], known_socs[i+1]
                if s1 < soc_int < s2:
                    p1, p2 = self.bms_learned_profile[s1], self.bms_learned_profile[s2]
                    ratio = (soc_int - s1) / (s2 - s1)
                    return round_f(p1 + (p2 - p1) * ratio, 3)

        # 3. Fallback to theoretical CC/CV model or user-defined max
        max_p = float(self.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
        ratio = self.get_cc_cv_ratio(soc)
        return round_f(max_p * ratio, 3)

    def get_budget_and_permissions(self, days_for_profile=14, skip_strategy_check=False):
        """Analyze current day state and return permissions for heavy loads."""
        return self.strategy_engine.get_budget_and_permissions(days_for_profile, skip_strategy_check)

    def run_soc_simulation(self, start_soc, sim_hours_abs, now=None, commands=None, **kwargs):
        """Universal SOC simulation engine."""
        now = now or dt_util.now()
        return self.strategy_engine.run_soc_simulation(start_soc, sim_hours_abs, now, commands, **kwargs)

    def _update_bms_learned_profile(self, now):
        """Analyze stable power history to learn BMS charging limits.
        v11.1.96 - Corrected logic:
        1. sale_pv: Full learning (up + down). Requires export > 0.5kW as proof.
        2. buy: Upward-only learning. Grid = unlimited source, so if battery takes
           MORE than profile says, the profile was wrong. No downward updates
           (inverter may intentionally limit charge current).
        3. Monotonicity enforced after every update.
        """
        # Condition 0: Stability - need at least 5 samples (5 minutes)
        if len(self.power_history) < 5: return
        
        mode = self.current_inverter_mode
        # Only learn in sale_pv or buy
        if mode not in ("sale_pv", "buy"):
            return 

        hist_list = list(self.power_history)
        relevant_history = hist_list[-5:]

        # Stability check: ensure power doesn't jump too much in the window (max 150W variance)
        batt_samples = [float(x.get("batt_kw") or 0.0) for x in relevant_history]
        power_spread = max(batt_samples) - min(batt_samples)
        if power_spread > 0.15: # 150W spread is too noisy for precise BMS learning
            return

        avg_batt = sum(batt_samples) / len(relevant_history)
        if avg_batt < -0.05: # At least 50W average charge observed
            # Use MAX charge power (most negative value), not average.
            charge_power_limit = abs(min(batt_samples))
            soc, _, _ = self.get_battery_state()
            soc_int = int(round_f(float(soc or 0.0), 0))
            
            # v11.6.117: Improved Learning Logic
            # 1. Export Proof: If we are exporting > 0.5kW, the battery is REFUSING power.
            #    This is a hard proof of the BMS limit. Full learning (up/down).
            avg_export = sum(float(x.get("grid_kw", 0.0)) for x in relevant_history) / len(relevant_history)
            has_export_proof = bool(avg_export > 0.5)
            
            max_batt_p = float(self.get_setting(CONF_BATTERY_MAX_POWER, 5.0))
            old_val = self.bms_learned_profile.get(soc_int)
            
            do_update = False
            new_val = 0.0

            if has_export_proof:
                # Golden proof: update if different from old or if first time
                if old_val is None or abs(charge_power_limit - old_val) > 0.05:
                    new_val = charge_power_limit
                    do_update = True
            elif mode == "buy":
                # Buy mode: Grid is unlimited, but we don't know if inverter is limiting.
                # Only trust UPWARD updates (High Water Mark).
                check_val = old_val if old_val is not None else max_batt_p
                if charge_power_limit > check_val + 0.05:
                    new_val = charge_power_limit
                    do_update = True
                
            if do_update:
                self.bms_learned_profile[soc_int] = round_f(new_val, 3)
                
                # --- Monotonicity Enforcement ---
                # Downward pass: SOC < current must have AT LEAST this power
                for s in range(soc_int - 1, -1, -1):
                    if s in self.bms_learned_profile and self.bms_learned_profile[s] < new_val:
                        self.bms_learned_profile[s] = new_val
                
                # Upward pass: SOC > current must have AT MOST this power
                for s in range(soc_int + 1, 101):
                    if s in self.bms_learned_profile and self.bms_learned_profile[s] > new_val:
                        self.bms_learned_profile[s] = new_val


class UniversalPriceSensor(SensorEntity):
    """Exposes 48-hour price data with price_today/price_tomorrow attributes for templates."""
    def __init__(self, manager, mode, name):
        self.manager = manager
        self.mode = mode
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_{mode}_price_48h"
        self._attr_icon = "mdi:cash-clock"
        self._attr_native_unit_of_measurement = "/kWh"
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
                self._attr_native_unit_of_measurement = f"{currency}/kWh"
        except Exception:
            pass
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        now = dt_util.now()
        today = now.strftime("%Y-%m-%d")
        return self.manager.get_price(self.mode, today, now.hour)

    @property
    def extra_state_attributes(self):
        """Standard array format for external automation/templates."""
        now = dt_util.now()
        today_str = now.strftime("%Y-%m-%d")
        tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")

        def build_array(date_str):
            prices_dict = self.manager.data.get(f"prices_{self.mode}", {}).get(date_str, {})
            arr = []
            if not isinstance(prices_dict, dict):
                return []
            
            for h, p in sorted(prices_dict.items(), key=lambda x: int(x[0]) if str(x[0]).isdigit() else 99):
                if not str(h).isdigit():
                    continue
                # Start time in ISO format for template compatibility
                start_dt = dt_util.parse_datetime(f"{date_str}T{int(h):0>2}:00:00")
                if start_dt:
                    arr.append({
                        "start": start_dt.isoformat(),
                        "price": float(normalize_float(p))
                    })
            return arr

        return {
            "price_today": build_array(today_str),
            "price_tomorrow": build_array(tomorrow_str)
        }

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
        return round_f(sum(profile.values()), 3)

    @property
    def extra_state_attributes(self):
        # Specific day index (0-6)
        curr_day = self.manager.day_type
        
        # v5.2 - Show if we are using 7-day or standard window
        learning_mode = "Standard"
        if self.days <= 7:
            learning_mode = "Fast Adaptive"

        if self.ptype == "generation":
            profile = self.manager.get_average_profile("generation", self.days, "all")
            
            # Check if we have hourly distribution sensors
            dist_source = "historical"
            if self.manager.forecast_today_hourly_sensor:
                dist = self.manager.get_forecast_hourly_distribution(self.manager.forecast_today_hourly_sensor)
                if dist:
                    dist_source = "forecast_hourly"
                    
            return {
                "period_days": self.days,
                "current_day_index": curr_day,
                "learning_mode": learning_mode,
                "dist_source": dist_source,
                "profile": profile
            }
        else:
            base_profile = self.manager.get_average_profile("consumption_base", self.days, curr_day)
            total_profile = self.manager.get_average_profile("consumption_total", self.days, curr_day)
            return {
                "period_days": self.days,
                "current_day_index": curr_day,
                "learning_mode": learning_mode,
                "base_profile": base_profile,
                "total_profile": total_profile,
                "total_daily_average": round_f(sum(total_profile.values()), 3)
            }


class BMSLearnedProfileSensor(SensorEntity):
    """Diagnostic sensor showing the learned BMS charge limit profile."""
    _attr_has_entity_name = True
    def __init__(self, manager):
        self.manager = manager
        # v11.1.89 - Forced unique_id change to clear HA registry cache
        self._attr_name = "Обученный профиль заряда BMS"
        self._attr_translation_key = "bms_learned_profile"
        self._attr_unique_id = f"{manager.entry.entry_id}_bms_profile_v2"
        self.entity_id = f"sensor.{DOMAIN}_bms_learned_profile"
        self._attr_icon = "mdi:battery-charging-high"
        
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
        return len(self.manager.bms_learned_profile)

    @property
    def extra_state_attributes(self):
        profile = self.manager.bms_learned_profile
        sorted_profile = {str(k): v for k, v in sorted(profile.items())}
        
        # Calculate some stats for the user
        max_p = max(profile.values()) if profile else 0.0
        min_p = min(profile.values()) if profile else 0.0
        
        return {
            "profile": sorted_profile,
            "learned_points_count": len(sorted_profile),
            "max_charge_power_observed": max_p,
            "min_charge_power_observed": min_p,
            "last_update": self.manager.now.isoformat() if profile else None
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
        is_day = (int(sunrise_hour) <= now.hour <= int(sunset_hour)) or is_gen_active

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

        # 1. Get active strategy constraints to merge into simulation
        buy_strat = self.manager.strategy_engine.get_market_strategy("buy", allow_recalc=False) or {}
        sell_strat = self.manager.strategy_engine.get_market_strategy("sell", allow_recalc=False) or {}
        
        charge_commands = buy_strat.get("charge_commands", {})
        planned_sell = sell_strat.get("planned_sell", {})
        
        merged_commands = {**charge_commands}
        for h, v in planned_sell.items():
            merged_commands[int(h)] = -abs(v) # Ensure sell commands are negative
            
        buy_sim = buy_strat.get("buy_simulation", {})
        no_battery_charge_until = buy_sim.get("no_battery_charge_until")
        pv_curtail_hours = buy_sim.get("pv_curtail_hours")

        # 2. Run Unified Simulation Engine
        # v11.6.355: Comprehensive Sell Debug
        _sell_debug = {
            "server_time": now.strftime("%H:%M:%S"),
            "cur_hour": int(now.hour),
            "b_soc": round_f(batt_soc, 1),
            "max_p": round_f(0.0, 2)
        }
        simulated_soc, charge_log, _ = self.manager.run_soc_simulation(
            batt_soc, sim_hours, now,
            commands=merged_commands,
            no_battery_charge_until=no_battery_charge_until,
            pv_curtail_hours=pv_curtail_hours,
            mode_overrides=getattr(self.manager, "planned_mode_overrides", None)
        )
        
        # 3. Run Base-only simulation for comparison
        simulated_soc_base, _, _ = self.manager.run_soc_simulation(
            batt_soc, sim_hours, now,
            commands=merged_commands,
            no_battery_charge_until=no_battery_charge_until,
            pv_curtail_hours=pv_curtail_hours,
            mode_overrides=getattr(self.manager, "planned_mode_overrides", None),
            house_profile_override="consumption_base"
        )
        
        # v11.3.63: Inject Budget Diagnostics for transparency (FIX: Restored missing definition)
        budget_data = self.manager.strategy_engine.get_budget_and_permissions(skip_strategy_check=True)
        debug_attrs = {k: v for k, v in budget_data.items() if k.startswith("debug_")}

        f_raw = self.manager.get_forecast_value(self.manager.forecast_today_sensor)
        coeff = getattr(self.manager, "last_blended_coeff", 1.0)
        f_val = f_raw * coeff if f_raw is not None else 0.0


        self._attr_extra_state_attributes = {
            "prediction_target": target_label,
            "target_hour": f"{target_hour:02d}:00",
            "current_soc_pct": round_f(batt_soc, 1),
            "projected_soc_base_pct": round_f(simulated_soc_base, 1),
            "load_profile_used": "total",
            "forecast_income_remaining_kwh": round_f(f_val, 2),
            "forecast_raw_kwh": round_f(f_raw or 0.0, 2),
            "forecast_coefficient_blended": round_f(coeff, 3),
            "efficiency_coefficient": round_f(eff_coeff, 3),
            "simulation_log": charge_log
        }
        self._attr_extra_state_attributes.update(debug_attrs)
        return round_f(simulated_soc, 1)

    @property
    def extra_state_attributes(self):
        attrs = dict(getattr(self, "_attr_extra_state_attributes", {}))
        
        # Calculate expected vs actual so far (v11.9.118: Using hourly sensor and temp_daily_gen)
        now = dt_util.now()
        expected_so_far = 0.0
        actual_so_far = float(self.manager.data.get("temp_daily_gen", 0.0) or 0.0)
        
        try:
            today_str = now.strftime("%Y-%m-%d")
            # v11.9.118: Corrected sensor usage - must use HOURLY sensor for distribution
            f_dist = self.manager.get_forecast_hourly_distribution(self.manager.forecast_today_hourly_sensor, today_str)
            if f_dist:
                expected_so_far = sum(float(v) for h, v in f_dist.items() if int(h) < now.hour)
        except Exception:
            pass

        attrs["expected_kwh_so_far"] = round_f(expected_so_far, 2)
        attrs["actual_kwh_so_far"] = round_f(actual_so_far, 2)
        
        return attrs


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
        # v11.1.2 - Include current hour's accumulator to avoid -100% deviation
        today_total_prof = self.manager.get_todays_profile("consumption_total")
        # v11.1.8 - Removed double counting: get_todays_profile ALREADY includes current_consumption_total
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
            "actual_base_kwh": round_f(actual_base, 3),
            "expected_base_kwh": round_f(expected_so_far, 3),
            "managed_loads_kwh": round_f(deduct_sum, 3),
            "day_type": day_type,
            "status": "accumulating" if actual_base < 0.1 else "active"
        }

        return round_f(deviation, 1) if abs(deviation) < 1000 else 0.0


class InverterOperationModeSensor(SensorEntity):
    """Outputs the specific inverter command state based on logic."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_inverter_mode"
        self._attr_icon = "mdi:state-machine"
        self._state = "sale_pv"
        self._last_logged_params = {
            "mode": None,
            "power": 0.0,
            "target_soc": 0.0,
        }
        self._mode_lock_until = None
        self._locked_mode = None
        self._last_logged_hour = None
        # v11.9.705: Latch for inverter commands (Stability TS)
        self._latched_power = 0.0
        self._latched_amps = 0.0
        self._last_latch_mode = None
        self._last_latch_target_soc = -1.0
        self._last_latch_manual_power = -1.0

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
            config_error = getattr(self.manager, "config_error", None)
            if config_error:
                return config_error
            now = dt_util.now()
            plan = self.manager.global_plan
            if not plan:
                return "sale_pv"
                
            slot0 = plan.get_slot(0)
            if not slot0:
                return "sale_pv"
                
            raw_mode = slot0.mode
            batt_soc, _, _ = self.manager.get_battery_state(soc_default=100.0)
            min_soc = float(self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0))
            
            use_dp = self.manager.get_setting("use_dp", False)
            if not use_dp:
                # 2. Mode Lock Logic (Relaxed v11.9.361)
                # Only apply strict locking for critical transitions: buy <-> sale_pv_bat
                is_emergency = (raw_mode == "bat_emergency" or batt_soc <= (min_soc - 0.5))
                
                is_critical_pair = (
                    (self._locked_mode == "buy" and raw_mode == "sale_pv_bat") or
                    (self._locked_mode == "sale_pv_bat" and raw_mode == "buy")
                )
                
                if is_critical_pair and not is_emergency and self._mode_lock_until and now < self._mode_lock_until:
                    if self._locked_mode:
                        raw_mode = self._locked_mode
                
                # 3. Mode Change Detection
                if raw_mode != self._locked_mode:
                    # Lock for 10 minutes
                    self._mode_lock_until = now + timedelta(minutes=10)
                    self._locked_mode = raw_mode
            else:
                self._mode_lock_until = None
                self._locked_mode = raw_mode
                
            # Update manager with final mode
            self.manager.current_inverter_mode = self._locked_mode or raw_mode

            # Mirror mapped value if selector entity is configured
            inverter_select = self.manager.get_setting("inverter_modes_select_entity")
            if inverter_select and not use_dp:
                option_val = None
                if self.manager.current_inverter_mode == "buy":
                    option_val = self.manager.get_setting("dp_map_charge", "buy")
                elif self.manager.current_inverter_mode == "sale_pv_bat":
                    option_val = self.manager.get_setting("dp_map_discharge", "sale_pv_bat")
                elif self.manager.current_inverter_mode == "sale_pv":
                    option_val = self.manager.get_setting("dp_map_solar", "sale_pv")
                elif self.manager.current_inverter_mode == "stop_sale":
                    option_val = self.manager.get_setting("dp_map_self_consume", "stop_sale")
                elif self.manager.current_inverter_mode == "no_pv_sale_no_bat":
                    option_val = self.manager.get_setting("dp_map_grid", "no_pv_sale_no_bat")
                if option_val:
                    return option_val

            return self.manager.current_inverter_mode
        except Exception as e:
            _LOGGER.error("Error in InverterOperationModeSensor native_value: %s", e)
            return "sale_pv"

    @property
    def extra_state_attributes(self):
        try:
            config_error = getattr(self.manager, "config_error", None)
            if config_error:
                return {"status": config_error, "error": config_error}
            now = dt_util.now()
            plan = self.manager.global_plan
            if not plan:
                return {"status": "Waiting for plan update"}
                
            slot0 = plan.get_slot(0)
            if not slot0:
                return {"status": "No active slot"}

            # v11.9.750: Use real SOC for current status, simulated for projections
            batt_soc, _, _ = self.manager.get_battery_state()
            
            # Legacy parity attributes
            attrs = {
                "arbitrage_decision": slot0.sell_debug.get("arbitrage_decision", "Нет данных"),
                "buy_decision": slot0.buy_debug.get("strategy_decision", "Нет данных"),
                "sell_decision": slot0.sell_debug.get("strategy_decision", "Нет данных"),
                "power": round_f(slot0.power_ac, 2),
                "charge_amps": round_f(slot0.charge_amps, 2),
                "target_soc": round_f(slot0.target_soc, 1),
                "battery_soc": round_f(float(batt_soc), 1),
                "projected_soc": round_f(slot0.soc_start, 1),
                "mode_reason": slot0.reason,
                "mode_lock_until": self._mode_lock_until.isoformat() if self._mode_lock_until else "None",
                "planned_modes_24h": plan.to_planned_modes_24h(),
                "hourly_data": plan.to_hourly_data_attr(),
                "use_dp": self.manager.get_setting("use_dp", False),
                "heuristic_hourly_data": getattr(self.manager, "heuristic_hourly_data", {}),
                "dp_hourly_data": getattr(self.manager, "dp_hourly_data", {}),
                "forecast_coefficient_blended": round_f(self.manager.last_blended_coeff, 3),
                "plan_last_updated": plan._last_updated.isoformat(),
                "actual_kwh_so_far": round_f(float(self.manager.data.get("temp_daily_gen", 0.0) or 0.0), 2)
            }
            
            # Expected generation
            expected_so_far = 0.0
            try:
                today_str = now.strftime("%Y-%m-%d")
                f_dist = self.manager.get_forecast_hourly_distribution(self.manager.forecast_today_hourly_sensor, today_str)
                if f_dist:
                    expected_so_far = sum(float(v) for h, v in f_dist.items() if int(h) < now.hour)
            except Exception: pass
            attrs["expected_kwh_so_far"] = round_f(expected_so_far, 2)

            # Inverter Change Logging
            mode = self.manager.current_inverter_mode
            curr_params = {
                "mode": mode,
                "power": round_f(slot0.power_ac, 1),
                "target_soc": round_f(slot0.target_soc, 1),
                "charge_amps": round_f(slot0.charge_amps, 1),
            }
            
            has_changed = (
                curr_params["mode"] != self._last_logged_params.get("mode") or
                abs(float(curr_params["power"]) - float(self._last_logged_params.get("power", 0.0))) >= 0.1 or
                abs(float(curr_params["target_soc"]) - float(self._last_logged_params.get("target_soc", 0.0))) >= 0.1 or
                abs(float(curr_params["charge_amps"]) - float(self._last_logged_params.get("charge_amps", 0.0))) >= 0.5 or
                now.hour != self._last_logged_hour
            )
            
            if has_changed:
                log_tag = "[Inverter Status]" if now.hour != self._last_logged_hour and curr_params["mode"] == self._last_logged_params.get("mode") else "[Inverter Change]"
                old_mode = self._last_logged_params.get("mode", "initial")
                log_msg = f"{log_tag} {old_mode} -> {mode} | SOC: {slot0.soc_start:.1f}% | Power: {slot0.power_ac:.1f} kW | Target SOC: {slot0.target_soc:.1f}% | Amps: {slot0.charge_amps:.1f} A | Reason: {slot0.reason}"
                _LOGGER.warning(log_msg)
                self.manager.log_to_file(log_msg)
                self._last_logged_params = curr_params
                self._last_logged_hour = now.hour

            attrs["strategy_version"] = VERSION
            attrs["server_today"] = now.strftime("%Y-%m-%d")
            return attrs
        except Exception as e:
            _LOGGER.error("Error in InverterOperationModeSensor extra_state_attributes: %s", e)
            return {"error": str(e)}

    def _get_mode_at(self, dt_now, batt_soc, is_forecast=False, abs_hour=None, avg_gen_override=None, avg_load_override=None):
        """Calculates the inverter mode for a given timestamp and SOC."""
        mode = "sale_pv" # default
        # v11.6.63: now_wall MUST be the real wall clock time, NOT the forecast time.
        # Using dt_now here caused _now_h_for_forecast = forecast_hour (e.g. 19),
        # making check_h_abs == _now_h_for_forecast always True for the target hour,
        # which then used sell_strategy.get("state") == "active" (False at 11:00) 
        # instead of the correct active_hours lookup → is_selling_active always False → sale_pv.
        now_wall = dt_util.now()
        now_h_wall = now_wall.hour

        # 0. Check for HOURLY Manual Overrides (v11.9.370 Premium)
        ts_key = dt_now.strftime("%Y-%m-%d %H:00")
        h_override = self.manager.hourly_manual_overrides.get(ts_key)
        if h_override:
            return h_override["mode"], f"Manual Override ({ts_key})", None, None

        # v11.9.333: Manual Overrides support (Auto-reset at hour change)
        if not is_forecast:
            # Clear old overrides if hour changed
            if self.manager._last_override_hour != -1 and self.manager._last_override_hour != now_h_wall:
                self.manager.manual_mode_overrides = {}
                self.manager._last_override_hour = -1
            
            manual_override = self.manager.manual_mode_overrides.get(now_h_wall)
            if manual_override:
                # v11.9.334: Don't return early, just remember the override and continue 
                # to calculate bms_debug/diagnostics for the UI card.
                pass
        
        # v11.4.21: Fix date and hour alignment for forecast
        # today_str MUST be relative to the simulated time (dt_now)
        today_str = dt_now.strftime("%Y-%m-%d")
        sim_h = dt_now.hour
        
        # Calculate relative hour from simulation start for indexing into strategy results
        # This prevents the 'Ghost Tomorrow' issue where indices and hours mismatch.
        now_h_start = now_wall.replace(minute=0, second=0, microsecond=0)
        dt_h_start = dt_now.replace(minute=0, second=0, microsecond=0)
        rel_h = int((dt_h_start - now_h_start).total_seconds() // 3600)
        
        # Target check hour (use rel_h for strategy alignment, sim_h for price alignment)
        check_h_rel = rel_h
        # v11.6.10: Use abs_hour if provided (fixes tomorrow's forecast seeing today's peaks)
        check_h_abs = sim_h if abs_hour is None else abs_hour

        try:
            from .const import CONF_PRICE_STOP_SELL, CONF_PRICE_SELL_ONLY_PV, CONF_SALE_PV_NO_BAT_MAX_HOUR, CONF_PRICE_SELL_LIMIT, CONF_MIN_SOC_BAT, CONF_AI_DISCHARGE_LIMIT, CONF_AI_CHARGE_LIMIT
            price_stop_sell = self.manager.get_setting(CONF_PRICE_STOP_SELL, 0.0)
            price_sell_only_pv = self.manager.get_setting(CONF_PRICE_SELL_ONLY_PV, 999.0)
            sale_pv_no_bat_max_hour = self.manager.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0)
            price_sell_limit = self.manager.get_setting(CONF_PRICE_SELL_LIMIT, 5.0)
        except ImportError:
            price_stop_sell = 0.0
            price_sell_only_pv = 999.0
            sale_pv_no_bat_max_hour = 13.0
            price_sell_limit = 5.0

        min_soc = self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0)
        # Use absolute hour of simulated date for price
        cur_price = self.manager.get_price("sell", today_str, sim_h)

        # Strategy results
        sell_strategy = self.manager.get_market_strategy("sell", allow_recalc=False) or {}
        buy_strategy = self.manager.get_market_strategy("buy", allow_recalc=False) or {}
        
        # When forecasting, we use absolute hours to match strategy indices (v11.4.20)
        if is_forecast:
            # v11.6.10: check_h_abs is now correctly absolute, so no +24 hack is needed
            # v11.6.54: For the CURRENT wall-clock hour in the forecast, use real-time state
            # (respects Safety Block). For future hours, use active_hours as before.
            # This prevents planned_modes_24h from showing sale_pv_bat at the current hour
            # while the actual mode stays sale_pv due to Safety Block firing.
            _now_h_for_forecast = now_wall.hour
            if check_h_abs == _now_h_for_forecast:
                is_selling_active = sell_strategy.get("state") == "active"
                is_buying_active = buy_strategy.get("state") == "active"
            else:
                _active_h_raw = sell_strategy.get("active_hours", [])
                is_selling_active = check_h_abs in _active_h_raw
                is_buying_active = check_h_abs in buy_strategy.get("active_hours", [])
        else:
            is_selling_active = sell_strategy.get("state") == "active"
            is_buying_active = buy_strategy.get("state") == "active"

        # SOC and Capacity
        _, batt_cap, _ = self.manager.get_battery_state(soc_default=100.0)

        # Peak preparation logic
        is_preparing_for_peak = False
        target_hours_sell = sell_strategy.get("active_hours", [])
        peak_start_abs = sell_strategy.get("next_peak_h") # v11.9.104 fix
        if peak_start_abs is None:
            for h in sorted(target_hours_sell):
                if h > check_h_abs:
                    peak_start_abs = h
                    break
        
        bms_debug = {"status": "Ожидание" if not is_forecast else "Прогноз"}
        
        # v11.1.61: Differentiate target by current strategic mode for diagnostics
        buy_p_cur = self.manager.get_price("buy", today_str, sim_h)
        is_neg_buy = bool(buy_p_cur is not None and buy_p_cur <= 0.0)
        # Target SOC Logic for diagnostics
        # Target SOC Logic for diagnostics (v11.8.528: Corrected charge target)
        ai_discharge_limit = self.manager.get_setting(CONF_AI_DISCHARGE_LIMIT, 100.0)
        ai_charge_limit = self.manager.get_setting(CONF_AI_CHARGE_LIMIT, 100.0)
        
        # Use AI Charge Limit (target to fill) for peak preparation
        active_target = ai_charge_limit
        if is_neg_buy:
            active_target = ai_charge_limit
        
        # Determine if we are in "Buy" strategic mode
        if is_buying_active:
            active_target = ai_charge_limit

        # v11.8.526: Always run simulation if a peak exists to provide accurate debug_soc_at_peak
        if not is_forecast and batt_cap > 0:
            end_h = peak_start_abs if peak_start_abs is not None else (now_h_wall + 24)
            sim_range = [h for h in range(now_h_wall, end_h) if h < 48]
            sim_soc, sim_log, _ = self.manager.strategy_engine.run_soc_simulation(
                batt_soc, sim_range, now_wall,
                mode_overrides=getattr(self.manager, "planned_mode_overrides", None)
            )
            
            if batt_soc >= (active_target - 0.5):
                bms_debug["status"] = "Батарея уже заряжена"
                bms_debug["target_soc"] = active_target
                bms_debug["current_soc"] = batt_soc
            else:
                ever_fully_charged = any(
                    (val.get("soc", 0.0) if isinstance(val, dict) else val) >= (ai_discharge_limit - 0.5) 
                    for val in sim_log.values()
                )
                total_needed = 0
                for i, val in enumerate(sim_log.values()):
                    val_soc = val.get("soc", 0.0) if isinstance(val, dict) else val
                    if val_soc >= (ai_discharge_limit - 0.5):
                        total_needed = i + 1
                        break
                
                if peak_start_abs is not None:
                    # v11.6.56: Only prepare for peak if peak price is higher than current price
                    # to avoid blocking profitable sales now for less profitable peaks later.
                    peak_price = self.manager.get_price("sell", today_str, peak_start_abs % 24)
                    cur_p = cur_price if cur_price is not None else 0.0
                    
                    if peak_price is not None and peak_price > (cur_p + 0.1): # 0.1 margin
                        if not ever_fully_charged:
                            is_preparing_for_peak = True
                            bms_debug["status"] = "Внимание: АКБ не успеет зарядиться к Пику!"
                        else:
                            latest_start = peak_start_abs - total_needed
                            if now_h_wall < latest_start:
                                bms_debug["status"] = f"Зарядка отложена (хватит {total_needed}ч)"
                            else:
                                is_preparing_for_peak = True
                                bms_debug["status"] = "Штатный заряд к пику"
                    else:
                        p_p_disp = f"{peak_price:.2f}" if peak_price is not None else "N/A"
                        bms_debug["status"] = f"Продажа выгоднее ({cur_p:.2f} >= {p_p_disp})"
                
                # Debug attributes moved to end of function (v11.8.525)

        # State Machine
        reason = "Значения по умолчанию"
        fixed_buy = self.manager.fixed_strategy_data["buy"]
        fixed_sell = self.manager.fixed_strategy_data["sell"]
        
        # Pre-calculate common conditions
        # v11.7.72: Re-synced with user preference (5m averages)
        avg_load = self.manager.avg_load_5m_kw if not is_forecast else (avg_load_override if avg_load_override is not None else 0.5)
        avg_gen = self.manager.avg_gen_5m_kw if not is_forecast else (avg_gen_override if avg_gen_override is not None else 0.0)
        has_surplus = bool(avg_gen > (avg_load + 0.05))
        is_before_limit_hour = bool(sim_h < sale_pv_no_bat_max_hour) # v11.4.20: Fixed limit comparison
        limit_hour = int(sale_pv_no_bat_max_hour)

        # State Machine Ladder
        # v11.1.22: For Negative Prices, always use 'buy' mode to power house from grid
        buy_p_cur = self.manager.get_price("buy", today_str, sim_h)
        is_neg_buy = bool(buy_p_cur is not None and buy_p_cur <= 0.0)

        # v11.6.22: Use strategy engine's precise survival simulation instead of rough heuristics
        # Removed avg_gen > 0.01 requirement because curtailment or clouds could falsely drop the mode
        is_waiting_for_neg = False
        neg_h = buy_strategy.get("first_negative_hour")
        can_wait = buy_strategy.get("can_wait_for_negative", False)
        
        # Безопасный отладочный вывод (теперь после определения переменных)
        bms_debug["debug_can_wait"] = can_wait
        bms_debug["debug_neg_h"] = neg_h
        bms_debug["debug_soc_at_neg"] = buy_strategy.get("debug_soc_at_neg", "N/A")
        bms_debug["debug_threshold"] = buy_strategy.get("debug_threshold", "N/A")
        bms_debug["debug_cur_price"] = cur_price
        
        # We drop the cur_price < price_sell_only_pv condition here.
        # If we can wait for a negative price, we MUST block charging. 
        # Inside the ladder, we will decide whether to sell PV or just wait.
        # v11.6.568: Night Guard. Don't block charging if history says there's no PV anyway.
        # This prevents the mode from sticking at night due to sensor noise (e.g. 0.06 kW).
        is_gen_night = False
        try:
            prof_gen = self.manager.get_average_profile("generation", self.manager.custom_period, "all")
            is_gen_night = float(prof_gen.get(str(sim_h), 0.0)) < 0.01
        except Exception: pass

        if can_wait and neg_h is not None and not is_gen_night:
            # v11.9.165: Extended to include the negative hours themselves to prevent solar charging
            last_neg_h = buy_strategy.get("last_negative_hour") or neg_h
            if not is_forecast or check_h_abs <= last_neg_h:
                # 1. Check if there are any planned AI sales between now and the negative price
                planned_sales = [h for h in sell_strategy.get("active_hours", []) if check_h_abs <= h < neg_h]
                if not planned_sales:
                    is_waiting_for_neg = True

        # State Machine Ladder (v11.9.691: Re-ordered by TS 4.1 Priority)
        
        # Priority 1: Emergency SOC management (Survival First)
        if batt_soc <= min_soc:
            mode = "bat_emergency"
            reason = f"Заряд ({round_f(batt_soc, 1)}%) <= Минимума ({min_soc}%): Ожидание добора"

        # Priority 2: Buying (Strictly restricted to active charging window)
        elif is_buying_active and (
            (buy_strategy.get("is_charging_now") or is_neg_buy) if not is_forecast 
            else (check_h_abs in buy_strategy.get("active_hours", []) or is_neg_buy)
        ):
            mode = "buy"
            reason = "Активна стратегия ПОКУПКИ"

        # Priority 3: AI / Arbitrage Strategy (sale_pv_bat)
        # v11.9.691: Elevated priority to override Morning Mode heuristic
        elif is_selling_active:
            mode = "sale_pv_bat"
            reason = "Активна стратегия ПРОДАЖИ (AI)"

        # Priority 4: Morning Mode / Solar Surplus Heuristics
        elif cur_price is not None and cur_price >= price_sell_only_pv:
            # SAFE MORNING MODE (User's 4 conditions)
            man = self.manager
            morning_soc_proj = (sell_strategy.get("sell_simulation") or {}).get("projected_soc_morning_pct", 0.0)
            target_morning = (sell_strategy.get("arbitrage_buyback") or {}).get("target_morning_soc_pct", (min_soc + 5.0))
            
            hys = 0.5 if mode == "sale_pv_no_bat" else 0.0
            is_low_for_morning = bool(morning_soc_proj < (target_morning + hys))
            hit_full_before = (sell_strategy.get("sell_simulation") or {}).get("hit_full_before", False)
            latest_charge_start = (sell_strategy.get("sell_simulation") or {}).get("latest_charge_start", sim_h)
            
            is_profitable_to_save = False
            if peak_start_abs is not None:
                deg_cost = self.manager.get_setting("degradation_cost", 0.15)
                peak_p = self.manager.get_price("sell", today_str, peak_start_abs % 24) or 0.0
                cur_p = cur_price or 0.0
                if (peak_p - deg_cost) > cur_p and (sim_soc if 'sim_soc' in locals() else batt_soc) < 90.0:
                    is_profitable_to_save = True
            
            is_energy_low_for_evening = bool((is_preparing_for_peak or is_low_for_morning or is_profitable_to_save) and not hit_full_before)
            
            is_throttled = False
            if not is_forecast:
                is_throttled = bool(sell_strategy.get("recommended_power_kw", 0.0) < 0.01 and dt_now.hour in sell_strategy.get("active_hours", []))
            
            if is_throttled or is_energy_low_for_evening:
                is_preparing_for_peak = True

            _need_charge_for_morning = bool(is_low_for_morning)
            _need_charge_for_peak = bool((is_preparing_for_peak or is_profitable_to_save) and not hit_full_before)
            
            # v11.9.703: Dynamic Sale PV window. 
            # We block ONLY if we have passed the "Latest Charge Start" hour.
            _block_sale_pv_no_bat = bool(sim_h >= latest_charge_start)

            if is_before_limit_hour and has_surplus and not _block_sale_pv_no_bat and cur_price > 0:
                mode = "sale_pv_no_bat"
                reason = f"Продажа только солнца: Цена ({cur_price or 0.0:.2f}) >= Порога ({price_sell_only_pv or 0.0:.2f}), утро, есть излишек и запас энергии"
            else:
                mode = "sale_pv"
                if is_throttled or is_energy_low_for_evening:
                    if is_throttled: reason = "Продажа АКБ ограничена AI"
                    elif is_low_for_morning: reason = f"Защита Gatekeeper: Рассвет {morning_soc_proj:.1f}% < {target_morning:.1f}%"
                    elif is_energy_low_for_evening and sell_strategy.get("morning_autopilot_active"):
                         prefix = "Продажа" if mode == "sale_pv_bat" else "Питание дома"
                         sun_note = " (мало солнца)" if not hit_full_before else ""
                         reason = f"{prefix} до {sell_strategy.get('morning_autopilot_floor')}% (защита Gatekeeper{sun_note})"
                    elif peak_start_abs is not None and peak_start_abs < 48:
                         reason = f"Подготовка к Пику {self.manager.strategy_engine._format_h(peak_start_abs)}"
                    elif is_profitable_to_save:
                         reason = "Сохранение заряда: Пик выгоднее текущей цены"
                    else: reason = "Экономия заряда: Дефицит до рассвета"
                elif _block_sale_pv_no_bat: reason = f"Окно продажи PV закрыто: Начало плановой зарядки (лимит {latest_charge_start}:00)"
                else: reason = "Стандартная работа (ожидание излишков или команды AI)"
        
        # Priority 5: Wait for negative price
        elif is_waiting_for_neg:
            # v11.6.567 - Priority 3: Wait for negative price
            # We ONLY wait if there's actually something to wait for (solar presence or daytime)
            # v11.9.108/v11.9.109: Added 500W hysteresis for wait mode to avoid toggling
            # Drop to sale_pv ONLY if house has significant deficit (load > gen + 0.5kW)
            has_significant_deficit = bool(avg_load > (avg_gen + 0.5))
            
            can_sell_pv = False
            if cur_price is not None and cur_price >= price_sell_only_pv and has_surplus:
                if is_before_limit_hour and cur_price > 0:
                    can_sell_pv = True
            
            if can_sell_pv:
                mode = "sale_pv_no_bat"
                reason = f"Продажа только солнца: Цена ({cur_price:.2f}) >= Порога ({price_sell_only_pv:.2f}) (Ожидаем отриц. цену)"
            elif not has_significant_deficit:
                mode = "no_pv_sale_no_bat"
                neg_h_disp = neg_h if neg_h < 24 else f"{neg_h-24} (Завтра)"
                reason = f"Ожидание отриц. цен ({neg_h_disp}г): Экономим место в АКБ"
            else:
                # v11.6.567/v11.9.109: Fallback to sale_pv if house needs significant power
                mode = "sale_pv"
                reason = "Ожидание отриц. цен: Нагрузка > генерации на 500Вт (sale_pv)"

        # v11.9.106: Global Price Floor (TS 4.1 Priority 3)
        # Moved above AI and Morning logic to ensure it works even without peaks.
        elif cur_price is not None and cur_price < price_stop_sell:
            mode = "stop_sale"
            reason = f"Продажа заблокирована: Цена ({cur_price:.2f}) < Порога ({price_stop_sell:.2f})"

        elif is_selling_active:
            # Active AI / Arbitrage strategy
            mode = "sale_pv_bat"
            reason = "Активна стратегия ПРОДАЖИ (AI)"
            
        # v11.9.106: Day/Morning analysis works even without surplus or peaks to provide Gatekeeper protection visibility.
            
        elif cur_price is None:
            # v11.6.65: Handle missing future prices gracefully
            mode = "sale_pv"
            reason = "Нет данных о цене (завтра?)"
            
        # Standard daytime operation
        else:
            mode = "sale_pv"
            reason = f"Стандартная работа: Цена ({cur_price:.2f}) выше порога остановки ({price_stop_sell:.2f})"

        # v11.9.334: Apply manual override if active
        manual_override = self.manager.manual_mode_overrides.get(now_h_wall) if not is_forecast else None
        if manual_override:
            mode = manual_override
            reason = f"Manual Override: {manual_override}"
            bms_debug["status"] = "Ручное управление"

        # v11.9.333: Finalize debug attributes for always-on visibility (TS 199)
        bms_debug["v"] = VERSION
        bms_debug["limit_h"] = limit_hour if 'limit_hour' in locals() else "N/A"
        bms_debug["proj_morning"] = round_f(morning_soc_proj, 1) if 'morning_soc_proj' in locals() else "N/A"

        if peak_start_abs is not None:
            h_disp = f"{peak_start_abs % 24:02d}:00" + (" (Завтра)" if peak_start_abs >= 24 else "")
            bms_debug["next_peak"] = h_disp
            # Use sim_soc if available (always should be in v526+), fallback to batt_soc
            proj_soc = sim_soc if 'sim_soc' in locals() else batt_soc
            bms_debug["soc_at_peak"] = round_f(proj_soc, 1)
            
            p_at_p = self.manager.get_price("sell", today_str, peak_start_abs % 24)
            bms_debug["price_at_peak"] = round_f(p_at_p, 3) if p_at_p is not None else "N/A"
        else:
            bms_debug["next_peak"] = "Нет"
            bms_debug["soc_at_peak"] = "N/A"
            bms_debug["price_at_peak"] = "N/A"

        return mode, reason, bms_debug, peak_start_abs

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
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in ("unknown", "unavailable"):
            val = normalize_float(last_state.state)
            # Recover into manager if it hasn't accumulated anything since restart
            if self.ptype == "consumption" and (self.manager.current_consumption_base or 0) == 0:
                self.manager.current_consumption_base = val
                # v11.1.14/58 - Restore true total from attributes to reconstruct the deduction
                total_val = normalize_float(last_state.attributes.get("total_consumption"))
                if total_val >= val:
                    self.manager.current_consumption_total = total_val
                    self.manager.current_hourly_deduct = round_f((total_val or 0.0) - (val or 0.0), 3)
                else:
                    self.manager.current_consumption_total = val # Fallback
                    
            if self.ptype == "generation" and self.manager.current_generation == 0:
                self.manager.current_generation = val
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def native_value(self):
        if self.ptype == "consumption":
            return round_f(self.manager.current_consumption_base, 3)
        return round_f(self.manager.current_generation, 3)

    @property
    def extra_state_attributes(self):
        if self.ptype == "consumption":
            return {
                "total_consumption": round_f(self.manager.current_consumption_total, 3)
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
        return round_f(sum(profile.values()), 3)

    @property
    def extra_state_attributes(self):
        query_type = "consumption_base" if self.ptype == "consumption" else self.ptype
        profile = self.manager.get_todays_profile(query_type)

        if self.ptype == "consumption":
            total_profile = self.manager.get_todays_profile("consumption_total")
            return {
                "base_profile": profile,
                "total_profile": total_profile,
                "total_daily_sum": round_f(sum(total_profile.values()), 3)
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
        return round_f(self._state, 3)

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
                    return round_f(float(v if v is not None else default), 3)
                except (TypeError, ValueError):
                    return round_f(float(default), 3)

            self._state = float(res.get("initial_budget", 0.0) or 0.0)
            self._attrs = {
                "permissions": res.get("permissions", {}),
                "permissions_reasons": res.get("permissions_reasons", {}),
                "forecast_remaining_adjusted_kwh": _sr(res.get("forecast_val")),
                "battery_energy_kwh": _sr(res.get("batt_energy_val")),
                "expected_consumption_kwh": _sr(res.get("expected_consumption")),
                "expected_base_load_kw": _sr(float(res.get("expected_consumption") or 0.0) / (12.5 if 12.5 > 0 else 1)), # Approximate average load
                "forecast_coefficient": _sr(res.get("forecast_coefficient", 1.0), 1.0),
                "forecast_coefficient_today": _sr(res.get("forecast_today_coefficient", 1.0), 1.0),
                "occupancy_coefficient": _sr(res.get("occupancy_coefficient", 1.0), 1.0),
                "efficiency_coefficient": _sr(res.get("efficiency_coefficient", 1.0), 1.0),
                "survival_floor": self.manager.strategy_engine.get_survival_floor(dt_util.now().hour, (self.manager.get_sunrise_hour() or 8) + (24 if dt_util.now().hour >= 4 else 0)),
                "current_battery_soc": _sr(self.manager.get_battery_state()[0]),
                "projected_morning_soc": _sr(res.get("projected_morning_soc"))
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
        res = self.manager.get_market_strategy(self.mode, allow_recalc=False)
        # v11.9.429: Prioritize descriptive text for the main dashboard
        return res.get("power_decision", res.get("state", "idle"))

    @property
    def extra_state_attributes(self):
        res = self.manager.get_market_strategy(self.mode, allow_recalc=False)
        _LOGGER.debug("[UI Debug] Strategy %s res keys: %s", self.mode, list(res.keys()))
        if "limit_used" in res:
            _LOGGER.debug("[UI Debug] Strategy %s limit_used: %s", self.mode, res["limit_used"])

        now = dt_util.now()
        cur_hour = now.hour

        def safe_round(val):
            return round_f(normalize_float(val), 3)

        today_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res.get("today_prices", {}).items(), key=lambda item: int(item[0])) if int(k) >= cur_hour}
        tom_fmt = {f"{int(k):02d}:00": safe_round(v) for k, v in sorted(res.get("tomorrow_prices", {}).items(), key=lambda item: int(item[0]))}

        attrs = {
            "strategy_version": VERSION,
            "analyzed_window": res.get("analyzed_window", "Неизвестно"),
            "price_limit_used": safe_round(res.get("limit_used")),
            "discharge_limit_used": safe_round(res.get("discharge_limit", 20.0)),
            "charge_limit_used": safe_round(res.get("charge_limit", 100.0)),
            "target_price": safe_round(res.get("target_price")),
            "arbitrage_profit_threshold": res.get("profit_threshold", 0.0),
            "deg_cost": res.get("deg_cost", 0.0),
            "strategy_candidates": res.get("strategy_candidates", []),
            "active_periods": res.get("active_periods", ""),
            "double_cycle_opportunity": res.get("multi_cycle", "Не предвидится"),
            "arbitrage_decision": res.get("arbitrage_decision", "Нет данных"),
            "strategy_decision": res.get("strategy_decision", "Нет данных"),
            "gatekeeper_floor": res.get("gatekeeper_floor", 0.0),
            "planned_power": {h: f"{d['power']} кВт ({d['reason'] if d.get('reason') else f'Цель: {d['soc']}%'})" if isinstance(d, dict) else d for h, d in res.get("planned_power_per_h", {}).items()},
            "power_decision": res.get("power_decision", "Ожидание"),
        }

        # v11.9.587: Move SOC projections up (before prices)
        if self.mode == "sell":
            attrs.update({
                "projected_soc_at_sale_start": res.get("sell_simulation", {}).get("projected_soc_at_sale_start_pct", 0.0),
                "projected_soc_after_sale": res.get("sell_simulation", {}).get("projected_soc_after_sale_pct", 0.0),
                "projected_soc_morning": res.get("sell_simulation", {}).get("projected_soc_morning_pct", 0.0),
                "projected_soc_morning_base": res.get("sell_simulation", {}).get("projected_soc_morning_base_pct", 0.0),
            })
        else: # buy
            attrs.update({
                "projected_soc_at_buy_start": res.get("buy_simulation", {}).get("projected_soc_at_start_pct", 0.0),
                "projected_soc_at_end": res.get("buy_simulation", {}).get("projected_soc_at_end_pct", 0.0),
                "projected_soc_morning": res.get("buy_simulation", {}).get("projected_soc_morning_pct", 0.0),
                "projected_soc_morning_base": res.get("buy_simulation", {}).get("projected_soc_morning_base_pct", 0.0),
            })

        attrs["prices_today"] = today_fmt
        attrs["prices_tomorrow"] = tom_fmt

        # v7.2 - Hide detailed projections if nothing is planned for today
        # (reduces clutter and avoids 'nonsense' values for future/past windows)
        is_active = res.get("state") == "active"
        has_planned = bool(res.get("active_hours", []))
        show_extra = is_active or has_planned

        b_coeff = round_f(float(getattr(self.manager, "last_blended_coeff", 1.0)), 3)
        if self.mode == "sell":
            attrs["sell_debug"] = res.get("arbitrage_sell_debug", "Нет данных")
            attrs.update({
                "projected_soc_at_sale_start": res.get("sell_simulation", {}).get("projected_soc_at_sale_start_pct", 0.0),
                "projected_soc_after_sale": res.get("sell_simulation", {}).get("projected_soc_after_sale_pct", 0.0),
                "projected_soc_morning": res.get("sell_simulation", {}).get("projected_soc_morning_pct", 0.0),
                "arbitrage_buyback_power": res.get("arbitrage_buyback", {}).get("power_kw", 0.0),
                "arbitrage_buyback_note": res.get("arbitrage_buyback", {}).get("note", ""),
                "arbitrage_sunrise_hour": res.get("arbitrage_buyback", {}).get("sunrise_hour", 0),
                "arbitrage_available_kwh": res.get("arbitrage_buyback", {}).get("available_kwh", 0.0),
                "arbitrage_reserve_kwh": res.get("arbitrage_buyback", {}).get("reserve_kwh", 0.0),
                "arbitrage_energy_to_wait_kwh": res.get("arbitrage_buyback", {}).get("energy_to_wait_kwh", 0.0),
                "arbitrage_limit_reason": res.get("arbitrage_sell_limit_reason", ""),
                "arbitrage_gatekeeper_floor": res.get("gatekeeper_floor", 0.0),
                "sim_log": res.get("sell_simulation", {}).get("sim_log", ""),
                "blended_coeff": b_coeff
            })
        else: # buy
            attrs.update({
                "projected_soc_at_buy_start": res.get("buy_simulation", {}).get("projected_soc_at_start_pct", 0.0),
                "projected_soc_at_end": res.get("buy_simulation", {}).get("projected_soc_at_end_pct", 0.0),
                "projected_soc_morning": res.get("buy_simulation", {}).get("projected_soc_morning_pct", 0.0),
                "projected_soc_morning_base": res.get("buy_simulation", {}).get("projected_soc_morning_base_pct", 0.0),
                "gatekeeper_floor": res.get("gatekeeper_floor", 0.0),
                "buy_debug": res.get("buy_debug", "Нет данных"),
                "debug_eff": res.get("buy_simulation", {}).get("eff", 0.0),
                "debug_b_cap": res.get("buy_simulation", {}).get("b_cap", 0.0),
                "debug_needed_kwh": res.get("buy_simulation", {}).get("needed_kwh_dc", 0.0),
                "debug_max_p": res.get("buy_simulation", {}).get("max_p", 0.0),
                "debug_planned_kwh": res.get("buy_simulation", {}).get("p_total_planned", 0.0),
                "sim_log": res.get("buy_simulation", {}).get("sim_log", ""),
                "blended_coeff": b_coeff
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
            monthly[m] = round_f(monthly.get(m, 0.0) + v.get(cat, 0.0), 4)

        daily = {}
        for i in range(30):
            d_str = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            val = _day(d_str)
            if val > 0 or d_str == today_str:
                daily[d_str] = round_f(val, 4)

        monthly_sorted = sorted(monthly.items())
        # Slicing via loop to avoid linter confusion with SupportsIndex
        recent_monthly = []
        si = max(0, len(monthly_sorted) - 13)
        for i in range(len(monthly_sorted)):
            if i >= si:
                recent_monthly.append(monthly_sorted[i])
        
        return {
            "today":          round_f(today_val,     4),
            "yesterday":      round_f(yesterday_val, 4),
            "last_7_days":    round_f(last7,   4),
            "last_30_days":   round_f(last30,  4),
            "this_month":     round_f(this_month, 4),
            "last_month":     round_f(last_month, 4),
            "monthly_totals": {str(k): round_f(float(v), 2) for k, v in recent_monthly},
            "daily_history":  dict(sorted(daily.items())),
        }

    @property
    def native_value(self):
        return round_f(self._get_summary().get("last_30_days", 0.0), 2)

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
                "solar_benefit_today":     round_f(t_data.get("solar", 0.0), 4),
                "arbitrage_benefit_today": round_f(t_data.get("arbitrage", 0.0), 4),
                "sell_benefit_today":      round_f(t_data.get("sell", 0.0), 4),

                "solar_benefit_yesterday":     round_f(y_data.get("solar", 0.0), 4),
                "arbitrage_benefit_yesterday": round_f(y_data.get("arbitrage", 0.0), 4),
                "sell_benefit_yesterday":      round_f(y_data.get("sell", 0.0), 4),
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
            "today":      round_f(today_val, 2),
            "yesterday":  round_f(yesterday_val, 2),
            "week":       round_f(last_7_days, 2),
            "month":      round_f(last_30_days, 2),
            "lifetime":   round_f(total_balance, 2),
        }

    @property
    def native_value(self):
        # The sensor state shows the real-time 'Today' balance.
        return self._get_balance_summary()["today"]

    @property
    def extra_state_attributes(self):
        s = self._get_balance_summary()
        dbg = self.manager.data.get("wallet_debug", {})
        return {
            "today_earnings": s["today"],
            "grid_import_kw": round_f(dbg.get("grid_imp", 0.0), 3),
            "grid_export_kw": round_f(dbg.get("grid_exp", 0.0), 3),
            "last_step_gain": round_f(dbg.get("step", 0.0), 6),
            "price_buy": dbg.get("p_buy", 0.0),
            "price_sell": dbg.get("p_sell", 0.0),
            "dt_h": round_f(dbg.get("dt_h", 0.0), 6),
            "lifetime_all_time": s["lifetime"],
            "last_update": datetime.fromtimestamp(self.manager.data.get("last_balance_poll_time", 0)).isoformat() if self.manager.data.get("last_balance_poll_time") else None,
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
        return round_f(score, 2)

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
            "expected_kw": round_f(expected, 3),
            "actual_kw": round_f(actual_kw, 3),
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
        return round_f(roi, 2)

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
                payback_years_upgrade = round_f(float(battery_cost / (extra_monthly * 12)), 2)
                roi_upgrade = round_f(float(((extra_monthly * 12) / battery_cost) * 100), 1)
        except Exception:
            pass

        return {
            "total_investment": f"{total_cost} {self._currency}",
            "cumulative_savings": f"{round_f(float(total_saved or 0.0), 2)} {self._currency}",
            "remaining_amount": f"{round_f(float(remaining or 0.0), 2)} {self._currency}",
            "average_daily_saving": f"{round_f(float(avg_daily or 0.0), 2)} {self._currency}",
            "estimated_payback_days": days_rem if total_cost > 0 else "N/A",
            "estimated_payback_date": payback_date if total_cost > 0 else "N/A",
            "simulation_days": sim_batt_double.get("days_simulated", 0),
            "upgrade_batt_cap_kwh": round_f(float(batt_cap or 0.0), 2),
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
        return round_f(self.manager.get_battery_degradation_cost(), 4)

    @property
    def extra_state_attributes(self):
        cost_per_kwh = self.manager.get_battery_degradation_cost()
        min_p = self.manager.get_setting(CONF_ARBITRAGE_PROFIT_THRESHOLD, 0.0)
        threshold = min_p if min_p >= cost_per_kwh else (2 * cost_per_kwh)

        batt_cost = self.manager.get_setting(CONF_BATTERY_COST, 0.0)
        cycles = self.manager.get_setting(CONF_BATTERY_RATED_CYCLES, 6000)

        return {
            "wear_cost_per_kwh_cycle": round_f(cost_per_kwh, 4),
            "arbitrage_profit_threshold": round_f(threshold, 4),
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
        return round_f(self.manager.data.get("temp_daily_waste", 0.0), 3)

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
        coeff = float(getattr(self.manager, "last_blended_coeff", 1.0))
        potential_kw = round_f(max(prof_val * coeff, self.manager.avg_gen_kw), 3)

        # Truncate impossible value (one-time fix for existing corruption)
        if waste_kwh > 50.0 and self.manager.avg_gen_kw < 20.0:
             # If waste is > 50kWh but current gen is normal, something is wrong. 
             # We don't reset fully to not lose history, but we could cap it.
             pass 

        return {
            "current_waste_kw": self.manager.current_solar_waste_power,
            "lost_potential_revenue": round_f(waste_kwh * cur_price, 2),
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
        return round_f(float(hours), 2)

    @property
    def extra_state_attributes(self):
        soc, cap, energy_dc = self.manager.get_battery_state()
        eff = self.manager.get_efficiency_coefficient()
        load_kw = self.manager.avg_load_kw

        # 1. Total Autonomy (to 0%)
        total_hours = (energy_dc * eff) / load_kw if load_kw > 0.005 else 99.0

        # 2. Survival Autonomy (to min_soc_buy)
        min_soc = self.manager.get_setting(CONF_MIN_SOC_BAT, 10.0)
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
            "current_load_avg_kw": round_f(float(load_kw), 3),
            "usable_energy_ac_kwh": round_f(float(energy_dc * eff), 3),
            "reserve_soc_target": min_soc
        }

class GridBalanceSensor(SensorEntity):
    """Real-time grid balance (Import/Export)."""
    def __init__(self, manager, name):
        self.manager = manager
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_grid_balance"
        self._attr_icon = "mdi:transmission-tower"
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
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
        # We try to use the real grid sensor first
        if self.manager.grid_power_sensor:
            st = self.manager.hass.states.get(self.manager.grid_power_sensor)
            val = get_kwh_val(st)
            if val is not None:
                # Return direct value (assuming +import, -export as requested)
                return round_f(float(val), 3)

        # Fallback: calculate from (Gen + Batt - Load)
        load_kw = self.manager.avg_load_kw
        gen_kw = self.manager.avg_gen_kw
        batt_p = 0.0
        if self.manager.battery_power_sensor:
            batt_st = self.manager.hass.states.get(self.manager.battery_power_sensor)
            batt_p = get_kwh_val(batt_st) or 0.0

        # Conv (User): positive is import, negative is export
        balance = load_kw - (gen_kw + batt_p)
        return round_f(float(balance), 3)

    @property
    def extra_state_attributes(self):
        mode = "Calculated" if not self.manager.grid_power_sensor else "Direct Sensor"
        return {
            "measurement_method": mode,
            "sensor_id": self.manager.grid_power_sensor or "None",
            "convention": "Positive = Import, Negative = Export (+беру, -отдаю)"
        }



class PotentialExportTodaySensor(SensorEntity):
    """Calculates potential energy export for today (surplus after 100% SOC)."""
    def __init__(self, manager):
        self.manager = manager
        self._attr_name = "Потенциальный экспорт сегодня"
        self._attr_unique_id = f"{manager.entry.entry_id}_potential_export"
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_icon = "mdi:export"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_suggested_display_precision = 2
        self.entity_id = f"{DOMAIN}.energy_management_potential_export"

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Antigravity AI",
            model="Energy Optimization Engine",
        )

    @property
    def native_value(self):
        budget_res = self.manager.get_budget_and_permissions(self.manager.custom_period)
        return float(budget_res.get("potential_export_kwh", 0.0))

    @property
    def extra_state_attributes(self):
        budget_res = self.manager.get_budget_and_permissions(self.manager.custom_period)
        return {
            "forecast_remaining": budget_res.get("forecast_val", 0.0),
            "expected_consumption": budget_res.get("expected_consumption_kwh", 0.0),
            "battery_to_full": round_f(max(0.0, float(budget_res.get("battery_capacity_kwh", 0.0)) - float(budget_res.get("battery_energy_kwh", 0.0))), 3),
            "sun_overflow": budget_res.get("sun_overflow_kwh", 0.0),
            "battery_surplus": budget_res.get("battery_surplus_kwh", 0.0)
        }

    async def async_added_to_hass(self):
        self.manager.update_listeners.append(self.async_write_ha_state)

    async def async_will_remove_from_hass(self):
        if self.async_write_ha_state in self.manager.update_listeners:
            self.manager.update_listeners.remove(self.async_write_ha_state)



class EnergyDPAdviceSensor(SensorEntity):
    """Hourly advice sensor based on Dynamic Programming optimization."""
    def __init__(self, manager, name):
        self.manager = manager
        self.planner = DPPlanner(manager)
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_dp_advice"
        self._attr_icon = "mdi:brain"
        self.entity_id = f"{DOMAIN}.energy_dp_advice"
        self._state = "Calculating..."
        self._advice = {}
        self._last_run_time = 0
        self._is_calculating = False
        self._last_calc_soc = None
        self._last_calc_hour = -1

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Antigravity AI",
            model="DP Optimizer Advisor",
        )

    @property
    def native_value(self):
        now_h = f"{datetime.now().hour:02d}:00"
        return self._advice.get("plan", {}).get(now_h, {}).get("mode", "Idle")

    @property
    def extra_state_attributes(self):
        return {
            "profitability_score": self._advice.get("best_value", 0.0),
            "hourly_plan": self._advice.get("formatted_plan", {}),
            "calculation_debug": self._advice.get("debug", {}),
            "last_update": datetime.now().strftime("%H:%M:%S")
        }

    async def async_added_to_hass(self):
        if self.manager:
            self.manager.update_listeners.append(self._trigger_update)
            # Initial run
            self._trigger_update()

    async def async_will_remove_from_hass(self):
        if self.manager and self._trigger_update in self.manager.update_listeners:
            self.manager.update_listeners.remove(self._trigger_update)

    def _trigger_update(self):
        """Prepare snapshot in main thread and trigger background worker."""
        if not self.hass: return
        
        t_now = time.time()
        # v11.9.74: Prevent parallel calculations and excessive spam
        if self._is_calculating: return
        if (t_now - self._last_run_time) < 60: return

        # 1. Startup Protection: If battery SOC is not yet fully available, skip calculation
        if self.manager.battery_soc_sensor:
            try:
                st = self.hass.states.get(self.manager.battery_soc_sensor)
                if not st or st.state in ["unavailable", "unknown", "none", ""]:
                    _LOGGER.info("DP Advice: Startup protection active. Battery SOC sensor not ready yet. Skipping calculation.")
                    return
            except Exception as e_st:
                _LOGGER.warning("DP Advice: Error checking SOC sensor during startup: %s", e_st)
                return

        # 2. Startup Protection: If Nord Pool Price Data is not yet parsed, skip calculation
        try:
            prices_buy = self.planner._get_prices("prices_buy")
            if not prices_buy or len(prices_buy) < 12:
                _LOGGER.info("DP Advice: Startup protection active. Nord Pool price data not ready yet. Skipping calculation.")
                return
        except Exception as e_pr:
            _LOGGER.warning("DP Advice: Error checking price data during startup: %s", e_pr)
            return

        # 3. Startup Protection: If Solar Forecast sensors are not yet ready, skip calculation
        if self.manager.forecast_today_sensor:
            try:
                for s in self.manager.forecast_today_sensor:
                    if s:
                        st = self.hass.states.get(s)
                        if not st or st.state in ["unavailable", "unknown", "none", ""]:
                            _LOGGER.info("DP Advice: Startup protection active. Solar forecast sensor '%s' not ready yet. Skipping calculation.", s)
                            return
            except Exception as e_fc:
                _LOGGER.warning("DP Advice: Error checking forecast sensor during startup: %s", e_fc)
                return

        # 4. Startup Protection: If Load/Gen Power sensors are not yet ready, skip calculation
        if self.manager.power_load_sensors:
            try:
                for s in self.manager.power_load_sensors:
                    if s:
                        st = self.hass.states.get(s)
                        if not st or st.state in ["unavailable", "unknown", "none", ""]:
                            _LOGGER.info("DP Advice: Startup protection active. Power load sensor '%s' not ready yet. Skipping calculation.", s)
                            return
            except Exception as e_ld:
                _LOGGER.warning("DP Advice: Error checking power load sensor during startup: %s", e_ld)
                return

        if self.manager.power_gen_sensors:
            try:
                for s in self.manager.power_gen_sensors:
                    if s:
                        st = self.hass.states.get(s)
                        if not st or st.state in ["unavailable", "unknown", "none", ""]:
                            _LOGGER.info("DP Advice: Startup protection active. Power generation sensor '%s' not ready yet. Skipping calculation.", s)
                            return
            except Exception as e_gn:
                _LOGGER.warning("DP Advice: Error checking power generation sensor during startup: %s", e_gn)
                return

        # Capture critical data in main thread where it's safe
        soc, cap, _ = self.manager.get_battery_state()

        # 5. SOC Deadband Filter (Option C): Skip recalculation if SOC change is tiny within the same hour
        # Bypass deadband filter if there is no successful advice yet, or if more than 10 minutes (600s) have passed since last run
        if self._advice and "plan" in self._advice:
            try:
                from datetime import datetime
                current_hour = datetime.now().hour
                if self._last_calc_soc is not None and self._last_calc_hour == current_hour:
                    if abs(t_now - self._last_run_time) < 600:
                        if abs(soc - self._last_calc_soc) < 0.5:
                            return
            except Exception as e_deadband:
                _LOGGER.warning("DP Advice: Error in deadband check: %s", e_deadband)

        snapshot = {
            "soc": soc,
            "capacity": cap,
            "prices_buy": self.planner._get_prices("prices_buy"),
            "prices_sell": self.planner._get_prices("prices_sell")
        }
        
        self.hass.async_add_executor_job(self._update_advice_threaded, snapshot)

    async def _async_on_calc_complete(self):
        """Callback to safely store results and trigger global plan recalculation."""
        import copy
        self.manager.dp_advice_stable = copy.deepcopy(self._advice)
        self.async_write_ha_state()
        await self.manager.async_update_global_plan(force_strategy_recalc=False)

    def _update_advice_threaded(self, snapshot):
        """Threaded DP computation using pre-captured snapshot."""
        self._is_calculating = True
        try:
            res = self.planner.get_dp_advice(snapshot)
            self._advice = res
            self._last_run_time = time.time()
            # Store the SOC that was actually computed successfully
            self._last_calc_soc = snapshot.get("soc")
            try:
                from datetime import datetime
                self._last_calc_hour = datetime.now().hour
            except:
                self._last_calc_hour = -1
            if self.hass:
                self.hass.add_job(self._async_on_calc_complete)
        except Exception as e:
            _LOGGER.exception(f"DP Update error: {e}")
            self._advice = {
                "best_value": 0.0,
                "formatted_plan": {"Ошибка": f"Сбой расчета DP: {str(e)}"},
                "debug": {"error": str(e)},
                "plan": {}
            }
            # Reset tracking on failure to force next run
            self._last_calc_soc = None
            self._last_calc_hour = -1
            if self.hass:
                self.hass.add_job(self._async_on_calc_complete)
        finally:
            self._is_calculating = False


