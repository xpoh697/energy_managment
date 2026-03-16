"""Utility functions for Energy Management."""
import logging
from typing import Any
from homeassistant.core import State
# v2.1.2 - Verified logging import
_LOGGER = logging.getLogger(__name__)

def normalize_float(val: Any) -> float:
    """Safely convert any value to float, handling comma decimals."""
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(',', '.'))
    except (ValueError, TypeError):
        return 0.0

def get_kwh_val(state_obj: State) -> float | None:
    """Normalize state value to kWh based on unit_of_measurement."""
    if not state_obj or state_obj.state in ("unknown", "unavailable", "None"):
        return None
    
    val = normalize_float(state_obj.state)
    unit = state_obj.attributes.get("unit_of_measurement")
    
    if unit in ("Wh", "W"):
        return val / 1000.0
    elif unit in ("MWh", "MW"):
        return val * 1000.0
    return val

def get_price_from_store(store: dict, date_str: str, hour: str | int) -> float | None:
    """Safely extract price from the internal data store."""
    try:
        day_prices = store.get(date_str, {})
        val = day_prices.get(str(hour))
        if val is None:
            return None
        return normalize_float(val)
    except Exception:
        return None

def round_f(val: Any, precision: int = 2) -> float:
    """Safely round value to float with precision."""
    try:
        v = float(val)
        return float(f"{v:.{precision}f}")
    except (ValueError, TypeError):
        return 0.0
