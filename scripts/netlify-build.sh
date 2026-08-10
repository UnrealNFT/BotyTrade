#!/usr/bin/env bash
set -e

echo "window.API_BASE = '${API_BASE_URL:-}';" > api-config.js
echo "Netlify API_BASE_URL injected: ${API_BASE_URL:-(empty)}"
