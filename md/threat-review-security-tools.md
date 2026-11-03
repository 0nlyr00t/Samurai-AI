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
  из vault по имени; значение не появляется ни в argv, ни в журнале. Канал
  описан в 2.7.
- **Вывод**: R2, egress через proxy, ключи из vault.

### 2.7 Канал доставки секретов в sandbox (реализован)

Ключ, положенный в argv (`--env K=V`), виден в списке процессов и в
evidence; ключ, положенный в конфиг-файл, требует bind-mount и второго
канала с временным файлом 0600. Принят третий путь: имя переменной уходит в
argv, значение — в окружение клиента `docker`, откуда docker сам забирает его
по `--env NAME` без `=`.

Правила канала:

1. Значение даёт только vault (`{{secret:name}}` в параметре); плейсхолдер
   разворачивается на границе исполнения, после approval, и сразу
   регистрируется в редаторе — попадание в stdout/stderr/артефакт режется.
2. Имена переменных объявляет профиль (`env_names`, ≤16), а не модель:
   имя вне списка профиля — отказ, иначе контейнер получает произвольный env.
3. Имя — `^[A-Z][A-Z0-9_]{0,63}$`, значение непустое, однострочное, ≤8 КиБ.
4. Обычные (не секретные) переменные, нужные инструменту, идут значением
   (`--env K=V`) — HOME/TMPDIR на tmpfs.
5. Сетевые флаги, которым нужен proxy URL, берут URL из профиля
   (`proxy_flag`), чтобы токен не уезжал в argv параметров.
6. Passive-инструмент говорит с третьими сторонами, а не с целью: их
   хосты — список `egress_hosts` в подписанном профиле (точный хост или
   `*.suffix`). Промах списка даёт обычный `denied-scope` в аудите, так что
   дрейф виден, а не молча проходит.
7. Образы projectdiscovery объявляют свой entrypoint, поэтому исполняемый
   файл едет на `--entrypoint`: иначе имя дублировалось и инструмент падал
   с «no input list provided».
8. Инструмент, которому нужен только SOCKS (`dnsx -proxy`), получает второй
   вход прокси — SOCKS5 CONNECT-only (RFC 1928/1929, имя `samurai`, пароль =
   per-run токен). Вердикт, resolver и события те же, что у CONNECT; BIND и
   UDP ASSOCIATE отвечают `REP=7`. Схема прокси и DoH-резолвер — поля
   профиля (`proxy_scheme`, `resolver_url`), то есть в подписи.
9. Токен в этом случае неизбежно уезжает в argv (`-proxy socks5://samurai:<t>@…`),
   потому что env-пути у инструмента нет; токен регистрируется в редаторе и
   режется в выводе и артефактах.
10. Инструменты, которые прокси не может провести, живут классом `direct`:
   контейнер остаётся на bridge NAT, scope проверяется только в адаптере до
   spawn, egress-аудит у такого запуска отсутствует. Сейчас это nmap; на
   Docker Desktop Mac мост и так обходим, так что класс честно называет то, что
   происходит, вместо мнимого enforcement.

Остаточный риск: окружение процесса `docker` читается тем же пользователем
(`ps e`), а на Docker Desktop for Mac scoped sandbox остаётся soft
enforcement. Оба ограничения уже зафиксированы выше и не новы для канала.

Подпись профилей — HMAC от мастер-ключа vault, лежащего рядом в каталоге
состояния. Она пининит digest'ы и лимиты одним артефактом и делает дрейф
видимым, но не защищает от того, кто уже может писать в код: он подпишет то же
самое. Поэтому самолечение при расхождении принимается, а не считается дырой.

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
| httpx (probe) | **реализован** (`recon.httpx-probe`) | R2 | scoped egress | target через scope.evaluate; флаги из enum; JSON output; rate cap |
| dnsx | **реализован** (`recon.dnsx-resolve`) | R2 | scoped egress (SOCKS5) | resolver закреплён в подписи профиля (`doh:`), имена через stdin, `-rcode noerror,nxdomain`, молчащий резолвер = failed |
| subfinder | **реализован** (`recon.subfinder-passive`) | R2 | scoped egress | domain через scope.evaluate; закрытый enum источников; ключи только по имени через env-канал; `-proxy` из профиля; JSON output; без `-all` |
| amass | defer | — | — | перекрывается subfinder; вернуться при необходимости |
| nmap | **реализован** (`scan.nmap`) | R2 | direct | `-sT -Pn`, без NET_RAW; порты профилем или регексом; rate зашит; egress не аудируется — это класс direct |
| naabu | defer | — | — | перекрывается nmap profile; SYN-scan требует raw sockets — отдельный разговор про capabilities контейнера |
| ffuf | **реализован** (`fuzz.ffuf`) | R2 | scoped egress | образ `secsi/ffuf` по digest; словарь из репозитория через stdin; `-ac` от soft-404; rate зашит |
| gobuster | defer | — | — | перекрывается ffuf |
| nuclei | **реализован** (`scan.nuclei`) | R3 | scoped egress | templates из образа; `-disable-update-check` зашит; severity enum; без headless/fuzzing-шаблонов |
| nikto | defer | — | — | перекрывается nuclei; высокий FP-rate |
| sqlmap | **reject на текущем governance** | R4 | — | активная эксплуатация БД; требует ROE-binding (red-team операция с approved ROE) — вернуться после появления ROE-gated execution |
| gowitness | sandbox adapter | R2 | scoped egress | скриншоты в sandbox.default-профиле (без сети наружу мимо proxy); output — файлы-артефакты |
| tshark | sandbox adapter | R1 | none | анализ локального pcap в sandbox без сети; capture-функциональность не допускается |

Общие требования к каждому одобренному адаптеру: fail-closed params schema;
нет free-form argv; target валидируется `scope.check` до spawn; сетевой выход
только через egress proxy; throttle зашит; structured output + bounded raw
artifact; версия инструмента и digest образа в evidence; секреты только
vault placeholders.

## 3b. Манифесты вместо адаптеров (волна 6)

Инструмент без парсинга больше не требует кода: он описывается JSON-манифестом
и ставится `samurai tool install <file> --trusted`. Манифест — это поверхность
исполнения, поэтому ограничения жёсткие:

1. argv собирается из **типизированных плейсхолдеров**; значение проверяется на
   пробелы, NUL и ведущий дефис, поэтому флаг не проедет значением. Дефис
   разрешён только для перечислимых значений — они ревьюятся в манифесте.
2. Парсеры — закрытый реестр (`jsonl`, `xml-ports`, `text-lines`); манифест
   называет вид, но не описывает разбор.
3. Валидация fail-closed: висячий плейсхолдер, параметр вне argv/stdin,
   непиннутый образ, чужой парсер, несогласованные риск и сеть — отказ.
   Каждый рендеримый параметр обязан быть `required` или иметь `default`.
4. sandbox-профиль генерируется из манифеста (`sandbox.<id>@v<версия>`) и входит
   в общий подписываемый набор; подпись лечится сама, как и раньше.
5. Установка — акт доверия: `--trusted` обязателен, sha256 пишется рядом, правка
   после установки видна в `samurai tool manifests` как `tampered`.
6. Исполнение идёт тем же путём, что у код-адаптеров: scope до спавна, approval
   по риску, сырьё в артефакт, egress только через прокси.

Остаточные риски: манифест всё равно перечисляет литеральные флаги (ревью
обязательно), а `--trusted` проверяется выборочно по id, а не по содержимому.

## 3c. Браузер в песочнице (волна 7)

Разведка невозможна без браузера, а браузер исполняет чужой JavaScript.
Решения:

1. Профиль `sandbox.camoufox@v1` — образ по digest, allowlist **только**
   `python3`; драйвер едет текстом (`python3 -c`), параметры — argv.
2. Единственный сетевой путь — scoped-прокси, браузер получает его явно
   (`proxy` в launch-опциях) с логином и паролем. `407` теперь предлагает и
   `Basic`, иначе Firefox, умеющий только Basic, сдаётся.
3. Контейнер в этом профиле работает **от root**: кеш сборки браузера лежит в
   `/root/.cache` (режим 700), а непривилегированный nobody до него не достанет.
   Root тут ограничен `--cap-drop ALL`, `no-new-privileges` и read-only
   корнем — но это минутс против nobody, и его надо называть вслух.
4. `HOME`/`TMPDIR` — на tmpfs, а кеш адресуется `XDG_CACHE_HOME`, иначе
   camoufox качает 1.3 ГБ на каждый запуск.
5. `egress_hosts`: DDG и `github.com`/`api.github.com`/`raw.githubusercontent.com`
   — это собственные проверки и geoip самого camoufox, а не цель.
6. Прочитанное возвращается кандидатом пары трафика и записывается сервисом:
   цитата без пары неотличима от выдумки.

Остаточные риски: браузер от root, сторонний образ с широкой поверхностью,
скриншот едет base64 внутри артефакта, geoip-запрос раскрывает третьей стороне
факт использования прокси.

## 3d. Удалённое администрирование: туннели и операционный белт (волна 8)

Админские инструменты (ssh, SMB, RDP, ADB) не умеют HTTP-прокси, поэтому введён
**scoped-туннель**: эфемерный localhost-порт на одну цель, вердикт один раз до
первого байта, дальше чистые байты, одно `EgressEvent` на соединение и счётчики
объёма на туннеле. Порт берётся из параметра или дефолта инструмента; вне списка
известных админских портов и без явно названного порта — отказ.

1. Запрещено всё, что не прошло `scope.evaluate` и не резолвится; исключённые
   адреса тоже отказ.
2. Туннель живёт только на один запуск и закрывается после него.
3. Белт — собственный образ `packaging/sandbox/ops` (база по digest): ssh,
   smbclient, impacket, xfreerdp, adb, pwntools, gcc, gdb, nmap, socat. Свой
   образ нельзя запушить без прав на реестр, поэтому профиль пиннится локальным
   id `sha256:…`, а пересборка требует перепина (валидатор научен обоим видам).
4. Драйверы — текстом в `python3 -c`; ключи приходят из vault плейсхолдером и
   живут в tmpfs с правами 0600.
5. VM и эмуляторы — хостовые бинари (`VBoxManage`, `virsh`, `qemu-img`, `adb`)
   через `process.exec`-путь: закрытый argv по backend/action, имена VM и пути
   валидируются, отсутствующий бинарь даёт отказ, а не ошибку спавна.
6. Эксплойты: `exploit.analyze` (checksec/ROPgadget), `exploit.build` (gcc или
   синтаксис pwntools) без сети, `exploit.run` — скрипт по туннелю.

Удалённый доступ проверен живым прогоном: sshd-контейнер как цель, ssh через
туннель, команда отработала в чужом контейнере, объём учтён в egress.

Остаточные риски: белт работает от root только в профиле браузера (ops-профиль
остаётся на nobody), образ тяжёлый (748 МБ) и локально собранный, `qemu-img
convert` пишет в хостовую ФС, `exploit.run` исполняет присланный моделью скрипт.

## 3e. Лестница импакта (волна 9)

Уровни `read → write → auth-bypass → data-export → rce` с баллами 1–5 и порогом
отчётности 3. Каждая попытка требует доказательство (пара трафика или артефакт),
меняющие состояние уровни — ещё и заметку об откате, иначе отказ. Подтверждённая
попытка бампит `evidence_version` находки, поэтому отчёт против устаревшего
набора не выйдет. Бюджет эскалации (8 попыток, 120 запросов, час) считается из
журнала: потраченное не возвращается при возобновлении сессии.

## 3f. Гены и гейт продвижения (волна 10)

Эволюционирует только слой решений: гены — это блоки промпта, политики
инструментов и подсказки плейбуков. Ген рождается в `shadow`, фитнес копится
медианами из eval (нужно ≥3 замера), продвижение требует строгого превосходства
над активным геном, откат ретирует текущий и возвращает предыдущий. Слой
принуждения (scope, правила, бюджеты, доказательства) генами не меняется.

Остаточные риски: пул хранится файлом в состоянии (не в журнале), гены
подмешиваются в промпт вручную, оценка фитнеса — доверие к отчёту eval.

## 4. Appendix — спецификации адаптеров

### `recon.httpx-probe` v1 (R2, scoped egress) — реализован

- params: `targets: array<string>` (1..50, каждый — scope.check),
  `profile: enum [headers, tech, full]`, `timeout_seconds` (≤60).
- шаблон argv: `httpx -json -timeout <t> -rate-limit 50 -no-color
  [-title -status-code | -tech-detect | -follow-redirects ...]` по profile.
- output: NDJSON → структура (host, status, title, tech[]); raw в артефакт
  (≤256KB).

### `recon.subfinder-passive` v1 (R2, scoped egress) — реализован

- params: `domain: string` (scope.check), `sources: array<enum>` из
  безключевых источников (`crtsh`, `anubis`, `waybackarchive`,
  `commoncrawl`, `sitedossier`, `threatcrowd`, `rapiddns`, `hackertarget`,
  `subdomaincenter`), `keys: object` — карта источник → `{{secret:name}}`
  для источников, которым нужен ключ, `timeout_seconds` (≤60).
- шаблон argv: `subfinder -silent -json -nc -disable-update-check
  -t 10 -timeout <t> -d <domain> [-s <sources>]`; `-proxy` и env подставляет
  раннер профиля `sandbox.subfinder@v1`. Ключи уезжают как
  `{SOURCE}_API_KEY` по каналу 2.7, ключ для источника вне enum — отказ.
- output: JSONL → список хостов (кандидаты в asset ledger через
  `asset add`, не прямая запись); raw в артефакт (≤256KB).
- `egress_hosts` профиля — API-хосты принятых источников, снятые прогоном
  (crt.sh, anubisdb.com, *.commoncrawl.org, api.shodan.io и т.д.); список
  часть подписи, расширение = переподпись.
- источники без ключа работают без `keys`; resolvers по UDP 53 идут мимо
  proxy — принятое ослабление, а не регрессия.
- набор sandbox-профилей меняется с каждым новым адаптером: подпись allowlist
  расходится, и первый же запуск переподписывает её сам, предупреждая в stderr и
  оставляя след в `samurai sandbox profiles`. Ручной путь (`samurai sandbox sign`)
  и проверка без записи (`samurai sandbox verify`, код 1 при расхождении)
  остаются.

### `recon.dnsx-resolve` v1 (R2, scoped egress через SOCKS5) — реализован

- params: `hosts: array<string>` (1..50, каждый — scope.check), `types: array<enum
  [A,AAAA,CNAME]>` (default `[A]`).
- резолвер в argv не выбирается: он из профиля (`resolver_url =
  doh:https://cloudflare-dns.com/dns-query`, хост в `egress_hosts`), имена уезжают
  в stdin (флаг `-l` в образе 1.2.2 молча не работает).
- шаблон: `dnsx -silent -json -omit-raw -nc -disable-update-check -rcode
  noerror,nxdomain [-a|-aaaa|-cname] -r <resolver>`; `-proxy` и env
  подставляет раннер профиля `sandbox.dnsx@v1`.
- output: JSONL → строки `host<TAB>STATUS<TAB>записи` (кандидаты для
  `asset add`); NXDOMAIN остаётся строкой со статусом, все-без-записей даёт
  `no answers`. Ноль разобранных строк при exit 0 — failed с именем резолвера
  (различение «имён нет» и «резолвер молчит»).
- ограничение: у инструмента нет env-пути для прокси, токен виден в argv;
  UDP ASSOCIATE не поддерживается — только DoH-over-TCP.

### `scan.nmap` v1 (R2, класс direct) — реализован

- params: `targets` (1..10, scope.check), `profile: enum [fast, standard]` →
  `-F` / `--top-ports 1000` (взаимоисключимы с `ports`), `ports: string`
  (regex `^\d+(-\d+)?(,\d+(-\d+)?)*$`, верхняя граница 65535),
  `service_detection: bool`.
- шаблон: `nmap -Pn -sT -n --max-rate 300 <порты> -oX - [-sV --version-light]`
  — connect-скан, чтобы остаться на `--cap-drop ALL` без NET_RAW; `-Pn`
  всегда, потому что ping тоже требует raw ICMP.
- output: XML → строки `host:port/proto open service version` (≤100, закрытые
  порты не печатаются), raw XML в артефакт.
- почем прокси нет: `nmap --proxies` действует только на `-sV`/NSE и не умеет
  авторизацию, а фазы ping/port-scan его игнорируют — так что nmap отнесён к
  классу direct (см. 2.8).

### `scan.nuclei` v1 (R3, scoped egress, approval обязателен) — реализован

- params: `targets` (1..10, scope.check), `severity: array<enum
  [info,low,medium,high,critical]>`, `template_set: enum [cves, exposures,
  misconfig]` → bundled манифест.
- шаблон: `nuclei -jsonl -disable-update-check -rate-limit 50 -c 10
  -bulk-size 5`.
- output: JSONL → findings candidates (через `finding propose` с dedup),
  raw в артефакт.

### `fuzz.ffuf` v1 (R2, scoped egress) — реализован

- params: `url` (scope.check по хосту, обязателен литерал `FUZZ`),
  `extensions: array<enum [.php,.bak,.js,.txt,.zip]>`.
- образ: официального нет, взят общинный `secsi/ffuf` по digest индекса;
  словарей в нём нет, а монтирования мы не используем — поэтому словарь
  лежит в репозитории (`assets/wordlists/common.txt`, SecLists `0c618cf`,
  sha256 сверяется тестом) и едет через stdin (`-w /dev/stdin:FUZZ`).
- шум гасится штатным статусным матчером и `-ac` (автокалибровка): на
  soft-404 и SPA-роутерах без неё «находится» всё.
- шаблон: `ffuf -s -noninteractive -of json -o /dev/stdout -ac -mc <набор>
  -rate 50 -t 20 -w /dev/stdin:FUZZ -u <url> [-e ...]`; `-x` и HOME подставляет
  раннер профиля `sandbox.ffuf@v1`.
- output: JSON `results[]` → `url status size words` (≤100); пусто —
  `no matches`; raw JSON в артефакт.
- ограничение: словарь ≤64 КиБ (кап stdin), поэтому только `common`;
  `raft-small-directories` не влезает.

## 5. Критерии отказа (действуют и для будущих кандидатов)

Не допускаются в registry:

1. инструменты, требующие free-form shell/argv из модели;
2. self-updating или скачивающие код/шаблоны в runtime без pinned digest;
3. с обязательной телеметрией без off-флага;
4. R4-инструменты (эксплуатация, DoS-capable) без привязки к approved ROE
   red-team операции;
5. требующие прямого сетевого доступа в обход egress proxy — кроме
   резолвинга по UDP 53 (HTTP CONNECT его не переносит); при разрешённом
   исключении оно фиксируется в матрице и в спецификации адаптера.

## 6. Открытые вопросы к реализации

- ~~Передача API keys через env внутри sandbox с redaction~~ — закрыто
  каналом 2.7.
- Raw-sockets сканеры (naabu/masscan) — capabilities контейнера, отдельный
  review при появлении потребности.
- Rate enforcement слоем egress proxy (сейчас throttle зашит в argv;
  дублирующий лимит на proxy — задел).
