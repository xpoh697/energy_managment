"""Dynamic Programming engine for HACS Energy Scheduler."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Action type constants
ACT_SOL = 0
ACT_DIS = 1
ACT_PV_CHARGE = 2
ACT_GRID_CHARGE = 3
ACT_SELF_CONSUME = 4
ACT_PAID_IMPORT = 5


@dataclass
class DPConfig:
    """Configuration parameters needed by the DP engine."""

    min_sell_price: float
    battery_max_discharge_power: float
    battery_max_charge_power: float
    battery_min_soc: int
    battery_capacity: float


def hours_from_now(price_entry: dict) -> float:
    """Calculate hours from now for a price entry."""
    now = dt_util.now()
    entry_date = price_entry.get("date", now.strftime("%Y-%m-%d"))
    entry_hour = price_entry.get("hour", 0)

    try:
        entry_time = datetime.strptime(f"{entry_date} {entry_hour}:00", "%Y-%m-%d %H:%M")
        entry_time = entry_time.replace(tzinfo=now.tzinfo)
        return (entry_time - now).total_seconds() / 3600
    except ValueError:
        return 0


def run_unified_dp(
    slots: list[dict[str, Any]],
    current_usable: float,
    usable_capacity: float,
    cycle_cost: float,
    terminal_value_per_kwh: float,
    min_end_usable: float,
    config: DPConfig,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Unified DP with six inverter actions.

    DIS: discharge battery to grid (Sell All mode)
    SOL: battery idle, PV surplus to grid (Sell Surplus mode)
    PV_CHARGE: PV surplus charges battery, overflow to grid (Default mode)
    GRID_CHARGE: charge battery from grid (Buy mode)
    SELF_CONSUME: battery covers consumption deficit (no grid export)
    PAID_IMPORT: home from grid + PV curtailed (only when buy_price < 0, paid to consume)

    Returns (charge_hours, discharge_hours, pv_charge_hours, self_consume_hours,
    paid_import_hours, stats).
    """
    empty_stats = {
        "slot_count": len(slots),
        "initial_usable": current_usable,
        "terminal_value_per_kwh": terminal_value_per_kwh,
        "min_end_usable": min_end_usable,
        "planned_export_kwh": 0.0,
        "planned_grid_charge_kwh": 0.0,
        "planned_paid_import_kwh": 0.0,
        "pv_charge_hours": 0,
        "paid_import_hours": 0,
    }
    if not slots or usable_capacity <= 0:
        return [], [], [], [], [], empty_stats

    energy_step = 0.1
    max_energy_idx = max(0, int(round(usable_capacity / energy_step)))
    initial_idx = min(max_energy_idx, max(0, int(round(current_usable / energy_step))))
    neg_inf = float("-inf")

    n_slots = len(slots)
    dp: list[list[float]] = [
        [neg_inf] * (max_energy_idx + 1) for _ in range(n_slots + 1)
    ]
    prev_state: list[list[int]] = [
        [-1] * (max_energy_idx + 1) for _ in range(n_slots + 1)
    ]
    prev_type: list[list[int]] = [
        [ACT_SOL] * (max_energy_idx + 1) for _ in range(n_slots + 1)
    ]
    prev_amount: list[list[float]] = [
        [0.0] * (max_energy_idx + 1) for _ in range(n_slots + 1)
    ]

    dp[0][initial_idx] = 0.0

    for slot_idx, slot in enumerate(slots, start=1):
        sell_price = slot.get("sell_price", 0.0)
        buy_price = slot.get("buy_price", 0.0)
        pv_kwh = slot.get("pv_kwh", 0.0)
        consumption_kwh = slot.get("consumption_kwh", 0.0) + slot.get("ev_kwh", 0.0)
        pv_surplus = max(0.0, pv_kwh - consumption_kwh)
        pv_deficit = max(0.0, consumption_kwh - pv_kwh)

        for state_idx, current_value in enumerate(dp[slot_idx - 1]):
            if current_value == neg_inf:
                continue

            usable_energy = state_idx * energy_step

            def _update(nsi: int, rwd: float, act: int, amt: float) -> None:
                val = current_value + rwd
                if val > dp[slot_idx][nsi]:
                    dp[slot_idx][nsi] = val
                    prev_state[slot_idx][nsi] = state_idx
                    prev_type[slot_idx][nsi] = act
                    prev_amount[slot_idx][nsi] = amt

            # === SOL: battery idle, PV surplus -> grid ===
            _update(state_idx, sell_price * pv_surplus - buy_price * pv_deficit, ACT_SOL, 0.0)

            # === DIS: discharge battery to grid ===
            if sell_price > config.min_sell_price and sell_price > 0:
                max_exp = min(config.battery_max_discharge_power, usable_energy)
                for ei in range(1, int(round(max_exp / energy_step)) + 1):
                    exp = ei * energy_step
                    nsi = min(max_energy_idx, max(0, int(round((usable_energy - exp) / energy_step))))
                    to_grid = max(0.0, exp + pv_kwh - consumption_kwh)
                    grid_imp = max(0.0, consumption_kwh - exp - pv_kwh)
                    _update(nsi, sell_price * to_grid - cycle_cost * exp - buy_price * grid_imp, ACT_DIS, exp)

            # === PV_CHARGE: PV surplus -> battery, overflow -> grid ===
            # Tiny tie-break bonus: when rewards are equal (e.g. sell_price = 0),
            # prefer storing PV in the battery over passive export. Epsilon is
            # orders of magnitude smaller than any realistic price, so it cannot
            # flip an economically meaningful decision.
            avail_cap = usable_capacity - usable_energy
            if pv_surplus > 0 and avail_cap >= energy_step:
                max_pvc = min(pv_surplus, avail_cap, config.battery_max_charge_power)
                for ci in range(1, int(max_pvc / energy_step) + 1):
                    chg = ci * energy_step
                    nsi = min(max_energy_idx, max(0, int(round((usable_energy + chg) / energy_step))))
                    reward = sell_price * max(0.0, pv_surplus - chg) - buy_price * pv_deficit
                    reward += 1e-6 * chg
                    _update(nsi, reward, ACT_PV_CHARGE, chg)

            # === GRID_CHARGE: charge battery from grid ===
            # Allowed even with PV surplus: when buy_price is negative, paid grid
            # charge can outvalue free PV charge. Reward formula already handles
            # both cases — sell_price * pv_surplus accounts for excess PV when
            # battery is filled from grid.
            if avail_cap >= energy_step:
                max_gc = min(config.battery_max_charge_power, avail_cap)
                for ci in range(1, int(max_gc / energy_step) + 1):
                    chg = ci * energy_step
                    nsi = min(max_energy_idx, max(0, int(round((usable_energy + chg) / energy_step))))
                    _update(nsi, sell_price * pv_surplus - buy_price * (chg + pv_deficit) - cycle_cost * chg, ACT_GRID_CHARGE, chg)

            # === SELF_CONSUME: battery covers consumption deficit (no grid export) ===
            if pv_deficit >= energy_step and usable_energy >= energy_step:
                max_sc = min(usable_energy, pv_deficit)
                for sci in range(1, int(round(max_sc / energy_step)) + 1):
                    sc = sci * energy_step
                    nsi = min(max_energy_idx, max(0, int(round((usable_energy - sc) / energy_step))))
                    remaining_deficit = max(0.0, pv_deficit - sc)
                    # No cycle cost — shallow self-consume cycling
                    _update(nsi, -buy_price * remaining_deficit, ACT_SELF_CONSUME, sc)

            # === PAID_IMPORT: home from grid, PV curtailed, battery untouched ===
            # Useful only when buy_price < 0 (we get paid to consume from grid).
            # Battery state unchanged. PV would be lost anyway in negative-price
            # hours (sell typically 0), so curtailment has no opportunity cost.
            if buy_price < 0 and consumption_kwh >= energy_step:
                _update(state_idx, -buy_price * consumption_kwh, ACT_PAID_IMPORT, 0.0)

    # Terminal value with reserve enforcement
    min_end_idx = max(0, int(round(min_end_usable / energy_step)))

    best_final_idx = 0
    best_total_value = neg_inf
    for state_idx, value in enumerate(dp[n_slots]):
        if value == neg_inf:
            continue
        if state_idx < min_end_idx:
            continue
        usable_energy = state_idx * energy_step
        total_value = value + usable_energy * terminal_value_per_kwh
        if total_value > best_total_value:
            best_total_value = total_value
            best_final_idx = state_idx

    if best_total_value == neg_inf:
        for state_idx, value in enumerate(dp[n_slots]):
            if value == neg_inf:
                continue
            usable_energy = state_idx * energy_step
            total_value = value + usable_energy * terminal_value_per_kwh
            if total_value > best_total_value:
                best_total_value = total_value
                best_final_idx = state_idx
        if best_total_value != neg_inf:
            _LOGGER.warning(
                "Could not satisfy energy reserve %.1f kWh, relaxing constraint",
                min_end_usable,
            )

    if best_total_value == neg_inf:
        return [], [], [], [], [], empty_stats

    # Backtrack
    types_by_slot = [ACT_SOL] * n_slots
    amounts_by_slot = [0.0] * n_slots
    state_idx = best_final_idx
    for slot_idx in range(n_slots, 0, -1):
        types_by_slot[slot_idx - 1] = prev_type[slot_idx][state_idx]
        amounts_by_slot[slot_idx - 1] = prev_amount[slot_idx][state_idx]
        state_idx = prev_state[slot_idx][state_idx]
        if state_idx < 0:
            break

    # Build result lists
    charge_hours: list[dict[str, Any]] = []
    discharge_hours: list[dict[str, Any]] = []
    pv_charge_hours: list[dict[str, Any]] = []
    self_consume_hours: list[dict[str, Any]] = []
    paid_import_hours: list[dict[str, Any]] = []
    usable_energy = current_usable
    total_export = 0.0
    total_battery_discharge = 0.0
    total_grid_charge = 0.0
    total_paid_import = 0.0

    for slot, act, amount in zip(slots, types_by_slot, amounts_by_slot, strict=False):
        start_usable = usable_energy

        if act == ACT_DIS and amount > 0:
            end_usable = usable_energy - amount
            total_battery_discharge += amount
            total_consumption = slot["consumption_kwh"] + slot.get("ev_kwh", 0.0)
            home_deficit = max(0.0, total_consumption - slot["pv_kwh"])
            battery_to_home = min(amount, home_deficit)
            battery_to_grid = max(0.0, amount - battery_to_home)
            grid_import = max(0.0, home_deficit - amount)
            total_export += battery_to_grid
            soc_limit = max(
                config.battery_min_soc,
                config.battery_min_soc + (max(0.0, end_usable) / config.battery_capacity * 100),
            )
            discharge_hours.append({
                "date": slot["date"],
                "hour": slot["hour"],
                "value": slot["sell_price"],
                "buy_price": slot["buy_price"],
                "pv_kwh": slot["pv_kwh"],
                "consumption_kwh": slot["consumption_kwh"],
                "ev_kwh": slot.get("ev_kwh", 0.0),
                "profit": slot["sell_price"] - cycle_cost,
                "hours_from_now": hours_from_now(slot),
                "planned_energy_kwh": round(amount, 2),
                "planned_battery_out_kwh": round(amount, 2),
                "planned_export_kwh": round(battery_to_grid, 2),
                "planned_home_supply_kwh": round(battery_to_home, 2),
                "planned_grid_import_kwh": round(grid_import, 2),
                "soc_limit": round(soc_limit, 2),
                "expected_start_usable_kwh": round(start_usable, 2),
                "expected_end_usable_kwh": round(end_usable, 2),
            })
            usable_energy = end_usable

        elif act == ACT_PV_CHARGE and amount > 0:
            end_usable = min(usable_capacity, usable_energy + amount)
            pv_charge_hours.append({
                "date": slot["date"],
                "hour": slot["hour"],
                "charge_kwh": round(amount, 2),
                "pv_kwh": slot["pv_kwh"],
                "sell_price": slot["sell_price"],
                "expected_start_usable_kwh": round(start_usable, 2),
                "expected_end_usable_kwh": round(end_usable, 2),
            })
            usable_energy = end_usable

        elif act == ACT_GRID_CHARGE and amount > 0:
            end_usable = min(usable_capacity, usable_energy + amount)
            total_grid_charge += amount
            charge_hours.append({
                "date": slot["date"],
                "hour": slot["hour"],
                "value": slot["buy_price"],
                "effective_price": slot["buy_price"] + cycle_cost,
                "hours_from_now": hours_from_now(slot),
                "planned_energy_kwh": round(amount, 2),
                "expected_start_usable_kwh": round(start_usable, 2),
                "expected_end_usable_kwh": round(end_usable, 2),
            })
            usable_energy = end_usable

        elif act == ACT_SELF_CONSUME and amount > 0:
            end_usable = max(0.0, usable_energy - amount)
            self_consume_hours.append({
                "date": slot["date"],
                "hour": slot["hour"],
                "sell_price": slot["sell_price"],
                "buy_price": slot["buy_price"],
                "pv_kwh": slot["pv_kwh"],
                "consumption_kwh": slot["consumption_kwh"],
                "planned_energy_kwh": round(amount, 2),
                "expected_start_usable_kwh": round(start_usable, 2),
                "expected_end_usable_kwh": round(end_usable, 2),
            })
            usable_energy = end_usable

        elif act == ACT_PAID_IMPORT:
            total_consumption = slot["consumption_kwh"] + slot.get("ev_kwh", 0.0)
            total_paid_import += total_consumption
            paid_import_hours.append({
                "date": slot["date"],
                "hour": slot["hour"],
                "buy_price": slot["buy_price"],
                "consumption_kwh": slot["consumption_kwh"],
                "ev_kwh": slot.get("ev_kwh", 0.0),
                "pv_kwh": slot["pv_kwh"],
                "planned_grid_import_kwh": round(total_consumption, 2),
                "expected_revenue": round(-slot["buy_price"] * total_consumption, 4),
                "expected_start_usable_kwh": round(start_usable, 2),
                "expected_end_usable_kwh": round(start_usable, 2),
            })
            # battery state unchanged

        # else: SOL -- battery unchanged

    return charge_hours, discharge_hours, pv_charge_hours, self_consume_hours, paid_import_hours, {
        "slot_count": n_slots,
        "initial_usable": round(current_usable, 2),
        "terminal_value_per_kwh": round(terminal_value_per_kwh, 4),
        "min_end_usable": round(min_end_usable, 2),
        "planned_battery_discharge_kwh": round(total_battery_discharge, 2),
        "planned_export_kwh": round(total_export, 2),
        "planned_grid_charge_kwh": round(total_grid_charge, 2),
        "planned_paid_import_kwh": round(total_paid_import, 2),
        "pv_charge_hours": len(pv_charge_hours),
        "paid_import_hours": len(paid_import_hours),
        "best_value": round(best_total_value, 2),
        "end_usable_kwh": round(usable_energy, 2),
    }
