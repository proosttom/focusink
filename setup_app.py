"""py2app build for FocusInk.app — run ./build_app.sh (no Xcode tools needed)."""
from setuptools import setup

setup(
    name="FocusInk",
    app=["focus_app.py"],
    py_modules=["focus"],
    options={
        "py2app": {
            "iconfile": "icon/FocusInk.icns",
            "packages": ["bleak", "rumps"],
            "includes": ["ServiceManagement", "AppKit", "Foundation", "CoreBluetooth", "libdispatch"],
            "plist": {
                "CFBundleName": "FocusInk",
                "CFBundleDisplayName": "FocusInk",
                "CFBundleIdentifier": "local.focusink.menubar",
                "CFBundleShortVersionString": "1.0",
                "LSUIElement": True,
                "NSBluetoothAlwaysUsageDescription": "FocusInk connects to your e-paper focus display.",
            },
        }
    },
)
