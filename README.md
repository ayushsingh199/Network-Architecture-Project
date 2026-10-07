# BHP/1 — HTTP, in binary

A course project in two tracks with one protocol. **`bserve`** is a static file server and
**`bcurl`** is a client. Between them they speak **BHP/1**: HTTP-style GET and HEAD carried in
fixed-header binary frames over a single, persistent TCP connection.

```
$ ./bserve ./www 9000
$ ./bcurl -v localhost:9000/index.html
```

Python ≥ 3.11 and the standard library are all it needs. There is nothing to install to run it.

## What you hand in

| # | Deliverable | Where |
|---|---|---|
| 1 | **The spec**: two pages, enough for a stranger | [`docs/SPEC.md`](docs/SPEC.md) |
| 2 | **The programs**: server, client, conformance checker | [`bserve`](bserve), [`bcurl`](bcurl), [`bconform`](bconform), [`bhp/`](bhp) |
| 3 | **Annotated hexdump** of one complete request + response | [`docs/HEXDUMP.md`](docs/HEXDUMP.md) (raw capture: [`docs/capture.txt`](docs/capture.txt)) |

## The protocol in one screen

```
 Frame header (8 bytes, never changes)
+--------------------------------+--------+--------+----------------+
|           Length (32)          |Type (8)|Flags(8)| Stream ID (16) |
+--------------------------------+--------+--------+----------------+
 Types: 0x01 REQUEST  0x02 RESPONSE  0x03 DATA  0x04 GOAWAY  — anything else: SKIP IT
 Flags: 0x01 END_STREAM (last frame of this message) — other bits ignored
```

* **REQUEST** = `method u8 | path_len u16 | path | header block`
* **RESPONSE** = `status u16 | header block`, followed by DATA frames
* **Header block** = `count u8`, then for each header `name_ref u8` (1–10 picks from a static
  table of the ten names we actually send, 0 means a length-prefixed literal name follows) and
  `value_len u16 | value`
* **Malformed payload → 400, connection stays open.** Broken framing → GOAWAY + close.
* **Unknown frame type → read `Length` bytes, discard, carry on.** This is the hook that lets
  version 2 add frames without breaking version 1 peers.

The spec explains why the header is 32/8/8/16 rather than HTTP/2's 24/8/8/31.

## Track 1 — the server

```
./bserve ROOT PORT [--host 0.0.0.0] [--idle-timeout 30] [-q]
```

1. It accepts TCP connections, one thread each.
2. It reads REQUEST frames, decodes `%XX` escapes, and maps the path under ROOT. A directory
   maps to its `index.html`. `..` and symlink escapes get a 404.
3. It replies with a RESPONSE (status plus `content-type`, `content-length`, `last-modified`,
   `etag`, `server` and `date`), then the file as 64 KiB DATA frames.
4. Error replies:
   * **404** if the file is not there
   * **400** if the frame is malformed
   * **405** for an unknown method
   * **403** if the file is unreadable
5. **It keeps the connection open** for the next request. It closes the connection with a GOAWAY
   after 30 s idle or when the framing is broken.

## Track 2 — the client

```
./bcurl [-v | -vv] [-I] [-o FILE] [-H 'name: value'] URL [URL | /path ...]
```

* It builds the binary REQUEST frame and writes the response body to stdout (or `-o FILE`).
* `-v` hexdumps every frame, sent `>` and received `<`, with a decoded summary. `-vv` does not
  truncate payloads.
* Exit codes:
  * `0` success
  * `4` any 4xx
  * `5` any 5xx
  * `3` connection or protocol error
  * `2` usage error
* **It never opens a second connection.** Several URLs share one socket, and a URL for another
  host:port is refused:

```
$ ./bcurl -v localhost:9000/index.html /about.html /img/logo.png
```

## Interop: test against a stranger's server

> *A client that only works against your own server is an implementation, not a protocol.*

`bconform` speaks raw frames to **any** BHP/1 server and checks every MUST in the spec:

```
$ ./bconform partner-host:9000/index.html
PASS  check_get
PASS  check_keepalive_404
PASS  check_unknown_frame_skipped
PASS  check_malformed_is_400_and_open
PASS  check_head
PASS  check_unknown_flags_ignored
PASS  check_request_body_drained
PASS  check_oversized_frame_goaway

8/8 checks passed
```

## Layout

```
bhp/
  wire.py         bounds-checked byte cursor (every overrun → MalformedPayloadError)
  frame.py        8-byte header, FrameReader (with the MUST-skip rule), FrameWriter
  headers.py      static table + literal header block codec
  messages.py     REQUEST / RESPONSE / GOAWAY payload codecs
  errors.py       ProtocolError (connection scope) vs MalformedPayloadError (stream scope)
  files.py        safe path → file mapping, MIME types
  hexdump.py      -v rendering
  server.py       bserve
  client.py       bcurl
  conformance.py  bconform
tests/            68 tests: codecs, framing, live server, client, fake server, conformance
www/              demo site (HTML, CSS, PNG, text)
docs/             SPEC.md, HEXDUMP.md, capture.txt
```

## Development

```
make setup     # .venv with pytest, ruff, mypy
make check     # ruff lint + mypy --strict + pytest
make fmt
make serve     # in one terminal
make demo      # in another: two files over one connection, every frame hexdumped
make conform
```

CI runs formatting, lint, strict typing, the test suite, and an end-to-end run of the real
executables on Python 3.11, 3.12 and 3.13.
