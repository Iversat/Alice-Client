![Alice Client — независимое исследование протокола](docs/banner.svg)

# Alice Client

Исследовательский Python-клиент для обмена текстом и аудио с Алисой через наблюдаемый WebSocket-протокол Uniproxy. Одна библиотека, четыре сценария и сохранённые имена исходных скриптов.

**Python 3.11+** · **Asyncio** · **CLI** · **Неофициальный проект**

[Быстрый старт](#быстрый-старт) · [Команды](#команды) · [Python API](#python-api) · [Проверки](docs/TESTING.md) · [Правовое уведомление](LEGAL_NOTICE.md)

> Проект не связан с Яндексом и не является официальным SDK. Используется недокументированный интерфейс; доступность функций зависит от сервера и прав аккаунта. История происхождения кода, включая сведения из прежнего README, открыто описана в [PROVENANCE.md](PROVENANCE.md).

## Возможности

| Сценарий | Команда | Результат |
| --- | --- | --- |
| Диалог | `python -m alice_client chat` | Ответ в терминале |
| Озвученный ответ | `python -m alice_client chat --audio` | Текст и аудиофайл |
| Синтез речи | `python -m alice_client tts "Привет"` | Аудио переданного текста |
| Распознавание | `python -m alice_client asr` | Текст с микрофона; нужны PyAudio, токен и ключ ASR |

Клиент сопоставляет ответы с запросами по `messageId`, собирает бинарные аудиочасти по `streamId`, ограничивает время ожидания и объём аудио. Запрос регистрируется до отправки, поэтому быстрый ответ не теряется. При закрытии соединения, отмене или таймауте ожидающие операции завершаются и освобождают ресурсы.

Получение текста или аудио не подтверждает доступ к конкретной версии модели, подписке или умному дому. Названия старых файлов со `speechkit` сохранены для удобства; это не реализация официального облачного SpeechKit SDK.

## Быстрый старт

Скачайте репозиторий и создайте отдельное окружение.

```bash
git clone https://github.com/Iversat/Alice-Client.git
cd Alice-Client
python -m venv .venv
```

Активируйте окружение: `.venv\Scripts\Activate.ps1` в PowerShell или `source .venv/bin/activate` в Linux/macOS. Если активация PowerShell недоступна, используйте `.venv\Scripts\python.exe` вместо `python` в командах ниже.

```bash
python -m pip install -e .
python -m alice_client chat --anonymous --text "Привет"
```

Для своего аккаунта используйте скрытый ввод:

```bash
python -m alice_client chat --token-prompt
```

Токен не печатается и не записывается в файлы. Для автоматизации CLI читает `YANDEX_OAUTH_TOKEN` из окружения процесса. Файл `.env.example` показывает имена переменных, но `.env` автоматически не загружается. Не вставляйте токены в исходники, аргументы команд, issue или скриншоты. Флаг `--anonymous` игнорирует токен окружения.

## Команды

```bash
# Диалог: exit, quit или выход завершают сеанс.
python -m alice_client chat --token-prompt

# Один ответ в формате JSON.
python -m alice_client chat --anonymous --text "Привет" --json

# Текст и озвученный ответ. По умолчанию: responses/reply-001.opus и далее.
python -m alice_client chat --audio --token-prompt

# Синтез текста в файл. Без текста включается интерактивный режим.
python -m alice_client tts "Привет, мир" --output response.opus --token-prompt

# Справка по режиму.
python -m alice_client chat --help
```

Аудио сохраняется в том виде, в котором его прислал сервер: конвертация и автоматическое воспроизведение не выполняются. Повторное использование явно заданного `--output` заменяет файл по этому пути.

Режим CLI `chat --audio` получает текст ответа, затем отправляет его на синтез речи отдельным запросом. Это проверенный сценарий для анонимного соединения: встроенный аудиоответ на `voice_session=True` в реальной проверке не пришёл. Низкоуровневый аргумент API `is_tts_response=True` сохранён для исследования такого ответа; его доступность не гарантируется.

Для микрофона установите дополнительную зависимость и задайте доступный вам ключ в переменной `YANDEX_ASR_KEY`:

```bash
python -m pip install -e ".[audio]"
python -m alice_client asr --token-prompt --duration 30 --partial
```

Запись: **PCM16, mono, 16 кГц**, блок 1024 кадра, то есть 64 мс. Без `--duration` распознавание продолжается до Ctrl+C. Промежуточные результаты `--partial` идут в stderr, окончательные — в stdout. Ключ по умолчанию не подставляется. Правомерное получение токена и ключа остаётся за пользователем; проект не предоставляет права на закрытые или платные функции.

В Linux для сборки PyAudio могут понадобиться системные заголовки PortAudio и Python. Чат и синтез речи работают без установки PyAudio и без доступа к микрофону.

## Python API

```python
import asyncio
import os

from alice_client import YandexAliceClient


async def main():
    async with YandexAliceClient(
        oauth_token=os.getenv("YANDEX_OAUTH_TOKEN"),
        request_timeout=10,
    ) as client:
        result = await client.send_text("Привет")
        print(result["response_payload"])


asyncio.run(main())
```

Контекстный менеджер открывает соединение, отправляет состояние клиента и закрывает ресурсы. Отправка состояния не означает подтверждения авторизации. Методы `send_text()` и `tts()` возбуждают `YandexAliceClientError`; таймаут — `AliceTimeoutError`. Ошибка больше не маскируется предположением о неверном токене.

Синхронный адаптер `YandexAliceClientPython` доступен из `alice_client.sync` и исходного `alice_client_terminal.py`. При переходе со старого кода учтите [изменения API](CHANGELOG.md).

## Исходные скрипты

| Файл | Эквивалент |
| --- | --- |
| `alice_client_terminal.py` | `python -m alice_client chat` |
| `alice_client_receiving_tts.py` | `python -m alice_client chat --audio` |
| `alice_client_speechkit_tts.py` | `python -m alice_client tts` |
| `alice_client_speechkit_asr.py` | `python -m alice_client asr` |

Например: `python alice_client_receiving_tts.py --token-prompt`. Вся общая логика теперь находится в пакете `alice_client/`.

## Разработка и проверка

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m build
```

Обычные тесты используют локальные данные и локальный WebSocket-сервер; они не обращаются к Яндексу и не включают микрофон. Подробности, результаты реальной проверки и её ограничения — в [docs/TESTING.md](docs/TESTING.md).

## Правовой статус

Подготовлено [уведомление об исследовательском характере проекта](LEGAL_NOTICE.md). Оно описывает цель исследования, права третьих лиц и пределы использования. Оно не подтверждает согласие Яндекса и не гарантирует отсутствие претензий.

Открытая лицензия на весь репозиторий пока не объявлена: происхождение отдельных элементов требует отдельной проверки. См. [PROVENANCE.md](PROVENANCE.md).
