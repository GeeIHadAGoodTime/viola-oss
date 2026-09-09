/* Embed player controller. */
(function() {
  'use strict';

  var player = null;
  var positionInterval = null;
  var currentVideoId = null;

  // Parse video ID from URL
  var params = new URLSearchParams(window.location.search);
  var initialVideoId = params.get('v');
  // New clients supply their own origin. Referrer preserves compatibility with
  // older clients; a missing/invalid identity disables the message bridge.
  var parentOrigin = null;
  try {
    var parentUrl = new URL(params.has('parent_origin') ? params.get('parent_origin') : document.referrer);
    if ((parentUrl.protocol === 'http:' || parentUrl.protocol === 'https:') && !parentUrl.username && !parentUrl.password) {
      parentOrigin = parentUrl.origin;
    }
  } catch (_) { /* No trustworthy parent origin available. */ }
  // Multiroom spokes receive their audio as a PCM stream from the hub, so the
  // embedded video must play MUTED to avoid double audio. Muted is also what
  // lets the video autoplay at all (browsers block unmuted autoplay). Default
  // to muted; an explicit mute=0 opts back into the player's own audio.
  var initialMuted = params.get('mute') !== '0';

  // Load YouTube IFrame API
  var tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);

  // Send message to parent
  function sendToParent(msg) {
    if (parentOrigin && window.parent && window.parent !== window) {
      window.parent.postMessage(msg, parentOrigin);
    }
  }

  // Error code names
  var ERROR_NAMES = {
    2: 'INVALID_PARAM',
    5: 'HTML5_ERROR',
    100: 'NOT_FOUND',
    101: 'EMBED_RESTRICTED',
    150: 'EMBED_RESTRICTED',
    153: 'CLIENT_IDENTITY_REQUIRED'
  };

  // State number to name
  var STATE_NAMES = {
    '-1': 'UNSTARTED',
    '0': 'ENDED',
    '1': 'PLAYING',
    '2': 'PAUSED',
    '3': 'BUFFERING',
    '5': 'CUED'
  };

  // Start position reporting
  function startPositionReporting() {
    stopPositionReporting();
    positionInterval = setInterval(function() {
      if (player && typeof player.getCurrentTime === 'function') {
        sendToParent({
          type: 'viola_position',
          position: player.getCurrentTime(),
          duration: player.getDuration()
        });
      }
    }, 1000);
  }

  // Stop position reporting
  function stopPositionReporting() {
    if (positionInterval) {
      clearInterval(positionInterval);
      positionInterval = null;
    }
  }

  // Show error overlay
  function showError(message) {
    var overlay = document.getElementById('error-overlay');
    overlay.textContent = message;
    overlay.style.display = 'block';
    setTimeout(function() {
      overlay.style.display = 'none';
    }, 5000);
  }

  // Create YouTube player
  function createPlayer(videoId) {
    currentVideoId = videoId;
    player = new YT.Player('player', {
      videoId: videoId,
      width: '100%',
      height: '100%',
      playerVars: {
        autoplay: 1,
        mute: initialMuted ? 1 : 0,
        controls: 1,
        modestbranding: 1,
        rel: 0,
        playsinline: 1,
        enablejsapi: 1,
        // Identify the actual helper host. Do not spoof another site's origin.
        origin: window.location.origin,
        // Preserve the existing helper-origin attribution used by playback.
        widget_referrer: window.location.origin
      },
      events: {
        onReady: function() {
          sendToParent({ type: 'viola_ready' });
        },
        onStateChange: function(event) {
          var stateName = STATE_NAMES[String(event.data)] || 'UNKNOWN';
          sendToParent({ type: 'viola_state', state: stateName });

          if (event.data === YT.PlayerState.PLAYING) {
            startPositionReporting();
          } else {
            stopPositionReporting();
          }
        },
        onError: function(event) {
          var code = event.data;
          var name = ERROR_NAMES[code] || 'UNKNOWN';
          sendToParent({ type: 'viola_error', code: code, name: name });

          if (code === 150 || code === 101) {
            showError("This track can't play here. Try a different version.");
          }
        }
      }
    });
  }

  // YouTube API ready callback
  window.onYouTubeIframeAPIReady = function() {
    if (initialVideoId) {
      createPlayer(initialVideoId);
    } else {
      sendToParent({ type: 'viola_ready' });
    }
  };

  // Listen for commands from parent
  window.addEventListener('message', function(event) {
    if (!parentOrigin || event.source !== window.parent || event.origin !== parentOrigin) return;
    var data = event.data;
    if (!data || typeof data.type !== 'string' || !data.type.startsWith('viola_')) return;

    switch (data.type) {
      case 'viola_play':
        if (!data.videoId) return;
        // A spoke joining mid-song sends startAt (hub position). Consume it
        // once per viola_play so the video starts at the right spot instead of
        // waiting for drift correction. Guarded: non-numeric/non-finite or
        // <=0 startAt falls through to the plain load (the old behavior).
        var startAt = (typeof data.startAt === 'number' && isFinite(data.startAt) && data.startAt > 0) ? data.startAt : 0;
        if (player && typeof player.loadVideoById === 'function') {
          currentVideoId = data.videoId;
          if (startAt > 0) {
            player.loadVideoById({ videoId: data.videoId, startSeconds: startAt });
          } else {
            player.loadVideoById(data.videoId);
          }
        } else {
          createPlayer(data.videoId);
        }
        break;

      case 'viola_pause':
        if (player && typeof player.pauseVideo === 'function') {
          player.pauseVideo();
        }
        break;

      case 'viola_resume':
        if (player && typeof player.playVideo === 'function') {
          player.playVideo();
        }
        break;

      case 'viola_seek':
        if (player && typeof player.seekTo === 'function' && typeof data.position === 'number') {
          player.seekTo(data.position, true);
        }
        break;

      case 'viola_volume':
        if (player && typeof player.setVolume === 'function' && typeof data.level === 'number') {
          player.setVolume(data.level);
        }
        break;
    }
  });
})();
