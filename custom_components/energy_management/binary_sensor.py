import logging
from datetime import timedelta
from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
    CONF_DEDUCT_SETTINGS,
    CONF_CUSTOM_PERIOD,
    CONF_IS_CYCLIC,
    CONF_POWER_SENSOR,
    CONF_ONLY_SOLAR,
    CONF_ACTIVE_SENSOR,
)
from .utils import get_kwh_val, round_f

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
            if not isinstance(config, dict) or not isinstance(sensor_id, str):
                continue
            
            clean_sensor_id = sensor_id.strip()
            # Clean power sensor ID in config copy
            config_copy = dict(config)
            if CONF_POWER_SENSOR in config_copy and isinstance(config_copy[CONF_POWER_SENSOR], str):
                config_copy[CONF_POWER_SENSOR] = config_copy[CONF_POWER_SENSOR].strip()
            
            fallback_id = clean_sensor_id.replace("sensor.", "").replace("_", " ").title()
            clean_name = config_copy.get("name") or fallback_id
            entities.append(
                EnergyPermissionSensor(
                    manager,
                    clean_sensor_id,
                    f"Разрешение: {clean_name}",
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
        only_solar_free = settings.get(CONF_ONLY_SOLAR, False)
        learned_kw = float(self.manager.learned_real_power.get(self.target_sensor_id, 0.0)) / 1000.0
        config_kw = float(settings.get("required_kw", 0.0))
        req_kw = max(learned_kw, config_kw)

        attrs = {
            "controlled_entity_id": self.target_sensor_id,
            "status_reason": budget_res.get("permissions_reasons", {}).get(self.target_sensor_id, "Unknown"),
            "daily_consumption_kwh": round_f(float(settings.get("required_kwh", 0.0)), 2),
            "priority": settings.get("priority", 99),
            "already_consumed_today_kwh": round_f(float(consumed_today), 2),
            "estimated_initial_budget_kwh": round_f(budget_res.get("initial_budget", 0.0), 3),
            "forecast_correction_coefficient": round_f(budget_res.get("forecast_coefficient", 1.0), 3),
            # Learned power values
            "learned_peak_power_w": round_f(learned_kw * 1000.0, 1),
            "configured_peak_power_w": round_f(config_kw * 1000.0, 1),
            "learned_standby_power_w": round_f(
                self.manager.learned_standby_power.get(self.target_sensor_id, 0.0), 1
            ),
            "is_cyclic": is_cyclic,
            "only_solar_checked": only_solar_free,
            "sunrise_hour": budget_res.get("sunrise_hour", 8),
            "available_power_total_kw": round_f(budget_res.get("available_power_total_kw", 0.0), 2),
            "available_gen_kw": round_f(budget_res.get("available_gen_kw", 0.0), 2),
            "available_gen_surplus_initial_kw": round_f(budget_res.get("available_gen_surplus_initial", 0.0), 2),
            "only_solar_threshold_kw": round_f(float(req_kw) * (0.8 if settings.get("required_kwh", 2.5) == 0 else 0.6) if only_solar_free else 0.0, 2),
            "waste_compensation_kw": round_f(budget_res.get("waste_compensation_kw", 0.0), 2),
            "battery_flexible_kw": round_f(budget_res.get("battery_flexible_kw", 0.0), 2),
            "battery_discharge_budget_kw": round_f(budget_res.get("battery_discharge_budget_kw", 0.0), 2),
            "reserved_by": budget_res.get("reserved_by", []),
        }

        # Device detection and Status
        is_actually_working = self.manager._is_currently_pulling_power(self.target_sensor_id)
        if is_actually_working:
            if self._is_on:
                status = "Работает" # (Working)
            else:
                status = "Работает (Принудительно)" # (Manual Overrun)
        else:
            if self._is_on:
                if is_cyclic:
                    status = "Разрешено (ожидает запуска)"
                else:
                    status = "Зарезервировано"
            else:
                # v4.8 - More informative restricted state
                status = "Ожидание (запрещено)" if not is_cyclic else "Ожидание солнца/цены"
        
        attrs["device_status"] = status

        # Only show cyclic attributes for cyclic devices
        if is_cyclic:
            attrs["learned_avg_cycle_power_w"] = round_f(
                self.manager.learned_avg_cycle_power.get(self.target_sensor_id, 0.0), 1
            )
            attrs["learned_cycle_total_kwh"] = round_f(
                self.manager.learned_cycle_total_kwh.get(self.target_sensor_id, 0.0), 3
            )
            
            avg_dur_min = round_f(self.manager.learned_avg_cycle_duration.get(self.target_sensor_id, 0.0) / 60.0, 1)
            attrs["learned_avg_cycle_duration_min"] = avg_dur_min

            if self.target_sensor_id in self.manager.cycle_actual_start_time:
                start_dt = self.manager.cycle_actual_start_time[self.target_sensor_id]
                attrs["cycle_start_time"] = start_dt.strftime("%H:%M:%S")
                
                avg_dur_sec = self.manager.learned_avg_cycle_duration.get(self.target_sensor_id, 0.0)
                if avg_dur_sec > 0:
                    end_dt = start_dt + timedelta(seconds=avg_dur_sec)
                    attrs["predicted_cycle_end_time"] = end_dt.strftime("%H:%M:%S")

        self._attrs = attrs

        # Add active sensor info if configured
        active_ent = settings.get(CONF_ACTIVE_SENSOR)
        if active_ent:
            self._attrs["configured_active_sensor"] = active_ent
            st_active = self.manager.hass.states.get(active_ent)
            self._attrs["active_sensor_state"] = st_active.state if st_active else "unknown"

        # Add current power only if configured
        if settings.get(CONF_POWER_SENSOR):
            p_ent = settings.get(CONF_POWER_SENSOR)
            self._attrs["configured_power_sensor"] = p_ent
            st = self.manager.hass.states.get(p_ent)
            
            # Forced update of manager's last known power if we have a state
            if st and st.state not in ("unknown", "unavailable"):
                try:
                    val = float(str(st.state).replace(',', '.'))
                    if st.attributes.get("unit_of_measurement") == "kW":
                        val *= 1000.0
                    self.manager.last_known_power[self.target_sensor_id] = val
                except:
                    pass

            self._attrs["current_power_w"] = round_f(
                self.manager.last_known_power.get(self.target_sensor_id, 0.0), 1
            )

    @property
    def extra_state_attributes(self) -> dict:
        """Return attributes."""
        return self._attrs
