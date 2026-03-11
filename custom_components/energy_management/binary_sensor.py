import logging
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
    CONF_DEDUCT_SETTINGS,
    CONF_CUSTOM_PERIOD,
    CONF_IS_CYCLIC,
    CONF_POWER_SENSOR,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the binary_sensor platform."""
    manager = hass.data[DOMAIN][entry.entry_id]

    # Merge data + options (options take priority from re-configure)
    config_data = {**entry.data, **entry.options}
    custom_period = config_data.get(CONF_CUSTOM_PERIOD, 14)
    deduct_settings = config_data.get(CONF_DEDUCT_SETTINGS) or {}

    entities = []
    if isinstance(deduct_settings, dict):
        for sensor_id, config in deduct_settings.items():
            if not isinstance(config, dict):
                continue
            fallback_id = sensor_id.replace("sensor.", "").replace("_", " ").title()
            clean_id = config.get("name") or fallback_id
            entities.append(
                EnergyPermissionSensor(
                    manager,
                    sensor_id,
                    f"Разрешение: {clean_id}",
                    custom_period,
                )
            )

    if entities:
        async_add_entities(entities)


class EnergyPermissionSensor(BinarySensorEntity):
    """Binary sensor representing permission to run a specific managed load."""

    def __init__(self, manager, target_sensor_id: str, name: str, custom_period: int):
        self.manager = manager
        self.target_sensor_id = target_sensor_id
        self._custom_period = custom_period
        self._attr_name = name
        self._attr_unique_id = (
            f"{manager.entry.entry_id}_permission_{target_sensor_id.replace('.', '_')}"
        )
        self._attr_device_class = BinarySensorDeviceClass.POWER
        self._attr_icon = "mdi:check-network-outline"
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
    def is_on(self) -> bool:
        """Return True if the entity is permitted to run."""
        budget_res = self.manager.get_budget_and_permissions(self._custom_period)
        self._is_on = budget_res.get("permissions", {}).get(self.target_sensor_id, False)
        self._build_attrs(budget_res)
        return self._is_on

    def _build_attrs(self, budget_res: dict):
        """Build rich attributes, hiding cyclic data for non-cyclic devices."""
        settings = self.manager.deduct_settings.get(self.target_sensor_id, {})
        if not isinstance(settings, dict):
            settings = {}

        is_cyclic = settings.get(CONF_IS_CYCLIC, False)
        consumed_today = self.manager.daily_deduct_consumption.get(self.target_sensor_id, 0.0)

        attrs = {
            "controlled_entity_id": self.target_sensor_id,
            "priority": settings.get("priority", "?"),
            "target_required_kwh": settings.get("required_kwh", 0.0),
            "is_cyclic": is_cyclic,
            "already_consumed_today_kwh": round(consumed_today, 3),
            "estimated_initial_budget_kwh": round(budget_res.get("initial_budget", 0.0), 3),
            "forecast_correction_coefficient": round(budget_res.get("forecast_coefficient", 1.0), 3),
            "reason": budget_res.get("permissions_reasons", {}).get(self.target_sensor_id, "Нет данных"),
            # Learned power values
            "learned_peak_power_w": round(
                self.manager.learned_real_power.get(self.target_sensor_id, 0.0), 1
            ),
            "learned_standby_power_w": round(
                self.manager.learned_standby_power.get(self.target_sensor_id, 0.0), 1
            ),
        }

        # Only show cyclic attributes for cyclic devices
        if is_cyclic:
            attrs["learned_avg_cycle_power_w"] = round(
                self.manager.learned_avg_cycle_power.get(self.target_sensor_id, 0.0), 1
            )
            attrs["learned_cycle_total_kwh"] = round(
                self.manager.learned_cycle_total_kwh.get(self.target_sensor_id, 0.0), 3
            )

        self._attrs = attrs

    @property
    def extra_state_attributes(self) -> dict:
        """Return attributes."""
        return self._attrs
