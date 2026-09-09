# Hosting the YouTube video helper

Viola's multiroom browser display can use a separately hosted static YouTube
helper. It does not require a Viola subscription, billing service, or company
website. Without configuration, the existing hosted endpoint remains
`https://useviola.com/embed`. This setting affects spoke video; the hub's local
player and streamed multiroom audio keep their existing paths.

## Export and configure

From the repository root, export into a new directory:

```sh
python scripts/build_youtube_embed.py --output /path/to/new-youtube-helper
```

The exporter writes only `embed.html`, `js/embed.js`, and `_headers`. It refuses
to overwrite an existing directory. Publish those files together on a static
HTTPS host you control, then set `VITE_YOUTUBE_EMBED_URL` to the full public URL,
for example `https://player.example.org/embed.html`, when building the frontend:

```sh
cd ui/react-app
VITE_YOUTUBE_EMBED_URL=https://player.example.org/embed.html npm run build
```

In PowerShell, set `$env:VITE_YOUTUBE_EMBED_URL` before `npm run build`.
The value is public configuration and must contain no credentials. HTTPS is
required except for HTTP loopback development (`localhost`, `127.0.0.1`, `::1`).
The URL must serve the helper without redirecting to another origin or adding
authentication. Keep the relative `js/embed.js` path accessible beside it.

Deploy the whole frontend build, including `youtube-embed.json`. This artifact
records the URL compiled into the JavaScript and supplies the backend's
Content Security Policy (CSP), which restricts which hosts the browser may load.
An explicit runtime setting with a different origin causes a setup error asking
for a rebuild. Changing the endpoint requires rebuilding, including when making
an installer; changing an environment variable after installing does not rewrite
the bundled UI. Existing installer rules include the entire `ui/static` tree.
Unbuilt source or older bundles without the artifact use the setting/default.

## Host headers and message checks

`_headers` contains the helper's CSP, referrer policy, and other response headers.
Hosts that support this file can apply it directly. On other servers, configure
equivalent HTTP response headers; merely serving `_headers` as a file has no
effect. Do not inherit a website-wide `X-Frame-Options: DENY`/`SAMEORIGIN` or
`frame-ancestors 'none'` policy: those prevent Viola from framing the helper.
The supplied policy permits HTTP/HTTPS parent pages, including LAN displays.

The display sends its origin in `parent_origin`. Both sides check the exact
origin and the actual parent/iframe window before accepting messages, and use
an exact target origin for outgoing messages. Older clients can identify their
origin through the browser referrer. Missing or invalid parent identity disables
the message bridge. The helper is a public video widget; these checks bind its
messages to its own parent, rather than granting access to Viola credentials.

## Playback and validation

The helper preserves muted autoplay, inline playback, track changes, pause,
resume, seek, volume, midpoint joins, position reports, and playback/error events.
Spoke video remains muted because the hub supplies its audio separately. The
existing display drift correction and iOS recovery logic remain in place.

YouTube requires embedded clients to provide their identity through the HTTP
referrer or an equivalent mechanism. The helper sends its real origin and uses
`strict-origin-when-cross-origin`; avoid a proxy or browser policy that strips
this identity. The existing player configuration identifies the helper itself
through both `origin` and `widget_referrer`. YouTube documents `widget_referrer`
as the traffic-source attribution for nested widgets; this boundary change
preserves the existing value pending separate live playback qualification.
See [YouTube client identification requirements](https://developers.google.com/youtube/terms/required-minimum-functionality#embedded-player-api-client-identity)
and [player parameters](https://developers.google.com/youtube/player_parameters#widget_referrer).

An independent host does not guarantee that every video can play. YouTube may
restrict embedding, and browsers may block autoplay. Error 153 indicates missing
client identification; 101/150 indicate embedding restrictions. See the
[IFrame API error reference](https://developers.google.com/youtube/iframe_api_reference#onError).
After deploying your host, verify video startup, a midpoint join, track changes,
pause/resume, and sync on the actual spoke browsers you support. Local mocked
API tests verify the bridge and security contract, not YouTube's acceptance of
a deployment or physical audio/video synchronization.

The focused regression commands are:

```sh
cd ui/react-app
npx vitest run src/utils/youtubeEmbed.test.js tests/youtube_embed_bridge.test.js
```
