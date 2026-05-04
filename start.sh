#!/bin/bash
set -e

uwsgi --ini /app/uwsgi.ini &

nginx -g "daemon off;"