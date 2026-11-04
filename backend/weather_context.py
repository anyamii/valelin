"""Offline weather data only."""
WEATHER_CITY_KEY="valelin_weather_city"
DEFAULT_CITY="示例城市"
def normalize_weather_city(value):
    return str(value or DEFAULT_CITY).strip()[:60] or DEFAULT_CITY
def cached_weather_line(city):
    return normalize_weather_city(city)+" · 晴 · 20°（本地示例）"
