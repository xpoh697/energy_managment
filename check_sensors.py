import json

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

last_vals = data.get("sensor_last_values", {})
for k, v in last_vals.items():
    if 'battery' in k or 'soc' in k or 'cap' in k or 'forecast' in k:
        print(f"{k}: {v}")

print("-" * 20)
# Let's see what are the actual keys in sensor_last_values
print("All keys in sensor_last_values:")
for k in last_vals.keys():
    print(f"  {k}")
