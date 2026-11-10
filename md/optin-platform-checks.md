# Opt-in проверки на живых платформах

Стандартный CI живых пакетных менеджеров не касается: он не должен вызывать
настоящие `apt`, `brew`, `sudo`, `docker pull` и внешние цели. Эти сценарии
запускаются только явно, на одноразовой машине, и в CI не входят.

## Где что проверено

| Платформа | Статус | Чем |
|---|---|---|
| macOS arm64 (эта машина) | проверено живьём | capabilities, dry-run, реальная установка hashcat через brew, no-op, verify |
| Parrot Security 7.4 aarch64 (VM 10.211.55.27) | проверено живьём | capabilities, dry-run (apt), реальная установка nuclei через `sudo -n`, no-op, verify, отказ manual-only |
| Kali Linux | не проверено | только fake-окружение и юниты |
| macOS x86_64 | не проверено | только fake-окружение и юниты |

Прогон на Parrot (2026-11-09): `os` = `linux parrot arm64`, docker отсутствует и
честно помечен, `nmap/ffuf/hashcat/curl` найдены нативно, `nuclei` отсутствовал;
dry-run показал `sudo -n apt-get install -y --no-install-recommends nuclei` и не
оставил следов; установка дала `ready` и версию после healthcheck, повтор —
`provision.skipped`, `verify` вернул версию с кодом 0. Живой прогон вскрыл две
шероховатости и обе исправлены: цветовые ANSI-последовательности в записи
версии и выбор строки-предупреждения вместо строки версии.

Непроверенная платформа — это непроверенная платформа: Kali и macOS x86_64
живьём не запускались, их рецепты опираются на apt/brew и на разбор
`/etc/os-release`, но проверены только fake-окружением.

## Ручной сценарий (свежая VM, любой дистрибутив из таблицы)

Все шаги — в изолированном `SAMURAI_HOME`, чтобы не трогать домашнее состояние.

```bash
export SAMURAI_HOME=$(mktemp -d)
samurai tool capabilities --json                 # 1. детект: distro, arch, apt/brew, sudo, docker
samurai tool provision scan.nmap --dry-run       # 2. план: платформа, пакет, команды, привилегии
samurai --db "$SAMURAI_HOME/t.db" operation create "bench" --mode ctf-htb --include 127.0.0.1
samurai --db "$SAMURAI_HOME/t.db" tool provision scan.nmap   # 3. approval → установка → healthcheck
samurai --db "$SAMURAI_HOME/t.db" tool provision scan.nmap   # 4. повтор: должен быть no-op
samurai tool verify scan.nmap                    # 5. read-only: версия
```

Что должно получиться на каждом шаге:

1. `os_family` совпадает с платформой, `arch` — с железом; при отсутствии `sudo`
   без пароля `sudo` = `false`, и шаг 2 говорит `missing-privilege` с ручной
   командой — это норма, установка тогда выполняется руками.
2. Dry-run печатает `apt-get install -y --no-install-recommends nmap` (или
   `brew install nmap`), строку `check nmap -V` и требование привилегий; в
   `$SAMURAI_HOME` ничего не появляется.
3. Статусы идут `awaiting-approval → installing → verifying → ready`, событие
   `provision.ready` в журнале, версия в состоянии.
4. Второй прогон — `provision.skipped`, установка заново не идёт.
5. `verify` печатает версию, код 0.

## Project-local на той же машине

```bash
samurai --db "$SAMURAI_HOME/t.db" tool provision web.http-headers --dry-run
samurai --db "$SAMURAI_HOME/t.db" tool provision web.http-headers
ls "$SAMURAI_HOME/tools/bin"          # скрипт установлен копией, без pip
```

## Отказы, которые надо увидеть своими глазами

- `SAMURAI_HOME=$(mktemp -d) samurai tool provision crack.hashcat --dry-run` на
  машине без GPU — `unsupported-platform` с упоминанием GPU.
- То же на машине без `brew`/`apt-get` — `unsupported-platform`.
- С `--offline` у операции — `offline`.
- `tool provision crack.john --dry-run` — `manual-only`.

## Если проверяешь на Kali/Parrot

Записать в отчёт: дистрибутив и версия, `arch`, был ли `sudo -n`, что ответил
dry-run, чем закончилась установка, есть ли `provision.ready` и что показал
`verify`. Непроверенное не объявлять проверенным.
