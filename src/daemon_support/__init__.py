"""Shared scaffolding for the project's continuous-polling daemons.

See base_daemon.py - extracted from battery_mode_daemon.py and
hotwater_mode_daemon.py, which independently reimplemented the same
fast-config-reload / slow-scheduled-checks loop, rotating-log setup, and
signal handling.
"""
