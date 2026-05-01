import json

path = r'\\192.168.100.5\config\energy_management_backup.json'

with open(path, 'r', encoding='utf-8') as f:
    data = json.load(f)

p = "losses"
print(f"\n--- Profile: {p} ---")
prof = data.get(p, {})

# Check weekday 4 (Friday)
avg_data = prof.get("4", [])
if isinstance(avg_data, list):
    for h, item in enumerate(avg_data):
        val = item.get('v', 0.0) if isinstance(item, dict) else 0.0
        if val > 1.0:
            print(f"  !!! HIGH LOSS FOUND !!! Hour {h:02d}: {val:.3f} kW")
        else:
            print(f"  {h:02d}: {val:.3f}", end=" | " if (h+1)%6 != 0 else "\n")
else:
    print("  No average data found")
