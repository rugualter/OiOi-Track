#!/bin/sh

set -e

python manage.py migrate --noinput
python manage.py compilemessages


exec /usr/bin/supervisord -c /etc/supervisor/conf.d/supervisord.conf
