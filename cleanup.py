import sys
path = r'e:\systemair\energy_mamagment\custom_components\energy_management\sensor.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    new_lines.append(line.rstrip() + '\n')

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
