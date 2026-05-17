from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
    CONF_DYNAMIC_SOC_BUY,
    CONF_DYNAMIC_SOC_SELL,
    CONF_FORCE_MARKET_SELL,
    CONF_USE_DP
)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the switch platform."""
    manager = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        EnergyProfileSwitch(manager, CONF_DYNAMIC_SOC_BUY, "Smart Charge AI", "mdi:brain", True),
        EnergyProfileSwitch(manager, CONF_DYNAMIC_SOC_SELL, "Smart Sell AI", "mdi:brain", True),
        EnergyProfileSwitch(manager, CONF_FORCE_MARKET_SELL, "Force Market Sell", "mdi:flash-red-eye", False),
        EnergyProfileSwitch(manager, CONF_USE_DP, "Использовать DP стратегию", "mdi:brain", False),
    ]
    
    async_add_entities(entities)


class EnergyProfileSwitch(SwitchEntity):
    def __init__(self, manager, key, name, icon, default_value):
        self.manager = manager
        self.key = key
        self._attr_name = name
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )
        
        self._attr_icon = icon
        self.default_value = default_value

    @property
    def is_on(self):
        return self.manager.get_setting(self.key, self.default_value)

    async def async_turn_on(self, **kwargs):
        await self.manager.async_set_setting(self.key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.manager.async_set_setting(self.key, False)
        self.async_write_ha_state()
