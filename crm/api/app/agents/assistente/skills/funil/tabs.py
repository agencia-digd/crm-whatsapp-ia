"""Lista as abas da planilha e suas dimensões."""
from _helper import open_sheet

sh = open_sheet()
print(f"Planilha: {sh.title}")
for ws in sh.worksheets():
    print(f"  - {ws.title} (gid={ws.id}, {ws.row_count}x{ws.col_count})")
