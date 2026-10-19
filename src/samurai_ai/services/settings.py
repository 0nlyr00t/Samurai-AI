import os

from samurai_ai.storage.recovery import RecoveryRepository

LOCALES: tuple[str, ...] = ("ru", "en")
DEFAULT_LOCALE = "en"
_LOCALE_SETTING_KEY = "locale"


class InvalidLocaleError(ValueError):
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
