"""
Global Dispatch Plan Registry for Energy Management System.
Defines the structure for hourly planning slots and the unified plan.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Dict, Any, List, Optional
import json
from .utils import normalize_float
from .const import (
    CONF_BATTERY_MAX_POWER,
    CONF_BATTERY_CAPACITY,
    CONF_MIN_SOC_BAT,
    CONF_PRICE_STOP_SELL,
    CONF_PRICE_SELL_ONLY_PV,
    CONF_SALE_PV_NO_BAT_MAX_HOUR
)

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
    
    # 5m average real-time metrics for slot 0
    avg_gen: Optional[float] = None
    avg_load: Optional[float] = None

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
                "load": round(s.load_total, 2),
                "power": round(s.power_ac, 2),
                "amps": round(s.charge_amps, 1)
            }
            if s.avg_gen is not None:
                res[key]["avg_gen"] = round(s.avg_gen, 2)
            if s.avg_load is not None:
                res[key]["avg_load"] = round(s.avg_load, 2)
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
        abs_hour: Optional[int] = None,
        profiles: Optional[Dict[str, Any]] = None,
        buy_strategy: Optional[Dict[str, Any]] = None,
        sell_strategy: Optional[Dict[str, Any]] = None,
        log_func: Optional[Any] = None,
        avg_gen: Optional[float] = None,
        avg_load: Optional[float] = None
    ) -> tuple:
        """
        Calculates the inverter mode for a given timestamp and SOC.
        FULL PORT of sensor.py _get_mode_at (v11.9.749).
        """
        from homeassistant.util import dt as dt_util
        now_wall = dt_util.now()
        now_h_wall = now_wall.hour
        today_str = dt_now.strftime("%Y-%m-%d")
        sim_h = dt_now.hour

        # 0. Check for HOURLY Manual Overrides
        ts_key = dt_now.strftime("%Y-%m-%d %H:00")
        h_override = manager.hourly_manual_overrides.get(ts_key)
        if h_override:
            return h_override["mode"], f"Manual Override ({ts_key})", h_override.get("mode") == "buy", h_override.get("mode") == "sale_pv_bat", float(h_override.get("soc_limit", 100.0))

        # 0.1 Check for Legacy Manual Overrides (Buttons) - Only for today
        if today_str == now_wall.strftime("%Y-%m-%d"):
            legacy_override = manager.manual_mode_overrides.get(sim_h)
            if legacy_override:
                # Legacy buttons always target 100% for buy, or min_soc for others
                l_target = 100.0 if legacy_override == "buy" else 10.0
                return legacy_override, f"Legacy Manual Override ({sim_h}:00)", legacy_override == "buy", legacy_override == "sale_pv_bat", l_target

        # 1. Fetch Strategies
        if not sell_strategy:
            sell_strategy = manager.get_market_strategy("sell") or {}
        if not buy_strategy:
            buy_strategy = manager.get_market_strategy("buy") or {}

        # 2. Timing & Indices
        now_h_start = now_wall.replace(minute=0, second=0, microsecond=0)
        dt_h_start = dt_now.replace(minute=0, second=0, microsecond=0)
        rel_h = int((dt_h_start - now_h_start).total_seconds() // 3600)
        check_h_abs = sim_h if abs_hour is None else abs_hour

        if is_forecast:
            _now_h_for_forecast = now_h_wall
            if check_h_abs == _now_h_for_forecast:
                is_selling_active = sell_strategy.get("state") == "active"
                is_buying_active = buy_strategy.get("state") == "active"
            else:
                _active_h_sell = sell_strategy.get("active_hours", [])
                is_selling_active = check_h_abs in _active_h_sell
                is_buying_active = check_h_abs in buy_strategy.get("active_hours", [])
                
                # v12.1.0: Dynamic Safety Verification for Forecast Slots moved below load/gen resolution
                pass
        else:
            is_selling_active = sell_strategy.get("state") == "active"
            is_buying_active = buy_strategy.get("state") == "active"

        # 3. Settings & Prices
        from .const import CONF_PRICE_STOP_SELL, CONF_PRICE_SELL_ONLY_PV, CONF_SALE_PV_NO_BAT_MAX_HOUR, CONF_MIN_SOC_BAT, CONF_BATTERY_CAPACITY
        price_stop_sell = float(manager.get_setting(CONF_PRICE_STOP_SELL, 0.0) or 0.0)
        price_sell_only_pv = float(manager.get_setting(CONF_PRICE_SELL_ONLY_PV, 999.0) or 999.0)
        sale_pv_no_bat_max_hour = float(manager.get_setting(CONF_SALE_PV_NO_BAT_MAX_HOUR, 13.0) or 13.0)
        min_soc = float(manager.get_setting(CONF_MIN_SOC_BAT, 10.0) or 10.0)
        
        cur_price = manager.get_price("sell", today_str, sim_h)
        p_sell_val = float(cur_price) if cur_price is not None else 0.0
        buy_p_cur = manager.get_price("buy", today_str, sim_h)
        is_neg_buy = bool(buy_p_cur is not None and buy_p_cur <= 0.0)

        # 4. Gen/Load Data (5m average for real-time, profile for forecast)
        # if log_func: log_func("LDIAG: Loading profiles")
        if not is_forecast:
            if avg_load is None:
                avg_load = float(getattr(manager, "avg_load_5m_kw", 0.5) or 0.5)
            if avg_gen is None:
                avg_gen = float(getattr(manager, "avg_gen_5m_kw", 0.0) or 0.0)
        else:
            h_rel_str = str(sim_h)
            if avg_gen is None or avg_load is None:
                if profiles:
                    prof_gen = profiles.get("gen", {})
                    prof_cons = profiles.get("cons", {})
                else:
                    prof_gen = manager.get_predicted_profile("generation")
                    prof_cons = manager.get_predicted_profile("consumption_total")
                
                if avg_gen is None:
                    avg_gen = float(prof_gen.get(h_rel_str, 0.0) or 0.0)
                if avg_load is None:
                    avg_load = float(prof_cons.get(h_rel_str, 0.5) or 0.5)

        # v12.1.0: Dynamic Safety Verification for Selling Slots
        if is_selling_active:
            floors_sliding = sell_strategy.get("floors_sliding", {})
            safety_floor = floors_sliding.get(check_h_abs)
            if safety_floor is None:
                safety_floor = floors_sliding.get(str(check_h_abs))
            if safety_floor is None:
                # Normalization for "20h" keys
                safety_floor = floors_sliding.get(f"{(check_h_abs % 24):02d}h") or floors_sliding.get(f"{(check_h_abs % 24)}h")
            
            if safety_floor is None:
                is_night = ((check_h_abs % 24) >= 20) or ((check_h_abs % 24) < 8)
                min_soc_sell = float(manager.get_setting("min_soc_sell", 60.0) or 60.0)
                safety_floor = min_soc_sell if is_night else float(sell_strategy.get("arbitrage_sell_debug", {}).get("active_safety_floor", 100.0))
            
            # High-fidelity Ending SOC Verification to prevent target-undershooting
            b_cap = float(manager.get_setting("battery_capacity", 10.0) or 10.0)
            eff = float(sell_strategy.get("arbitrage_sell_debug", {}).get("efficiency", 0.95) or 0.95)
            
            # Robust retrieve of the planned commercial power
            p_sell_comm = sell_strategy.get("raw_commands", {}).get(check_h_abs)
            if p_sell_comm is None:
                p_sell_comm = sell_strategy.get("raw_commands", {}).get(str(check_h_abs), 0.0)
            p_sell_comm = float(p_sell_comm or 0.0)
            
            step_duration = 1.0
            if not is_forecast:
                step_duration = max(0.0, min(1.0, (60.0 - dt_now.minute) / 60.0))
            
            p_sale_dc = p_sell_comm / eff if eff > 0.1 else p_sell_comm
            p_house_dc = max(0.0, avg_load - avg_gen) / eff if eff > 0.1 else max(0.0, avg_load - avg_gen)
            
            soc_drop = ((p_sale_dc + p_house_dc) / b_cap * 100.0) * step_duration if b_cap > 0.1 else 0.0
            projected_soc_end = batt_soc - soc_drop
            
            if batt_soc < (safety_floor - 0.5) or projected_soc_end < (safety_floor - 0.5):
                if log_func:
                    log_func(f"DIAG: Hour {check_h_abs} blocked. SOC start/end ({batt_soc:.1f}% -> {projected_soc_end:.1f}%) < Floor {safety_floor:.1f}%")
                is_selling_active = False

        has_surplus = bool(avg_gen > (avg_load + 0.05))
        is_before_limit_hour = bool(sim_h < sale_pv_no_bat_max_hour)
        
        # 5. Negative Price Waiting Logic
        # if log_func: log_func("LDIAG: Negative price logic")
        neg_h = buy_strategy.get("first_negative_hour")
        can_wait = buy_strategy.get("can_wait_for_negative", False)
        is_gen_night = avg_gen < 0.01
        is_waiting_for_neg = bool(can_wait and neg_h is not None and not is_gen_night)

        # 6. Peak Preparation Logic
        # if log_func: log_func("LDIAG: Peak preparation logic")
        peak_start_abs = sell_strategy.get("next_peak_h")
        if peak_start_abs is None:
            for h in sorted(sell_strategy.get("active_hours", [])):
                if h > check_h_abs:
                    peak_start_abs = h
                    break
        
        is_preparing_for_peak = False
        # (Heuristic from V1: projected_soc_morning_pct etc.)
        morning_soc_proj = (sell_strategy.get("sell_simulation") or {}).get("projected_soc_morning_pct", 0.0)
        target_morning = float(min_soc + 5.0)
        is_low_for_morning = bool(morning_soc_proj < target_morning)
        hit_full_before = (sell_strategy.get("sell_simulation") or {}).get("hit_full_before", False)
        latest_charge_start = (sell_strategy.get("sell_simulation") or {}).get("latest_charge_start", check_h_abs)

        is_profitable_to_save = False
        if peak_start_abs is not None:
            deg_cost = float(manager.get_setting("degradation_cost", 0.15) or 0.15)
            peak_p = manager.get_price("sell", today_str, peak_start_abs % 24) or 0.0
            if (peak_p - deg_cost) > p_sell_val and batt_soc < 90.0:
                is_profitable_to_save = True
        
        # =====================================================================
        # STATE MACHINE LADDER (V1 Parity)
        # =====================================================================
        # if log_func: log_func("LDIAG: Entering State Machine Ladder")
        mode = "sale_pv"
        reason = "Стандартная работа"
        target_soc = 100.0

        # P1: Emergency
        if round(batt_soc, 1) <= min_soc:
            mode = "bat_emergency"
            reason = f"Заряд ({round(batt_soc, 1)}%) <= Минимума ({min_soc}%): Ожидание добора"
            target_soc = 100.0
        
        # P2: Negative Buy
        elif is_neg_buy:
            mode = "buy"
            reason = "Отрицательная цена"
            target_soc = 100.0
        
        # P3: AI Buy
        elif is_buying_active:
            mode = "buy"
            reason = buy_strategy.get("charge_reason", "Активна стратегия ПОКУПКИ")
            
            # v12.0.83: Target SOC must match the hourly strategy plan, not the global plan
            global_t_soc = float(buy_strategy.get("target_soc", 100.0))
            hour_key = f"{abs_hour%24:02d}:00"
            hourly_plan = buy_strategy.get("planned_power_per_h", {}).get(hour_key, {})
            target_soc = float(hourly_plan.get("soc", global_t_soc))
            
        # P4: AI Sell (Elevated Priority in v11.9.691)
        elif is_selling_active:
            mode = "sale_pv_bat"
            reason = sell_strategy.get("strategy_decision", "Активна стратегия ПРОДАЖИ (AI)")
            target_soc = min_soc
            
        elif cur_price is not None and cur_price >= price_sell_only_pv:
            _block_sale_pv_no_bat = bool((dt_now.date() == manager.now.date()) and (check_h_abs >= latest_charge_start))
            if is_before_limit_hour and has_surplus and not _block_sale_pv_no_bat and cur_price > 0:
                mode = "sale_pv_no_bat"
                reason = f"Продажа только солнца: Цена ({p_sell_val:.2f}) >= Порога ({price_sell_only_pv:.2f}), утро"
                target_soc = min_soc
            else:
                mode = "sale_pv"
                target_soc = min_soc
                if is_low_for_morning: reason = f"Защита Gatekeeper: Рассвет {morning_soc_proj:.1f}% < {target_morning:.1f}%"
                elif is_profitable_to_save: reason = "Сохранение заряда: Пик выгоднее текущей цены"
                elif _block_sale_pv_no_bat: reason = f"Окно продажи PV закрыто (лимит {latest_charge_start}:00)"
                else: reason = "Стандартная работа (ожидание команды AI)"

        # P6: Wait for negative price
        elif is_waiting_for_neg:
            has_significant_deficit = bool(avg_load > (avg_gen + 0.5))
            if cur_price is not None and cur_price >= price_sell_only_pv and has_surplus and is_before_limit_hour:
                mode = "sale_pv_no_bat"
                reason = "Продажа только солнца (ожидаем отрицательную цену)"
            elif not has_significant_deficit:
                mode = "no_pv_sale_no_bat"
                neg_h_disp = neg_h if neg_h < 24 else f"{neg_h-24} (Завтра)"
                reason = f"Ожидание отриц. цен ({neg_h_disp}г): Экономим место"
            else:
                mode = "sale_pv"
                reason = "Ожидание отриц. цен: Высокая нагрузка (sale_pv)"

        # P7: Price Floor
        elif cur_price is not None and p_sell_val < price_stop_sell:
            mode = "stop_sale"
            reason = f"Продажа заблокирована: Цена ({p_sell_val:.2f}) < Порога ({price_stop_sell:.2f})"

        # P8: Missing prices
        elif cur_price is None:
            mode = "sale_pv"
            reason = "Нет данных о цене"

        # P9: Standard
        else:
            mode = "sale_pv"
            reason = f"Стандартная работа: Цена ({p_sell_val:.2f}) выше порога"

        return mode, reason, mode == "buy", mode == "sale_pv_bat", target_soc

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
        max_batt_p = float(normalize_float(manager.get_setting(CONF_BATTERY_MAX_POWER, 3.0)))
        
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
            
            # v12.0.84: Calculate amps dynamically from the planned power rather than global limit
            v_val = manager.get_sensor_float(manager.battery_voltage_sensor) or 52.0
            c_amps_fixed = round((p_val * 1000.0) / max(10.0, v_val), 2)
            
            if h_override and h_override.get("mode") == "buy":
                f_target_soc = float(h_override.get("soc_limit", t_soc))
                if batt_soc < (f_target_soc - 0.05):
                    eff = 0.98
                    b_cap = float(manager.get_setting(CONF_BATTERY_CAPACITY, 10.0))
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
            
            # v12.0.84: Calculate amps dynamically from the planned power rather than global limit
            v_val = manager.get_sensor_float(manager.battery_voltage_sensor) or 52.0
            c_amps_fixed = round((p_val * 1000.0) / max(10.0, v_val), 2)
            
            if h_override and h_override.get("mode") == "sale_pv_bat":
                t_soc = float(h_override.get("soc_limit", t_soc))
                if batt_soc > (t_soc + 0.2):
                    eff = 0.98
                    b_cap = float(manager.get_setting(CONF_BATTERY_CAPACITY, 10.0))
                    time_fraction = max(0.01, (60.0 - now.minute) / 60.0)
                    delta_soc = max(0.0, batt_soc - t_soc)
                    delta_kwh = (delta_soc / 100.0) * b_cap
                    req_p = (delta_kwh / time_fraction) * eff
                    p_val = min(max_batt_p, round(req_p, 2))
                    
                    v_val = manager.get_sensor_float(manager.battery_voltage_sensor) or 52.0
                    c_amps_fixed = round((p_val * 1000.0) / max(10.0, v_val), 2)

        return p_val, t_soc, c_amps_fixed
