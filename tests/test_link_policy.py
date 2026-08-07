from PySide6.QtCore import Qt

from lumen_reader.ui import control_link_activation_allowed


def test_only_control_modifier_authorizes_link_activation() -> None:
    assert not control_link_activation_allowed(Qt.KeyboardModifier.NoModifier)
    assert not control_link_activation_allowed(Qt.KeyboardModifier.ShiftModifier)
    assert not control_link_activation_allowed(Qt.KeyboardModifier.AltModifier)
    assert control_link_activation_allowed(Qt.KeyboardModifier.ControlModifier)
    assert control_link_activation_allowed(
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )
