🎯 QT NATIVE UI - PRIMARY INTERFACE
====================================

This directory contains the Qt NATIVE DESKTOP interface for Viola.

⚠️ CRITICAL:
  This is the PRIMARY interface that most users use!
  When fixing bugs, fix HERE FIRST!

SECONDARY interface:
  Web browser interface is in ui/static/ (alternative access only)

When fixing bugs:
  1. Ask user which interface they're using
  2. Default assumption: Qt (this directory) - most users use it
  3. If web: Fix ui/static/ instead

These are SEPARATE codebases:
  ❌ Fixing web UI (ui/static/) does NOT fix this Qt UI
  ❌ They use different files and technologies
  ✅ Backend (gpt_handler.py, routing/) is shared

Files in this directory:
  - window.py - Main Qt window
  - widgets/settings_widget.py - Qt settings dialog
  - widgets/chat_widget.py - Qt chat interface
  - widgets/music_widget.py - Qt music controls
  - styles/ - Qt stylesheets

Run Qt application:
  python viola_qt.py

Test Qt interface:
  pytest tests/ui/test_qt_ui_automation.py -v

Status:
  ⚠️ Tests are BASIC (only 5 tests)
  ⚠️ Needs comprehensive test suite like web has

See UI_ARCHITECTURE_EXPLAINED.md for full details.
