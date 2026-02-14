# Klay Extensions

Klay can load extension modules that add import sources without changing core
files.

Extension discovery:

- Directory: `~/.config/klay/extensions/*.py`
- Environment variable: `KLAY_EXTENSIONS_PATH`

`KLAY_EXTENSIONS_PATH` supports multiple paths separated by `:` on Linux.
Each path can be a Python file or a directory containing Python files.

## Extension Contract

An extension module can expose either:

- `SOURCE_CLASSES = [MySourceClass, ...]`
- `def get_source_classes() -> iterable[type[Source]]`

Source classes must inherit from `klay.importer.source.Source`.

```python
from klay.importer.source import Source, SourceIterable


class MySourceIterable(SourceIterable):
    def __iter__(self):
        return
        yield


class MySource(Source):
    source_id = "my_source"
    name = "My Source"
    available_on = {"linux"}
    iterable_class = MySourceIterable
    locations = ()


SOURCE_CLASSES = [MySource]
```
