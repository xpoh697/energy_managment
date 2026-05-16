import json
import os

path = r'\\192.168.100.5\config\.storage\energy_management_01KKCHQC1H76XNA33EYXJKFB4T'
if os.path.exists(path):
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    payload = data.get("data", {})
    overrides = payload.get("hourly_manual_overrides", {})
    print("Hourly Manual Overrides:")
    for k, v in overrides.items():
        print(f"  {k} -> {v}")
