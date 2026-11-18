"""Open-Meteo weather (no API key). Hourly values are stored in weather_hours.

Each fetch overwrites the stored value for each hour, so past hours end up
holding the most recent (near-observed) estimate and future hours the latest
forecast. ``lead_hours`` records how far ahead the stored value was forecast.
Weather data by Open-Meteo.com, CC BY 4.0.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .config import Settings, stamp, utcnow
from .db import Database

FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
ARCHIVE_URL = 'https://archive-api.open-meteo.com/v1/archive'
HOURLY = 'temperature_2m,apparent_temperature,shortwave_radiation,cloud_cover'


async def _get(url, params):
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        response = await client.get(url, params=params, headers={'User-Agent': 'kWattson'})
    response.raise_for_status()
    return response.json()


def store_hourly(db: Database, data: dict, fetched: datetime | None = None) -> int:
    fetched = fetched or utcnow()
    hourly = data['hourly']
    rows = []
    for i, text in enumerate(hourly['time']):
        at = datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        rows.append((stamp(at), hourly['temperature_2m'][i], hourly['apparent_temperature'][i],
                     hourly['shortwave_radiation'][i], hourly['cloud_cover'][i], stamp(fetched),
                     round((at - fetched).total_seconds() / 3600, 2)))
    rows = [r for r in rows if r[1] is not None or r[3] is not None]
    with db.connect() as c:
        c.executemany('INSERT OR REPLACE INTO weather_hours VALUES(?,?,?,?,?,?,?)', rows)
    return len(rows)


def located(settings: Settings) -> bool:
    return settings.site.latitude is not None and settings.site.longitude is not None


async def refresh(db: Database, settings: Settings) -> int:
    if not located(settings):
        return 0  # no location set yet: forecasts fall back to a cautious clear-sky shape
    data = await _get(FORECAST_URL, {'latitude': settings.site.latitude, 'longitude': settings.site.longitude,
                                     'hourly': HOURLY, 'forecast_days': 4, 'past_days': 2, 'timezone': 'UTC'})
    current = db.settings().site
    if (current.latitude, current.longitude) != (settings.site.latitude, settings.site.longitude):
        return 0
    return store_hourly(db, data)


async def backfill(db: Database, settings: Settings, start_day: str, end_day: str) -> int:
    """Historical reanalysis for training the solar/temperature models on old data."""
    if not located(settings):
        raise ValueError('Set the site location first')
    data = await _get(ARCHIVE_URL, {'latitude': settings.site.latitude, 'longitude': settings.site.longitude,
                                    'hourly': HOURLY, 'start_date': start_day, 'end_date': end_day, 'timezone': 'UTC'})
    return store_hourly(db, data, fetched=utcnow())


def current_hour(db: Database, now: datetime):
    return db.one('SELECT ts, temperature, cloud_cover, fetched_at FROM weather_hours WHERE ts=?',
                  (stamp(now.replace(minute=0, second=0, microsecond=0)),))


def freshness_hours(db: Database, now: datetime | None = None) -> float | None:
    now = now or utcnow()
    row = current_hour(db, now)
    if not row:
        return None
    return (now - datetime.fromisoformat(row['fetched_at'])).total_seconds() / 3600


async def addresses(query: str) -> list[dict]:
    data = await _get('https://photon.komoot.io/api/', {'q': query, 'limit': 5})
    matches = []
    for feature in data['features']:
        props = feature['properties']
        lon, lat = feature['geometry']['coordinates']
        street = ' '.join(filter(None, (props.get('housenumber'), props.get('street'))))
        labels = dict.fromkeys(filter(None, (props.get('name'), street, props.get('city'), props.get('state'),
                                             props.get('postcode'), props.get('country'))))
        if labels and -90 <= lat <= 90 and -180 <= lon <= 180:
            matches.append(dict(label=', '.join(labels), latitude=lat, longitude=lon))
    return matches


async def timezone_at(latitude: float, longitude: float) -> str:
    from zoneinfo import ZoneInfo
    data = await _get(FORECAST_URL, {'latitude': latitude, 'longitude': longitude, 'timezone': 'auto',
                                     'forecast_days': 1, 'daily': 'temperature_2m_max'})
    return ZoneInfo(data['timezone']).key
