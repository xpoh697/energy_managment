import json

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

for ptype in ['generation', 'consumption_total', 'consumption_base']:
    p_data = data.get(ptype, {})
    max_val = -1e18
    min_val = 1e18
    for h_str, entries in p_data.items():
        for entry in entries:
            v = entry.get('v', 0.0)
            if v > max_val: max_val = v
            if v < min_val: min_val = v
    print(f"{ptype}: Min={min_val}, Max={max_val}")
