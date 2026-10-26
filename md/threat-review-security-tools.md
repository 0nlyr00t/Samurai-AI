# Threat review — расширенный набор security tools

Закрывает отложенный пункт 5 Stage 2b. Цель: решить, какие внешние security
tools и в какой форме допускаются в управляемый tool registry Samurai AI, и
какие контроли обязательны для каждого класса. Реализация одобренных
адаптеров — отдельным этапом; этот документ — основание для неё.

Дата: 2026-10-26. Статус: принят.

## 1. Методология

Каждый кандидат оценивается по осям:

- **Кто контролирует ввод**: оператор (доверенный), модель (prompt-driven,
  частично доверенная), цель отвечает (недоверенный ввод).
- **Куда утекают данные**: цель (in-scope), третьи стороны (API сервисов
  recon, телеметрия инструмента), локальный диск (артефакты, кэши).
- **Классы угроз**: выход за scope / нарушение ROE, argv/flag injection,
  injection через output цели, supply chain инструмента, телеметрия/phone-home,
  утечка секретов, ресурсный вред цели (DoS по неосторожности), ложное
  доверие к результату (false positives в отчётах).

Существующие границы enforcement, на которые опирается review: scope
проверяется broker/egress-слоем, а не prompt; fail-closed JSON-schema
параметров; risk classes R0–R4 с approvals; signed sandbox profiles с
pinned-by-digest образами; egress proxy с per-run token и DNS pinning; vault
placeholders с redaction; tool output помечается untrusted-external с
injection warnings.

Известные принятые ослабления (на момент review): на Docker Desktop for Mac
scoped sandbox не даёт hard egress enforcement (bridge NAT); полный
enforcement — только на Linux-harness. Это **блокер для direct-network
инструментов вне egress proxy на macOS** и учитывается в решениях ниже.

## 2. Модель угроз по классам инструментов

### 2.1 Активный сетевой контакт (nmap, naabu, nuclei, nikto, ffuf, sqlmap)

- **Scope/ROE**: сканирование вне scope — юридический инцидент. Текстовый
  анализ команды недостаточен (requirements.md): каждый target-аргумент
  должен проходить `scope.check` в адаптере до запуска процесса, а сетевой
  выход — через scoped egress proxy (DNS pinning, per-run token).
- **Ресурсный вред цели**: default rate инструментов может смотреться как
  DoS. Обязателен явный throttle в шаблоне флагов (rate/delay/threads),
  верхняя граница зашита в адаптер, не выбирается моделью.
- **Вывод**: только R3, только через scoped egress, throttle зашит.

### 2.2 Passive recon с внешними API (subfinder, amass, httpx probe)

- **Утечка намерения**: DNS-запросы и API-запросы к third-party источникам
  раскрывают интерес к цели третьим сторонам. Допустимо в bug bounty scope,
  фиксируется в журнале операции (кто и когда запрашивал).
- **API keys**: провайдерские ключи (SecurityTrails, Shodan и т.п.) — только
  через `{{secret:name}}` placeholders; в argv/процессе plaintext не
  появляется (env-передача с redaction — отдельный пункт реализации).
- **Вывод**: R2, egress через proxy, ключи из vault.

### 2.3 argv/flag injection

Модель формирует параметры инструмента. Free-form argv (как в
`process.exec`) для сетевых инструментов недопустим: `-oX /etc/passwd`,
`--resume`, `--config`, подстановки `--` и file-аргументы ломают границы.
**Решение: dedicated адаптеры с фиксированным шаблоном флагов** — модель
выбирает значения из enum/regex-валидированных параметров (target, profile,
rate class), адаптер строит argv сам. Generic `process.exec` остаётся
локальным (без сети, allowlist executables) и не расширяется.

### 2.4 Output цели как injection-канал

Баннеры, HTML, заголовки, DNS TXT — контролируемый целью контент. Уже
покрыто: output помечается untrusted-external, scanner ставит injection
warnings, память read-only для агентов (6b prompt-injection suite).
Дополнительное требование к адаптерам: **structured output** (JSON-режимы
инструментов) с парсингом и bounded размером предпочтительнее raw stdout;
raw stdout остаётся артефактом, в prompt попадает только извлечённая
структура.

### 2.5 Supply chain

- Образы — только pinned by digest в signed sandbox profiles (уже есть);
  смена digest = переподпись allowlist.
- Инструменты с self-update (nuclei `-update`, загрузка templates по сети)
  — update-функциональность отключается флагами; версии шаблонов поставляются
  в образе и фиксируются в evidence (`tool version` уже пишется в FR-12).
- Wordlists — только bundled в образ с sha256-манифестом; загрузка
  wordlist по URL из модели запрещена.

### 2.6 Телеметрия инструментов

Часть инструментов звонит домой (version check, аналитика). Правила:
инструмент без off-флага телеметрии **не допускается**; у допущенных
off-флаги зашиты в шаблон. Контрольным рубежом служит egress proxy:
единственный сетевой выход — через него, любой посторонний хост отклоняется
и попадает в аудит.

## 3. Матрица решений

| Инструмент | Решение | Risk | Сеть | Обязательные контроли |
|---|---|---|---|---|
| httpx (probe) | dedicated adapter | R2 | scoped egress | target через scope.check; флаги из enum; JSON output; rate cap |
| dnsx | dedicated adapter | R2 | scoped egress | как у httpx; resolver pinned через proxy |
| subfinder | dedicated adapter | R2 | scoped egress | passive-only флаги зашиты; API keys из vault; JSON output |
| amass | defer | — | — | перекрывается subfinder; вернуться при необходимости |
| nmap | dedicated adapter | R3 | scoped egress | profile enum (`fast`/`standard`, зашитые `-T`/rate cap); без `--script vuln/exploit` на первом этапе; XML output парсится |
| naabu | defer | — | — | перекрывается nmap profile; SYN-scan требует raw sockets — отдельный разговор про capabilities контейнера |
| ffuf | dedicated adapter | R3 | scoped egress | wordlist из bundled манифеста (id, не path); rate/threads зашиты; JSON output |
| gobuster | defer | — | — | перекрывается ffuf |
| nuclei | dedicated adapter | R3 | scoped egress | templates из образа с sha256-манифестом; `-disable-update-check` зашит; severity enum; без `-headless`/fuzzing-шаблонов на первом этапе |
| nikto | defer | — | — | перекрывается nuclei; высокий FP-rate |
| sqlmap | **reject на текущем governance** | R4 | — | активная эксплуатация БД; требует ROE-binding (red-team операция с approved ROE) — вернуться после появления ROE-gated execution |
| gowitness | sandbox adapter | R2 | scoped egress | скриншоты в sandbox.default-профиле (без сети наружу мимо proxy); output — файлы-артефакты |
| tshark | sandbox adapter | R1 | none | анализ локального pcap в sandbox без сети; capture-функциональность не допускается |

Общие требования к каждому одобренному адаптеру: fail-closed params schema;
нет free-form argv; target валидируется `scope.check` до spawn; сетевой выход
только через egress proxy; throttle зашит; structured output + bounded raw
artifact; версия инструмента и digest образа в evidence; секреты только
vault placeholders.

## 4. Appendix — спецификации первых адаптеров (готово к реализации)

### `recon.httpx_probe` v1 (R2, scoped egress)

- params: `targets: array<string>` (1..50, каждый — scope.check),
  `profile: enum [headers, tech, full]`, `timeout_seconds` (≤60).
- шаблон argv: `httpx -json -timeout <t> -rate-limit 50 -no-color
  [-title -status-code | -tech-detect | -follow-redirects ...]` по profile.
- output: NDJSON → структура (host, status, title, tech[]); raw в артефакт
  (≤256KB).

### `recon.subfinder_passive` v1 (R2, scoped egress)

- params: `domain: string` (scope.check), `sources: array<enum>`
  (бесплатные без ключей по умолчанию), опционально `key_ref: string` —
  имя vault-секрета, никогда не значение.
- шаблон argv: `subfinder -silent -json -all=false [-sources ...]`;
  ключи через env из vault с redaction.
- output: список доменов → candidates для asset ledger (через
  `asset add`, не прямая запись).

### `scan.nmap` v1 (R3, scoped egress, approval обязателен)

- params: `targets` (1..10, scope.check), `profile: enum [fast, standard]`
  → `-F -T3` / `-sV -T3 --max-rate 300`, `ports: string` (regex
  `^\d+(-\d+)?(,\d+(-\d+)?)*$`).
- output: `-oX -` → парсинг портов; raw XML в артефакт.

### `scan.nuclei` v1 (R3, scoped egress, approval обязателен)

- params: `targets` (1..10, scope.check), `severity: array<enum
  [info,low,medium,high,critical]>`, `template_set: enum [cves, exposures,
  misconfig]` → bundled манифест.
- шаблон: `nuclei -jsonl -disable-update-check -rate-limit 50 -c 10
  -bulk-size 5`.
- output: JSONL → findings candidates (через `finding propose` с dedup),
  raw в артефакт.

### `fuzz.ffuf` v1 (R3, scoped egress, approval обязателен)

- params: `target` (scope.check, URL с плейсхолдером FUZZ), `wordlist:
  enum` (bundled: `common`, `raft-small`, `params`), `extensions`,
  `rate: enum [low, medium]` → зашитые `-rate`/`-t`.
- output: JSON → список путей; raw в артефакт.

## 5. Критерии отказа (действуют и для будущих кандидатов)

Не допускаются в registry:

1. инструменты, требующие free-form shell/argv из модели;
2. self-updating или скачивающие код/шаблоны в runtime без pinned digest;
3. с обязательной телеметрией без off-флага;
4. R4-инструменты (эксплуатация, DoS-capable) без привязки к approved ROE
   red-team операции;
5. требующие прямого сетевого доступа в обход egress proxy.

## 6. Открытые вопросы к реализации

- Передача API keys через env внутри sandbox с redaction (текущий механизм
  placeholders покрывает argv; env — расширить).
- Raw-sockets сканеры (naabu/masscan) — capabilities контейнера, отдельный
  review при появлении потребности.
- Rate enforcement слоем egress proxy (сейчас throttle зашит в argv;
  дублирующий лимит на proxy — задел).
