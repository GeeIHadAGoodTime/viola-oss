"""CAPTCHA handling - Viola solves it herself.

When a site shows a human check (captcha), the browser view stays on screen and
Viola works the check with the ordinary browser tools: she clicks the "I'm not a
robot" checkbox and completes any image, audio, or text challenge from the page
snapshot, the same way a person would. She does not stop or hand the task off; the
user is only an emergent last resort if she genuinely cannot get past it.

This module is intentionally empty: solving uses the normal browser tools, not a
separate solver. It exists only to avoid import errors from code that may still
reference it. server.py surfaces bot-protection page facts to the caller.
"""
