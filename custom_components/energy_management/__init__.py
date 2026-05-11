import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "number", "switch"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Energy Profile from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # v11.9.333: Register static path for the UI card
    hass.http.register_static_path(
        "/api/energy_management/static",
        hass.config.path("custom_components/energy_management/www"),
        cache_headers=False
    )
    
    # We delay import to avoid circular dependency
    from .sensor import EnergyProfileManager
    manager = EnergyProfileManager(hass, entry)
    await manager.async_load()
    await manager.async_start()
    
    hass.data[DOMAIN][entry.entry_id] = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Register integration service to clear statistics
    async def handle_reset_data(call):
        manager.data = {
            "generation": {str(i): [] for i in range(24)},
            "consumption_total": {str(i): [] for i in range(24)},
            "consumption_base": {str(i): [] for i in range(24)},
            "settings": manager.settings,
            "forecast_history": []
        }
        await manager.store.async_save(manager.data)
        manager._notify_update()
        
    hass.services.async_register(DOMAIN, "reset_data", handle_reset_data)
    
    # Register export and import services
    async def handle_export_data(call):
        file_path = call.data.get("file_path", hass.config.path("energy_management_backup.json"))
        await hass.async_add_executor_job(manager.export_data, file_path)
        _LOGGER.info(f"Energy Management statistics exported to {file_path}")

    async def handle_import_data(call):
        file_path = call.data.get("file_path", hass.config.path("energy_management_backup.json"))
        success = await hass.async_add_executor_job(manager.import_data, file_path)
        if success:
            await manager.store.async_save(manager.data)
            manager._notify_update()
            _LOGGER.info(f"Energy Management statistics imported from {file_path}")
        else:
            _LOGGER.error(f"Failed to import Energy Management statistics from {file_path}")

    hass.services.async_register(DOMAIN, "export_data", handle_export_data)
    hass.services.async_register(DOMAIN, "import_data", handle_import_data)

    # Register service to reset BMS profile
    async def handle_reset_bms(call):
        manager.data["bms_learned_profile"] = {}
        manager.bms_learned_profile = {}
        await manager.store.async_save(manager.data)
        manager._notify_update()
        _LOGGER.info("Learned BMS profile has been reset.")

    hass.services.async_register(DOMAIN, "reset_bms_profile", handle_reset_bms)

    # v11.9.333: Manual Override Services
    async def handle_force_buy(call):
        manager.async_set_manual_override("buy")
    
    async def handle_stop_sale(call):
        manager.async_set_manual_override("stop_sale")

    async def handle_ai_mode(call):
        manager.async_set_manual_override("ai_mode")

    hass.services.async_register(DOMAIN, "force_buy", handle_force_buy)
    hass.services.async_register(DOMAIN, "stop_sale", handle_stop_sale)
    hass.services.async_register(DOMAIN, "ai_mode", handle_ai_mode)
    
    # Reload integration on options change
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        manager = hass.data[DOMAIN].get(entry.entry_id)
        if manager:
            await manager.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id)
    if not hass.data[DOMAIN]:
        for service in ["reset_data", "reset_bms_profile", "export_data", "import_data"]:
            hass.services.async_remove(DOMAIN, service)
        
    return unload_ok

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry."""
    from homeassistant.helpers.storage import Store
    from .sensor import STORAGE_VERSION
    store = Store(hass, STORAGE_VERSION, f"energy_management_{entry.entry_id}")
    await store.async_remove()
