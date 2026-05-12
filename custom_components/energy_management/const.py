DOMAIN = "energy_management"
VERSION = "v11.9.486"
VERSION_CODE = 1109486

CONF_CONSUMPTION_SENSORS = "consumption_sensors"
CONF_GENERATION_SENSORS = "generation_sensors"
CONF_DEDUCT_SENSORS = "deduct_sensors"
CONF_CUSTOM_PERIOD = "custom_period"
CONF_FORECAST_TODAY_REMAINING = "forecast_today_remaining"
CONF_FORECAST_TODAY_HOURLY = "forecast_today_hourly"
CONF_FORECAST_TOMORROW = "forecast_tomorrow"
CONF_BATTERY_SOC = "battery_soc"
CONF_BATTERY_CAPACITY = "battery_capacity"
CONF_BATTERY_POWER = "battery_power"
CONF_PRICE_BUY = "price_buy"
CONF_PRICE_SELL = "price_sell"
CONF_PRICE_BUY_LIMIT = "price_buy_limit"
CONF_PRICE_SELL_LIMIT = "price_sell_limit"
CONF_PRICE_STOP_SELL = "price_stop_sell"
CONF_PRICE_SELL_ONLY_PV = "price_sell_only_pv"
CONF_BATTERY_MAX_POWER = "battery_max_power"
CONF_AI_CHARGE_LIMIT = "ai_charge_limit_soc"
CONF_AI_DISCHARGE_LIMIT = "ai_discharge_limit_soc"
CONF_MIN_SOC_BAT = "emergency_soc_limit"
CONF_DYNAMIC_SOC_BUY = "dynamic_soc_buy"
CONF_DYNAMIC_SOC_SELL = "dynamic_soc_sell"
CONF_DEDUCT_SETTINGS = "deduct_settings"
CONF_SOC_BUFFER = "soc_buffer"


CONF_POWER_LOAD_SENSORS = "power_load_sensors"
CONF_POWER_GEN_SENSORS = "power_gen_sensors"

CONF_SALE_PV_NO_BAT_MAX_HOUR = "sale_pv_no_bat_max_hour"
CONF_FORCE_MARKET_SELL = "force_market_sell"
CONF_EMERGENCY_SOC_LIMIT = "emergency_soc_limit"
CONF_ARBITRAGE_PROFIT_THRESHOLD = "arbitrage_profit_threshold"

CONF_PRESENCE_SENSORS = "presence_sensors"
CONF_INVERTER_LOSSES_SENSOR = "inverter_losses_sensor"
CONF_GRID_IMPORT_SENSORS = "grid_import_sensors"
CONF_GRID_EXPORT_SENSORS = "grid_export_sensors"

# Investment and ROI settings
CONF_TOTAL_SYSTEM_COST = "total_system_cost"
CONF_BATTERY_COST = "battery_cost"
CONF_BATTERY_RATED_CYCLES = "battery_rated_cycles"
CONF_ANOMALY_THRESHOLD = "anomaly_threshold"

# Deduct/Load specific settings
CONF_POWER_SENSOR = "power_sensor"
CONF_ACTIVE_HOLD_TIME = "active_hold_time"
CONF_IS_CYCLIC = "is_cyclic"
CONF_ONLY_SOLAR = "only_solar_or_negative_price"
CONF_ACTIVE_SENSOR = "active_sensor"
CONF_GRID_POWER = "grid_power"
CONF_PRIORITY = "priority"
CONF_BATTERY_VOLTAGE = "battery_voltage"

# Boiler Optimizer settings
CONF_BOILER_ENABLE = "boiler_enable"
CONF_BOILER_POWER = "boiler_power"
CONF_BOILER_CAPACITY = "boiler_capacity"
CONF_BOILER_TEMP_SENSOR = "boiler_temp_sensor"
CONF_BOILER_DEADLINE = "boiler_deadline"
CONF_BOILER_MIN_TEMP = "boiler_min_temp"
CONF_BOILER_TARGET_TEMP = "boiler_target_temp"
CONF_MIN_SELL_POWER = "min_sell_power"
CONF_MIN_SELL_PRICE = "min_sell_price"
CONF_MAX_ARBITRAGE_HOURS = "max_arbitrage_hours"
CONF_MIN_DISCHARGE_KWH = "min_discharge_kwh"

from dataclasses import dataclass

@dataclass
class InverterModeClass:
    """Defines algorithmic behavior for a specific inverter mode."""
    name: str
    pv_to_house: bool         # Солнце идет на покрытие потребления дома
    charge_from_pv: bool      # Заряд АКБ от солнечных панелей
    charge_from_grid: bool    # Заряд АКБ напрямую из сети
    discharge_to_house: bool  # Разряд АКБ для покрытия потребления дома
    discharge_to_grid: bool   # Разряд АКБ на продажу в сеть (Арбитраж)
    export_pv_to_grid: bool   # Продажа излишков солнца в сеть
    is_grid_bypass: bool      # Питание дома напрямую из сети (байпас)
    curtail_pv: bool          # Принудительное ограничение (зажим) генерации панелей
    calibration_limit_soc: float # Лимит SOC, выше которого генерация не используется для калибровки точности

# Глобальный реестр режимов для симуляции и логики
INVERTER_MODES = {
    "buy": InverterModeClass(
        name="buy",
        pv_to_house=True,
        charge_from_pv=True,
        charge_from_grid=True,
        discharge_to_house=False,
        discharge_to_grid=False,
        export_pv_to_grid=False,
        is_grid_bypass=True,
        curtail_pv=False,
        calibration_limit_soc=100.0  # В байпасе калибровка затруднена, но 100% — безопасный дефолт
    ),
    "no_pv_sale_no_bat": InverterModeClass(
        name="no_pv_sale_no_bat",
        pv_to_house=True,
        charge_from_pv=False,
        charge_from_grid=False,
        discharge_to_house=False,
        discharge_to_grid=False,
        export_pv_to_grid=False,
        is_grid_bypass=False,
        curtail_pv=True,
        calibration_limit_soc=0.0    # Зажим всегда — калибровка невозможна
    ),
    "sale_pv_no_bat": InverterModeClass(
        name="sale_pv_no_bat",
        pv_to_house=True,
        charge_from_pv=False,
        charge_from_grid=False,
        discharge_to_house=False,
        discharge_to_grid=False,
        export_pv_to_grid=True,
        is_grid_bypass=False,
        curtail_pv=False,
        calibration_limit_soc=100.0
    ),
    "sale_pv_bat": InverterModeClass(
        name="sale_pv_bat",
        pv_to_house=True,
        charge_from_pv=False,
        charge_from_grid=False,
        discharge_to_house=True,
        discharge_to_grid=True,
        export_pv_to_grid=True,
        is_grid_bypass=False,
        curtail_pv=False,
        calibration_limit_soc=100.0
    ),
    "stop_sale": InverterModeClass(
        name="stop_sale",
        pv_to_house=True,
        charge_from_pv=True,
        charge_from_grid=False,
        discharge_to_house=True,
        discharge_to_grid=False,
        export_pv_to_grid=False,
        is_grid_bypass=False,
        curtail_pv=True,
        calibration_limit_soc=90.0    # Калибруем только пока АКБ может принимать ток (до 90%)
    ),
    "sale_pv": InverterModeClass(
        name="sale_pv",
        pv_to_house=True,
        charge_from_pv=True,
        charge_from_grid=False,
        discharge_to_house=True,
        discharge_to_grid=False,
        export_pv_to_grid=True,
        is_grid_bypass=False,
        curtail_pv=False,
        calibration_limit_soc=100.0
    ),
    "bat_emergency": InverterModeClass(
        name="bat_emergency",
        pv_to_house=True,
        charge_from_pv=True,
        charge_from_grid=False,
        discharge_to_house=False,
        discharge_to_grid=False,
        export_pv_to_grid=False,
        is_grid_bypass=True,
        curtail_pv=False,
        calibration_limit_soc=100.0
    )
}
