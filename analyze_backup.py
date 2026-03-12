import json

try:
    with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
        data = json.load(f)

    print("--- Detailed Analysis for anomalies ---")

    for ptype in ['generation', 'consumption_total', 'consumption_base']:
        print(f"\nScanning: {ptype}")
        profile_data = data.get(ptype, {})
        for h in range(24):
            sh = str(h)
            values = profile_data.get(sh, [])
            for i, item in enumerate(values):
                v = item.get('v', 0)
                if abs(v) > 50:
                    print(f"  ALERT! {ptype} Hour {sh} Index {i}: Value = {v}")
    
    # Check hourly accumulators
    print("\nHourly Accumulators:")
    accs = data.get('hourly_accumulators', {})
    for k, v in accs.items():
        print(f"  {k}: {v}")

except Exception as e:
    print(f"Error: {e}")
