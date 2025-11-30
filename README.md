# Sopel Grok Bot

[![License: GPL v3](https://img.shields.io/badge/License-GPL%20v3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)

A Sopel plugin that brings xAI's Grok directly into your IRC channels. The bot can answer questions, join conversations, or respond to direct commands using the Grok API.

## Features

- Real-time responses powered by Grok (including Grok-2 / Grok-beta)
- Works with addressed messages (`bot: hello`) or explicit commands
- Fully configurable via `sopel.cfg`
- Lightweight — just one Python file + config

## Requirements

- Python 3.8+
- Sopel ≥ 7.1
- `requests` library
- A valid Grok API key from https://x.ai/api

## Installation

```bash
# 1. Clone the repo
git clone https://github.com/lord3nd3r/sopel-grok-bot.git
cd sopel-grok-bot

# 2. Install dependencies
pip install sopel requests

# 3. Copy the plugin to your Sopel scripts folder
cp grok.py ~/.sopel/scripts/
#4. copy the text from the cfg into your sopel config and edit it as needed.
