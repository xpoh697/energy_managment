
import os

path = r"e:\systemair\energy_mamagment\custom_components\energy_management\strategy_base.py"
with open(path, "r", encoding="utf-8") as f:
    lines = f.readlines()

new_lines = []
in_loop = False
in_try = False
for line in lines:
    if "for i, h_abs in enumerate(sim_range):" in line:
        in_loop = True
        new_lines.append(line)
        continue
    
    if in_loop:
        if "try:" in line and not in_try:
            in_try = True
            new_lines.append(line)
            continue
        
        if in_try:
            if "except Exception as e:" in line:
                in_try = False
                in_loop = False
                new_lines.append(line)
                continue
            
            # Indent if not already indented inside try
            if line.strip() and not line.startswith(" " * 16):
                new_lines.append("    " + line)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)
    else:
        new_lines.append(line)

with open(path, "w", encoding="utf-8") as f:
    f.writelines(new_lines)
print("Indentation fixed.")
