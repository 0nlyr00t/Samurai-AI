SUPPORTED_LOCALES: tuple[str, ...] = ("ru", "en")
DEFAULT_LOCALE = "en"

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "input.placeholder": "Enter a command; /help lists Operation Kernel capabilities",
        "welcome.title": "Samurai AI 0.1.0 — governed operation shell",
        "welcome.stage": "Stage 1: plans, tasks, gates, approvals, evidence and findings",
        "welcome.loaded": "Loaded {operation_id}: {name}",
        "welcome.no-operation": (
            "No operation loaded. Create one with: samurai operation create --help"
        ),
        "welcome.help-hint": "Use /help to list available commands.",
        "help.help": "/help             Show available commands",
        "help.status": "/status           Show the current operation and kernel summary",
        "help.scope": "/scope [target]   Show scope rules or evaluate a target",
        "help.plan": "/plan             Show the current plan and lifecycle stage",
        "help.tasks": "/tasks            Show plan tasks",
        "help.gates": "/gates            Show stage gates",
        "help.findings": "/findings         Show findings",
        "help.evidence": "/evidence         Show evidence artifacts",
        "help.approvals": "/approvals        Show approval requests",
        "help.snapshots": "/snapshots        Show operation snapshots",
        "help.reconcile": "/reconcile        Reconcile interrupted recovery actions",
        "help.tools": "/tools            Show registered tool definitions",
        "help.runs": "/runs             Show tool runs of the current operation",
        "help.agents": "/agents          Show agent runs of the current operation",
        "help.swarm": "/swarm           Show parallel agent swarms of the current operation",
        "help.memory": "/memory [ws]     Show campaign memory entries (workspace filter)",
        "help.skills": "/skills          Show installed trusted skills",
        "help.playbooks": "/playbooks       Show installed custom playbooks",
        "help.secrets": "/secrets         Show secret metadata (values are never shown)",
        "help.assets": "/assets           Show the bug bounty asset ledger",
        "help.report": "/report           Show generated reports",
        "help.hypotheses": "/hypotheses       Show the CTF/HTB hypothesis log",
        "help.flags": "/flags            Show the CTF/HTB flag ledger",
        "help.attack-path": "/attack-path      Show the red team attack path",
        "help.debrief": "/debrief          Show generated debriefs",
        "help.test-cases": "/test-cases       Show planned test cases",
        "help.runbook": "/runbook          Show generated runbooks",
        "help.lang": "/lang [ru|en]     Show or change the interface language",
        "help.model": "/model           Pick the default model and reasoning effort",
        "help.exit": "/exit             Close Samurai AI",
        "help.ctrl-o": "Ctrl-O            Toggle the event journal",
        "help.ctrl-c": "Ctrl-C            Close Samurai AI",
        "error.unknown-command": "Unknown command: {name}. Use /help.",
        "info.ai-disabled": "AI orchestration is not enabled yet. Use /help.",
        "status.no-operation": "No operation loaded",
        "scope.includes": "includes: {rules}",
        "scope.excludes": "excludes: {rules}",
        "scope.none": "(none)",
        "plan.stage": "stage={stage}; objective={objective}",
        "plan.objective-unset": "(objective not set)",
        "plan.no-plan": "No current plan. Create one with: samurai plan create",
        "plan.approved": "approved",
        "plan.not-approved": "not-approved",
        "tasks.empty": "No current plan tasks",
        "gates.empty": "No current plan gates",
        "findings.empty": "No findings",
        "evidence.empty": "No artifacts",
        "approvals.empty": "No approvals",
        "snapshots.empty": "No snapshots",
        "tools.empty": "No registered tools",
        "tools.network": "network",
        "tools.no-network": "no-network",
        "runs.empty": "No tool runs",
        "agents.empty": "No agent runs",
        "swarm.empty": "No agent swarms",
        "memory.empty": "No campaign memory entries",
        "skills.empty": "No skills installed",
        "playbooks.custom-empty": "No custom playbooks installed",
        "assets.empty": "No assets recorded",
        "report.empty": "No reports generated",
        "hypotheses.empty": "No hypotheses recorded",
        "flags.empty": "No flags captured",
        "attack-path.empty": "No attack steps planned",
        "debrief.empty": "No debriefs generated",
        "test-cases.empty": "No test cases planned",
        "runbook.empty": "No runbooks generated",
        "secrets.empty": "No secrets in this scope",
        "secrets.global": "global",
        "header.with-operation": (
            "SAMURAI AI  {operation_id}  {mode}  SCOPE: OK  MODEL: NOT CONFIGURED"
        ),
        "header.no-operation": "SAMURAI AI  NO OPERATION  SCOPE: NOT SET",
        "statusbar.with-operation": (
            "{status} | TASKS {tasks_done}/{tasks_total} | GATES {gates_passed}/"
            "{gates_total} | PENDING {pending} | APPROVAL: ASK | /help"
        ),
        "statusbar.idle": "IDLE | APPROVAL: ASK | /help",
        "lang.current": "Current language: {locale} (available: ru, en)",
        "lang.changed": "Language changed to {locale}",
        "lang.invalid": "Unsupported language: {locale}. Use ru or en.",
        "chooser.title": "Choose your language / Выберите язык",
        "chooser.english": "English",
        "chooser.russian": "Русский",
        "model.picker-title": "Select model and reasoning",
        "model.picker-models": "Model (from registry and provider APIs)",
        "model.picker-reasoning": "Reasoning effort",
        "model.picker-hint": "Arrows to move, Tab switches column, Enter applies, Esc cancels",
        "model.selected": "default model: {model} | reasoning: {reasoning}",
        "model.empty": "No models available; check provider health",
        "model.picker-add-custom": "+ Add custom OpenAI-compatible endpoint…",
        "model.custom-title": "Add custom OpenAI-compatible endpoint",
        "model.custom-id": "endpoint id (lowercase, e.g. giga)",
        "model.custom-url": "base URL (e.g. https://api.example.com/v1)",
        "model.custom-key": "API key (optional; stored encrypted in the vault)",
        "model.custom-tls": "TLS certificate verification",
        "model.custom-tls-on": "on (recommended)",
        "model.custom-tls-off": "off — accept any certificate (MITM roots)",
        "model.custom-fetch": "Connect & fetch models",
        "model.custom-fetching": "connecting and fetching models…",
        "model.custom-pick": "Pick a model with arrows and Enter — or type a name below",
        "model.custom-no-models": (
            "endpoint registered, but no models discovered; "
            "type the model name below and press Enter"
        ),
        "model.custom-manual": "or type a model name and press Enter",
        "model.custom-invalid": "endpoint id and base URL are required",
        "model.custom-failed": "failed: {error}",
        "reconcile.report": (
            "actions-marked-unknown={actions} | tool-runs-marked-failed={runs} | "
            "resumable-tasks={tasks} | pending-approvals={approvals}"
        ),
        "list.none": "(none)",
    },
    "ru": {
        "input.placeholder": "Введите команду; /help показывает возможности ядра операции",
        "welcome.title": "Samurai AI 0.1.0 — управляемая оболочка операций",
        "welcome.stage": "Этап 1: планы, задачи, гейты, согласования, доказательства и находки",
        "welcome.loaded": "Загружена {operation_id}: {name}",
        "welcome.no-operation": (
            "Операция не загружена. Создайте её: samurai operation create --help"
        ),
        "welcome.help-hint": "Введите /help для списка команд.",
        "help.help": "/help             Показать доступные команды",
        "help.status": "/status           Показать текущую операцию и сводку ядра",
        "help.scope": "/scope [цель]     Показать правила области или проверить цель",
        "help.plan": "/plan             Показать текущий план и этап жизненного цикла",
        "help.tasks": "/tasks            Показать задачи плана",
        "help.gates": "/gates            Показать контрольные гейты",
        "help.findings": "/findings         Показать находки",
        "help.evidence": "/evidence         Показать артефакты-доказательства",
        "help.approvals": "/approvals        Показать запросы на согласование",
        "help.snapshots": "/snapshots        Показать снимки операции",
        "help.reconcile": "/reconcile        Свести прерванные действия восстановления",
        "help.tools": "/tools            Показать зарегистрированные инструменты",
        "help.runs": "/runs             Показать запуски инструментов текущей операции",
        "help.agents": "/agents          Показать запуски агентов текущей операции",
        "help.swarm": "/swarm           Показать параллельные swarm-запуски операции",
        "help.memory": "/memory [ws]     Показать записи campaign memory (фильтр workspace)",
        "help.skills": "/skills          Показать установленные доверенные skills",
        "help.playbooks": "/playbooks       Показать установленные custom playbooks",
        "help.secrets": "/secrets         Показать метаданные секретов (значения скрыты)",
        "help.assets": "/assets           Показать реестр активов bug bounty",
        "help.report": "/report           Показать сгенерированные отчеты",
        "help.hypotheses": "/hypotheses       Показать журнал гипотез CTF/HTB",
        "help.flags": "/flags            Показать реестр флагов CTF/HTB",
        "help.attack-path": "/attack-path      Показать attack path red team",
        "help.debrief": "/debrief          Показать сгенерированные debrief-отчеты",
        "help.test-cases": "/test-cases       Показать запланированные test cases",
        "help.runbook": "/runbook          Показать сгенерированные runbook",
        "help.lang": "/lang [ru|en]     Показать или сменить язык интерфейса",
        "help.model": "/model           Выбрать модель по умолчанию и уровень рассуждений",
        "help.exit": "/exit             Закрыть Samurai AI",
        "help.ctrl-o": "Ctrl-O            Переключить журнал событий",
        "help.ctrl-c": "Ctrl-C            Закрыть Samurai AI",
        "error.unknown-command": "Неизвестная команда: {name}. Используйте /help.",
        "info.ai-disabled": "AI-оркестрация пока не включена. Используйте /help.",
        "status.no-operation": "Операция не загружена",
        "scope.includes": "включения: {rules}",
        "scope.excludes": "исключения: {rules}",
        "scope.none": "(нет)",
        "plan.stage": "этап={stage}; цель={objective}",
        "plan.objective-unset": "(цель не задана)",
        "plan.no-plan": "Нет текущего плана. Создайте его: samurai plan create",
        "plan.approved": "утверждён",
        "plan.not-approved": "не утверждён",
        "tasks.empty": "Нет задач текущего плана",
        "gates.empty": "Нет гейтов текущего плана",
        "findings.empty": "Нет находок",
        "evidence.empty": "Нет артефактов",
        "approvals.empty": "Нет согласований",
        "snapshots.empty": "Нет снимков",
        "tools.empty": "Нет зарегистрированных инструментов",
        "tools.network": "сеть",
        "tools.no-network": "без-сети",
        "runs.empty": "Нет запусков инструментов",
        "agents.empty": "Нет запусков агентов",
        "swarm.empty": "Нет swarm-запусков агентов",
        "memory.empty": "Нет записей campaign memory",
        "skills.empty": "Нет установленных skills",
        "playbooks.custom-empty": "Нет установленных custom playbooks",
        "assets.empty": "Нет записанных активов",
        "report.empty": "Нет сгенерированных отчетов",
        "hypotheses.empty": "Нет записанных гипотез",
        "flags.empty": "Нет захваченных флагов",
        "attack-path.empty": "Нет запланированных шагов атаки",
        "debrief.empty": "Нет сгенерированных debrief-отчетов",
        "test-cases.empty": "Нет запланированных test cases",
        "runbook.empty": "Нет сгенерированных runbook",
        "secrets.empty": "Нет секретов в этой области",
        "secrets.global": "глобальный",
        "header.with-operation": (
            "SAMURAI AI  {operation_id}  {mode}  ОБЛАСТЬ: OK  МОДЕЛЬ: НЕ НАСТРОЕНА"
        ),
        "header.no-operation": "SAMURAI AI  НЕТ ОПЕРАЦИИ  ОБЛАСТЬ: НЕ ЗАДАНА",
        "statusbar.with-operation": (
            "{status} | ЗАДАЧИ {tasks_done}/{tasks_total} | ГЕЙТЫ {gates_passed}/"
            "{gates_total} | ОЖИДАЮТ {pending} | СОГЛАСОВАНИЕ: СПРОСИТЬ | /help"
        ),
        "statusbar.idle": "ОЖИДАНИЕ | СОГЛАСОВАНИЕ: СПРОСИТЬ | /help",
        "lang.current": "Текущий язык: {locale} (доступно: ru, en)",
        "lang.changed": "Язык изменён на {locale}",
        "lang.invalid": "Неподдерживаемый язык: {locale}. Используйте ru или en.",
        "chooser.title": "Choose your language / Выберите язык",
        "chooser.english": "English",
        "chooser.russian": "Русский",
        "model.picker-title": "Выбор модели и уровня рассуждений",
        "model.picker-models": "Модель (из реестра и API провайдеров)",
        "model.picker-reasoning": "Уровень рассуждений",
        "model.picker-hint": (
            "Стрелки — перемещение, Tab — другая колонка, "
            "Enter — применить, Esc — отмена"
        ),
        "model.selected": "модель по умолчанию: {model} | рассуждения: {reasoning}",
        "model.empty": "Нет доступных моделей; проверьте health провайдеров",
        "model.picker-add-custom": "+ Добавить свой OpenAI-совместимый endpoint…",
        "model.custom-title": "Добавление своего OpenAI-совместимого endpoint",
        "model.custom-id": "id endpoint'а (строчными, например giga)",
        "model.custom-url": "base URL (например https://api.example.com/v1)",
        "model.custom-key": "API-ключ (необязательно; хранится в зашифрованном vault)",
        "model.custom-tls": "Проверка TLS-сертификата",
        "model.custom-tls-on": "вкл (рекомендуется)",
        "model.custom-tls-off": "выкл — принимать любой сертификат (MITM-руты)",
        "model.custom-fetch": "Подключиться и получить модели",
        "model.custom-fetching": "подключение и получение списка моделей…",
        "model.custom-pick": (
            "Выберите модель стрелками и Enter — или введите имя ниже"
        ),
        "model.custom-no-models": (
            "endpoint зарегистрирован, но модели не получены; "
            "введите имя модели ниже и нажмите Enter"
        ),
        "model.custom-manual": "или введите имя модели и нажмите Enter",
        "model.custom-invalid": "нужны endpoint id и base URL",
        "model.custom-failed": "ошибка: {error}",
        "reconcile.report": (
            "отмечено-unknown={actions} | tool-runs-failed={runs} | "
            "возобновимые-задачи={tasks} | ожидающие-согласования={approvals}"
        ),
        "list.none": "(нет)",
    },
}

_HELP_KEYS: tuple[str, ...] = (
    "help.help",
    "help.status",
    "help.scope",
    "help.plan",
    "help.tasks",
    "help.gates",
    "help.findings",
    "help.evidence",
    "help.approvals",
    "help.snapshots",
    "help.reconcile",
    "help.tools",
    "help.runs",
    "help.agents",
    "help.swarm",
    "help.memory",
    "help.skills",
    "help.playbooks",
    "help.secrets",
    "help.assets",
    "help.report",
    "help.hypotheses",
    "help.flags",
    "help.attack-path",
    "help.debrief",
    "help.test-cases",
    "help.runbook",
    "help.lang",
    "help.model",
    "help.exit",
    "help.ctrl-o",
    "help.ctrl-c",
)


class Translator:
    def __init__(self, locale: str = DEFAULT_LOCALE) -> None:
        self.locale = locale if locale in _TRANSLATIONS else DEFAULT_LOCALE

    def set_locale(self, locale: str) -> None:
        if locale in _TRANSLATIONS:
            self.locale = locale

    def t(self, key: str, **parameters: object) -> str:
        template = _TRANSLATIONS[self.locale].get(key) or _TRANSLATIONS[DEFAULT_LOCALE].get(
            key, key
        )
        if parameters:
            return template.format(**parameters)
        return template

    def help_lines(self) -> list[str]:
        return [self.t(key) for key in _HELP_KEYS]
