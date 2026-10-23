"""Turn pytest JUnit failures into GitHub annotations (visible without opening logs)."""
import xml.etree.ElementTree as ET
try:
    root = ET.parse("junit.xml").getroot()
except (OSError, ET.ParseError):
    raise SystemExit(0)
for case in root.iter("testcase"):
    for bad in list(case.findall("failure")) + list(case.findall("error")):
        name = f"{case.get('classname')}::{case.get('name')}"
        detail = (bad.get("message") or "") + "\n" + (bad.text or "")[-1500:]
        detail = detail.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
        print(f"::error title={name}::{detail}")
