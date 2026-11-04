# Пример плагина

`demo-webscan/` — минимальный, но настоящий пакет: инструмент `demo.webscan`
(манифест на закреплённом образе nuclei), профиль `demo.webscan@v1` и плейбук
`demo.recon-then-scan`.

```bash
samurai plugin install examples/plugins/demo-webscan --trusted
samurai plugin show demo
samurai plugin check demo
samurai plugin rm demo
```

Что видно на нём:

- имя инструмента обязано начинаться с имени плагина (`demo.`);
- профиль может нести только свои лимиты, образ берётся по digest;
- каждый файл перечислен с sha256, правка после установки видна как `tampered`;
- `rm` забирает и инструмент, и профиль, и плейбук.

Свой пакет: скопируйте каталог, поменяйте `name`, соберите `sha256` каждого файла
(`shasum -a 256 <файл>`) и перечислите их в `plugin.json`. Валидатор не пропустит
лишний файл, чужой префикс и ослабленный профиль.
