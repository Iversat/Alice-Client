# Авторизация в LS Client

Для обычного входа я предусмотрел `python yandex_login.py`: подтверждение выполняется на сайте Яндекса, токен автоматически сохраняется в `.env`. Повторный запуск позволяет выбрать другой аккаунт.

## Официальное OAuth-приложение

Если нужно управлять собственным набором OAuth-доступов, зарегистрируйте приложение на [Яндекс OAuth](https://oauth.yandex.ru/) и используйте его ClientID:

```bash
python -m alice_client auth login --client-id YOUR_CLIENT_ID
python -m alice_client auth status
python -m alice_client auth check
```

Здесь я использую официальный [Device Authorization Flow](https://yandex.ru/dev/id/doc/ru/codes/screen-code-oauth). Токен сохраняется в системном хранилище через keyring. Если приложение требует Client secret, задайте `YANDEX_CLIENT_SECRET` в окружении; `YANDEX_CLIENT_ID` заменяет аргумент `--client-id`. `--no-browser` отключает автоматическое открытие страницы, `--wait` задаёт срок ожидания.

Альтернатива с сохранением в `.env`: `python yandex_login.py --oauth-app`. Обновление такого токена: `python yandex_login.py --oauth-app --refresh`.

## Управление токеном

| Команда | Действие |
| --- | --- |
| `auth status` | Показать наличие токена без вывода секрета |
| `auth check` | Проверить Яндекс ID и чтение умного дома отдельно |
| `auth import` | Ввести существующий токен скрыто и сохранить в keyring |
| `auth refresh` | Обновить OAuth-токен в keyring при наличии refresh-токена |
| `auth logout` | Удалить токен из keyring |

В Python я рекомендую `get_token()`. Приоритет источников: окружение процесса → `.env` текущей папки → keyring. `--anonymous` игнорирует все источники; `--token-prompt` задаёт токен на один запуск. Для совместимости имя сервиса keyring остаётся `Alice-Client/Yandex-OAuth`.

`auth check` выполняет чтение и не выводит имена или идентификаторы устройств. Код выхода `1` означает, что хотя бы одна проверка не прошла. Токен может подходить для одного API и не иметь прав на другое.

Доступ к аккаунту сам по себе не подтверждает доступ к умному дому. Для официального API применяются отдельные OAuth-права, в том числе `iot:view` и `iot:control`. Используйте только необходимые доступы и учитывайте [документацию платформы](https://yandex.ru/dev/dialogs/smart-home/doc/ru/concepts/platform-protocol).
