# Remote Settings Ingestion Job Example

Turn a source of data into records on Remote Settings.

* [Compliant with specs](https://remote-settings.readthedocs.io/en/latest/support.html#how-do-i-automate-the-publication-of-records-forever)
* Dry run
* Dual sign-off
* Lint
* Github Action CI and CD
* Tests with `kinto_http` mocks
* Sentry integration

## Run

With local Remote Settings server:

```
$ docker run --rm --detach \
    -p 8888:8888 \
    --env KINTO_INI=config/testing.ini \
    mozilla/remote-settings
```

Create the source collection:

```
$ curl -X PUT http://localhost:8888/v1/buckets/main-workspace/collections/product-integrity
```

And run the script:

```
$ uv sync
$ SERVER="http://localhost:8888/v1" uv run script.py

Fetch server info...✅
⚠️ Anonymous
Fetch records from source of truth...✅
Fetch current destination records...✅
Batch #0: PUT /buckets/main-workspace/collections/product-integrity/records/release - 201
Batch #1: PUT /buckets/main-workspace/collections/product-integrity/records/beta - 201
Batch #2: PUT /buckets/main-workspace/collections/product-integrity/records/esr - 201
Apply changes...3 operations ✅
Request review...✅
```

Or from the Docker container:

```
$ docker build -t ingestion:latest .
$ docker run -e ENVIRONMENT=local ingestion:latest
...
...
Records are in sync. Nothing to do ✅.
```

### On Remote Settings official servers

([List of environment servers](https://remote-settings.readthedocs.io/en/latest/getting-started.html#environments))

**As yourself**:

Login on the Admin UI and copy the Bearer header value (UI top right bar)

And use it to run the script

```
$ read -s BEARER
$ AUTHORIZATION=$BEARER SERVER="http://remote-settings.mozilla.org/v1" python script.py
```

**Using an account**:

```
$ read -s PASSWD
$ AUTHORIZATION=fxrelay-publisher:$PASSWD ENVIRONMENT=prod python script.py
```

## Develop

See available operations using:

```
make help
```

And see how unit and integration tests are run on Github Action CI.
