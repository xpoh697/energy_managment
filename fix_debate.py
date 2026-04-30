import os

path = r'e:\systemair\energy_mamagment\DEBATE.md'
with open(path, 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# Find the last valid line (before the question marks)
# The user's edit started after line 4226: **Одобрение Archi (v11.6.234)**: ✅
valid_lines = []
for line in lines:
    if '????' in line:
        break
    valid_lines.append(line)

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(valid_lines)

print(f"Fixed {path}, kept {len(valid_lines)} lines.")
