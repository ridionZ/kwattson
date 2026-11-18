#!/bin/sh
# Exercise a container upgrade and a second restart using only synthetic records.
# Usage: tools/check-image-upgrade.sh PREVIOUS_IMAGE CANDIDATE_IMAGE
set -eu
previous=${1:?Supply the previous image}
candidate=${2:?Supply the candidate image}
volume=$(docker volume create)
trap 'docker volume rm "$volume" >/dev/null' EXIT

docker run --rm -i --network none -v "$volume:/data" --entrypoint python "$previous" - <<'PY'
from datetime import datetime, timezone

from kwattson.ingest import store_vehicle
from kwattson.plugins.base import VehicleReading
from kwattson.web.app import create_app

app = create_app().state
app.security.create_owner('upgradeowner', 'synthetic-upgrade-passphrase')
app.security.save_secret('synthetic:device', 'test-credential')
settings = app.db.settings()
settings.site.name = 'Upgrade fixture'
settings.site.poll_seconds = 300
app.db.save_settings(settings, 'fixture')
reading = VehicleReading(ts=datetime.now(timezone.utc), time_verified=True, soc=37)
store_vehicle(app.db, 'fixture', 'manual', reading)
PY

for attempt in 1 2; do
    docker run --rm -i --network none -v "$volume:/data" -e INITIAL_ADMIN_PASSWORD=another-synthetic-passphrase \
        --entrypoint python "$candidate" - <<'PY'
from kwattson.security import PASSWORDS
from kwattson.web.app import create_app

app = create_app().state
user = app.db.one('SELECT * FROM users')
assert user['username'] == 'upgradeowner'
assert PASSWORDS.verify(user['password_hash'], 'synthetic-upgrade-passphrase')
assert app.db.one('SELECT COUNT(*) AS n FROM users')['n'] == 1
settings = app.db.settings()
assert settings.site.name == 'Upgrade fixture' and settings.site.poll_seconds == 300
assert app.security.secret('synthetic:device') == 'test-credential'
assert app.db.latest_vehicle('fixture')['soc'] == 37
assert app.db.one('PRAGMA integrity_check')['integrity_check'] == 'ok'
print('Owner password, settings, encrypted credential, reading and SQLite integrity verified.')
PY
done
