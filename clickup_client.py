"""
Утилиты для создания задач в ClickUp через REST API.
"""

import time
from datetime import datetime
from typing import Any, Dict, Optional

import requests


def to_epoch_millis(date_str: str) -> int:
    """Преобразует дату YYYY-MM-DD в миллисекунды Unix времени (UTC)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return int(dt.timestamp() * 1000)


def build_clickup_payload(
    task: Dict[str, Any],
    default_priority: int = 3,
    assignees_map: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Подготавливает полезную нагрузку для API ClickUp из словаря задачи.
    Некорректные значения дедлайна и приоритета игнорируются.
    """
    name = task.get("name") or "Без названия"
    description = task.get("description") or ""
    priority_raw = task.get("priority")
    priority = default_priority
    if isinstance(priority_raw, int):
        priority = priority_raw
    elif isinstance(priority_raw, str):
        try:
            priority = int(priority_raw)
        except ValueError:
            priority = default_priority

    if priority not in (1, 2, 3, 4):
        priority = default_priority

    payload: Dict[str, Any] = {
        "name": name,
        "description": description,
        "priority": priority,
    }

    due_date_str = task.get("due_date")
    if due_date_str and isinstance(due_date_str, str):
        try:
            payload["due_date"] = to_epoch_millis(due_date_str)
        except ValueError:
            # Игнорируем некорректный формат дедлайна
            pass

    assignee_name = task.get("assignee")
    if assignee_name and isinstance(assignee_name, str) and assignees_map:
        assignee_id = assignees_map.get(assignee_name)
        if assignee_id:
            payload["assignees"] = [assignee_id]

    return payload


def create_clickup_task(token: str, list_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Создает задачу в ClickUp. При получении 429 выполняет повторную попытку.
    """
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
    }

    for attempt in range(2):
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        if response.status_code == 429 and attempt == 0:
            retry_after = int(response.headers.get("Retry-After", "2"))
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response.json()

    # Если две попытки не увенчались успехом, поднимем исключение
    response.raise_for_status()
    return {}
