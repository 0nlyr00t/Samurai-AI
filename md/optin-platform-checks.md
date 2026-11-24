# Opt-in проверки на изолированных платформах

Стандартный CI живых пакетных менеджеров не касается: он не должен вызывать
настоящие `apt`, `brew`, `sudo`, `docker pull` и внешние цели. Эти сценарии
запускаются только явно, на одноразовой изолированной машине, и в CI не входят.

Отчёты ручных прогонов, адреса машин, имена целей, доказательства и любые
секреты остаются в локальном хранилище и не публикуются в git. Непроверенная
платформа не должна объявляться проверенной.

## Подготовка

Все шаги выполняются в изолированном `SAMURAI_HOME`, чтобы не трогать домашнее
состояние оператора:

```bash
export SAMURAI_HOME=$(mktemp -d)
```

## Базовый сценарий

```bash
samurai tool capabilities --json                 # 1. детект: distro, arch, apt/brew, sudo, docker
samurai tool provision scan.nmap --dry-run       # 2. план: платформа, пакет, команды, привилегии
samurai --db "$SAMURAI_HOME/t.db" operation create "bench" --mode ctf-htb --include 127.0.0.1
samurai --db "$SAMURAI_HOME/t.db" tool provision scan.nmap   # 3. approval → установка → healthcheck
samurai --db "$SAMURAI_HOME/t.db" tool provision scan.nmap   # 4. повтор: должен быть no-op
samurai tool verify scan.nmap                    # 5. read-only: версия
```

Что должно получиться на каждом шаге:

1. `os_family` совпадает с платформой, `arch` — с железом; при отсутствии `sudo`
   без пароля `sudo` = `false`, и шаг 2 сообщает `missing-privilege` с ручной
   командой — это норма, установка тогда выполняется вручную.
2. Dry-run печатает план установки и healthcheck, не создавая файлы в
   `$SAMURAI_HOME`.
3. Статусы идут `awaiting-approval → installing → verifying → ready`, событие
   `provision.ready` появляется в журнале, версия сохраняется в состоянии.
4. Второй прогон возвращает `provision.skipped`, повторная установка не идёт.
5. `verify` печатает версию и завершается с кодом 0.

## Project-local на той же машине

```bash
samurai --db "$SAMURAI_HOME/t.db" tool provision web.http-headers --dry-run
samurai --db "$SAMURAI_HOME/t.db" tool provision web.http-headers
ls "$SAMURAI_HOME/tools/bin"          # скрипт установлен копией, без pip
```

## Отказы, которые надо проверить

- `SAMURAI_HOME=$(mktemp -d) samurai tool provision crack.hashcat --dry-run` на
  машине без GPU — `unsupported-platform` с упоминанием GPU.
- То же на машине без `brew`/`apt-get` — `unsupported-platform`.
- С `--offline` у операции — `offline`.
- `tool provision crack.john --dry-run` — `manual-only`.

## Локальный отчёт без публикации

Для внутреннего отчёта достаточно записать дистрибутив и версию, `arch`, наличие
`sudo -n`, результат dry-run и установки, наличие `provision.ready` и вывод
`verify`. Такой отчёт хранится вне публичного git и перед передачей очищается от
адресов, имён целей, секретов и содержимого доказательств.
