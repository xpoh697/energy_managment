import json
from datetime import datetime, timedelta

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

def get_average_profile(profile_type, days):
    profile = {}
    profile_data = data.get(profile_type, {})
    for h in range(24):
        sh = str(h)
        history = profile_data.get(sh, [])
        valid_vals = [v.get('v', 0.0) for v in history]
        if valid_vals:
            profile[str(h)] = sum(valid_vals) / len(valid_vals)
        else:
            profile[str(h)] = 0.0
    return profile

prof_today = get_average_profile("consumption_base", 14)

print("Budget calculation for each hour:")
for cur_h in range(24):
    expected_consumption = 0.0
    # From cur_h to 24
    for h in range(cur_h, 24):
        expected_consumption += prof_today.get(str(h), 0.0)
    # From 0 to 8 tomorrow
    for h in range(0, 8):
        expected_consumption += prof_today.get(str(h), 0.0)
    
    forecast_adj = 20 # Dummy
    batt_energy = 5 # Dummy
    budget = (forecast_adj + batt_energy) - expected_consumption
    print(f"  Hour {cur_h:02d}: Cons={expected_consumption:.2f}, Budget={budget:.2f}")

print("\nChecking for any huge values in any profile...")
for ptype in ['generation', 'consumption_total', 'consumption_base']:
    for h in range(24):
        for item in data.get(ptype, {}).get(str(h), []):
            v = item.get('v', 0)
            if v > 100 or v < -100:
                print(f"ANOMALY: {ptype} h={h} v={v}")
