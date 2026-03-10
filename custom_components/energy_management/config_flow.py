import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers import selector
import homeassistant.helpers.config_validation as cv

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
    CONF_DEDUCT_SETTINGS,
    CONF_POWER_LOAD_SENSORS,
    CONF_POWER_GEN_SENSORS,
    CONF_PRESENCE_SENSORS,
    CONF_INVERTER_LOSSES_SENSOR,
)


from homeassistant.core import callback

class EnergyManagementConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Energy Management."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return EnergyManagementOptionsFlow(config_entry)

    def __init__(self):
        """Initialize."""
        self._user_input = {}

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            self._user_input.update(user_input)
            deduct_sensors = user_input.get(CONF_DEDUCT_SENSORS, [])
            
            # If user selected controllable loads, ask for their priorities and capacities
            if deduct_sensors:
                return await self.async_step_deduct_settings()

            # Otherwise, just finish
            return self.async_create_entry(
                title=self._user_input.get("name", "Energy Management"), data=self._user_input
            )

        schema = vol.Schema(
            {
                vol.Required("name", default="Energy Management"): cv.string,
                vol.Required(CONF_CONSUMPTION_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_GENERATION_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_DEDUCT_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_FORECAST_TODAY_REMAINING, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_FORECAST_TOMORROW, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_POWER_LOAD_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_POWER_GEN_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain="sensor")
                ),
                vol.Optional(CONF_PRESENCE_SENSORS, default=[]): selector.EntitySelector(
                    selector.EntitySelectorConfig(multiple=True, domain=["person", "binary_sensor"])
                ),
                vol.Optional(CONF_BATTERY_SOC): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_BATTERY_CAPACITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_PRICE_BUY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_PRICE_SELL): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(CONF_CUSTOM_PERIOD, default=14): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_deduct_settings(self, user_input=None):
        """Handle settings for deducted sensors (Priority and Energy)."""
        errors = {}
        deduct_sensors = self._user_input.get(CONF_DEDUCT_SENSORS, [])

        if "deduct_settings_index" not in self._user_input:
            self._user_input["deduct_settings_index"] = 0
            self._user_input[CONF_DEDUCT_SETTINGS] = {}
            
        idx = self._user_input["deduct_settings_index"]

        if idx >= len(deduct_sensors):
            # Clean up temporary index
            self._user_input.pop("deduct_settings_index", None)
            return self.async_create_entry(
                title=self._user_input.get("name", "Energy Management"), data=self._user_input
            )

        current_sensor = deduct_sensors[idx]

        if user_input is not None:
            self._user_input[CONF_DEDUCT_SETTINGS][current_sensor] = {
                "name": user_input.get("name", current_sensor.split('.')[-1].replace('_', ' ').title()),
                "priority": user_input.get("priority", 1),
                "required_kwh": user_input.get("required_kwh", 0.0),
                "required_kw": user_input.get("required_kw", 0.0),
                "only_solar_or_negative_price": user_input.get("only_solar_or_negative_price", False),
            }
            self._user_input["deduct_settings_index"] += 1
            return await self.async_step_deduct_settings()

        # Build schema for a SINGLE sensor
        schema_dict = {
            vol.Optional("name", default=current_sensor.split('.')[-1].replace('_', ' ').title()): str,
            vol.Required("priority", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required("required_kwh", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Required("required_kw", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional("only_solar_or_negative_price", default=False): bool,
        }

        # Nice clean name for display
        sensor_display = current_sensor.replace("sensor.", "").replace("_", " ").title()

        return self.async_show_form(
            step_id="deduct_settings",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"sensor_name": sensor_display}
        )

class EnergyManagementOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        self._user_input = dict(config_entry.data)
        if config_entry.options:
            self._user_input.update(config_entry.options)

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        errors = {}

        if user_input is not None:
            self._user_input.update(user_input)
            deduct_sensors = user_input.get(CONF_DEDUCT_SENSORS, [])
            
            if deduct_sensors:
                return await self.async_step_deduct_settings()

            return self.async_create_entry(title="", data=self._user_input)

        schema_dict = {}
        
        # Helper to ensure lists for multiple=True selectors
        def get_list(key):
            val = self._user_input.get(key, [])
            if isinstance(val, tuple):
                val = list(val)
            elif not isinstance(val, list):
                val = [val] if val else []
            # remove empty strings, Nones, or weird vol values
            return [v for v in val if v and isinstance(v, str) and v != "undefined"]

        # Helper to ensure string/none for multiple=False selectors
        def get_str(key):
            val = self._user_input.get(key)
            if not val or val == "undefined":
                return None
            if isinstance(val, (list, tuple)):
                return str(val[0]) if val else None
            return str(val)

        cons_val = get_list(CONF_CONSUMPTION_SENSORS)
        schema_dict[vol.Required(CONF_CONSUMPTION_SENSORS, default=cons_val)] = selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True, domain="sensor")
        )
        
        for key in [CONF_GENERATION_SENSORS, CONF_DEDUCT_SENSORS, CONF_FORECAST_TODAY_REMAINING, CONF_FORECAST_TOMORROW, CONF_POWER_LOAD_SENSORS, CONF_POWER_GEN_SENSORS]:
            val = get_list(key)
            schema_dict[vol.Optional(key, default=val)] = selector.EntitySelector(
                selector.EntitySelectorConfig(multiple=True, domain="sensor")
            )
        
        # Presence sensors (person / binary_sensor / zone domains)
        presence_val = get_list(CONF_PRESENCE_SENSORS)
        schema_dict[vol.Optional(CONF_PRESENCE_SENSORS, default=presence_val)] = selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True, domain=["person", "binary_sensor", "zone"])
        )
        
        # Inverter losses sensor (optional)
        losses_val = get_str(CONF_INVERTER_LOSSES_SENSOR)
        if losses_val:
            schema_dict[vol.Optional(CONF_INVERTER_LOSSES_SENSOR, default=losses_val)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
        else:
            schema_dict[vol.Optional(CONF_INVERTER_LOSSES_SENSOR)] = selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )

        for key in [CONF_BATTERY_SOC, CONF_BATTERY_CAPACITY, CONF_PRICE_BUY, CONF_PRICE_SELL]:
            val = get_str(key)
            if val:
                schema_dict[vol.Optional(key, default=val)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                )
            else:
                schema_dict[vol.Optional(key)] = selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                )

        try:
            period = int(self._user_input.get(CONF_CUSTOM_PERIOD, 14))
        except (ValueError, TypeError):
            period = 14
            
        schema_dict[vol.Optional(CONF_CUSTOM_PERIOD, default=period)] = vol.All(vol.Coerce(int), vol.Range(min=1, max=365))

        schema = vol.Schema(schema_dict)

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_deduct_settings(self, user_input=None):
        """Handle settings for deducted sensors in options flow."""
        errors = {}
        deduct_sensors = self._user_input.get(CONF_DEDUCT_SENSORS, [])

        if "deduct_settings_index" not in self._user_input:
            self._user_input["deduct_settings_index"] = 0
            if CONF_DEDUCT_SETTINGS not in self._user_input:
                self._user_input[CONF_DEDUCT_SETTINGS] = {}
            
        idx = self._user_input["deduct_settings_index"]

        if idx >= len(deduct_sensors):
            self._user_input.pop("deduct_settings_index", None)
            return self.async_create_entry(title="", data=self._user_input)

        current_sensor = deduct_sensors[idx]

        if user_input is not None:
            self._user_input[CONF_DEDUCT_SETTINGS][current_sensor] = {
                "name": user_input.get("name", current_sensor.split('.')[-1].replace('_', ' ').title()),
                "priority": user_input.get("priority", 1),
                "required_kwh": user_input.get("required_kwh", 0.0),
                "required_kw": user_input.get("required_kw", 0.0),
                "only_solar_or_negative_price": user_input.get("only_solar_or_negative_price", False),
            }
            self._user_input["deduct_settings_index"] += 1
            return await self.async_step_deduct_settings()

        existing = self._user_input.get(CONF_DEDUCT_SETTINGS, {}).get(current_sensor, {})
        
        schema_dict = {
            vol.Optional("name", default=existing.get("name", current_sensor.split('.')[-1].replace('_', ' ').title())): str,
            vol.Required("priority", default=existing.get("priority", 1)): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required("required_kwh", default=existing.get("required_kwh", 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=100.0)),
            vol.Required("required_kw", default=existing.get("required_kw", 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=50.0)),
            vol.Optional("only_solar_or_negative_price", default=existing.get("only_solar_or_negative_price", False)): bool,
        }

        sensor_display = current_sensor.replace("sensor.", "").replace("_", " ").title()

        return self.async_show_form(
            step_id="deduct_settings",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
            description_placeholders={"sensor_name": sensor_display}
        )
