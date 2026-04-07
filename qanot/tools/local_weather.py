"""Ob-havo ma'lumoti."""

from __future__ import annotations

import json
import logging

import aiohttp

from qanot.registry import ToolRegistry

logger = logging.getLogger(__name__)


def register_weather_tools(registry: ToolRegistry) -> None:
    """Register weather tools."""

    async def get_weather(params: dict) -> str:
        """Ob-havo ma'lumoti."""
        city = params.get("city", "Tashkent")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"https://wttr.in/{city}?format=j1",
                    headers={"Accept-Language": "uz"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)

            # wttr.in wraps everything under "data" key
            inner = data.get("data", data)
            current = inner.get("current_condition", [{}])[0]
            today = inner.get("weather", [{}])[0]
            tomorrow = inner.get("weather", [{}, {}])[1] if len(inner.get("weather", [])) > 1 else {}

            result = {
                "city": city,
                "now": {
                    "temp": f"{current.get('temp_C', '?')}°C",
                    "feels_like": f"{current.get('FeelsLikeC', '?')}°C",
                    "condition": current.get("lang_uz", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "")) if current.get("lang_uz") else current.get("weatherDesc", [{}])[0].get("value", ""),
                    "humidity": f"{current.get('humidity', '?')}%",
                    "wind": f"{current.get('windspeedKmph', '?')} km/s",
                },
                "today": {
                    "max": f"{today.get('maxtempC', '?')}°C",
                    "min": f"{today.get('mintempC', '?')}°C",
                },
            }

            if tomorrow:
                result["tomorrow"] = {
                    "max": f"{tomorrow.get('maxtempC', '?')}°C",
                    "min": f"{tomorrow.get('mintempC', '?')}°C",
                }

            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Ob-havo olishda xatolik: {e}"})

    registry.register(
        name="weather",
        description="Weather information — today's and tomorrow's forecast, temperature, wind, humidity.",
        parameters={
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "Shahar nomi (default: Tashkent). Masalan: Samarkand, Bukhara, Namangan",
                },
            },
        },
        handler=get_weather,
    )
