# QR File Transfer

Send a file from one device to another using nothing but a stream of QR
codes and a camera — no internet connection, no file upload to any server,
no pairing/bluetooth. One device displays the file as a cycling sequence of
QR codes; the other device points its camera at the screen and reassembles
the file as it scans.

Everything the browser needs — UI, QR encoder, QR decoder — lives in a
single HTML file, `index.html`. `server.py` just serves that file to other
devices on the same local network.

## Quick start

```sh
python3 server.py
```

This prints one or more URLs, e.g.:

```
QR File Transfer — serving /home/you/sendFile

  https://localhost:8443/
  https://192.168.1.23:8443/

Self-signed certificate: the browser will show a 'connection is not
private' warning on first visit from each device — this is expected
for a local, offline server. Proceed / accept the certificate once.

Press Ctrl+C to stop.
```

- On the **sending** device, open the `https://<lan-ip>:8443/` URL and use
  the **Send** tab.
- On the **receiving** device (typically a phone), open the same URL and
  use the **Receive** tab.
- Both devices just need to be on the same Wi-Fi/LAN as the machine running
  `server.py` — no internet access is required by either device.

### Why HTTPS, and why a scary certificate warning?

Browsers only allow camera access (`getUserMedia`) on a "secure context":
HTTPS, or `http://localhost` on the same machine. Since the receiving
device isn't `localhost`, plain HTTP would get the camera request silently
blocked. `server.py` therefore generates a self-signed TLS certificate
(via `openssl`, cached in `.certs/`) and serves over HTTPS by default. Every
browser will warn that the certificate isn't from a trusted authority —
that's expected for a private, local, self-signed cert; accept/proceed past
the warning once per device. If `openssl` isn't installed, the server falls
back to plain HTTP automatically (sending still works fine; receiving will
only work via `http://localhost` on that same machine).

```sh
python3 server.py --http          # force plain HTTP
python3 server.py --port 9000     # custom port
python3 server.py --dir /path/to  # serve a different directory
```

## Using it

### Send

1. Choose a file. It's compressed automatically (see *Compression* below)
   before being split into chunks.
2. Optionally adjust **chunk size** (bytes encoded per QR frame — bigger
   chunks mean fewer frames but denser, harder-to-scan codes) and **error
   correction level** (higher = more scan-robust but lower data capacity
   per frame; QR's own Reed-Solomon correction already recovers from
   partial damage/blur — level M is a good default).
3. Use **Prev / Next** to step through frames manually, or **Start Auto**
   to continuously cycle through every frame at the configured speed. The
   sequence always starts with one metadata (**META**) frame — describing
   the file name, size, and total chunk count — followed by all the data
   chunks, then loops.
4. Changing the chunk size starts a new transfer (new transfer ID) since it
   changes how the file is split.

### Receive

1. Tap **Start** to turn on the camera (grant the permission prompt) and
   point it at the sending device's screen, filling the frame with the QR
   code.
2. Once a META frame is scanned, the **transfer status** section shows the
   file name/size and a **chunk map**: a grid with one cell per chunk —
   green once received, red if a chunk was scanned but failed its checksum
   (rare — QR codes are already self-correcting, but a bad scan can still
   occasionally produce a corrupt read), and empty for anything not seen
   yet. A live count and a compressible "missing chunk indexes" list are
   shown below the grid.
3. Frames can arrive in any order and duplicates are ignored, so the sender
   doesn't need to slow down for the receiver, and the receiver doesn't
   need to catch every single loop — just keep scanning until the counter
   reads "N / N".
4. Once complete, the app verifies the transmitted checksum, transparently
   decompresses if the sender compressed it, then enables **Download file**
   to save the reconstructed original file (name, type, and integrity check
   all come from the META frame).
5. If a different file starts being broadcast (a META frame with a
   different transfer ID) while the current transfer is incomplete, a
   banner offers to switch to it rather than silently discarding progress.

### Compression

Before chunking, the sender compresses the whole file with the browser's
built-in raw-DEFLATE codec (no library — see *Implementation notes*) and
keeps the compressed form only if it's actually smaller; otherwise it sends
the file as-is. This is automatic and shows up in the **Send** panel as a
compression ratio (e.g. "17.6 KB → 116 B (99% smaller)" for a
highly-redundant file) or a note that compression wasn't worth it (already-
compressed formats like JPEG, MP4, or ZIP typically don't shrink further).
Since fewer transmitted bytes means fewer QR frames — the real bottleneck
in this transport — a compressible file can turn a multi-minute transfer
into a handful of frames. It can be turned off per-transfer with the
**Compress before sending** checkbox. The receiver reverses this
automatically; no user action is needed on that side.

## Protocol

Every QR code carries one binary frame — no JSON, no base64: the payload
bytes go into the QR "byte mode" data essentially raw (see *Implementation
notes* below), so a QR that can hold 300 bytes carries a 300-byte chunk,
not a ~225-byte chunk inflated by base64/JSON overhead. All multi-byte
integers are little-endian.

Common header (first 4 bytes of every frame):

| Offset | Size | Field                          |
|-------:|-----:|---------------------------------|
| 0      | 2    | Magic `"QF"` (`0x51 0x46`)      |
| 2      | 1    | Protocol version (`2`)          |
| 3      | 1    | Frame type: `0`=META, `1`=DATA  |

**META** (type `0`) — sent once per loop, describes the whole transfer:

| Offset | Size | Field                                            |
|-------:|-----:|---------------------------------------------------|
| 4      | 4    | `transferId` (random, identifies this send session) |
| 8      | 4    | `totalChunks`                                    |
| 12     | 4    | `fileSize` (size of the transmitted — possibly compressed — payload) |
| 16     | 2    | `chunkSize` (nominal bytes per DATA payload)     |
| 18     | 4    | `fileCrc32` (CRC-32 of the transmitted payload, pre-decompression) |
| 22     | 1    | `compression` (`0`=none, `1`=raw DEFLATE)        |
| 23     | 1+n  | `nameLen` + UTF-8 file name                      |
| ...    | 1+m  | `mimeLen` + UTF-8 MIME type                      |

**DATA** (type `1`) — one per chunk:

| Offset | Size | Field                                |
|-------:|-----:|----------------------------------------|
| 4      | 4    | `transferId`                          |
| 8      | 4    | `index` (0-based chunk index)         |
| 12     | 4    | `crc32` (CRC-32 of this chunk's payload) |
| 16     | ...  | payload bytes                         |

The receiver assembles the transmitted payload by placing each chunk at
offset `index * chunkSize` in a buffer of size `fileSize` (the last chunk is
naturally shorter than `chunkSize`), checks it against `fileCrc32`, then —
if `compression` is `1` — decompresses it to recover the original file
before it's offered for download.

### Implementation notes

- `qrcode-generator`'s default "byte mode" string encoder assumes UTF-8
  text, which would corrupt raw binary payloads (bytes ≥ 0x80 get
  re-encoded as multi-byte UTF-8 sequences). `index.html` patches
  `qrcode.stringToBytesFuncs['default']` to a raw 1-char-per-byte
  passthrough instead, and represents each frame as a JS string where
  `charCodeAt(i)` is the literal byte value (0–255) before handing it to
  the encoder.
- On the way back, `jsQR`'s decode result exposes the scanned bytes
  directly via `binaryData` (as opposed to its UTF-8-interpreted `data`
  string), so no decoding/round-trip step is needed there either.
- This round trip (arbitrary bytes 0–255, through real `qrcode-generator`
  encoding and real `jsQR` decoding) is covered by an automated test — see
  *Testing* below.
- Compression uses `CompressionStream`/`DecompressionStream('deflate-raw')`
  — a codec built into the browser, so it adds no bytes to `index.html`.
  `deflate-raw` is used specifically because it has zero framing overhead
  (gzip adds a header+trailer, zlib's `deflate` adds a smaller one); since
  the protocol already carries its own CRC-32, that framing would be pure
  waste. Browsers without support (older Safari) just skip compression —
  everything still works, only slower for compressible files.

## Architecture

```
index.html   single-file app: markup + CSS + embedded qrcode-generator.js
             (MIT) + embedded jsQR.js (Apache-2.0) + the app's own JS
             (protocol framing, chunking, camera scanning, UI).
server.py    stdlib-only static file server (http.server), HTTPS via a
             locally-generated self-signed cert, prints LAN URLs.
```

There is no backend logic beyond serving the static file — all encoding,
decoding, and reassembly happens in the browser. Nothing is uploaded
anywhere; the two devices never need to exchange anything except the video
of the screen showing the QR codes.

## Limitations

- QR streaming is slow relative to real networking — this is meant for
  small-to-modest files (well under a few MB) where no other transfer
  method is available (air-gapped machines, no shared network, no cable).
  A 1 MB file at the default settings is roughly 3,500 frames.
- One transfer is tracked at a time on the receiving side (plus a small
  buffer for a handful of chunks that arrive slightly ahead of their META
  frame).
- Requires a reasonably modern browser with `getUserMedia` and `Canvas2D`
  support (any current Chrome, Firefox, Safari, or Edge, desktop or
  mobile). Compression additionally needs `CompressionStream` (Chrome/Edge
  80+, Safari 16.4+, Firefox 113+); without it the transfer still works,
  just uncompressed.
- Camera scanning needs a secure context (see the HTTPS section above).

## Testing

`server.py` and the protocol/rendering logic in `index.html` were verified
with:
- Unit tests of the CRC-32 implementation and frame build/parse round trip
  (including the full 0–255 byte range and corruption detection).
- An integration test that renders a QR with the real embedded
  `qrcode-generator`, decodes it with the real embedded `jsQR`, and checks
  the recovered bytes match exactly.
- A headless-browser test (Puppeteer + Chrome) that drives the actual page:
  uploads a file, reads back the *live* rendered `<canvas>` QR pixels via
  an independent `jsQR` decode, verifies the decoded payload matches the
  source file byte-for-byte, and exercises chunk-size changes, auto-play,
  and camera start/stop.
- The same approach for compression: a highly-redundant file is confirmed
  to shrink (compression flag set, transmitted size well below the
  original) while high-entropy pseudo-random data is confirmed to be left
  uncompressed (flag stays `0`, transmitted bytes match the original
  exactly), plus a direct `CompressionStream`/`DecompressionStream`
  round-trip check in the real browser.

These were run ad hoc during development and aren't checked into the repo
as a test suite.

## Third-party code

Embedded in `index.html`:
- [`qrcode-generator`](https://github.com/kazuhikoarase/qrcode-generator)
  v1.4.4 — MIT License — © 2009 Kazuhiko Arase.
- [`jsQR`](https://github.com/cozmo/jsQR) v1.4.0 — Apache License 2.0.

Full license texts are in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
