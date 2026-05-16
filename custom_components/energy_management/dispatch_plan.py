"""
Global Dispatch Plan Registry for Energy Management System.
Defines the structure for hourly planning slots and the unified plan.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Any, List, Optional
import json

@dataclass
class GlobalSlot:
    """A single hourly slot in the dispatch plan."""
    hour_abs: int  # Absolute hour offset from now (0-47)
    dt_iso: str    # ISO timestamp for UI
    
    # Inverter Command (Final decision)
    mode: str = "sale_pv"
    power_ac: float = 0.0
    charge_amps: float = 0.0
    target_soc: float = 100.0
    reason: str = "Standard"
    
    # Financials
    price_buy: float = 0.0
    price_sell: float = 0.0
    
    # Forecasts (Inputs)
    gen_raw: float = 0.0
    gen_adj: float = 0.0
    load_base: float = 0.0
    load_total: float = 0.0
    
    # Projections (Outputs of simulation)
    soc_start: float = 0.0
    soc_end: float = 0.0
    net_p_bat: float = 0.0  # Real battery flow in simulation
    
    # Flags & Metadata
    is_manual: bool = False
    is_locked: bool = False
    strategy_source: str = "Heuristics"  # 'buy', 'sell', 'manual', 'heuristics'
    
    # Debug containers (preserving existing sensor logic)
    buy_debug: Dict[str, Any] = field(default_factory=dict)
    sell_debug: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Returns a dict representation for JSON serialization."""
        return asdict(self)

class DispatchPlan:
    """Unified 48-hour plan containing GlobalSlots."""
    def __init__(self, slots: List[GlobalSlot]):
        self.slots = slots
        self._last_updated = datetime.now()

    def get_slot(self, hour_abs: int) -> Optional[GlobalSlot]:
        if 0 <= hour_abs < len(self.slots):
            return self.slots[hour_abs]
        return None

    def to_json(self) -> str:
        """Serializes the plan for UI attributes."""
        return json.dumps([s.to_dict() for s in self.slots])

    def to_hourly_data_attr(self) -> Dict[str, Any]:
        """Converts plan to the legacy 'hourly_data' attribute format (preserving compatibility)."""
        res = {}
        for s in self.slots:
            # Match the legacy key format: "YYYY-MM-DD HH:00"
            dt_obj = datetime.fromisoformat(s.dt_iso)
            key = dt_obj.strftime("%Y-%m-%d %H:00")
            res[key] = {
                "sell_price": round(s.price_sell, 2),
                "buy_price": round(s.price_buy, 2),
                "mode": s.mode,
                "soc": round(s.soc_end, 2),
                "soc_limit": round(s.target_soc, 1),
                "is_manual": s.is_manual,
                "reason": s.reason,
                "gen": round(s.gen_raw, 2),
                "load": round(s.load_total, 2)
            }
        return res

    def to_planned_modes_24h(self) -> Dict[str, str]:
        """Converts plan to the legacy 'planned_modes_24h' format (preserving clean look)."""
        res = {}
        for s in self.slots[:24]:
            dt_obj = datetime.fromisoformat(s.dt_iso)
            key = dt_obj.strftime("%H:00")
            
            # Legacy format: "mode (price): reason"
            price_tag = f" (SP: {s.price_sell:.2f})" if "sell" in s.mode or s.mode == "sale_pv" else f" (BP: {s.price_buy:.2f})"
            
            # Smart Forecast Logic: Hide 'boring' reasons (v11.9.749 parity)
            is_boring = any(s.reason.startswith(p) for p in ["Стандартная работа", "Экономия", "Значения по умолчанию"])
            
            if is_boring:
                res[key] = f"{s.mode}{price_tag}"
            else:
                res[key] = f"{s.mode}{price_tag}: \"{s.reason}\""
        return res

class EnergyLogicEngine:
    """Pure logic engine for inverter mode and power decisions."""
    
    @staticmethod
    def get_mode_at(
        dt_now: datetime, 
        batt_soc: float, 
        manager: Any, 
        is_forecast: bool = False,
        abs_hour: Optional[int] = None
    ) -> tuple:
        """
        Calculates the inverter mode for a given timestamp and SOC.
        Mirror of sensor.py _get_mode_at (v11.9.749).
        """
        # In HA environment, we'll pass dt_util from manager
        from homeassistant.util import dt as dt_util
        now_wall = dt_util.now()
        now_h_wall = now_wall.hour
        
        # 0. Check for HOURLY Manual Overrides
        ts_key = dt_now.strftime("%Y-%m-%d %H:00")
        h_override = manager.hourly_manual_overrides.get(ts_key)
        if h_override:
            return h_override["mode"], f"Manual Override ({ts_key})", False, False

        today_str = dt_now.strftime("%Y-%m-%d")
        sim_h = dt_now.hour
        
        # Calculate relative hour from simulation start
        now_h_start = now_wall.replace(minute=0, second=0, microsecond=0)
        dt_h_start = dt_now.replace(minute=0, second=0, microsecond=0)
        rel_h = int((dt_h_start - now_h_start).total_seconds() // 3600)
        check_h_abs = sim_h if abs_hour is None else abs_hour

        # Strategy results
        sell_strategy = manager.get_market_strategy("sell") or {}
        buy_strategy = manager.get_market_strategy("buy") or {}
        
        if is_forecast:
            _now_h_for_forecast = now_h_wall
            if check_h_abs == _now_h_for_forecast:
                is_selling_active = sell_strategy.get("state") == "active"
                is_buying_active = buy_strategy.get("state") == "active"
            else:
                _active_h_sell = sell_strategy.get("active_hours", [])
                is_selling_active = check_h_abs in _active_h_sell
                is_buying_active = check_h_abs in buy_strategy.get("active_hours", [])
        else:
            is_selling_active = sell_strategy.get("state") == "active"
            is_buying_active = buy_strategy.get("state") == "active"

        # 1. Base Decision (Standard Ladder)
        cur_price_sell = manager.get_price("sell", today_str, sim_h)
        # Handle None price gracefully
        p_sell_val = float(cur_price_sell) if cur_price_sell is not None else 0.0
        
        price_stop_sell = float(manager.get_setting("price_stop_sell", 0.0) or 0.0)
        min_soc = float(manager.get_setting("min_soc_bat", 10.0) or 10.0)
        
        # v11.9.749 Logic Tree
        mode = "sale_pv"
        reason = "Стандартная работа"

        if is_buying_active:
            mode = "buy"
            reason = buy_strategy.get("charge_reason", "Покупка")
        elif is_selling_active:
            mode = "sale_pv_bat"
            reason = sell_strategy.get("strategy_decision", "Продажа")
        elif p_sell_val < price_stop_sell:
            mode = "no_pv_sale_no_bat"
            reason = "Ожидание отрицательных цен"
        elif batt_soc < min_soc + 2.0:
            mode = "sale_pv"
            reason = "Экономия (Low SOC)"
            
        return mode, reason, is_buying_active, is_selling_active

    @staticmethod
    def calculate_realtime_power(
        mode: str,
        now: datetime,
        batt_soc: float,
        manager: Any,
        buy_strategy: dict,
        sell_strategy: dict,
        h_override: Optional[dict] = None
    ) -> tuple:
        """
        Original power calculation logic from sensor.py (v11.9.749).
        Returns (p_val, t_soc, c_amps_fixed).
        """
        p_val = 0.0
        t_soc = batt_soc
        c_amps_fixed = 0.0
        max_batt_p = float(manager.get_setting("battery_max_power", 3.0))
        
        # v11.9.452: Manual Power Sync calculation
        if mode == "buy":
            # (Logic from lines 3216-3277 of sensor.py)
            hour_key = f"{now.hour:02d}:00"
            plan = buy_strategy.get("planned_power_per_h", {})
            h_plan = plan.get(hour_key)
            
            if isinstance(h_plan, dict):
                p_val = h_plan.get("power", 0.0)
                t_soc = h_plan.get("soc", 0.0)
            else:
                p_val = buy_strategy.get("recommended_power_kw", 0.0)
                t_soc = buy_strategy.get("target_soc", 0.0)
            c_amps_fixed = buy_strategy.get("recommended_amps", 0.0)
            
            if h_override and h_override.get("mode") == "buy":
                f_target_soc = float(h_override.get("soc_limit", t_soc))
                if batt_soc < (f_target_soc - 0.05):
                    eff = 0.98
                    b_cap = float(manager.get_setting("battery_capacity_kwh", 10.0))
                    time_fraction = max(0.01, (60.0 - now.minute) / 60.0)
                    
                    delta_soc = max(0.0, f_target_soc - batt_soc)
                    delta_kwh = (delta_soc / 100.0) * b_cap
                    p_calc = (delta_kwh / time_fraction) / eff
                    p_val = min(max_batt_p, round(p_calc, 2))
                    t_soc = f_target_soc
                    
                    v_val = manager.get_sensor_float(manager.battery_voltage_sensor) or 52.0
                    c_amps_fixed = round((p_val * 1000.0) / max(10.0, v_val), 2)
                    
        elif mode == "sale_pv_bat":
            # (Logic from lines 3283-3330 of sensor.py)
            hour_key = f"{now.hour:02d}:00"
            plan = sell_strategy.get("planned_power_per_h", {})
            h_plan = plan.get(hour_key)
            
            if isinstance(h_plan, dict):
                p_val = h_plan.get("power", 0.0)
                t_soc = h_plan.get("soc", 0.0)
            else:
                p_val = sell_strategy.get("recommended_power_kw", 0.0)
                t_soc = sell_strategy.get("target_soc", 0.0)
            c_amps_fixed = sell_strategy.get("recommended_amps", 0.0)
            
            if h_override and h_override.get("mode") == "sale_pv_bat":
                t_soc = float(h_override.get("soc_limit", t_soc))
                if batt_soc > (t_soc + 0.2):
                    eff = 0.98
                    b_cap = float(manager.get_setting("battery_capacity_kwh", 10.0))
                    time_fraction = max(0.01, (60.0 - now.minute) / 60.0)
                    delta_soc = max(0.0, batt_soc - t_soc)
                    delta_kwh = (delta_soc / 100.0) * b_cap
                    req_p = (delta_kwh / time_fraction) * eff
                    p_val = min(max_batt_p, round(req_p, 2))
                    
                    v_val = manager.get_sensor_float(manager.battery_voltage_sensor) or 52.0
                    c_amps_fixed = round((p_val * 1000.0) / max(10.0, v_val), 2)

        return p_val, t_soc, c_amps_fixed
