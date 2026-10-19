# -*- coding: utf-8 -*-
"""行程字段的完整度、住宿默认退房日与手工修正，共用一套规则。"""
from __future__ import annotations

import datetime as dt
import re

TRAVEL_KINDS = frozenset({"train", "air", "taxi", "bus", "road", "transit", "hotel"})
_DATE = re.compile(r"^(\d{4})\D+(\d{1,2})\D+(\d{1,2})")
_TEXT_FIELDS = frozenset({"from_", "to_", "passenger", "train"})


def date_value(value) -> str:
    """识别票面的日期写法；无效日期绝不充当已补全的日期。"""
    match = _DATE.match(str(value or "").strip())
    if not match:
        return ""
    try:
        return dt.date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ""


def effective_meta(meta: dict | None) -> dict:
    """住宿无入住、退房日期时，仅把开票日作为默认退房日。"""
    result = dict(meta or {})
    if result.get("kind") == "hotel" and not result.get("checkin") and not result.get("checkout"):
        invoice_day = date_value(result.get("invoice_date") or result.get("date"))
        if invoice_day:
            result["checkout"] = invoice_day
            result["checkout_inferred"] = True
    return result


def journey(meta: dict | None) -> dict:
    m = effective_meta(meta)
    kind = str(m.get("kind") or "")
    if kind == "hotel":
        checkin = date_value(m.get("checkin"))
        checkout = date_value(m.get("checkout"))
        nights = None
        if checkin and checkout:
            delta = (dt.date.fromisoformat(checkout) - dt.date.fromisoformat(checkin)).days
            nights = delta if delta > 0 else None
        return {"checkin": checkin, "checkout": checkout,
                "checkout_inferred": bool(m.get("checkout_inferred")), "nights": nights}
    return {"from_": str(m.get("from_") or "").strip(),
            "to_": str(m.get("to_") or "").strip(),
            "from_airport": str(m.get("from_airport") or "").strip(),
            "to_airport": str(m.get("to_airport") or "").strip(),
            "passenger": str(m.get("passenger") or "").strip(),
            "travel_date": date_value(m.get("train_date") or m.get("date")),
            "train": str(m.get("train") or "").strip()}


def missing_fields(meta: dict | None) -> list[str]:
    m = effective_meta(meta)
    kind = str(m.get("kind") or "")
    if kind not in TRAVEL_KINDS:
        return []
    values = journey(m)
    if kind == "hotel":
        missing = [label for key, label in (("checkin", "入住日期"), ("checkout", "退房日期"))
                   if not values[key]]
        if not missing and values["nights"] is None:
            missing.append("退房日期须晚于入住日期")
        return missing
    fields = [("from_", "出发地"), ("to_", "目的地"), ("travel_date", "行程日期")]
    if kind in ("train", "air"):
        fields.append(("passenger", "乘客姓名"))
    return [label for key, label in fields if not values[key]]


def completeness(meta: dict | None) -> str:
    kind = str((meta or {}).get("kind") or "")
    if kind not in TRAVEL_KINDS:
        return "na"
    return "missing" if missing_fields(meta) else "complete"


def apply_updates(meta: dict, updates: dict) -> dict:
    """只修改行程字段；票面抬头、金额、原件均不受影响。"""
    if not isinstance(updates, dict):
        raise ValueError("请提供要保存的行程信息")
    result = effective_meta(meta)
    kind = str(result.get("kind") or "")
    if kind not in TRAVEL_KINDS:
        raise ValueError("这类发票没有可编辑的行程信息")
    allowed = {"checkin", "checkout"} if kind == "hotel" else {
        "from_", "to_", "travel_date", "train", "passenger"}
    if kind == "air":
        allowed.update({"from_airport", "to_airport"})
    if kind not in ("train", "air"):
        allowed.discard("passenger")
    if not updates or any(key not in allowed for key in updates):
        raise ValueError("包含不支持修改的行程字段")
    for key, raw in updates.items():
        value = str(raw or "").strip()
        if len(value) > 120:
            raise ValueError("行程信息过长，请缩短后保存")
        if key in ("checkin", "checkout", "travel_date"):
            if value and (not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                          or not date_value(value)):
                raise ValueError("日期请使用 YYYY-MM-DD 格式")
            result["train_date" if key == "travel_date" else key] = value
        else:
            result[key] = value
    if kind == "air":
        from . import airports
        for city_key, airport_key in (("from_", "from_airport"), ("to_", "to_airport")):
            city, airport = result.get(city_key), result.get(airport_key)
            if city and not airports.validate_airport(city, airport):
                raise ValueError("请选择列表中的城市和机场；机场也可以留空")
            if not city and airport:
                raise ValueError("请先选择城市，再选择机场")
    if kind == "hotel":
        a, b = date_value(result.get("checkin")), date_value(result.get("checkout"))
        if a and b and b <= a:
            raise ValueError("退房日期须晚于入住日期")
        if "checkout" in updates:
            result["checkout_inferred"] = False
        result = effective_meta(result)
    result["journey_edited"] = True
    return result


def apply_saved_values(parsed: dict, saved: dict | None) -> dict:
    """行程单重新解析时，仅用库中手工改过的行程字段覆盖票面结果。"""
    result = dict(parsed)
    if not isinstance(saved, dict) or not saved.get("journey_edited"):
        return result
    if result.get("kind") == "hotel":
        keys = ("checkin", "checkout", "checkout_inferred")
    else:
        keys = ("from_", "to_", "from_airport", "to_airport",
                "train_date", "passenger", "train")
    for key in keys:
        if key in saved:
            result[key] = saved[key]
    result["journey_edited"] = True
    return result
