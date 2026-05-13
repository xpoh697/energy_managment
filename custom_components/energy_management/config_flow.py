import logging
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_CONSUMPTION_SENSORS,
    CONF_GENERATION_SENSORS,
    CONF_DEDUCT_SENSORS,
    CONF_CUSTOM_PERIOD,
    CONF_FORECAST_TODAY_REMAINING,
    CONF_FORECAST_TODAY_HOURLY,
    CONF_FORECAST_TOMORROW,
    CONF_BATTERY_SOC,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER,
    CONF_PRICE_BUY,
    CONF_PRICE_SELL,
    CONF_DEDUCT_SETTINGS,
    CONF_POWER_LOAD_SENSORS,
    CONF_POWER_GEN_SENSORS,
    CONF_PRESENCE_SENSORS,
    CONF_INVERTER_LOSSES_SENSOR,
    CONF_TOTAL_SYSTEM_COST,
    CONF_BATTERY_COST,
    CONF_BATTERY_RATED_CYCLES,
    CONF_ANOMALY_THRESHOLD,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GRID_EXPORT_SENSORS,
    CONF_POWER_SENSOR,
    CONF_ACTIVE_HOLD_TIME,
    CONF_IS_CYCLIC,
    CONF_ONLY_SOLAR,
    CONF_ACTIVE_SENSOR,
    CONF_GRID_POWER,
    CONF_BATTERY_VOLTAGE,
    CONF_BOILER_ENABLE,
    CONF_BOILER_POWER,
    CONF_BOILER_CAPACITY,
    CONF_BOILER_TEMP_SENSOR,
    CONF_BOILER_DEADLINE,
    CONF_MIN_SELL_POWER,
    CONF_BATTERY_MAX_POWER,
    CONF_MIN_SELL_PRICE,
    CONF_MAX_ARBITRAGE_HOURS,
    CONF_MIN_DISCHARGE_KWH,
)

_LOGGER = logging.getLogger(__name__)

class EnergyManagementConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Energy Management."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return EnergyManagementOptionsFlow()

    def __init__(self):
        """Initialize."""
        self._user_input = {}

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        if user_input is not None:
            self._user_input.update(user_input)
            if user_input.get(CONF_DEDUCT_SENSORS):
                return await self.async_step_deduct_settings()
            return await self.async_step_investment_settings()

        schema = vol.Schema({
            vol.Required("name", default="Energy Management"): cv.string,
            vol.Required(CONF_CONSUMPTION_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_GENERATION_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_DEDUCT_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_FORECAST_TODAY_REMAINING, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_FORECAST_TODAY_HOURLY, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_FORECAST_TOMORROW, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_POWER_LOAD_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_POWER_GEN_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_PRESENCE_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain=["person", "binary_sensor"])),
            vol.Optional(CONF_BATTERY_SOC): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_BATTERY_CAPACITY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_BATTERY_POWER): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_GRID_POWER): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_BATTERY_VOLTAGE): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_PRICE_BUY): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_PRICE_SELL): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_GRID_IMPORT_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_GRID_EXPORT_SENSORS, default=[]): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
            vol.Optional(CONF_CUSTOM_PERIOD, default=14): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
        })

        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_deduct_settings(self, user_input=None):
        """Handle settings for deducted sensors."""
        return await self._async_step_deduct_logic(user_input)

    async def _async_step_deduct_logic(self, user_input=None):
        """Deduct settings looping logic."""
        deduct_sensors = self._user_input.get(CONF_DEDUCT_SENSORS, [])
        if "deduct_idx" not in self._user_input:
            self._user_input["deduct_idx"] = 0
            self._user_input[CONF_DEDUCT_SETTINGS] = {}
        
        idx = self._user_input["deduct_idx"]
        if idx >= len(deduct_sensors):
            self._user_input.pop("deduct_idx", None)
            return await self.async_step_investment_settings()

        curr = deduct_sensors[idx]
        if user_input is not None:
            self._user_input[CONF_DEDUCT_SETTINGS][curr] = user_input
            self._user_input["deduct_idx"] += 1
            return await self._async_step_deduct_logic()

        sensor_display = curr.replace("sensor.", "").replace("_", " ").title()
        schema = vol.Schema({
            vol.Optional("name", default=sensor_display): str,
            vol.Required("priority", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required("required_kwh", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Required("required_kw", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional(CONF_ONLY_SOLAR, default=False): bool,
            vol.Optional(CONF_POWER_SENSOR): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_ACTIVE_HOLD_TIME, default=15): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
            vol.Optional(CONF_IS_CYCLIC, default=False): bool,
            vol.Optional(CONF_ACTIVE_SENSOR): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
        })

        return self.async_show_form(step_id="deduct_settings", data_schema=schema, description_placeholders={"sensor_name": sensor_display})

    async def async_step_investment_settings(self, user_input=None):
        """Handle investment settings."""
        if user_input is not None:
            self._user_input.update(user_input)
            return self.async_create_entry(title=self._user_input.get("name", "Energy Management"), data=self._user_input)

        schema = vol.Schema({
            vol.Optional(CONF_TOTAL_SYSTEM_COST, default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
            vol.Optional(CONF_BATTERY_COST, default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
            vol.Optional(CONF_BATTERY_RATED_CYCLES, default=6000): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(CONF_ANOMALY_THRESHOLD, default=2.0): vol.All(vol.Coerce(float), vol.Range(min=1.1, max=10.0)),
        })
        return self.async_show_form(step_id="investment_settings", data_schema=schema)

class EnergyManagementOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow with category menu."""

    async def async_step_init(self, user_input=None):
        """Manage the options via a selection form."""
        self._user_input = dict(self.config_entry.data)
        if self.config_entry.options:
            self._user_input.update(self.config_entry.options)

        if user_input is not None:
            category = user_input.get("category")
            if category == "main_settings":
                return await self.async_step_main_settings()
            if category == "deduct_settings_init":
                return await self.async_step_deduct_settings_init()
            if category == "boiler_settings":
                return await self.async_step_boiler_settings()
            if category == "investment_settings":
                return await self.async_step_investment_settings()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("category", default="main_settings"): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": "main_settings", "label": "Основные настройки (Датчики, Цены, АКБ)"},
                            {"value": "deduct_settings_init", "label": "Управляемые нагрузки (Deduct)"},
                            {"value": "boiler_settings", "label": "Оптимизатор Бойлера"},
                            {"value": "investment_settings", "label": "Инвестиции и окупаемость"},
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            })
        )

    async def async_step_main_settings(self, user_input=None):
        """Main settings step."""
        if user_input is not None:
            self._user_input.update(user_input)
            return self.async_create_entry(title="", data=self._user_input)

        def get_l(k):
            v = self._user_input.get(k, [])
            if isinstance(v, (list, tuple)): return list(v)
            return [v] if v else []

        def get_s(k):
            v = self._user_input.get(k)
            if not v or v == "undefined": return None
            return str(v[0]) if isinstance(v, (list, tuple)) else str(v)

        schema_dict = {
            vol.Required(CONF_CONSUMPTION_SENSORS, default=get_l(CONF_CONSUMPTION_SENSORS)): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor")),
        }
        for k in [CONF_GENERATION_SENSORS, CONF_DEDUCT_SENSORS, CONF_FORECAST_TODAY_REMAINING, CONF_FORECAST_TODAY_HOURLY, CONF_FORECAST_TOMORROW, CONF_POWER_LOAD_SENSORS, CONF_POWER_GEN_SENSORS, CONF_GRID_IMPORT_SENSORS, CONF_GRID_EXPORT_SENSORS]:
            schema_dict[vol.Optional(k, default=get_l(k))] = selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain="sensor"))
        
        schema_dict[vol.Optional(CONF_PRESENCE_SENSORS, default=get_l(CONF_PRESENCE_SENSORS))] = selector.EntitySelector(selector.EntitySelectorConfig(multiple=True, domain=["person", "binary_sensor", "zone"]))
        
        for k in [CONF_BATTERY_SOC, CONF_BATTERY_CAPACITY, CONF_BATTERY_POWER, CONF_GRID_POWER, CONF_BATTERY_VOLTAGE, CONF_PRICE_BUY, CONF_PRICE_SELL, CONF_INVERTER_LOSSES_SENSOR]:
            v = get_s(k)
            schema_dict[vol.Optional(k, default=v) if v else vol.Optional(k)] = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))

        cp = self._user_input.get(CONF_CUSTOM_PERIOD)
        schema_dict[vol.Optional(CONF_CUSTOM_PERIOD, default=int(cp if cp is not None else 14))] = vol.All(vol.Coerce(int), vol.Range(min=1, max=365))

        # v11.9.556: Strategy Limits (Consolidated for data integrity)
        schema_dict[vol.Optional(CONF_BATTERY_MAX_POWER, default=float(self._user_input.get(CONF_BATTERY_MAX_POWER, 5.0)))] = vol.All(vol.Coerce(float), vol.Range(min=0.1, max=50.0))
        schema_dict[vol.Optional(CONF_MIN_SELL_POWER, default=float(self._user_input.get(CONF_MIN_SELL_POWER, 0.1)))] = vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0))
        schema_dict[vol.Optional(CONF_MIN_SELL_PRICE, default=float(self._user_input.get(CONF_MIN_SELL_PRICE, 0.01)))] = vol.All(vol.Coerce(float), vol.Range(min=0.0))
        schema_dict[vol.Optional(CONF_MAX_ARBITRAGE_HOURS, default=int(self._user_input.get(CONF_MAX_ARBITRAGE_HOURS, 24)))] = vol.All(vol.Coerce(int), vol.Range(min=1, max=24))
        schema_dict[vol.Optional(CONF_MIN_DISCHARGE_KWH, default=float(self._user_input.get(CONF_MIN_DISCHARGE_KWH, 0.1)))] = vol.All(vol.Coerce(float), vol.Range(min=0.0, max=10.0))

        return self.async_show_form(step_id="main_settings", data_schema=vol.Schema(schema_dict))

    async def async_step_deduct_settings_init(self, user_input=None):
        """Entry for deduct loads."""
        self._user_input.pop("deduct_idx", None)
        return await self.async_step_deduct_settings()

    async def async_step_deduct_settings(self, user_input=None):
        """Handle deduct loads settings."""
        deduct_sensors = self._user_input.get(CONF_DEDUCT_SENSORS, [])
        if "deduct_idx" not in self._user_input:
            self._user_input["deduct_idx"] = 0
            if CONF_DEDUCT_SETTINGS not in self._user_input:
                self._user_input[CONF_DEDUCT_SETTINGS] = {}

        idx = self._user_input["deduct_idx"]
        if idx >= len(deduct_sensors):
            self._user_input.pop("deduct_idx", None)
            return self.async_show_menu(step_id="init", menu_options=["main_settings", "deduct_settings_init", "boiler_settings", "investment_settings"])

        curr = deduct_sensors[idx]
        if user_input is not None:
            self._user_input[CONF_DEDUCT_SETTINGS][curr] = user_input
            self._user_input["deduct_idx"] += 1
            return await self.async_step_deduct_settings()

        existing = self._user_input.get(CONF_DEDUCT_SETTINGS, {}).get(curr, {})
        def gs(k):
            v = existing.get(k)
            if not v or v == "undefined": return None
            return str(v[0]) if isinstance(v, (list, tuple)) else str(v)

        sensor_display = curr.replace("sensor.", "").replace("_", " ").title()
        schema = vol.Schema({
            vol.Optional("name", default=existing.get("name", sensor_display)): str,
            vol.Required("priority", default=int(existing.get("priority", 1))): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required("required_kwh", default=float(existing.get("required_kwh", 0.0))): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Required("required_kw", default=float(existing.get("required_kw", 0.0))): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional(CONF_ONLY_SOLAR, default=existing.get(CONF_ONLY_SOLAR, False)): bool,
            vol.Optional(CONF_POWER_SENSOR, default=gs(CONF_POWER_SENSOR) or vol.UNDEFINED): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            vol.Optional(CONF_ACTIVE_HOLD_TIME, default=int(existing.get(CONF_ACTIVE_HOLD_TIME, 15))): vol.All(vol.Coerce(int), vol.Range(min=1, max=120)),
            vol.Optional(CONF_IS_CYCLIC, default=existing.get(CONF_IS_CYCLIC, False)): bool,
            vol.Optional(CONF_ACTIVE_SENSOR, default=gs(CONF_ACTIVE_SENSOR) or vol.UNDEFINED): selector.EntitySelector(selector.EntitySelectorConfig(domain="binary_sensor")),
        })

        return self.async_show_form(step_id="deduct_settings", data_schema=schema, description_placeholders={"sensor_name": sensor_display})

    async def async_step_boiler_settings(self, user_input=None):
        """Boiler settings."""
        if user_input is not None:
            self._user_input.update(user_input)
            return self.async_create_entry(title="", data=self._user_input)

        bp = self._user_input.get(CONF_BOILER_POWER)
        bc = self._user_input.get(CONF_BOILER_CAPACITY)
        bd = self._user_input.get(CONF_BOILER_DEADLINE)
        bs = self._user_input.get(CONF_BOILER_TEMP_SENSOR)

        schema = vol.Schema({
            vol.Optional(CONF_BOILER_ENABLE, default=self._user_input.get(CONF_BOILER_ENABLE, False)): bool,
            vol.Optional(CONF_BOILER_POWER, default=float(bp if bp is not None else 2.5)): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10.0)),
            vol.Optional(CONF_BOILER_CAPACITY, default=float(bc if bc is not None else 8.5)): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=50.0)),
            vol.Optional(CONF_BOILER_DEADLINE, default=int(bd if bd is not None else 18)): vol.All(vol.Coerce(int), vol.Range(min=0, max=23)),
            vol.Optional(CONF_BOILER_TEMP_SENSOR, default=bs if bs and bs != "undefined" else vol.UNDEFINED): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        })
        return self.async_show_form(step_id="boiler_settings", data_schema=schema)

    async def async_step_investment_settings(self, user_input=None):
        """Investment settings."""
        if user_input is not None:
            self._user_input.update(user_input)
            return self.async_create_entry(title="", data=self._user_input)

        sc = self._user_input.get(CONF_TOTAL_SYSTEM_COST)
        bc = self._user_input.get(CONF_BATTERY_COST)
        br = self._user_input.get(CONF_BATTERY_RATED_CYCLES)
        at = self._user_input.get(CONF_ANOMALY_THRESHOLD)

        schema = vol.Schema({
            vol.Optional(CONF_TOTAL_SYSTEM_COST, default=float(sc if sc is not None else 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
            vol.Optional(CONF_BATTERY_COST, default=float(bc if bc is not None else 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0.0)),
            vol.Optional(CONF_BATTERY_RATED_CYCLES, default=int(br if br is not None else 6000)): vol.All(vol.Coerce(int), vol.Range(min=1)),
            vol.Optional(CONF_ANOMALY_THRESHOLD, default=float(at if at is not None else 2.0)): vol.All(vol.Coerce(float), vol.Range(min=1.1, max=10.0)),
        })
        return self.async_show_form(step_id="investment_settings", data_schema=schema)
