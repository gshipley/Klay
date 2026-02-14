# Contributing to Klay

## Development Build

```bash
meson setup _build
meson compile -C _build
```

Install the Qt runtime for local runs:

```bash
sudo dnf install python3-pyside6
```

## Translations

Translation files are under `po/` and the gettext domain is `klay`.

## Notes

Klay is a standalone fork in `Klay/` and uses the `klay` module namespace.
