# Annotated hexdump — one complete request and response

This is a real capture, not a hand-made example. It was produced with:

```
./bserve ./www 9000 &
./bcurl -vv localhost:9000/hello.txt      # www/hello.txt contains "hi\n"
```

The raw `-vv` output is in [`capture.txt`](capture.txt). `>` marks bytes the client sent and
`<` marks bytes it received. Every byte is accounted for below. Offsets are relative to the
start of each frame.

---

## Frame 1 — REQUEST (client → server), 8 + 51 = 59 bytes

```
0000  00 00 00 33 01 01 00 01  01 00 0a 2f 68 65 6c 6c  |...3......./hell|
0010  6f 2e 74 78 74 03 01 00  0e 6c 6f 63 61 6c 68 6f  |o.txt....localho|
0020  73 74 3a 39 30 30 30 02  00 0b 62 63 75 72 6c 2f  |st:9000...bcurl/|
0030  31 2e 30 2e 30 03 00 03  2a 2f 2a                 |1.0.0...*/*|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–03 | `00 00 00 33` | **Length** | 0x33 = 51 payload bytes follow the header |
| 04 | `01` | **Type** | REQUEST |
| 05 | `01` | **Flags** | END_STREAM: no request body follows |
| 06–07 | `00 01` | **Stream ID** | 1, the first request on this connection |
| 08 | `01` | method | GET |
| 09–0A | `00 0a` | path_len | 10 |
| 0B–14 | `2f 68 65 6c 6c 6f 2e 74 78 74` | path | `/hello.txt` |
| 15 | `03` | header count | 3 headers |
| 16 | `01` | name_ref | static #1 = `host` (1 byte instead of 4) |
| 17–18 | `00 0e` | value_len | 14 |
| 19–26 | `6c 6f … 30 30` | value | `localhost:9000` |
| 27 | `02` | name_ref | static #2 = `user-agent` |
| 28–29 | `00 0b` | value_len | 11 |
| 2A–34 | `62 63 … 2e 30` | value | `bcurl/1.0.0` |
| 35 | `03` | name_ref | static #3 = `accept` |
| 36–37 | `00 03` | value_len | 3 |
| 38–3A | `2a 2f 2a` | value | `*/*` |

Check: 1 (method) + 2 + 10 (path) + 1 (count) + 17 + 14 + 6 (headers) = **51** ✓

---

## Frame 2 — RESPONSE (server → client), 8 + 129 = 137 bytes

```
0000  00 00 00 81 02 00 00 01  00 c8 06 06 00 0c 62 73  |..............bs|
0010  65 72 76 65 2f 31 2e 30  2e 30 07 00 1d 57 65 64  |erve/1.0.0...Wed|
0020  2c 20 30 37 20 4f 63 74  20 32 30 32 36 20 31 34  |, 07 Oct 2026 14|
0030  3a 32 33 3a 30 37 20 47  4d 54 04 00 19 74 65 78  |:23:07 GMT...tex|
0040  74 2f 70 6c 61 69 6e 3b  20 63 68 61 72 73 65 74  |t/plain; charset|
0050  3d 75 74 66 2d 38 05 00  01 33 08 00 1d 57 65 64  |=utf-8...3...Wed|
0060  2c 20 30 37 20 4f 63 74  20 32 30 32 36 20 31 34  |, 07 Oct 2026 14|
0070  3a 32 30 3a 30 30 20 47  4d 54 0a 00 0c 22 33 2d  |:20:00 GMT..."3-|
0080  36 61 63 36 35 35 31 30  22                       |6ac65510"|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–03 | `00 00 00 81` | **Length** | 0x81 = 129 |
| 04 | `02` | **Type** | RESPONSE |
| 05 | `00` | **Flags** | none: END_STREAM is *not* set, so DATA frames follow |
| 06–07 | `00 01` | **Stream ID** | 1, echoing the request |
| 08–09 | `00 c8` | status | 0xC8 = **200** |
| 0A | `06` | header count | 6 headers |
| 0B | `06` | name_ref | static #6 = `server` |
| 0C–0D | `00 0c` | value_len | 12 |
| 0E–19 | `62 73 … 2e 30` | value | `bserve/1.0.0` |
| 1A | `07` | name_ref | static #7 = `date` |
| 1B–1C | `00 1d` | value_len | 29 |
| 1D–39 | `57 65 … 4d 54` | value | `Wed, 07 Oct 2026 14:23:07 GMT` |
| 3A | `04` | name_ref | static #4 = `content-type` |
| 3B–3C | `00 19` | value_len | 25 |
| 3D–55 | `74 65 … 2d 38` | value | `text/plain; charset=utf-8` |
| 56 | `05` | name_ref | static #5 = `content-length` |
| 57–58 | `00 01` | value_len | 1 |
| 59 | `33` | value | `3`: the body will be exactly 3 bytes |
| 5A | `08` | name_ref | static #8 = `last-modified` |
| 5B–5C | `00 1d` | value_len | 29 |
| 5D–79 | `57 65 … 4d 54` | value | `Wed, 07 Oct 2026 14:20:00 GMT` |
| 7A | `0a` | name_ref | static #10 = `etag` |
| 7B–7C | `00 0c` | value_len | 12 |
| 7D–88 | `22 33 … 30 22` | value | `"3-6ac65510"` (size-mtime, in hex) |

Check: 2 (status) + 1 (count) + 15 + 32 + 28 + 4 + 32 + 15 = **129** ✓

---

## Frame 3 — DATA (server → client), 8 + 3 = 11 bytes

```
0000  00 00 00 03 03 01 00 01  68 69 0a                 |........hi.|
```

| Offset | Bytes | Field | Meaning |
|---|---|---|---|
| 00–03 | `00 00 00 03` | **Length** | 3 |
| 04 | `03` | **Type** | DATA |
| 05 | `01` | **Flags** | END_STREAM: last frame of response 1 |
| 06–07 | `00 01` | **Stream ID** | 1 |
| 08–0A | `68 69 0a` | body | `hi\n`, written to stdout by bcurl |

After this frame the response is complete. bcurl exits 0 and closes TCP between frames,
which is a graceful end. A second URL on the command line would have gone out on the **same**
connection as stream 2.

---

## Size versus HTTP/1.1

| | BHP/1 | HTTP/1.1 (same headers, as text) |
|---|---|---|
| Request | 59 B | 87 B |
| Response (head + body) | 137 + 11 = 148 B | 207 B |
| **Total** | **207 B** | **294 B** (−30 %) |

Most of the savings come from the static table, where each header name costs 1 byte, and from
having no `\r\n` delimiters. Every field's position is known from lengths, so nothing has to
be scanned for.
