"""Serve the recovery fixtures for the existing regression recordings."""

from pytest import MonkeyPatch
from test.demo_recovery import install

install(MonkeyPatch())
from harmonist.web import main

app = main.app
