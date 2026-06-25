#!/bin/bash
source venv/bin/activate
watchmedo auto-restart --directory=./ --pattern="*.py" --recursive -- python music_bot.py
