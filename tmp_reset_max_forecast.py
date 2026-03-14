import json

path = r'\\192.168.100.5\config\energy_management_backup.json'
with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Resetting temp_max_forecast to let it recalibrate...")

old_max = data.get("temp_max_forecast", 0.0)
data["temp_max_forecast"] = 0.0
print(f"  temp_max_forecast: {old_max} -> 0.0")

# Save
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Fix complete. Ready for import.")
