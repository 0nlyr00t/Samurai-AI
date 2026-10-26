# Политика совместимости Samurai AI

Действует с релиза 0.1.0. До версии 1.0.0 minor-релизы могут менять детали,
помеченные ниже как «pre-1.0»; все ломающие изменения фиксируются в changelog
релиза.

## Публичный контракт (стабильный в пределах major-версии)

- **CLI**: команды верхнего уровня (`operation`, `plan`, `task`, `tool`,
  `model`, `agent`, `swarm`, `playbook`, `report`, `challenge`, `roe`,
  `scenario`, `memory`, `skill`, `events`, `headless`, `eval` и др.).
  Переименование или удаление команды — только в major-релизе. Флаги —
  pre-1.0.
- **Exit codes**: 0 — успех, 1 — негативный доменный исход (deny, regression,
  unmet expectations), 2 — ошибка использования/домена/approval. Контракт
  зафиксирован тестами (`tests/integration/test_exit_codes.py`) и не меняется
  никогда внутри major-версии.
- **Журнал событий**: типы событий (`operation.created`, `model.call`,
  `agent.run.*`, …) только пополняются; существующие типы и семантика
  payload не удаляются и не меняют значение. Новые поля в payload —
  additive, потребители обязаны игнорировать неизвестные поля.
- **Экспорты**: NDJSON журнала (одно событие на строку, поля
  id/created_at/event_type/payload), VECTR subset `vectr-campaign@v1`,
  CALDERA pack layout (`adversaries/*.yml` + `abilities/<tactic>/*.yml`),
  eval suite/report JSON — версионируются полем `version`/`suite_version`;
  reader старой версии формата не обязан читать новую.

## База данных (SQLite)

- Миграции только вперёд и применяются автоматически при открытии БД
  (`Database.initialize` идемпотентен, цепочка v1→vN покрыта
  `tests/integration/test_migration_chain.py`).
- Downgrade БД (открытие базы новой версии старым бинарём) не
  поддерживается. Перед апгрейдом делайте снапшот через
  `Database.backup()` (sqlite online backup) или копию файла при остановленном
  приложении.

## Модели и провайдеры

- Model id имеет формат `<provider>/<model>@v<N>`; регистрация модели без
  версии отклоняется.
- Смена провайдера/endpoint для существующего model id — конфигурационное
  изменение, отражается в журнале (`model.call` payload), но не ломает
  чтение старых журналов.

## Сборки и подписи

- sdist/wheel воспроизводимы побитно при фиксированном `SOURCE_DATE_EPOCH`
  (проверяется `packaging/check_reproducible.sh` в CI).
- Standalone-бинарники (PyInstaller) побитно не воспроизводимы; доверие к
  ним обеспечивается keyless-подписью Sigstore в release workflow
  (сертификат выдаётся на GitHub Actions OIDC-identity репозитория).
- Проверка артефакта релиза:
  `cosign verify-blob --bundle <file>.bundle <file>
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
  --certificate-identity-regexp
  https://github.com/0nlyr00t/Samurai-AI/.github/workflows/release.yml`
