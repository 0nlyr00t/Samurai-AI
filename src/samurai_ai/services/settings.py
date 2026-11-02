import json
import os

from samurai_ai.domain.permission import (
    MAX_RULES,
    PermissionRule,
    PermissionRuleError,
)
from samurai_ai.storage.recovery import RecoveryRepository

LOCALES: tuple[str, ...] = ("ru", "en")
DEFAULT_LOCALE = "en"
_LOCALE_SETTING_KEY = "locale"
_RULES_SETTING_KEY = "permission.rules"


class InvalidLocaleError(ValueError):
    pass


class TooManyRulesError(ValueError):
    pass


class SettingsService:
    def __init__(self, repository: RecoveryRepository) -> None:
        self.repository = repository

    def stored_locale(self) -> str | None:
        value = self.repository.get_setting(_LOCALE_SETTING_KEY)
        return value if value in LOCALES else None

    def get_locale(self) -> str | None:
        stored = self.stored_locale()
        if stored is not None:
            return stored
        env_locale = os.environ.get("SAMURAI_LANG", "").strip().lower()
        return env_locale if env_locale in LOCALES else None

    def effective_locale(self) -> str:
        return self.get_locale() or DEFAULT_LOCALE

    def set_locale(self, locale: str) -> str:
        normalized = locale.strip().lower()
        if normalized not in LOCALES:
            raise InvalidLocaleError(f"unsupported locale: {locale!r}")
        self.repository.set_setting(_LOCALE_SETTING_KEY, normalized)
        return normalized

    def get_value(self, key: str) -> str | None:
        return self.repository.get_setting(key)

    def set_value(self, key: str, value: str) -> None:
        self.repository.set_setting(key, value)

    def permission_rules(self) -> tuple[PermissionRule, ...]:
        raw = self.repository.get_setting(_RULES_SETTING_KEY)
        if not raw:
            return ()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return ()
        rules: list[PermissionRule] = []
        for item in parsed if isinstance(parsed, list) else ():
            try:
                rules.append(PermissionRule.parse(str(item)))
            except PermissionRuleError:
                continue  # a broken line never takes the others down
        return tuple(rules)

    def add_permission_rule(self, text: str) -> PermissionRule:
        rule = PermissionRule.parse(text)
        rules = list(self.permission_rules())
        if rule in rules:
            return rule
        if len(rules) >= MAX_RULES:
            raise TooManyRulesError(f"at most {MAX_RULES} rules")
        rules.append(rule)
        self._store_rules(rules)
        return rule

    def remove_permission_rule(self, text: str) -> bool:
        rule = PermissionRule.parse(text)
        rules = list(self.permission_rules())
        if rule not in rules:
            return False
        rules.remove(rule)
        self._store_rules(rules)
        return True

    def _store_rules(self, rules: list[PermissionRule]) -> None:
        self.set_value(_RULES_SETTING_KEY, json.dumps([rule.format() for rule in rules]))
