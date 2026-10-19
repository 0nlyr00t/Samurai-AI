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
        "help.secrets": "/secrets         Show secret metadata (values are never shown)",
        "help.lang": "/lang [ru|en]     Show or change the interface language",
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
        "help.secrets": "/secrets         Показать метаданные секретов (значения скрыты)",
        "help.lang": "/lang [ru|en]     Показать или сменить язык интерфейса",
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
    "help.secrets",
    "help.lang",
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
