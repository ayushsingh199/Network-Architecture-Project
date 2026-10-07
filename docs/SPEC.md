# BHP/1 — Binary Hypertext Protocol, version 1

**Status:** complete. **Audience:** someone who has never seen our code and must build a
compatible server *or* client from this document alone. The key words MUST, MUST NOT,
SHOULD and MAY are used as in RFC 2119. All integers are **unsigned, big-endian**.
All text is **UTF-8**.

## 1. Connection model

A client opens **one** TCP connection to the server (default port 9000) and sends frames
directly. There is no preface or handshake. The client sends a request, the server answers,
and the connection **stays open** for the next request. A client MAY send several requests
before reading the replies (pipelining). The server MUST answer them **in the order received**.
Either side ends the connection by closing TCP **between** frames, optionally after sending a
GOAWAY. EOF in the middle of a frame is an error.

## 2. Frame header — 8 bytes, fixed forever

```
 0               1               2               3
+---------------+---------------+---------------+---------------+
|                          Length (32)                          |
+---------------+---------------+-------------------------------+
|   Type (8)    |   Flags (8)   |         Stream ID (16)        |
+---------------+---------------+-------------------------------+
|                    Payload (Length bytes) ...                 |
```

| Field | Bits | Meaning |
|---|---|---|
| Length | 32 | Payload size in bytes, excluding these 8. MUST be ≤ 16 777 216 (2²⁴). |
| Type | 8 | What the payload is (§3). |
| Flags | 8 | Bit `0x01` = END_STREAM. Other bits MUST be sent as 0 and MUST be ignored. |
| Stream ID | 16 | Pairs a response with its request. `0` = the connection itself. |

**Why these widths, and not HTTP/2's 24/8/8/31?** HTTP/2 multiplexes hundreds of concurrent
streams over one long-lived connection. It needs a 31-bit stream space, with odd IDs for the
client and even IDs for the server, plus one reserved bit. It keeps frames small (16 KiB by
default) so that streams interleave fairly, so 24 bits of length (16 MiB) is plenty and saves a
byte. That gives 9 bytes. BHP/1 makes different choices:

* **Length 32, policy cap 16 MiB.** It is one aligned word that any language reads with a
  single `ntohl` / `struct ">I"`, with no 24-bit splicing. The cap is the same as HTTP/2's
  ceiling, but it is a *policy*: version 2 can raise it without changing the header.
* **Type 8, Flags 8.** Same as HTTP/2. 256 types is plenty because the skip rule (§6) makes
  new types free to deploy. 8 flags cover every state bit we can foresee.
* **Stream ID 16.** We do not multiplex: a stream is one request and its one response, served in
  order. The ID only correlates a response with its request and detects desynchronisation.
  65 535 values wrap safely (skip 0) because only a handful are ever outstanding. Narrowing it
  makes the header 8 bytes instead of 9, so it is 64-bit aligned.

## 3. Frame types

| Type | Name | Sent by | Payload |
|---|---|---|---|
| `0x01` | REQUEST | client | `method u8` · `path_len u16` · `path` · header block |
| `0x02` | RESPONSE | server | `status u16` · header block |
| `0x03` | DATA | both | raw body bytes |
| `0x04` | GOAWAY | both | `last_stream_id u16` · `code u16` · reason text (rest of payload) |
| other | — | — | **MUST be skipped** (§6) |

* **Methods:** `0x01` GET, `0x02` HEAD. Any other value is well-formed and earns a **405**.
* **Path:** MUST start with `/`, MUST NOT contain NUL, and is at most 65 535 bytes.
  `?query` and `#fragment` are ignored. `%XX` escapes are decoded by the server.
* **Status:** 100–599. Its meaning is the same as in HTTP.
* **Stream rules:** the client numbers requests 1, 2, 3, … (mod 65 536, skipping 0). The server
  echoes the ID on every frame of the reply. END_STREAM marks the **last frame of a message**:
  * set on a REQUEST: no request body follows
  * set on a RESPONSE: no body follows (HEAD, empty file)
  * set on a DATA frame: this is the final body chunk

  A body is any number of DATA frames. bserve sends 64 KiB chunks.
* **Request bodies:** if a REQUEST lacks END_STREAM, DATA frames on the same stream follow until
  END_STREAM. GET and HEAD give a body no meaning, so the server MUST read it and discard it
  before answering.
* **GOAWAY** is always sent on stream 0. `code` is 0 for a graceful close or an HTTP-style
  status (400, 500) for an error. The sender closes TCP right after it.

## 4. Header block — static table + length-prefixed literals

```
count u8, then count × { name_ref u8 [, name_len u8, name] , value_len u16, value }
```

`name_ref` 1–10 selects a name from the static table. Each of these is a name that bcurl or
bserve actually sends, so in practice every header name costs **one byte**. `name_ref = 0`
means a literal name follows: `name_len u8` plus lowercase ASCII token characters. Values
from 11 to 255 are reserved and **malformed** in v1. Names are case-insensitive and sent in
lowercase. Values MUST NOT contain NUL, CR or LF. Order is preserved and duplicates are allowed.

| # | name | # | name |
|---|---|---|---|
| 1 | host | 6 | server |
| 2 | user-agent | 7 | date |
| 3 | accept | 8 | last-modified |
| 4 | content-type | 9 | allow |
| 5 | content-length | 10 | etag |

These are the first two mechanisms of HPACK (RFC 7541): a static index and length-prefixed
literals. We deliberately leave out HPACK's dynamic table and Huffman coding. Both make a
decoder stateful, so one corrupt byte poisons every later request on the connection. Without
them, each header block decodes on its own.

## 5. Server semantics (`bserve ROOT PORT`)

1. Decode `%XX` in the path and split it on `/`. Drop empty and `.` segments. **Any `..`
   segment, NUL, or a symlink resolving outside ROOT → 404.** A 404 is used rather than a 403
   so that the reply reveals nothing about what exists outside the root.
2. A directory maps to its `index.html`.
3. **200**: a RESPONSE with `content-type`, `content-length`, `last-modified` and `etag` (plus
   `server` and `date` on every response), then DATA frames totalling exactly
   `content-length`. For HEAD, the RESPONSE carries END_STREAM and no DATA follows.
4. Errors: **400** malformed, **403** unreadable, **404** missing, **405** method (+
   `allow: GET, HEAD`), **500** I/O failure. Each has a `text/plain` body that explains it
   (no body for HEAD).
5. After 30 s of idleness the server sends `GOAWAY code=0 "idle timeout"` and closes.

## 6. Errors and extensibility — the rules you may not skip

**Unknown frame type → skip it.** A receiver that meets a type it does not know MUST read
exactly `Length` payload bytes, discard them, and carry on with the next header. It MUST NOT
answer, error or close. This works because Length sits in the fixed header, which every
version shares. It is how version 2 adds frames without breaking version 1 peers.
Undefined flag bits are likewise ignored.

Errors have exactly **two scopes**:

| Scope | Trigger | Receiver MUST |
|---|---|---|
| **Stream** | The frame is well-framed but its payload is bad: truncated field, path without `/`, header index > 10, trailing bytes, bad UTF-8. Also a known frame in the wrong place (RESPONSE or stray DATA sent to a server). | Reply **400** on that stream ID and **keep the connection open**: the next header is still where Length says. |
| **Connection** | The framing itself is untrustworthy: Length > 16 MiB, EOF mid-frame, REQUEST or DATA on stream 0, or a non-DATA frame inside a request body. | Send **GOAWAY** (stream 0, code 400) and close. Skipping is impossible, so no byte after this point can be trusted. |

Example: a text client sending `GET / HTTP/1.1` makes the header read Length = `0x47455420`
(1.19 GB). That is over the cap, so the server sends GOAWAY and closes.

## 7. Client behaviour (`bcurl [-v] [-I] [-o FILE] URL [/path ...]`)

`URL` is `[bhp://]host[:port][/path]`. The client sends `host`, `user-agent` and `accept`. All
requests on the command line share **one** connection, so URLs that name a second host:port are
refused. The body goes to stdout. `-v` hexdumps every frame sent (`>`) and received (`<`) to
stderr, with payloads truncated to 256 bytes. `-vv` dumps them in full. The client also skips
unknown frames. Exit status: **0** success, **4** any 4xx, **5** any 5xx, **3** connection or
protocol error, **2** usage.

## 8. Conformance

`bconform host:port/existing-file` runs the MUST rules above against **any** server, speaking
raw frames: keep-alive, 404, unknown-type skip, malformed → 400 with the connection still
open, HEAD, ignored flags, drained request body, and oversized → GOAWAY. Run it against your
partner's server: an implementation that passes only against itself is not yet a protocol.
