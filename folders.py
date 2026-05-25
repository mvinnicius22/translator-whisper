import json
import re
from datetime import datetime
from pathlib import Path


def get_meeting_folder(meeting_name: str, session_start: datetime) -> Path:
    settings_path = Path(__file__).parent / "settings.json"
    folder_structure = "flat"
    if settings_path.exists():
        with open(settings_path, encoding="utf-8") as f:
            folder_structure = json.load(f).get("folder_structure", "flat")

    safe_name = re.sub(r"[^a-z0-9\-]", "", meeting_name.lower().replace(" ", "-"))
    if not safe_name:
        safe_name = "meeting"

    date_str = session_start.strftime("%Y%m%d")
    time_str = session_start.strftime("%H%M")
    base = Path.home() / "Documents" / "Meetings"

    if folder_structure == "daily":
        return base / date_str / f"{time_str}_{safe_name}"
    return base / f"{date_str}_{time_str}_{safe_name}"
