#!/usr/bin/env python3
"""
Скрипт для обработки голосовых сообщений из Telegram и создания задач в ClickUp
"""

import os
import json
import requests
from datetime import datetime, timedelta
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from openai import OpenAI

from clickup_client import build_clickup_payload, create_clickup_task

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "state.json"


def load_state() -> Dict[str, Any]:
    """Загружает сохраненное состояние обработки."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            # Игнорируем поврежденный файл состояния
            return {}
    return {}


def save_state(state: Dict[str, Any]) -> None:
    """Сохраняет состояние обработки."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# Загрузка конфигурации
def load_config():
    """Загружает конфигурацию из файла"""
    config_path = PROJECT_ROOT / "config.json"
    with open(config_path, 'r') as f:
        return json.load(f)

# Загрузка API секретов
def load_api_secrets():
    """Загружает API секреты из файла"""
    # 1) Пробуем взять из переменных окружения
    secrets = {
        'bot_token': os.getenv('TELEGRAM_BOT_TOKEN'),
        'chat_id': os.getenv('TELEGRAM_CHAT_ID'),
        'openai_api_key': os.getenv('OPENAI_API_KEY'),
        'clickup_token': os.getenv('CLICKUP_TOKEN'),
    }

    primary_keys = ('bot_token', 'chat_id', 'openai_api_key')
    missing_primary = [key for key in primary_keys if not secrets[key]]
    missing_clickup = not secrets['clickup_token']

    # 2) Файл секретов в домашней директории (дополняем недостающие значения)
    if missing_primary or missing_clickup:
        secrets_path = Path.home() / ".api_secret_infos" / "api_secrets.json"
        if secrets_path.exists():
            with open(secrets_path, 'r', encoding='utf-8') as f:
                all_secrets = json.load(f)
            telegram_secrets = all_secrets.get('TELEGRAM', {}).get('secrets', {})
            openai_secrets = all_secrets.get('OPENAI', {}).get('secrets', {})
            clickup_secrets = all_secrets.get('CLICKUP', {}).get('secrets', {})

            if not secrets['bot_token']:
                secrets['bot_token'] = telegram_secrets.get('BOT_TOKEN')
            if not secrets['chat_id']:
                secrets['chat_id'] = telegram_secrets.get('CHAT_ID')
            if not secrets['openai_api_key']:
                secrets['openai_api_key'] = openai_secrets.get('API_KEY')
            if not secrets['clickup_token']:
                secrets['clickup_token'] = clickup_secrets.get('API_TOKEN') or clickup_secrets.get('TOKEN')

    if any(not secrets[key] for key in primary_keys):
        raise FileNotFoundError(
            "Не найдены секреты Telegram/OpenAI. Установите переменные окружения TELEGRAM_BOT_TOKEN, "
            "TELEGRAM_CHAT_ID, OPENAI_API_KEY или дополните файл ~/.api_secret_infos/api_secrets.json."
        )

    if not secrets['clickup_token']:
        raise FileNotFoundError(
            "Не найден ClickUp токен. Установите переменную окружения CLICKUP_TOKEN или добавьте секцию CLICKUP "
            "в ~/.api_secret_infos/api_secrets.json."
        )

    return secrets

def get_recent_voice_messages(
    bot_token: str,
    chat_id: str,
    hours_back: int = 1,
    last_update_id: Optional[int] = None
) -> Tuple[List[Dict[str, Any]], Optional[int]]:
    """
    Получает голосовые и аудио сообщения из Telegram группы за последние N часов
    Обрабатывает: обычные голосовые, пересланные голосовые, аудио файлы, channel_post
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"

    params: Dict[str, Any] = {'limit': 100}
    if last_update_id is not None:
        params['offset'] = last_update_id + 1

    # Получаем обновления
    response = requests.get(url, params=params, timeout=30)
    response.raise_for_status()
    data = response.json()

    if not data.get('ok'):
        raise Exception(f"Telegram API error: {data}")

    # Фильтруем голосовые и аудио сообщения из нужного чата за последний час
    cutoff_time = datetime.now() - timedelta(hours=hours_back)
    voice_messages = []
    max_update_id = last_update_id

    for update in data.get('result', []):
        update_id = update.get('update_id')
        if update_id is not None:
            max_update_id = update_id if max_update_id is None else max(max_update_id, update_id)

        # Проверяем как обычные сообщения, так и channel_post
        message = update.get('message') or update.get('channel_post') or update.get('edited_message')

        if not message:
            continue

        # Проверяем, что сообщение из нужного чата
        chat_id_int = int(chat_id)
        message_chat_id = message.get('chat', {}).get('id')

        if message_chat_id != chat_id_int:
            continue

        msg_time = datetime.fromtimestamp(message['date'])
        if msg_time <= cutoff_time:
            continue

        # Определяем отправителя
        from_user = 'Unknown'
        if 'from' in message:
            from_user = message['from'].get('first_name', 'Unknown')
        elif 'forward_from' in message:
            from_user = message['forward_from'].get('first_name', 'Unknown')
        elif 'forward_origin' in message:
            origin = message['forward_origin']
            if origin.get('type') == 'user' and 'sender_user' in origin:
                from_user = origin['sender_user'].get('first_name', 'Unknown')
        elif 'sender_chat' in message:
            from_user = message['sender_chat'].get('title', 'Channel')
        
        # Проверяем наличие голосового или аудио
        audio_data = None
        audio_type = None
        
        if 'voice' in message:
            audio_data = message['voice']
            audio_type = 'voice'
        elif 'audio' in message:
            audio_data = message['audio']
            audio_type = 'audio'
        
        if audio_data:
            voice_messages.append({
                'file_id': audio_data['file_id'],
                'duration': audio_data.get('duration', 0),
                'date': msg_time.isoformat(),
                'from_user': from_user,
                'type': audio_type,
                'mime_type': audio_data.get('mime_type', 'unknown'),
                'is_forwarded': 'forward_from' in message or 'forward_origin' in message
            })

    return voice_messages, max_update_id

def download_audio_file(bot_token, file_id, output_path):
    """
    Скачивает голосовой или аудио файл из Telegram
    """
    # Получаем путь к файлу
    url = f"https://api.telegram.org/bot{bot_token}/getFile"
    response = requests.get(url, params={'file_id': file_id})
    response.raise_for_status()
    data = response.json()
    
    if not data.get('ok'):
        raise Exception(f"Failed to get file path: {data}")
    
    file_path = data['result']['file_path']
    
    # Скачиваем файл
    download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
    response = requests.get(download_url)
    response.raise_for_status()
    
    with open(output_path, 'wb') as f:
        f.write(response.content)
    
    return output_path

def transcribe_audio(client: OpenAI, audio_path: str) -> str:
    """
    Транскрибирует аудио файл через OpenAI Whisper API
    Возвращает текст транскрипции
    """
    with open(audio_path, 'rb') as audio_file:
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language="ru"
        )

    return transcript.text

T = TypeVar("T")


def execute_with_openai_retry(
    client: OpenAI,
    api_key: str,
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> Tuple[T, OpenAI]:
    """Выполняет вызов OpenAI с переинициализацией клиента при первом сбое."""

    try:
        result = func(client, *args, **kwargs)
        return result, client
    except Exception as err:
        print(
            "  Ошибка при обращении к OpenAI:"
            f" {err}. Повторная инициализация клиента и повтор запроса..."
        )
        new_client = OpenAI(api_key=api_key)
        try:
            result = func(new_client, *args, **kwargs)
        except Exception:
            # Если повторный вызов также провалился, пробрасываем исключение дальше
            raise
        return result, new_client


def extract_tasks_from_text(client: OpenAI, text: str) -> List[Dict[str, Any]]:
    """
    Использует GPT для извлечения задач из транскрипции
    Возвращает список задач с полями: название, описание, дедлайн, приоритет, ответственный
    """
    prompt = f"""
Проанализируй следующий текст из голосового сообщения и извлеки все упомянутые задачи.
Для каждой задачи определи:
- Название задачи (краткое, до 100 символов)
- Описание задачи (подробное)
- Дедлайн (если упомянут, в формате YYYY-MM-DD, если нет - оставь null)
- Приоритет (1 - срочно, 2 - высокий, 3 - нормальный, 4 - низкий)
- Ответственный (имя человека, если упомянуто, иначе null)

Верни результат в формате JSON массива:
[
  {{
    "name": "Название задачи",
    "description": "Подробное описание",
    "due_date": "2025-10-05" или null,
    "priority": 3,
    "assignee": "Имя" или null
  }}
]

Текст голосового сообщения:
{text}
"""
    
    response = client.chat.completions.create(
        model="gpt-4",
        messages=[
            {"role": "system", "content": "Ты помощник для извлечения задач из текста. Отвечай только валидным JSON."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.3
    )
    
    # Парсим JSON ответ
    choices = getattr(response, "choices", [])
    if not choices:
        raise ValueError("GPT не вернул ни одного варианта ответа при извлечении задач")

    message_content = choices[0].message.content if choices[0].message else None
    if not message_content:
        raise ValueError("GPT вернул пустой ответ при извлечении задач")

    tasks_json = message_content.strip()
    # Убираем markdown форматирование если есть
    if tasks_json.startswith("```json"):
        tasks_json = tasks_json.split("```json")[1].split("```")[0].strip()
    elif tasks_json.startswith("```"):
        tasks_json = tasks_json.split("```")[1].split("```")[0].strip()
    
    try:
        tasks = json.loads(tasks_json)
    except json.JSONDecodeError as json_err:
        raise ValueError(f"Не удалось распарсить ответ GPT как JSON: {json_err}") from json_err

    if not isinstance(tasks, list):
        raise ValueError("Ответ GPT должен быть списком задач")

    for idx, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"Элемент #{idx} ответа GPT не является объектом задачи")

    return tasks

def save_processing_log(log_data, log_file):
    """
    Сохраняет лог обработки в файл
    """
    with open(log_file, 'w', encoding='utf-8') as f:
        f.write("# Отчет об обработке голосовых и аудио сообщений Telegram\n\n")
        f.write(f"**Дата обработки:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        if not log_data['voice_messages']:
            f.write("## Результат\n\nНовых голосовых/аудио сообщений не найдено.\n")
            return
        
        f.write(f"## Обработано сообщений: {len(log_data['voice_messages'])}\n\n")
        
        for idx, vm in enumerate(log_data['voice_messages'], 1):
            msg_type = "Голосовое" if vm.get('type') == 'voice' else "Аудио"
            forwarded = " (пересланное)" if vm.get('is_forwarded') else ""
            f.write(f"### {msg_type} сообщение #{idx}{forwarded}\n\n")
            f.write(f"- **От:** {vm['from_user']}\n")
            f.write(f"- **Дата:** {vm['date']}\n")
            f.write(f"- **Длительность:** {vm['duration']} сек\n")
            f.write(f"- **Тип:** {vm.get('type', 'voice')}\n\n")
            
            if 'transcription' in vm:
                f.write(f"**Транскрипция:**\n```\n{vm['transcription']}\n```\n\n")
            
            if 'error' in vm:
                f.write(f"**⚠️ Ошибка:** {vm['error']}\n\n")
            elif 'tasks' in vm and vm['tasks']:
                f.write(f"**Извлечено задач:** {len(vm['tasks'])}\n\n")
                if vm.get('clickup_created'):
                    f.write(f"- **Создано в ClickUp:** {vm['clickup_created']}\n")
                if vm.get('clickup_failed'):
                    f.write(f"- **Ошибок создания:** {vm['clickup_failed']}\n")
                if vm.get('clickup_created') or vm.get('clickup_failed'):
                    f.write("\n")
                for task_idx, task in enumerate(vm['tasks'], 1):
                    f.write(f"#### Задача {task_idx}\n")
                    f.write(f"- **Название:** {task['name']}\n")
                    f.write(f"- **Описание:** {task['description']}\n")
                    f.write(f"- **Дедлайн:** {task.get('due_date', 'Не указан')}\n")
                    f.write(f"- **Приоритет:** {task.get('priority', 3)}\n")
                    f.write(f"- **Ответственный:** {task.get('assignee', 'Не указан')}\n")
                    if 'clickup_task_id' in task:
                        f.write(f"- **ClickUp Task ID:** {task['clickup_task_id']}\n")
                    if task.get('clickup_error'):
                        f.write(f"- **Ошибка ClickUp:** {task['clickup_error']}\n")
                    f.write("\n")
            else:
                f.write("**Задач не найдено**\n\n")
            
            f.write("---\n\n")
        
        f.write(f"\n## Итого создано задач в ClickUp: {log_data['total_tasks_created']}\n")
        if log_data.get('total_tasks_failed'):
            f.write(f"## Итого ошибок создания задач: {log_data['total_tasks_failed']}\n")

def main():
    """
    Основная функция обработки
    """
    print("Запуск обработки голосовых сообщений из Telegram...")
    
    # Загружаем конфигурацию и секреты
    config = load_config()
    secrets = load_api_secrets()
    
    bot_token = secrets['bot_token']
    chat_id = secrets['chat_id']
    openai_api_key = secrets['openai_api_key']
    clickup_token = secrets['clickup_token']
    
    if not bot_token or not chat_id:
        raise Exception("Не найдены BOT_TOKEN или CHAT_ID в секретах")
    
    if not openai_api_key:
        raise Exception("Не найден OPENAI API_KEY в секретах")

    clickup_list_id = str(config.get('clickup_list_id', '')).strip()
    if not clickup_list_id:
        raise Exception("В config.json отсутствует clickup_list_id")

    default_priority = config.get('default_priority', 3)
    
    print(f"Проверка голосовых сообщений в чате {chat_id}...")

    openai_client = OpenAI(api_key=openai_api_key)

    # Получаем голосовые сообщения за последний час
    state = load_state()
    last_update_id = state.get('last_update_id')
    hours_back = config.get('telegram_check_hours', 1)
    voice_messages, max_update_id = get_recent_voice_messages(
        bot_token,
        chat_id,
        hours_back=hours_back,
        last_update_id=last_update_id
    )

    if max_update_id is not None and max_update_id != last_update_id:
        state['last_update_id'] = max_update_id
        save_state(state)
    
    print(f"Найдено голосовых сообщений: {len(voice_messages)}")
    
    log_data = {
        'voice_messages': [],
        'total_tasks_created': 0,
        'total_tasks_failed': 0,
        'clickup_list_id': clickup_list_id
    }
    
    # Обрабатываем каждое голосовое/аудио сообщение
    for vm in voice_messages:
        audio_type_label = "голосового" if vm['type'] == 'voice' else "аудио"
        forwarded_label = " (пересланное)" if vm.get('is_forwarded') else ""
        print(f"\nОбработка {audio_type_label}{forwarded_label} от {vm['from_user']}...")
        
        vm_log = {
            'from_user': vm['from_user'],
            'date': vm['date'],
            'duration': vm['duration'],
            'type': vm['type'],
            'is_forwarded': vm.get('is_forwarded', False)
        }
        
        audio_path = None
        try:
            # Определяем расширение файла по mime_type
            mime_type = vm.get('mime_type', 'audio/ogg')
            if 'ogg' in mime_type:
                suffix = '.ogg'
            elif 'mpeg' in mime_type or 'mp3' in mime_type:
                suffix = '.mp3'
            elif 'mp4' in mime_type or 'm4a' in mime_type:
                suffix = '.m4a'
            elif 'wav' in mime_type:
                suffix = '.wav'
            else:
                suffix = '.ogg'  # по умолчанию
            
            # Скачиваем аудио файл
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp_file:
                audio_path = tmp_file.name

            download_audio_file(bot_token, vm['file_id'], audio_path)
            print(f"  Аудио файл скачан: {audio_path}")
            
            # Транскрибируем
            transcription, openai_client = execute_with_openai_retry(
                openai_client,
                openai_api_key,
                transcribe_audio,
                audio_path,
            )
            print(f"  Транскрипция: {transcription[:100]}...")
            vm_log['transcription'] = transcription

            # Извлекаем задачи
            tasks, openai_client = execute_with_openai_retry(
                openai_client,
                openai_api_key,
                extract_tasks_from_text,
                transcription,
            )
            print(f"  Извлечено задач: {len(tasks)}")
            vm_log['tasks'] = tasks

            created_for_message = 0
            for task in tasks:
                payload = build_clickup_payload(task, default_priority=default_priority)
                task_name = payload.get('name', 'Без названия')

                try:
                    response = create_clickup_task(clickup_token, clickup_list_id, payload)
                except Exception as create_err:
                    task['clickup_error'] = str(create_err)
                    log_data['total_tasks_failed'] += 1
                    vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                    print(f"  Ошибка создания задачи '{task_name}': {create_err}")
                    continue

                task_id = response.get("id") or response.get("task", {}).get("id")
                if task_id:
                    task['clickup_task_id'] = task_id
                    created_for_message += 1
                    log_data['total_tasks_created'] += 1
                    print(f"  Создана задача в ClickUp: {task_id} — {task_name}")
                else:
                    task['clickup_error'] = "Task created without ID in response"
                    log_data['total_tasks_failed'] += 1
                    vm_log['clickup_failed'] = vm_log.get('clickup_failed', 0) + 1
                    print(f"  Задача создана без ID в ответе — {task_name}")

            if created_for_message:
                vm_log['clickup_created'] = created_for_message
            
        except Exception as e:
            print(f"  Ошибка при обработке: {e}")
            vm_log['error'] = str(e)
        finally:
            if audio_path and os.path.exists(audio_path):
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
        
        log_data['voice_messages'].append(vm_log)
    
    # Сохраняем лог
    logs_dir = PROJECT_ROOT / "logs"
    os.makedirs(logs_dir, exist_ok=True)
    log_file = str(logs_dir / f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    save_processing_log(log_data, log_file)
    print(f"\nЛог сохранен: {log_file}")
    
    # Сохраняем данные задач для следующего шага
    tasks_file = str(PROJECT_ROOT / f"tasks_to_create_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(tasks_file, 'w', encoding='utf-8') as f:
        json.dump(log_data, f, ensure_ascii=False, indent=2)
    print(f"Данные задач сохранены: {tasks_file}")
    
    print(
        f"\nОбработка завершена. Всего задач создано: {log_data['total_tasks_created']}, "
        f"ошибок: {log_data['total_tasks_failed']}"
    )
    return tasks_file

if __name__ == "__main__":
    main()
