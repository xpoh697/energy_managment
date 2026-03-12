from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.const import UnitOfPower, PERCENTAGE

from .const import (
    DOMAIN,
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
    CONF_SALE_PV_NO_BAT_MAX_HOUR,
    CONF_ARBITRAGE_MIN_PROFIT
)

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the number platform."""
    manager = hass.data[DOMAIN][entry.entry_id]
    
    entities = [
        EnergyProfileNumber(manager, CONF_PRICE_BUY_LIMIT, "Buy Price Limit", None, -99.0, 999.0, 0.001, "mdi:cash-minus", 99.0),
        EnergyProfileNumber(manager, CONF_PRICE_SELL_LIMIT, "Sell Price Limit", None, -99.0, 999.0, 0.001, "mdi:cash-plus", -99.0),
        EnergyProfileNumber(manager, CONF_PRICE_STOP_SELL, "Stop Sell Threshold", None, -99.0, 999.0, 0.001, "mdi:cash-remove", 0.0),
        EnergyProfileNumber(manager, CONF_PRICE_SELL_ONLY_PV, "Sell PV Only (Block Bat/Loads)", None, -99.0, 999.0, 0.001, "mdi:weather-sunny", 1.5),
        EnergyProfileNumber(manager, CONF_PRICE_TOLERANCE, "Buy Price Tolerance", None, 0.0, 999.0, 0.001, "mdi:tune", 0.0),
        EnergyProfileNumber(manager, CONF_PRICE_SELL_TOLERANCE, "Sell Price Tolerance", None, 0.0, 999.0, 0.001, "mdi:tune", 0.0),
        EnergyProfileNumber(manager, CONF_BATTERY_MAX_POWER, "Battery Max Power", UnitOfPower.KILO_WATT, 0.0, 100.0, 0.1, "mdi:flash", 5.0),
        EnergyProfileNumber(manager, CONF_TARGET_SOC_BUY, "Target SOC Buy", PERCENTAGE, 0.0, 100.0, 1.0, "mdi:battery-arrow-up", 100.0),
        EnergyProfileNumber(manager, CONF_TARGET_SOC_SELL, "Target SOC Sell", PERCENTAGE, 0.0, 100.0, 1.0, "mdi:battery-arrow-down", 20.0),
        EnergyProfileNumber(manager, CONF_MIN_SOC_BUY, "Min Survival SOC", PERCENTAGE, 0.0, 100.0, 1.0, "mdi:shield-cross", 10.0),
        EnergyProfileNumber(manager, CONF_SALE_PV_NO_BAT_MAX_HOUR, "Max Hour for Sell PV Only", "h", 0.0, 23.0, 1.0, "mdi:clock-end", 13.0),
        EnergyProfileNumber(manager, CONF_ARBITRAGE_MIN_PROFIT, "Arbitrage Min Profit", None, 0.0, 999.0, 0.1, "mdi:hand-coin", 0.1),
    ]
    
    async_add_entities(entities)


class EnergyProfileNumber(NumberEntity):
    _attr_has_entity_name = True

    def __init__(self, manager, key, name, unit, min_v, max_v, step, icon, default_value):
        self.manager = manager
        self.key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(manager.entry.entry_id))},
            name=manager.entry.data.get("name", "Energy Management"),
            manufacturer="Energy AI",
            model="Energy Trader System",
        )
        
        self._attr_native_unit_of_measurement = unit
        self._attr_native_min_value = min_v
        self._attr_native_max_value = max_v
        self._attr_native_step = step
        self._attr_mode = NumberMode.BOX
        self._attr_icon = icon
        self.default_value = default_value

    @property
    def native_value(self):
        return float(self.manager.get_setting(self.key, self.default_value))

    async def async_set_native_value(self, value: float) -> None:
        await self.manager.async_set_setting(self.key, float(value))
        self.async_write_ha_state()
