import json

def search_value(obj, target, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            search_value(v, target, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            search_value(v, target, f"{path}[{i}]")
    else:
        try:
            if float(obj) == target:
                print(f"FOUND {target} at {path}")
        except:
            pass

with open(r'\\192.168.100.5\config\energy_management_backup.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

print("Searching for -3000...")
search_value(data, -3000.0)
print("Searching for 3000...")
search_value(data, 3000.0)
print("Searching for values < -100...")
def search_less(obj, target, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            search_less(v, target, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            search_less(v, target, f"{path}[{i}]")
    else:
        try:
            val = float(obj)
            if val < target:
                print(f"Value {val} < {target} at {path}")
        except:
            pass
search_less(data, -100.0)
