from __future__ import annotations

import array
import os
from pathlib import Path
import struct
import sys

from PySide6.QtCore import QObject, QSocketNotifier, QTimer, Qt, Signal

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows
    fcntl = None  # type: ignore[assignment]


ACTION_UP = "up"
ACTION_DOWN = "down"
ACTION_LEFT = "left"
ACTION_RIGHT = "right"
ACTION_ACTIVATE = "activate"
ACTION_BACK = "back"
ACTION_MENU = "menu"

DIRECTION_ACTIONS = frozenset(
    {ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT}
)

_JS_EVENT_BUTTON = 0x01
_JS_EVENT_AXIS = 0x02
_JS_EVENT_INIT = 0x80
_JS_EVENT = struct.Struct("IhBB")

_ABS_X = 0x00
_ABS_Y = 0x01
_ABS_HAT0X = 0x10
_ABS_HAT0Y = 0x11

_BTN_SOUTH = 0x130
_BTN_EAST = 0x131
_BTN_DPAD_UP = 0x220
_BTN_DPAD_DOWN = 0x221
_BTN_DPAD_LEFT = 0x222
_BTN_DPAD_RIGHT = 0x223
_BTN_SELECT = 0x13A
_BTN_START = 0x13B
_BTN_MODE = 0x13C

_BUTTON_ACTIONS = {
    _BTN_SOUTH: ACTION_ACTIVATE,
    _BTN_EAST: ACTION_BACK,
    _BTN_DPAD_UP: ACTION_UP,
    _BTN_DPAD_DOWN: ACTION_DOWN,
    _BTN_DPAD_LEFT: ACTION_LEFT,
    _BTN_DPAD_RIGHT: ACTION_RIGHT,
    _BTN_START: ACTION_MENU,
    _BTN_MODE: ACTION_MENU,
}

# Linux's joystick API normally supplies the evdev mappings through ioctl.
# These conventional positions keep common controllers usable if a driver does
# not expose those maps.
_BUTTON_INDEX_FALLBACK = {
    0: _BTN_SOUTH,
    1: _BTN_EAST,
    6: _BTN_SELECT,
    7: _BTN_START,
    8: _BTN_MODE,
}
_AXIS_INDEX_FALLBACK = {
    0: _ABS_X,
    1: _ABS_Y,
    6: _ABS_HAT0X,
    7: _ABS_HAT0Y,
}

_AXIS_ENGAGE = 0.55
_AXIS_RELEASE = 0.35
_INITIAL_REPEAT_MS = 350
_REPEAT_MS = 115
_SCAN_INTERVAL_MS = 1500


def _ioc_read(number: int, size: int) -> int:
    """Build the Linux _IOR ioctl used by the legacy joystick interface."""

    return (2 << 30) | (size << 16) | (ord("j") << 8) | number


class _JoystickDescription:
    def __init__(
        self,
        path: str,
        name: str,
        axis_codes: tuple[int, ...],
        button_codes: tuple[int, ...],
        *,
        axis_mapping_available: bool = True,
        button_mapping_available: bool = True,
    ) -> None:
        self.path = path
        self.name = name
        self.axis_codes = axis_codes
        self.button_codes = button_codes
        self.axis_mapping_available = axis_mapping_available
        self.button_mapping_available = button_mapping_available

    @property
    def is_controller(self) -> bool:
        mapped_buttons = set(self.button_codes)
        if mapped_buttons.intersection(_BUTTON_ACTIONS):
            return True

        lowered_name = self.name.casefold()
        name_suggests_controller = any(
            token in lowered_name
            for token in (
                "controller",
                "gamepad",
                "joystick",
                "xbox",
                "dualshock",
                "dualsense",
            )
        )
        return (
            name_suggests_controller
            and len(self.axis_codes) >= 2
            and len(self.button_codes) >= 2
        )


def _read_joystick_description(fd: int, path: str) -> _JoystickDescription:
    if fcntl is None:
        raise OSError("Linux joystick support is unavailable")

    name_buffer = bytearray(128)
    fcntl.ioctl(fd, _ioc_read(0x13, len(name_buffer)), name_buffer, True)
    name = bytes(name_buffer).split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if not name:
        name = Path(path).name

    axis_count = bytearray(1)
    button_count = bytearray(1)
    fcntl.ioctl(fd, _ioc_read(0x11, 1), axis_count, True)
    fcntl.ioctl(fd, _ioc_read(0x12, 1), button_count, True)

    axis_map = bytearray(0x40)
    axis_mapping_available = True
    try:
        fcntl.ioctl(fd, _ioc_read(0x32, len(axis_map)), axis_map, True)
    except OSError:
        axis_mapping_available = False

    # KEY_MAX - BTN_MISC + 1 Linux input codes, stored as unsigned shorts.
    button_map = array.array("H", [0] * (0x2FF - 0x100 + 1))
    button_mapping_available = True
    try:
        fcntl.ioctl(
            fd,
            _ioc_read(0x34, button_map.itemsize * len(button_map)),
            button_map,
            True,
        )
    except OSError:
        button_mapping_available = False

    return _JoystickDescription(
        path,
        name,
        tuple(int(code) for code in axis_map[: axis_count[0]]),
        tuple(int(code) for code in button_map[: button_count[0]]),
        axis_mapping_available=axis_mapping_available,
        button_mapping_available=button_mapping_available,
    )


def _direction_for_axes(
    x_value: float,
    y_value: float,
    previous: str | None,
) -> str | None:
    """Choose one dominant direction with deadzone hysteresis."""

    if previous in DIRECTION_ACTIONS:
        previous_value = {
            ACTION_LEFT: -x_value,
            ACTION_RIGHT: x_value,
            ACTION_UP: -y_value,
            ACTION_DOWN: y_value,
        }[previous]
        other_value = (
            abs(y_value)
            if previous in {ACTION_LEFT, ACTION_RIGHT}
            else abs(x_value)
        )
        if previous_value >= _AXIS_RELEASE and other_value <= previous_value * 1.2:
            return previous

    x_strength = abs(x_value)
    y_strength = abs(y_value)
    if max(x_strength, y_strength) < _AXIS_ENGAGE:
        return None
    if x_strength > y_strength:
        return ACTION_LEFT if x_value < 0 else ACTION_RIGHT
    return ACTION_UP if y_value < 0 else ACTION_DOWN


class _JoystickDevice(QObject):
    action_requested = Signal(str)
    direction_changed = Signal(str, bool)
    disconnected = Signal(str)

    def __init__(
        self,
        fd: int,
        description: _JoystickDescription,
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self.fd = fd
        self.description = description
        self._read_buffer = bytearray()
        self._pressed_buttons: set[int] = set()
        self._axis_values: dict[int, float] = {}
        self._source_directions: dict[str, str] = {}
        self._direction_sources: dict[str, set[str]] = {
            action: set() for action in DIRECTION_ACTIONS
        }
        self._axis_evaluation_pending = False

        self.notifier = QSocketNotifier(fd, QSocketNotifier.Type.Read, self)
        self.notifier.activated.connect(self._read_available)

    @property
    def name(self) -> str:
        return self.description.name

    def close(self) -> None:
        if self.fd < 0:
            return
        self.notifier.setEnabled(False)
        self.reset_state()
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = -1

    def reset_state(self) -> None:
        self._pressed_buttons.clear()
        self._axis_values.clear()
        self._axis_evaluation_pending = False
        self._release_all_directions()

    def _release_all_directions(self) -> None:
        for source in tuple(self._source_directions):
            self._set_source_direction(source, None)

    def _disconnect(self) -> None:
        path = self.description.path
        self.close()
        self.disconnected.emit(path)

    def _read_available(self, *_args: object) -> None:
        if self.fd < 0:
            return
        while True:
            try:
                chunk = os.read(self.fd, _JS_EVENT.size * 64)
            except BlockingIOError:
                break
            except OSError:
                self._disconnect()
                return
            if not chunk:
                self._disconnect()
                return
            self._read_buffer.extend(chunk)

        while len(self._read_buffer) >= _JS_EVENT.size:
            event_data = self._read_buffer[: _JS_EVENT.size]
            del self._read_buffer[: _JS_EVENT.size]
            _timestamp, value, event_type, number = _JS_EVENT.unpack(event_data)
            initial = bool(event_type & _JS_EVENT_INIT)
            event_type &= ~_JS_EVENT_INIT
            if event_type == _JS_EVENT_BUTTON:
                self._handle_button(number, value != 0, initial)
            elif event_type == _JS_EVENT_AXIS:
                self._handle_axis(number, value, initial)

    def _button_code(self, number: int) -> int | None:
        if (
            self.description.button_mapping_available
            and number < len(self.description.button_codes)
        ):
            code = self.description.button_codes[number]
            if code in _BUTTON_ACTIONS:
                return code
            return _BUTTON_INDEX_FALLBACK.get(number, code or None)
        return _BUTTON_INDEX_FALLBACK.get(number)

    def _axis_code(self, number: int) -> int | None:
        if (
            self.description.axis_mapping_available
            and number < len(self.description.axis_codes)
        ):
            return self.description.axis_codes[number]
        return _AXIS_INDEX_FALLBACK.get(number)

    def _handle_button(self, number: int, pressed: bool, initial: bool) -> None:
        was_pressed = number in self._pressed_buttons
        if pressed:
            self._pressed_buttons.add(number)
        else:
            self._pressed_buttons.discard(number)
        if initial or was_pressed == pressed:
            return

        action = _BUTTON_ACTIONS.get(self._button_code(number))
        if action is None:
            return
        if action in DIRECTION_ACTIONS:
            self._set_source_direction(
                f"button:{number}", action if pressed else None
            )
        elif pressed:
            self.action_requested.emit(action)

    def _handle_axis(self, number: int, value: int, initial: bool) -> None:
        code = self._axis_code(number)
        if code not in {_ABS_X, _ABS_Y, _ABS_HAT0X, _ABS_HAT0Y}:
            return
        self._axis_values[code] = max(-1.0, min(1.0, value / 32767.0))
        if initial or self._axis_evaluation_pending:
            return
        self._axis_evaluation_pending = True
        QTimer.singleShot(0, self._evaluate_axes)

    def _evaluate_axes(self) -> None:
        self._axis_evaluation_pending = False
        if self.fd < 0:
            return
        for source, x_code, y_code in (
            ("left-stick", _ABS_X, _ABS_Y),
            ("dpad", _ABS_HAT0X, _ABS_HAT0Y),
        ):
            previous = self._source_directions.get(source)
            direction = _direction_for_axes(
                self._axis_values.get(x_code, 0.0),
                self._axis_values.get(y_code, 0.0),
                previous,
            )
            self._set_source_direction(source, direction)

    def _set_source_direction(self, source: str, direction: str | None) -> None:
        previous = self._source_directions.get(source)
        if previous == direction:
            return

        if previous is not None:
            previous_sources = self._direction_sources[previous]
            previous_sources.discard(source)
            if not previous_sources:
                self.direction_changed.emit(previous, False)
            self._source_directions.pop(source, None)

        if direction is not None:
            direction_sources = self._direction_sources[direction]
            was_inactive = not direction_sources
            direction_sources.add(source)
            self._source_directions[source] = direction
            if was_inactive:
                self.direction_changed.emit(direction, True)


class ControllerManager(QObject):
    """Discover Linux gamepads and emit repeat-filtered semantic actions."""

    action = Signal(str)
    controllers_changed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._devices: dict[str, _JoystickDevice] = {}
        self._last_names: tuple[str, ...] = ()
        self._requested_enabled = False
        self._application_active = True
        self._input_enabled = False
        self._held_sources: dict[str, set[str]] = {
            action: set() for action in DIRECTION_ACTIONS
        }
        self._direction_order: list[str] = []
        self._active_direction: str | None = None
        self._direction_reconcile_pending = False

        self._scan_timer = QTimer(self)
        self._scan_timer.setInterval(_SCAN_INTERVAL_MS)
        self._scan_timer.timeout.connect(self._scan_devices)

        self._repeat_timer = QTimer(self)
        self._repeat_timer.setSingleShot(True)
        self._repeat_timer.timeout.connect(self._repeat_direction)

        from PySide6.QtWidgets import QApplication

        application = QApplication.instance()
        if application is not None:
            self._application_active = (
                application.applicationState() == Qt.ApplicationState.ApplicationActive
            )
            application.applicationStateChanged.connect(self._on_application_state_changed)

    @property
    def controller_names(self) -> tuple[str, ...]:
        return tuple(sorted(device.name for device in self._devices.values()))

    def start(self) -> None:
        if not self._scan_timer.isActive():
            self._scan_timer.start()
        self._scan_devices()

    def stop(self) -> None:
        self._scan_timer.stop()
        self.reset_input()
        for path in tuple(self._devices):
            self._detach_device(path)

    def set_enabled(self, enabled: bool) -> None:
        self._requested_enabled = bool(enabled)
        self._refresh_enabled_state()

    def reset_input(self) -> None:
        self._clear_repeat_state()
        for device in self._devices.values():
            device.reset_state()

    def _on_application_state_changed(self, state: Qt.ApplicationState) -> None:
        self._application_active = state == Qt.ApplicationState.ApplicationActive
        self._refresh_enabled_state()

    def _refresh_enabled_state(self) -> None:
        enabled = self._requested_enabled and self._application_active
        if self._input_enabled == enabled:
            return
        self._input_enabled = enabled
        self.reset_input()

    def _scan_devices(self) -> None:
        if sys.platform != "linux" or fcntl is None:
            return

        paths = {str(path) for path in Path("/dev/input").glob("js*")}
        for stale_path in set(self._devices).difference(paths):
            self._detach_device(stale_path)

        for path in sorted(paths.difference(self._devices)):
            fd = -1
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                description = _read_joystick_description(fd, path)
            except OSError:
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                continue

            if not description.is_controller:
                os.close(fd)
                continue

            device = _JoystickDevice(fd, description, self)
            device.action_requested.connect(self._on_action_requested)
            device.direction_changed.connect(
                lambda action, pressed, device_path=path: self._on_direction_changed(
                    device_path, action, pressed
                )
            )
            device.disconnected.connect(self._on_device_disconnected)
            self._devices[path] = device

        self._emit_controllers_changed()

    def _detach_device(self, path: str) -> None:
        device = self._devices.pop(path, None)
        if device is None:
            return
        device.close()
        device.deleteLater()
        self._remove_direction_source(path)
        self._emit_controllers_changed()

    def _on_device_disconnected(self, path: str) -> None:
        device = self._devices.pop(path, None)
        if device is not None:
            device.deleteLater()
        self._remove_direction_source(path)
        self._emit_controllers_changed()

    def _emit_controllers_changed(self) -> None:
        names = self.controller_names
        if names == self._last_names:
            return
        self._last_names = names
        self.controllers_changed.emit(names)

    def _on_action_requested(self, action: str) -> None:
        if self._input_enabled:
            self.action.emit(action)

    def _on_direction_changed(self, source: str, action: str, pressed: bool) -> None:
        sources = self._held_sources[action]
        if pressed:
            if source not in self._devices:
                return
            if source in sources:
                return
            was_inactive = not sources
            sources.add(source)
            if not was_inactive:
                return
            if action in self._direction_order:
                self._direction_order.remove(action)
            self._direction_order.append(action)
            if self._input_enabled:
                self._queue_direction_reconcile()
            return

        sources.discard(source)
        if sources or action not in self._direction_order:
            return
        self._direction_order.remove(action)
        if self._input_enabled:
            self._queue_direction_reconcile()

    def _remove_direction_source(self, source: str) -> None:
        for action in tuple(DIRECTION_ACTIONS):
            self._on_direction_changed(source, action, False)

    def _queue_direction_reconcile(self) -> None:
        if self._direction_reconcile_pending:
            return
        self._direction_reconcile_pending = True
        QTimer.singleShot(0, self._reconcile_direction)

    def _reconcile_direction(self) -> None:
        self._direction_reconcile_pending = False
        desired = (
            self._direction_order[-1]
            if self._input_enabled and self._direction_order
            else None
        )
        if desired == self._active_direction:
            return
        if desired is None:
            self._active_direction = None
            self._repeat_timer.stop()
            return
        self._activate_direction(desired)

    def _activate_direction(self, action: str) -> None:
        self._active_direction = action
        self.action.emit(action)
        self._repeat_timer.start(_INITIAL_REPEAT_MS)

    def _repeat_direction(self) -> None:
        action = self._active_direction
        if (
            not self._input_enabled
            or action is None
            or not self._held_sources[action]
        ):
            self._repeat_timer.stop()
            return
        self.action.emit(action)
        self._repeat_timer.start(_REPEAT_MS)

    def _clear_repeat_state(self) -> None:
        self._repeat_timer.stop()
        self._active_direction = None
        self._direction_reconcile_pending = False
        self._direction_order.clear()
        for sources in self._held_sources.values():
            sources.clear()
