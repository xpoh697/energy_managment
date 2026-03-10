import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.core import callback

from .const import DOMAIN, CONF_DEDUCT_SETTINGS, CONF_CUSTOM_PERIOD

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the binary_sensor platform."""
    manager = hass.data[DOMAIN][entry.entry_id]
    
    deduct_settings = entry.data.get(CONF_DEDUCT_SETTINGS, {})
    
    entities = []
    for sensor_id, config in deduct_settings.items():
        # Fallback readable name, using HA string tricks
        fallback_id = sensor_id.replace("sensor.", "").replace("_", " ").title()
        clean_id = config.get("name", fallback_id)
        
        entities.append(
            EnergyPermissionSensor(
                manager, 
                sensor_id, 
                f"Разрешение: {clean_id}",
                config.get("priority", 1),
                config.get("required_kwh", 2.5)
            )
        )
        
    if entities:
        async_add_entities(entities)


class EnergyPermissionSensor(BinarySensorEntity):
    """Binary sensor representing permission to run a specific managed load."""
    
    def __init__(self, manager, target_sensor_id, name, priority, required_kwh):
        self.manager = manager
        self.target_sensor_id = target_sensor_id
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_permission_{target_sensor_id.replace('.', '_')}"
        self._attr_device_class = BinarySensorDeviceClass.POWER
        self._attr_icon = "mdi:check-network-outline"
        
        self.priority = priority
        self.required_kwh = required_kwh
        self._is_on = False
        self._attrs = {}

    async def async_added_to_hass(self):
        """Register callbacks."""
        self.manager.register_listener(self.async_write_ha_state)

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, str(self.manager.entry.entry_id))},
            name=self.manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )

    @property
    def is_on(self):
        """Return True if the entity is permitted to run."""
        custom_period = self.manager.entry.data.get(CONF_CUSTOM_PERIOD, 14)
        budget_res = self.manager.get_budget_and_permissions(custom_period)
        
        self._is_on = budget_res["permissions"].get(self.target_sensor_id, False)
        
        consumed_today = self.manager.daily_deduct_consumption.get(self.target_sensor_id, 0.0)
        
        self._attrs = {
            "controlled_entity_id": self.target_sensor_id,
            "priority": self.priority,
            "target_required_kwh": self.required_kwh,
            "already_consumed_today_kwh": round(consumed_today, 3),
            "estimated_initial_budget_kwh": round(budget_res["initial_budget"], 3),
            "forecast_correction_coefficient": round(budget_res.get("forecast_coefficient", 1.0), 3)
        }
        
        return self._is_on

    @property
    def extra_state_attributes(self):
        """Return attributes."""
        return self._attrs
