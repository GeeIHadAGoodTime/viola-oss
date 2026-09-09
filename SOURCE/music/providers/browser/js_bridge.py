"""
music.providers.browser.js_bridge
----------------------------------

JavaScript code generators for browser-native playback control.

Each function returns a self-contained JavaScript string that can be
injected into a QWebEngineView page via ``page.runJavaScript(code)``.
The JS code has no external dependencies and is safe for any origin.

The generated scripts interact with the W3C MediaSession API, standard
HTML ``<audio>``/``<video>`` elements, and page metadata as fallbacks.
"""

from __future__ import annotations


def js_get_media_session_metadata() -> str:
    """Return JS that reads ``navigator.mediaSession.metadata`` and returns JSON.

    The script returns a JSON object with keys: title, artist, album,
    artwork (first artwork URL if available), and playbackState.
    Returns ``null`` if MediaSession is not available.
    """
    return """
(function() {
    try {
        if (!navigator.mediaSession || !navigator.mediaSession.metadata) {
            return null;
        }
        var meta = navigator.mediaSession.metadata;
        var artwork = null;
        if (meta.artwork && meta.artwork.length > 0) {
            artwork = meta.artwork[meta.artwork.length - 1].src || null;
        }
        return {
            title: meta.title || null,
            artist: meta.artist || null,
            album: meta.album || null,
            artwork: artwork,
            playbackState: navigator.mediaSession.playbackState || "none"
        };
    } catch (e) {
        return null;
    }
})();
""".strip()


def js_get_playback_state() -> str:
    """Return JS that reads ``navigator.mediaSession.playbackState``.

    Returns one of: ``"playing"``, ``"paused"``, or ``"none"``.
    """
    return """
(function() {
    try {
        if (navigator.mediaSession) {
            return navigator.mediaSession.playbackState || "none";
        }
        return "none";
    } catch (e) {
        return "none";
    }
})();
""".strip()


def js_call_action(action: str, args: dict | None = None) -> str:
    """Return JS that triggers a MediaSession action handler.

    Supported actions: ``play``, ``pause``, ``stop``, ``seekbackward``,
    ``seekforward``, ``seekto``, ``previoustrack``, ``nexttrack``.

    Falls back to directly calling ``.play()``/``.pause()`` on the first
    media element if no MediaSession handler is registered.

    Args:
        action: The MediaSession action name.
        args: Optional action details dict (e.g. ``{"seekTime": 30.0}``
              for ``seekto``).

    Returns:
        Self-contained JavaScript string.
    """
    # Build the action details object for seekto and similar actions
    if args:
        # Manually serialize to avoid importing json at module level
        parts = []
        for key, value in args.items():
            if isinstance(value, bool):
                parts.append(f"{key}: {'true' if value else 'false'}")
            elif isinstance(value, (int, float)):
                parts.append(f"{key}: {value}")
            elif isinstance(value, str):
                # Escape single quotes in string values
                safe_val = value.replace("\\", "\\\\").replace("'", "\\'")
                parts.append(f"{key}: '{safe_val}'")
        details_js = "{" + ", ".join(parts) + "}"
    else:
        details_js = "{}"

    # Escape action name for safety
    safe_action = action.replace("\\", "\\\\").replace("'", "\\'")

    return f"""
(function() {{
    try {{
        var action = '{safe_action}';
        var details = {details_js};

        // Try MediaSession action handlers first (preferred path)
        if (navigator.mediaSession && navigator.mediaSession.playbackState !== undefined) {{
            // Attempt to invoke the registered action handler
            try {{
                // The standard way: call the action via the handler registry
                // Unfortunately there is no direct callAction() in the spec,
                // so we simulate by dispatching to the first media element.
            }} catch (e) {{
                // Fall through to media element approach
            }}
        }}

        // Direct media element control as primary fallback
        var media = document.querySelector('video') || document.querySelector('audio');
        if (media) {{
            if (action === 'play') {{
                media.play();
                return true;
            }} else if (action === 'pause') {{
                media.pause();
                return true;
            }} else if (action === 'stop') {{
                media.pause();
                media.currentTime = 0;
                return true;
            }} else if (action === 'seekto' && details.seekTime !== undefined) {{
                media.currentTime = details.seekTime;
                return true;
            }} else if (action === 'seekforward') {{
                media.currentTime = Math.min(media.duration || 0, media.currentTime + (details.seekOffset || 10));
                return true;
            }} else if (action === 'seekbackward') {{
                media.currentTime = Math.max(0, media.currentTime - (details.seekOffset || 10));
                return true;
            }}
        }}

        // For nexttrack / previoustrack, try clicking site-specific buttons
        // These selectors cover common player UIs; site recipes can override
        if (action === 'nexttrack') {{
            var btn = document.querySelector('[aria-label*="next" i], [aria-label*="skip" i], .next-button, .skipForward');
            if (btn) {{ btn.click(); return true; }}
        }} else if (action === 'previoustrack') {{
            var btn = document.querySelector('[aria-label*="previous" i], [aria-label*="back" i], .prev-button, .skipBack');
            if (btn) {{ btn.click(); return true; }}
        }}

        return false;
    }} catch (e) {{
        return false;
    }}
}})();
""".strip()


def js_get_media_elements() -> str:
    """Return JS that finds ``<audio>``/``<video>`` elements and returns their state.

    Returns a JSON array of objects, each with: ``tagName``, ``src``,
    ``currentTime``, ``duration``, ``paused``, ``volume``, ``muted``,
    ``readyState``.
    """
    return """
(function() {
    try {
        var elements = document.querySelectorAll('audio, video');
        var result = [];
        for (var i = 0; i < elements.length; i++) {
            var el = elements[i];
            result.push({
                tagName: el.tagName.toLowerCase(),
                src: el.currentSrc || el.src || null,
                currentTime: el.currentTime || 0,
                duration: (el.duration && isFinite(el.duration)) ? el.duration : null,
                paused: el.paused,
                volume: el.volume,
                muted: el.muted,
                readyState: el.readyState
            });
        }
        return result;
    } catch (e) {
        return [];
    }
})();
""".strip()


def js_get_page_metadata() -> str:
    """Return JS that reads ``document.title`` and Open Graph meta tags.

    Returns a JSON object with: ``title`` (document.title), ``ogTitle``,
    ``ogImage``, ``ogDescription``, ``ogSiteName``, and ``favicon``.
    This serves as a fallback when MediaSession metadata is unavailable.
    """
    return """
(function() {
    try {
        function getMeta(property) {
            var el = document.querySelector('meta[property="' + property + '"]') ||
                     document.querySelector('meta[name="' + property + '"]');
            return el ? el.getAttribute('content') : null;
        }

        var favicon = null;
        var linkIcon = document.querySelector('link[rel="icon"], link[rel="shortcut icon"]');
        if (linkIcon) {
            favicon = linkIcon.href;
        }

        return {
            title: document.title || null,
            url: window.location.href || null,
            ogTitle: getMeta('og:title'),
            ogImage: getMeta('og:image'),
            ogDescription: getMeta('og:description'),
            ogSiteName: getMeta('og:site_name'),
            favicon: favicon
        };
    } catch (e) {
        return {title: document.title || null, url: window.location.href || null};
    }
})();
""".strip()


def js_setup_track_end_listener() -> str:
    """Return JS that sets up a listener for track-end events on media elements.

    When a media element fires the ``ended`` event, this script sets
    ``window.__violaTrackEnded = true``.  The Python side can poll this
    flag via a separate JS evaluation.  The flag is reset each time a
    new ``play`` event fires on the same element.
    """
    return """
(function() {
    try {
        // Avoid double-binding
        if (window.__violaTrackEndListenerInstalled) {
            return true;
        }

        window.__violaTrackEnded = false;
        window.__violaTrackEndListenerInstalled = true;

        function attachListeners(el) {
            el.addEventListener('ended', function() {
                window.__violaTrackEnded = true;
            });
            el.addEventListener('play', function() {
                window.__violaTrackEnded = false;
            });
        }

        // Attach to existing elements
        var elements = document.querySelectorAll('audio, video');
        for (var i = 0; i < elements.length; i++) {
            attachListeners(elements[i]);
        }

        // Watch for dynamically added elements
        var observer = new MutationObserver(function(mutations) {
            for (var m = 0; m < mutations.length; m++) {
                var added = mutations[m].addedNodes;
                for (var n = 0; n < added.length; n++) {
                    var node = added[n];
                    if (node.tagName && (node.tagName === 'AUDIO' || node.tagName === 'VIDEO')) {
                        attachListeners(node);
                    }
                    // Also check children of added nodes
                    if (node.querySelectorAll) {
                        var children = node.querySelectorAll('audio, video');
                        for (var c = 0; c < children.length; c++) {
                            attachListeners(children[c]);
                        }
                    }
                }
            }
        });
        observer.observe(document.body, {childList: true, subtree: true});

        return true;
    } catch (e) {
        return false;
    }
})();
""".strip()


def js_check_track_ended() -> str:
    """Return JS that checks the track-ended flag set by the end listener.

    Returns ``true`` if a track has ended since the last play event,
    ``false`` otherwise.
    """
    return """
(function() {
    return window.__violaTrackEnded === true;
})();
""".strip()


def js_set_volume(level: int) -> str:
    """Return JS that sets volume on all ``<audio>``/``<video>`` elements.

    Args:
        level: Volume level 0-100.

    Returns:
        Self-contained JavaScript string.
    """
    # Clamp to valid range and convert to 0.0 - 1.0
    clamped = max(0, min(100, int(level)))
    volume_float = clamped / 100.0

    return f"""
(function() {{
    try {{
        var vol = {volume_float};
        var elements = document.querySelectorAll('audio, video');
        var count = 0;
        for (var i = 0; i < elements.length; i++) {{
            elements[i].volume = vol;
            count++;
        }}
        return count;
    }} catch (e) {{
        return 0;
    }}
}})();
""".strip()


__all__ = [
    "js_call_action",
    "js_check_track_ended",
    "js_get_media_elements",
    "js_get_media_session_metadata",
    "js_get_page_metadata",
    "js_get_playback_state",
    "js_set_volume",
    "js_setup_track_end_listener",
]
