#!/usr/bin/env python3
"""
Создание задач в ClickUp из последнего файла tasks_to_create_*.json
"""

import os
import json
import glob
from pathlib import Path
import time

from clickup_client import build_clickup_payload, create_clickup_task


PROJECT_ROOT = Path(__file__).resolve().parent


def load_config() -> dict:
    """Загружает конфигурацию проекта."""
    config_path = PROJECT_ROOT / "config.json"
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def find_latest_tasks_file() -> Path:
    """Находит последний файл tasks_to_create_*.json в корне проекта."""
    pattern = str(PROJECT_ROOT / "tasks_to_create_*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    if not files:
        raise FileNotFoundError("Не найден tasks_to_create_*.json. Сначала запустите process_voice_messages.py")
    return Path(files[0])


def main():
    print("Поиск последнего файла с задачами...")
    tasks_file = find_latest_tasks_file()
    print(f"Найден файл: {tasks_file}")

    clickup_token = os.getenv("CLICKUP_TOKEN")
    if not clickup_token:
        raise RuntimeError("Не установлен CLICKUP_TOKEN")

    config = load_config()
    default_priority = config.get("default_priority", 3)
    assignees_map_raw = config.get("assignees", {})
    assignees_map = assignees_map_raw if isinstance(assignees_map_raw, dict) else {}

    with open(tasks_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    list_id = str(data.get("clickup_list_id") or "").strip()
    if not list_id:
        raise RuntimeError("В файле нет поля clickup_list_id")

    total_created = 0
    created_ids = []

    for vm in data.get("voice_messages", []):
        tasks = vm.get("tasks", [])
        for task in tasks:
            assignee_name = task.get("assignee")
            if assignee_name and isinstance(assignee_name, str):
                if not assignees_map.get(assignee_name):
                    print(
                        f"Предупреждение: для исполнителя '{assignee_name}' не найден ClickUp ID в конфигурации"
                    )

            payload = build_clickup_payload(
                task,
                default_priority=default_priority,
                assignees_map=assignees_map,
            )
            name = payload.get("name") or "Без названия"

            try:
                resp_json = create_clickup_task(clickup_token, list_id, payload)
                task_id = resp_json.get("id") or resp_json.get("task", {}).get("id")
                if task_id:
                    task["clickup_task_id"] = task_id
                    created_ids.append(task_id)
                    total_created += 1
                    print(f"Создана задача в ClickUp: {task_id} — {name}")
                else:
                    print(f"Создана задача без ID в ответе — {name}")
            except Exception as e:
                task["clickup_error"] = str(e)
                print(f"Ошибка создания задачи '{name}': {e}")

            # Небольшая пауза, чтобы не ловить rate limit
            time.sleep(0.3)

    print(f"\nИтого создано задач в ClickUp: {total_created}")

    # Сохраняем расширенный файл результатов рядом
    out_file = tasks_file.with_name(tasks_file.stem + "_with_clickup.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Результаты сохранены: {out_file}")


if __name__ == "__main__":
    main()
