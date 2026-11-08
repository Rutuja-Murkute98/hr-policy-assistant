"""Shared by all tests. A test starts from the default sign-in mode, whatever this PC's .env file says."""
import os

os.environ["AUTH_MODE"] = "demo"  # before anything imports the app's settings, which would read .env
