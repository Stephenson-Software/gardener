# Sharing run history across devices: the hub

gardener keeps its run history in a local SQLite file on each device that
runs it. When more than one device tends the same fleet (a desktop and a
phone, say), each one's dashboard, `gardener status`, and spend figures see
only that device's runs.

The **hub** is optional. It is one more gardener process (`gardener hub
serve`). Every configured device pushes its runs to it, and it serves the
normal dashboard over the combined history, with a Device column, at a URL
you choose.

It **adds to local storage and replaces none of it**:

- With no hub configured, gardener behaves exactly as it always has.
- With a hub configured, each device still writes its local store
  **first**. The push is a copy made after that. If the hub is down or the
  device is offline, runs wait in the local outbox (`pushed_at IS NULL`)
  and go with the next push. A dispatch never fails, blocks, or loses its
  record because of the hub.
- Unset `GARDENER_HUB_URL` to go back to local-only. Nothing is lost,
  because the local store was never not being written.

The design and the alternatives it was chosen over are in RFC 0007 in the
maintainer's RFC repository.

## Setting up a hub

The hub needs the gardener package and nothing else (stdlib only). It
refuses to start without at least one operator credential, so it can
never serve the history anonymously.

```bash
# 1. One device token per device. Run anywhere; nothing is stored.
gardener hub token --device box
gardener hub token --device phone
#    Each prints the token (it goes on that device) and a
#    `name:sha256` entry (it goes on the hub).

# 2. An operator credential for the browser: hex sha256 of "user:password".
printf '%s' 'dan:a long passphrase' | sha256sum

# 3. Run it.
export GARDENER_HUB_DEVICE_TOKENS='box:<digest>,phone:<digest>'
export GARDENER_HUB_OPERATOR_BASIC_SHA256='<digest from step 2>'
gardener hub serve --host 0.0.0.0 --port 8765 --data-dir /var/lib/gardener-hub
```

### In a container

The repo's `Dockerfile` builds an image that runs only the hub:
`python:3.12-slim`, a non-root user, the store in the `/data` volume
(`/data/hub/gardener.sqlite3`), and a `HEALTHCHECK` on `/healthz`. Pass the
settings below as environment variables:

```bash
docker build -t gardener-hub .
docker run -d --name gardener-hub -p 127.0.0.1:8765:8765 -v gardener-hub-data:/data \
  -e GARDENER_HUB_DEVICE_TOKENS='box:<digest>' \
  -e GARDENER_HUB_OPERATOR_BASIC_SHA256='<digest>' gardener-hub
```

With no operator credential, the container exits with status 2 instead of
serving. Measured on a 2-vCPU Linode, it idled at about 16 MiB after twenty
dashboard polls, so a 64–128 MiB limit is plenty.

Back up the store with SQLite's online backup, not a file copy (the store
is in WAL mode):

```bash
docker exec gardener-hub python -c "import sqlite3; s=sqlite3.connect('/data/hub/gardener.sqlite3'); d=sqlite3.connect('/data/hub/backup.sqlite3'); s.backup(d)"
```

Put TLS in front of it (a reverse proxy) before exposing it beyond
localhost. Device tokens and the operator password travel in headers.

| Hub setting (environment) | Meaning |
|---|---|
| `GARDENER_HUB_DEVICE_TOKENS` | Comma-separated `device:sha256hex(token)`. Every row a token pushes is stored under that token's device name, so one device's token can't write rows that look like another's. |
| `GARDENER_HUB_OPERATOR_BASIC_SHA256` | Hex sha256 of `user:password`, for HTTP basic auth on the dashboard and API. |
| `GARDENER_HUB_USERAUTH_URL` | Optional. Base URL of a [UserAuth](https://github.com/Preponderous-Software/UserAuth) service. Enables a `/login` page. |
| `GARDENER_HUB_OPERATORS` | Required with `GARDENER_HUB_USERAUTH_URL`: comma-separated usernames allowed to sign in. UserAuth registration is open, so this list is the authorisation. |

Either operator mode is enough on its own, and both can be enabled
together. UserAuth sign-in stores the access token in an `HttpOnly;
Secure; SameSite=Strict` cookie. Each session is re-checked against
UserAuth's `/session/validate` at most once a minute, so signing out (or
revoking the session in UserAuth) takes effect within a minute. The
session lasts as long as UserAuth's access token does; there is no
refresh.

## Configuring a device

On each device, put this in `~/.local/state/gardener/hub.env` (or export
the same names; the environment wins):

```
GARDENER_HUB_URL=https://gardener.example.com
GARDENER_HUB_TOKEN=<this device's token from `gardener hub token`>
```

The hub shows the device under the name its token was minted for
(`gardener hub token --device phone`), and ignores the name the device
recorded locally. The local name is the hostname unless
`GARDENER_DEVICE_NAME` is set (the setting alert footers use, see
docs/ALERTING.md), and it can change over a device's life. Then push the
device's existing history once:

```bash
gardener hub sync
```

`gardener hub status` shows the device's settings and how many runs are
still queued, without contacting the hub. `sync` is safe to repeat. Every run carries a `run_uuid`, and the hub
ignores a uuid it already holds. After that, every recorded run pushes the
outbox automatically. On the dispatch path that push is bounded (5 s per
request, 20 s in total). A longer backlog is left for the next run or the
next `gardener hub sync`.

## What the hub shows

| Panel | On the hub |
|---|---|
| Latest session, failures, per-night history, Recent runs | Combined across every device, ordered by timestamp. Recent runs gains a Device column. |
| Garden view | The union of every device's garden and merge allow-list, as each device last pushed them. These lists are display-only on the hub: they are never sent back to a device, and each device's own lists stay its own. |
| Currently tending, overnight progress bars, live log, `/live` | Not available: they read a device's own log and session files. The page says so and doesn't render them empty. Open the dashboard on the dispatching device for those. |

## API

Devices use these endpoints. Everything except `/healthz` needs a credential.

| Endpoint | Who | What |
|---|---|---|
| `GET /healthz` | anyone | `ok`, no data |
| `POST /api/v1/runs` | device token | Body `{"runs": [...], "garden"?, "merge_allowlist"?}`, at most 500 runs. Answers `{"held": [uuid, ...]}`, listing every uuid from the batch the hub now holds. A row with an unknown `outcome` or a malformed uuid, repo, or timestamp refuses the whole batch with a 400 naming the field; the device keeps it queued. |
| `GET /api/v1/runs?repo=&limit=` | device token or operator | Newest runs across devices (`gardener status --all-devices`) |
| `GET /api/v1/latest-success?repo=&mode=` | device token or operator | When `repo` last recorded a successful `mode` run on any device. Nothing in gardener consults this yet (RFC 0007 phase 2). |
| `GET /`, `GET /api/status` | operator | The dashboard |

## Upgrading

Upgrade the hub **before** its devices. A device on a newer gardener may
record an outcome the older hub doesn't know. The hub refuses that batch
rather than storing rows its aggregates would miscount, and the device
keeps it queued until the hub understands it. `gardener hub status` on the
device shows the queue growing, and each failed push prints a `NOTE` line
naming the refused field.
