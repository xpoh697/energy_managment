import logging
from pathlib import Path
from aiohttp import web

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.components import frontend, websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.loader import async_get_integration
import voluptuous as vol

from .const import DOMAIN, VERSION

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "number", "switch"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Energy Management component."""
    hass.data.setdefault(DOMAIN, {})
    _async_register_ws_version(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Energy Profile from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Register the static HTTP view immediately to prevent 404 errors during early boot
    www_path = Path(__file__).parent / "www"
    hass.http.register_view(CardStaticView(www_path))
    
    # Defer Lovelace card database resource registration to prevent startup deadlocks
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    
    if hass.state == CoreState.running:
        hass.async_create_task(_async_register_card(hass))
    else:
        async def _register_card_after_start(event):
            await _async_register_card(hass)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_card_after_start)
    
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

    async def handle_set_hourly_override(call):
        timestamp = call.data.get("timestamp")
        mode = call.data.get("mode")
        soc_limit = call.data.get("soc_limit", 100.0)
        _LOGGER.warning(f"[Service Call] set_hourly_override: timestamp={timestamp}, mode={mode}, soc={soc_limit}")
        await manager.async_set_hourly_override(timestamp, mode, soc_limit)

    hass.services.async_register(DOMAIN, "force_buy", handle_force_buy)
    hass.services.async_register(DOMAIN, "stop_sale", handle_stop_sale)
    hass.services.async_register(DOMAIN, "ai_mode", handle_ai_mode)
    hass.services.async_register(DOMAIN, "set_hourly_override", handle_set_hourly_override)
    
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


@callback
def _async_register_ws_version(hass: HomeAssistant) -> None:
    """Register a WebSocket command that reports the current integration version."""

    @callback
    def _ws_version(hass_: HomeAssistant, connection, msg) -> None:
        async def _send() -> None:
            connection.send_result(msg["id"], {"version": VERSION})

        hass_.async_create_task(_send())

    websocket_api.async_register_command(
        hass,
        f"{DOMAIN}/version",
        _ws_version,
        websocket_api.BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {vol.Required("type"): f"{DOMAIN}/version"}
        ),
    )

async def _async_register_card(hass: HomeAssistant) -> None:
    """Register the Lovelace card with a cache-busting version query string."""
    card_url = f"/api/{DOMAIN}/static/energy-management-card.js?v={VERSION}"

    # Try to register as a Lovelace resource (Storage Mode)
    registered_as_resource = await _async_register_lovelace_resource(hass, card_url)
    if not registered_as_resource:
        # Fallback for YAML mode
        frontend.add_extra_js_url(hass, card_url)
        _LOGGER.debug("Registered card via extra_js_url fallback: %s", card_url)
    else:
        _LOGGER.debug("Registered card via Lovelace resource: %s", card_url)

async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Create or update the Lovelace resource entry for the card."""
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        return False

    resources = getattr(lovelace_data, "resources", None)
    if resources is None:
        return False

    if not hasattr(resources, "async_create_item") or not hasattr(resources, "async_update_item"):
        return False



    existing = None
    try:
        for item in resources.async_items():
            if "energy-management-card.js" in item.get("url", ""):
                existing = item
                break
    except Exception:
        return False

    try:
        if existing is not None:
            if existing.get("url") != url:
                await resources.async_update_item(existing["id"], {"res_type": "module", "url": url})
                _LOGGER.info("Updated Lovelace resource: %s", url)
        else:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Created Lovelace resource: %s", url)
    except Exception as err:
        _LOGGER.warning("Failed to register Lovelace resource: %s", err)
        return False

    return True

class CardStaticView(HomeAssistantView):
    """View to serve static card files with CORS."""
    url = f"/api/{DOMAIN}/static/{{filename}}"
    name = f"api:{DOMAIN}:static"
    requires_auth = False
    cors_allowed = True

    def __init__(self, www_path: Path) -> None:
        self._www_path = www_path

    async def get(self, request, filename: str):
        """Handle GET request for static files."""
        if filename != "energy-management-card.js":
            return web.Response(status=404)

        file_path = self._www_path / filename
        if not file_path.exists():
            return web.Response(status=404)

        try:
            return web.FileResponse(file_path)
        except Exception:
            return web.Response(status=500)

