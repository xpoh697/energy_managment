import json
from datetime import datetime

path = r'\\192.168.100.5\config\energy_management_backup.json'
try:
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    print("--- Diagnostic Forecast Data ---")
    
    # Get current hour
    now = datetime.now()
    cur_h = now.hour
    print(f"Current local hour: {cur_h}")

    # SOC and Capacity
    soc = data.get('last_known_soc', 'N/A')
    cap = data.get('last_known_cap', 'N/A')
    print(f"Last known SOC: {soc}%")
    print(f"Last known Capacity: {cap} kWh")

    # Profiles for generation
    gen_profile = data.get('generation', {})
    print("\nGeneration Profile (Historical Averages):")
    total_hist_gen = 0
    for h in range(24):
        sh = str(h)
        vals = gen_profile.get(sh, [])
        if vals:
            # Calculate average for this hour from history
            avg_v = sum(item.get('v', 0) if isinstance(item, dict) else item for item in vals) / len(vals)
            total_hist_gen += avg_v
            if 10 <= h <= 18:
                print(f"  {h:02d}:00 -> {avg_v:.3f} kWh")
    print(f"Total historical daily generation: {total_hist_gen:.2f} kWh")

    # Forecast values? 
    # Usually forecast sensors are NOT in the store, they are fetched from HA.
    # But maybe they are cached somewhere?
    # Let's see all top level keys in data
    print("\nData keys:", list(data.keys()))

    # Check consumption profiles too
    cons_profile = data.get('consumption_total', {})
    print("\nConsumption Profile (Total):")
    for h in range(11, 17):
        sh = str(h)
        vals = cons_profile.get(sh, [])
        if vals:
            avg_v = sum(item.get('v', 0) if isinstance(item, dict) else item for item in vals) / len(vals)
            print(f"  {h:02d}:00 -> {avg_v:.3f} kWh")

except Exception as e:
    print(f"Error: {e}")
