"""The service calendar selected by the active master's daily_reset_time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

DAY = 86400
JST = timezone(timedelta(hours=9))


class NativeServiceCalendar:
    def __init__(self, master_directory, *, reset_parameter='daily_reset_time'):
        rows = json.loads((Path(master_directory) / 'MasterParameter.json').read_text('utf8'))['_allData']
        parameters = {row['_id']: row['_value'] for row in rows}
        if reset_parameter == 'shop_reset_time' and reset_parameter not in parameters:
            reset_parameter = 'daily_reset_time'
        value = parameters[reset_parameter]
        self.reset_parameter = reset_parameter
        hour, minute = (int(part) for part in value.split(':'))
        if not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError('Invalid daily_reset_time in selected master')
        self.reset_seconds = hour * 3600 + minute * 60
        self.offset = 9 * 3600 - self.reset_seconds

    def day(self, timestamp):
        return int((timestamp + self.offset) // DAY)

    def day_start(self, timestamp):
        return int(self.day(timestamp) * DAY - self.offset)

    def period_starts(self, timestamp):
        date = datetime.fromtimestamp(timestamp - self.reset_seconds, JST)
        day = date.replace(hour=0, minute=0, second=0, microsecond=0)
        week, month = day - timedelta(days=day.weekday()), day.replace(day=1)
        return tuple(int(value.timestamp()) + self.reset_seconds for value in (day, week, month))
