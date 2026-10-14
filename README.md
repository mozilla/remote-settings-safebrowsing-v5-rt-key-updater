# Remote Settings SafeBrowsing V5 Real-Time Key Updater

The updater fetches the latest SafeBrowsing V5 real-time OHTTP key config from
the Google endpoint and publishes it to the `sbv5-rt-key` collection on Remote
Settings.

Source: https://safebrowsingohttpgateway.googleapis.com/v1/ohttp/hpkekeyconfig

## Record format

The collection holds a single record with the fixed id `latest`. When Google
rotates the key, the record is updated in place. Previous keys keep working on
the Google side for about two weeks after rotation, which leaves time for the
change to be reviewed and to reach clients.

```json
{
  "id": "latest",
  "key_config": "HQAgLPp299yewWibKwPaxVRye21WjLCpEU75A0vrhBqgqyoABAABAAI=",
  "key_id": 29
}
```

* `key_config`: the bytes returned by Google, base64 encoded and published
  verbatim. This is a single OHTTP Key Config as defined in
  [RFC 9458 Section 3.1](https://www.rfc-editor.org/rfc/rfc9458#section-3.1)
  (no length prefix). Clients decode it and pass the bytes to their OHTTP
  implementation as is.
* `key_id`: the 8-bit key identifier parsed from `key_config`. Redundant with
  the blob, but it makes review diffs readable.

Before publishing, the script validates the response: HTTP status, content type
(`application/ohttp-keys`), a known HPKE KEM id with matching public key length,
and an exact total length. Anything else raises and nothing is written.

## Run

With local Remote Settings server:

```
$ docker run --rm --detach \
    -p 8888:8888 \
    --env KINTO_INI=config/testing.ini \
    mozilla/remote-settings
```

Create the destination collection:

```
$ curl -X PUT http://localhost:8888/v1/buckets/main-workspace/collections/sbv5-rt-key
```

And run the script:

```
$ uv sync
$ SERVER="http://localhost:8888/v1" uv run script.py

Fetch server info...✅
⚠️ Anonymous
Fetch records from source of truth...✅
Fetch current destination records...✅
Batch #0: PUT /buckets/main-workspace/collections/sbv5-rt-key/records/latest - 201
Apply changes...1 operations ✅
Request review...✅
```

Or from the Docker container:

```
$ docker build -t ingestion:latest .
$ docker run --network=host -e ENVIRONMENT=local ingestion:latest
...
...
Records are in sync. Nothing to do ✅.
```

### On Remote Settings official servers

([List of environment servers](https://remote-settings.readthedocs.io/en/latest/getting-started.html#environments))

On `dev`, the script approves its own changes. On `stage` and `prod`, it
requests a review instead, so a second person has to sign off.

**As yourself**:

Login on the Admin UI and copy the Bearer header value (UI top right bar)

And use it to run the script

```
$ read -s BEARER
$ AUTHORIZATION=$BEARER ENVIRONMENT=dev uv run script.py
```

**Using an account**:

```
$ read -s PASSWD
$ AUTHORIZATION=<account>:$PASSWD ENVIRONMENT=prod uv run script.py
```

Set `DRY_RUN=1` to print the requests without sending them.

## Develop

See available operations using:

```
make help
```

And see how unit and integration tests are run on Github Action CI.
