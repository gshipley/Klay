# extensions.py
#
# Copyright 2026
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType
from typing import Iterable

from klay import shared
from klay.importer.source import Source


def extension_paths() -> list[Path]:
    env_var = f"{shared.BINARY_NAME.upper()}_EXTENSIONS_PATH"
    raw_paths = os.getenv(env_var) or os.getenv("CARTRIDGES_EXTENSIONS_PATH", "")

    paths: list[Path] = []
    if raw_paths:
        for raw_path in raw_paths.split(os.pathsep):
            if raw_path:
                paths.append(Path(raw_path).expanduser())

    default_dir = shared.config_dir / shared.DATA_DIR_NAME / "extensions"
    if default_dir.is_dir():
        paths.append(default_dir)

    seen: set[Path] = set()
    resolved_paths: list[Path] = []
    for path in paths:
        if path.is_dir():
            candidates = sorted(
                sub_path
                for sub_path in path.iterdir()
                if sub_path.is_file()
                and sub_path.suffix == ".py"
                and not sub_path.name.startswith("_")
            )
        else:
            candidates = [path]

        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate in seen:
                continue
            seen.add(candidate)
            resolved_paths.append(candidate)

    return resolved_paths


def load_extension_modules() -> list[ModuleType]:
    modules: list[ModuleType] = []

    for extension_path in extension_paths():
        if not extension_path.is_file():
            continue

        try:
            digest = hashlib.sha1(str(extension_path).encode("utf-8")).hexdigest()
            module_name = f"{shared.BINARY_NAME}_extension_{digest}"
            spec = importlib.util.spec_from_file_location(module_name, extension_path)
            if not spec or not spec.loader:
                logging.warning("Failed to load extension spec: %s", extension_path)
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules.append(module)
            logging.debug("Loaded extension: %s", extension_path)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.exception("Failed to load extension module: %s", extension_path)

    return modules


def collect_source_classes(modules: Iterable[ModuleType]) -> tuple[type[Source], ...]:
    source_classes: list[type[Source]] = []

    for module in modules:
        module_source_classes = getattr(module, "SOURCE_CLASSES", ())

        if get_source_classes := getattr(module, "get_source_classes", None):
            try:
                module_source_classes = tuple(get_source_classes())
            except Exception:  # pylint: disable=broad-exception-caught
                logging.exception("Extension get_source_classes() failed: %s", module)
                continue

        for source_class in module_source_classes:
            if not isinstance(source_class, type) or not issubclass(source_class, Source):
                logging.warning(
                    "Extension source must subclass Source. Got %s in %s.",
                    source_class,
                    module,
                )
                continue
            source_classes.append(source_class)

    return tuple(source_classes)
