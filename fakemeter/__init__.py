"""fakemeter — a BLE peripheral emulator that impersonates Bluetooth multimeters.

A hardware-free black-box oracle for verifying BLE decode logic: we advertise a
GATT profile that looks exactly like a real meter, then push crafted frames on the
notify characteristic. A human reads how the vendor's phone app (or our own
Web-Bluetooth web app) decodes and displays them.

The package is intentionally instance-based — no global singletons — so two
independent emulators can run on two adapters (``--adapter hci0`` /
``--adapter hci1``) at once.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
