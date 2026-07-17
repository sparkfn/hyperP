"""Pure Celery beat schedule builders."""

from celery.schedules import crontab


def parse_cron(expression: str) -> crontab | None:
    """Parse a five-field cron expression, returning None when disabled/invalid."""
    parts = expression.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, day_of_month, month_of_year, day_of_week = parts
    return crontab(
        minute=minute,
        hour=hour,
        day_of_month=day_of_month,
        month_of_year=month_of_year,
        day_of_week=day_of_week,
    )


def add_sgbankruptcy_schedule(
    schedule: dict[str, dict[str, object]],
    cron_expression: str,
) -> None:
    """Add scheduled SG bankruptcy API ingestion when the cron is valid."""
    parsed_cron = parse_cron(cron_expression)
    if parsed_cron is None:
        return
    schedule["sgbankruptcy-ingest"] = {
        "task": "src.tasks.run_ingestion_task",
        "schedule": parsed_cron,
        "args": ("sgbankruptcy", "api"),
    }
