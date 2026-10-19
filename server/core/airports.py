# -*- coding: utf-8 -*-
"""国内航空地点目录；城市是地图定位键，机场是可选的具体站点。"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_FILE = os.path.join(os.path.dirname(__file__), "airports_cn.json")


@lru_cache(maxsize=1)
def catalog() -> dict:
    with open(_FILE, encoding="utf-8") as handle:
        return json.load(handle)


def city_location(city: str) -> tuple[str, str] | None:
    city = str(city or "").strip()
    for province, cities in catalog()["provinces"].items():
        if city in cities:
            return province, city
    return None


def validate_airport(city: str, airport: str) -> bool:
    airport = str(airport or "").strip()
    matches = [names for cities in catalog()["provinces"].values()
               for name, names in cities.items() if name == str(city or "").strip()]
    return bool(matches and (not airport or any(airport in names for names in matches)))
