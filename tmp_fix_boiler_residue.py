import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Fixing daily_deduct_consumption residues...")

# Reset boiler residue (2.4 kwh)
if "daily_deduct_consumption" in data:
    target = "sensor.1st_power_plug_boiler_energy"
    old_val = data["daily_deduct_consumption"].get(target, 0.0)
    data["daily_deduct_consumption"][target] = 0.0
    print(f"  {target}: {old_val} -> 0.0")

# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Fix complete. Ready for import.")
