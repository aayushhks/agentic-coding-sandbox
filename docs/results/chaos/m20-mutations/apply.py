import json, sys
from pathlib import Path
spec = json.loads(Path(sys.argv[1]).read_text())
for edit in spec.get("edits", [spec]):
    path = Path(edit["file"])
    text = path.read_text()
    assert text.count(edit["old"]) == 1, f"{edit['file']}: the target is not there exactly once"
    path.write_text(text.replace(edit["old"], edit["new"]))
