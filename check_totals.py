import json

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

for ptype in ['generation', 'consumption_total', 'consumption_base']:
    p_data = data.get(ptype, {})
    total = 0
    for h in range(24):
        vals = p_data.get(str(h), [])
        hour_sum = sum(v.get('v', 0) for v in vals)
        total += hour_sum
        if hour_sum > 100:
            print(f"ALERT: {ptype} Hour {h} has sum {hour_sum}")
    print(f"Total sum of {ptype}: {total}")
