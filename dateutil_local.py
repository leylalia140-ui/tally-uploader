from datetime import datetime


def format_date(dt: datetime) -> str:
    """Return e.g. '26th March 2026' (never '26-03-2026')."""
    day = dt.day
    suffix = (
        "th"
        if 11 <= day <= 13
        else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    )
    return f"{day}{suffix} {dt.strftime('%B %Y')}"
