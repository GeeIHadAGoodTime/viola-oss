"""General information, utility, and conversational instant command handlers."""

from __future__ import annotations

import datetime
import math
import random
import re

from ._base import _safe_eval_math, log


class InfoHandlersMixin:
    """General information, utility, and conversational instant command handlers."""

    _CITY_TIMEZONES: dict[str, str] = {
        "tokyo": "Asia/Tokyo",
        "london": "Europe/London",
        "new york": "America/New_York",
        "los angeles": "America/Los_Angeles",
        "la": "America/Los_Angeles",
        "chicago": "America/Chicago",
        "denver": "America/Denver",
        "paris": "Europe/Paris",
        "berlin": "Europe/Berlin",
        "moscow": "Europe/Moscow",
        "sydney": "Australia/Sydney",
        "melbourne": "Australia/Melbourne",
        "dubai": "Asia/Dubai",
        "singapore": "Asia/Singapore",
        "hong kong": "Asia/Hong_Kong",
        "shanghai": "Asia/Shanghai",
        "beijing": "Asia/Shanghai",
        "mumbai": "Asia/Kolkata",
        "delhi": "Asia/Kolkata",
        "cairo": "Africa/Cairo",
        "johannesburg": "Africa/Johannesburg",
        "sao paulo": "America/Sao_Paulo",
        "toronto": "America/Toronto",
        "vancouver": "America/Vancouver",
        "seattle": "America/Los_Angeles",
        "san francisco": "America/Los_Angeles",
        "miami": "America/New_York",
        "boston": "America/New_York",
        "atlanta": "America/New_York",
        "dallas": "America/Chicago",
        "houston": "America/Chicago",
        "phoenix": "America/Phoenix",
        "hawaii": "Pacific/Honolulu",
        "honolulu": "Pacific/Honolulu",
        "anchorage": "America/Anchorage",
        "rome": "Europe/Rome",
        "madrid": "Europe/Madrid",
        "amsterdam": "Europe/Amsterdam",
        "lisbon": "Europe/Lisbon",
        "istanbul": "Europe/Istanbul",
        "bangkok": "Asia/Bangkok",
        "seoul": "Asia/Seoul",
        "taipei": "Asia/Taipei",
        "jakarta": "Asia/Jakarta",
        "auckland": "Pacific/Auckland",
    }

    async def tell_time(self, params: dict[str, object]) -> dict[str, object]:
        """Tell the current local time."""
        try:
            now = datetime.datetime.now()
            time_str = now.strftime("%I:%M %p").lstrip("0")
            return {
                "ok": True,
                "message": "It's %s." % time_str,
                "data": {"time": time_str},
            }
        except Exception:
            log.exception("Command 'tell_time' failed")
            return {
                "ok": False,
                "message": "Couldn't check the time right now. Try again?",
                "data": {},
                "error": "tell_time_failed",
            }

    async def get_time_date(self, params: dict[str, object]) -> dict[str, object]:
        """Get current time, date, or both depending on the user query."""
        try:
            original = str(params.get("_original_text", ""))
            now = datetime.datetime.now()

            # Determine what user asked for
            wants_time = any(kw in original for kw in ("time",))
            wants_date = any(kw in original for kw in ("date", "day", "today"))

            # If neither specifically matched, give both
            if not wants_time and not wants_date:
                wants_time = True
                wants_date = True

            parts = []
            data: dict[str, object] = {}

            if wants_time:
                time_str = now.strftime("%I:%M %p").lstrip("0")
                parts.append("It's %s" % time_str)
                data["time"] = time_str

            if wants_date:
                # Format: Tuesday, February 17th
                day_name = now.strftime("%A")
                month_name = now.strftime("%B")
                day_num = now.day
                # Ordinal suffix
                if 11 <= day_num <= 13:
                    suffix = "th"
                else:
                    suffix = {1: "st", 2: "nd", 3: "rd"}.get(day_num % 10, "th")
                date_str = "%s, %s %d%s" % (day_name, month_name, day_num, suffix)
                parts.append(date_str)
                data["date"] = date_str
                data["year"] = now.year

            message = ", ".join(parts)
            return {"ok": True, "message": message, "data": data}

        except Exception:
            log.exception("Command 'get_time_date' failed")
            return {
                "ok": False,
                "message": "Couldn't check the time right now. Try again?",
                "data": {},
                "error": "time_date_failed",
            }

    async def get_time_in_city(self, params: dict[str, object]) -> dict[str, object]:
        """Get current time in a specific city or timezone."""
        import re
        import zoneinfo

        try:
            original = str(params.get("_original_text", ""))
            city_match = re.search(
                r"(?:time in|time is it in|current time in)\s+(.+?)\.?$",
                original,
                re.I,
            )
            city = city_match.group(1).strip().lower() if city_match else ""

            if not city:
                return {
                    "ok": False,
                    "message": "Which city do you want the time for?",
                    "data": {},
                }

            tz_name = self._CITY_TIMEZONES.get(city)

            if tz_name is None:
                try:
                    zoneinfo.ZoneInfo(city)
                    tz_name = city
                except (zoneinfo.ZoneInfoNotFoundError, KeyError):
                    pass

            if tz_name is None:
                for key, val in self._CITY_TIMEZONES.items():
                    if city in key or key in city:
                        tz_name = val
                        break

            if tz_name is None:
                return {
                    "ok": True,
                    "message": "I don't know the timezone for %s. Try asking with a major city name." % city.title(),
                    "data": {},
                }

            tz = zoneinfo.ZoneInfo(tz_name)
            now = datetime.datetime.now(tz)
            time_str = now.strftime("%I:%M %p").lstrip("0")
            tz_abbr = now.strftime("%Z")
            date_str = now.strftime("%A, %B %d")

            message = "It's %s in %s (%s, %s)" % (
                time_str,
                city.title(),
                date_str,
                tz_abbr,
            )

            return {
                "ok": True,
                "message": message,
                "data": {
                    "time": time_str,
                    "timezone": tz_name,
                    "tz_abbr": tz_abbr,
                    "city": city.title(),
                },
            }

        except Exception:
            log.exception("Command 'get_time_in_city' failed")
            return {
                "ok": False,
                "message": "Couldn't find the time for that location. Try a major city name?",
                "data": {},
                "error": "time_in_city_failed",
            }

    async def calculate(self, params: dict[str, object]) -> dict[str, object]:
        """Evaluate a math expression safely (no raw eval)."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract expression from various patterns
            expr = original
            for prefix in (
                "calculate ",
                "how much is ",
                "what's ",
                "what is ",
            ):
                if expr.startswith(prefix):
                    expr = expr[len(prefix) :]
                    break

            expr = expr.strip().rstrip("?.!")

            # Handle "percent of" pattern: "15 percent of 200" => 15 * 200 / 100
            pct_match = re.match(
                r"^(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+(\d+(?:\.\d+)?)$",
                expr,
                re.I,
            )
            if pct_match:
                pct_val = float(pct_match.group(1))
                base_val = float(pct_match.group(2))
                result_val = pct_val * base_val / 100.0
                # Format nicely: drop .0 for whole numbers
                result_str = "%g" % result_val if result_val == int(result_val) else "%.2f" % result_val
                return {
                    "ok": True,
                    "message": "%s percent of %s is %s" % (pct_match.group(1), pct_match.group(2), result_str),
                    "data": {"expression": expr, "result": result_val},
                }

            # Replace natural language operators with symbols
            math_expr = expr
            # Handle "square root of X" → "sqrt(X)" before other replacements
            math_expr = re.sub(
                r"square\s+root\s+of\s+(\S+)",
                r"sqrt(\1)",
                math_expr,
                flags=re.I,
            )
            replacements = [
                ("times", "*"),
                ("multiplied by", "*"),
                ("divided by", "/"),
                ("plus", "+"),
                ("minus", "-"),
                ("to the power of", "**"),
                ("squared", "**2"),
                ("cubed", "**3"),
                ("x", "*"),
            ]
            for word, symbol in replacements:
                math_expr = math_expr.replace(word, symbol)

            # Strip any remaining non-math characters (keep digits, operators, parens, dots, spaces, letters for sqrt/abs)
            sanitized = re.sub(r"[^0-9a-z+\-*/().%\s\^]", "", math_expr, flags=re.I)
            sanitized = sanitized.replace("^", "**").strip()

            if not sanitized:
                return {
                    "ok": False,
                    "message": "I couldn't understand that math expression.",
                    "data": {},
                    "error": "invalid_expression",
                }

            # Safe evaluation via AST walking (no eval)
            result_val = _safe_eval_math(sanitized)

            # Format result
            if isinstance(result_val, float):
                if result_val == int(result_val) and not math.isinf(result_val):
                    result_str = str(int(result_val))
                else:
                    result_str = "%.6g" % result_val
            else:
                result_str = str(result_val)

            return {
                "ok": True,
                "message": "%s is %s" % (expr, result_str),
                "data": {"expression": expr, "result": result_val},
            }

        except ZeroDivisionError:
            return {
                "ok": False,
                "message": "You can't divide by zero!",
                "data": {},
                "error": "division_by_zero",
            }
        except ValueError:
            # _safe_eval_math raises ValueError for unsupported syntax/nodes
            return {
                "ok": False,
                "message": "I handle basic math — addition, subtraction, multiplication, division, and powers. What operation do you need?",
                "data": {},
                "error": "unsafe_expression",
            }
        except (SyntaxError, TypeError):
            return {
                "ok": False,
                "message": "I couldn't calculate that. Try something like '15 times 7'.",
                "data": {},
                "error": "invalid_expression",
            }
        except Exception:
            log.exception("Command 'calculate' failed")
            return {
                "ok": False,
                "message": "I couldn't calculate that. Try something like '15 times 7'.",
                "data": {},
                "error": "calculate_failed",
            }

    async def define_word(self, params: dict[str, object]) -> dict[str, object]:
        """Look up a word definition using the free dictionary API."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract word from various patterns
            word = ""
            for pattern in (
                r"define\s+(\w+)",
                r"definition\s+of\s+(\w+)",
                r"meaning\s+of\s+(\w+)",
                r"what\s+does\s+(\w+)\s+mean",
            ):
                match = re.search(pattern, original, re.I)
                if match:
                    word = match.group(1)
                    break

            if not word:
                return {
                    "ok": False,
                    "message": "Please specify a word to define.",
                    "data": {},
                    "error": "no_word",
                }

            import httpx

            from core.constants import TIMEOUT_LONG

            url = "https://api.dictionaryapi.dev/api/v2/entries/en/%s" % word

            async with httpx.AsyncClient(timeout=TIMEOUT_LONG) as client:
                response = await client.get(url)

            if response.status_code == 404:
                return {
                    "ok": False,
                    "message": "I couldn't find a definition for '%s'" % word,
                    "data": {},
                    "error": "word_not_found",
                }

            response.raise_for_status()
            data = response.json()

            # Parse first definition
            if data and isinstance(data, list):
                meanings = data[0].get("meanings", [])
                if meanings:
                    definitions = meanings[0].get("definitions", [])
                    part_of_speech = meanings[0].get("partOfSpeech", "")
                    if definitions:
                        definition = definitions[0].get("definition", "")
                        pos_prefix = "(%s) " % part_of_speech if part_of_speech else ""
                        return {
                            "ok": True,
                            "message": "%s means %s%s" % (word.capitalize(), pos_prefix, definition),
                            "data": {
                                "word": word,
                                "definition": definition,
                                "part_of_speech": part_of_speech,
                            },
                        }

            return {
                "ok": False,
                "message": "I couldn't find a definition for '%s'" % word,
                "data": {},
                "error": "no_definition",
            }

        except Exception:
            log.exception("Command 'define_word' failed")
            return {
                "ok": False,
                "message": "Couldn't look up that word right now. Check your internet connection.",
                "data": {},
                "error": "define_word_failed",
            }

    async def spell_word(self, params: dict[str, object]) -> dict[str, object]:
        """Spell out a word letter by letter."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract word
            match = re.search(
                r"(?:spell|spelling\s+of|how\s+do\s+you\s+spell)\s+(\w+)",
                original,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Please specify a word to spell.",
                    "data": {},
                    "error": "no_word",
                }

            word = match.group(1)
            letters = ", ".join(letter.upper() for letter in word)
            message = "%s: %s" % (word.capitalize(), letters)

            return {
                "ok": True,
                "message": message,
                "data": {"word": word, "letters": list(word.upper())},
            }

        except Exception:
            log.exception("Command 'spell_word' failed")
            return {
                "ok": False,
                "message": "Couldn't spell that word. Try again?",
                "data": {},
                "error": "spell_word_failed",
            }

    async def read_clipboard(self, params: dict[str, object]) -> dict[str, object]:
        """Read the contents of the system clipboard."""
        try:
            try:
                import pyperclip
            except ImportError:
                return {
                    "ok": False,
                    "message": "Clipboard access requires the pyperclip package. Install it with: pip install pyperclip",
                    "data": {},
                    "error": "missing_dependency",
                }

            text = pyperclip.paste()

            if not text or not text.strip():
                return {
                    "ok": True,
                    "message": "Your clipboard is empty",
                    "data": {"content": ""},
                }

            # Truncate for TTS readability
            display = text.strip()
            if len(display) > 200:
                display = display[:200] + "..."

            return {
                "ok": True,
                "message": "Your clipboard says: %s" % display,
                "data": {"content": text, "truncated": len(text) > 200},
            }

        except Exception:
            log.exception("Command 'read_clipboard' failed")
            return {
                "ok": False,
                "message": "Couldn't access the clipboard. Check your system permissions.",
                "data": {},
                "error": "clipboard_failed",
            }

    async def countdown_to(self, params: dict[str, object]) -> dict[str, object]:
        """Calculate days until a given event or date."""
        try:
            original = str(params.get("_original_text", ""))

            # Extract event/date from text
            match = re.match(
                r"^(?:how\s+many\s+days\s+until|days\s+until|countdown\s+to|when\s+is)\s+(.+)$",
                original,
                re.I,
            )
            if not match:
                return {
                    "ok": False,
                    "message": "Please specify an event or date.",
                    "data": {},
                    "error": "no_event",
                }

            event = match.group(1).strip().rstrip("?.!")
            today = datetime.date.today()
            year = today.year
            target_date = None
            event_name = event.title()

            # Check known holidays
            holidays: dict[str, tuple[int, int]] = {
                "christmas": (12, 25),
                "christmas day": (12, 25),
                "new year": (1, 1),
                "new years": (1, 1),
                "new year's": (1, 1),
                "new year's day": (1, 1),
                "valentine's day": (2, 14),
                "valentines day": (2, 14),
                "valentine's": (2, 14),
                "valentines": (2, 14),
                "halloween": (10, 31),
                "independence day": (7, 4),
                "july 4th": (7, 4),
                "fourth of july": (7, 4),
                "4th of july": (7, 4),
            }

            event_lower = event.lower()

            if event_lower in holidays:
                month, day = holidays[event_lower]
                target_date = datetime.date(year, month, day)
                if target_date <= today:
                    target_date = datetime.date(year + 1, month, day)
            elif "thanksgiving" in event_lower:
                # 4th Thursday of November
                target_date = self._get_thanksgiving(year)
                if target_date <= today:
                    target_date = self._get_thanksgiving(year + 1)
                event_name = "Thanksgiving"
            elif "easter" in event_lower:
                target_date = self._get_easter(year)
                if target_date <= today:
                    target_date = self._get_easter(year + 1)
                event_name = "Easter"
            else:
                # Try to parse explicit dates like "March 15", "January 1st", "April 20"
                target_date = self._parse_date_reference(event, year, today)

            if target_date is None:
                return {
                    "ok": False,
                    "message": "I don't know when '%s' is. Try a holiday or a specific date like 'March 15'." % event,
                    "data": {},
                    "error": "unknown_event",
                }

            days_until = (target_date - today).days

            if days_until == 0:
                message = "%s is today!" % event_name
            elif days_until == 1:
                message = "%s is tomorrow!" % event_name
            else:
                message = "%s is in %d days" % (event_name, days_until)

            return {
                "ok": True,
                "message": message,
                "data": {
                    "event": event_name,
                    "date": target_date.isoformat(),
                    "days_until": days_until,
                },
            }

        except Exception:
            log.exception("Command 'countdown_to' failed")
            return {
                "ok": False,
                "message": "Couldn't calculate that countdown. Try a different date?",
                "data": {},
                "error": "countdown_failed",
            }

    def _get_thanksgiving(year: int) -> datetime.date:
        """Get Thanksgiving date (4th Thursday of November) for given year."""
        # Find first Thursday of November
        nov1 = datetime.date(year, 11, 1)
        # weekday(): Monday=0 ... Thursday=3
        first_thursday = nov1 + datetime.timedelta(days=(3 - nov1.weekday()) % 7)
        # 4th Thursday
        return first_thursday + datetime.timedelta(weeks=3)

    def _get_easter(year: int) -> datetime.date:
        """Calculate Easter Sunday using the Anonymous Gregorian algorithm."""
        a = year % 19
        b, c = divmod(year, 100)
        d, e = divmod(b, 4)
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d - g + 15) % 30
        i, k = divmod(c, 4)
        l_val = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l_val) // 451
        month = (h + l_val - 7 * m + 114) // 31
        day = ((h + l_val - 7 * m + 114) % 31) + 1
        return datetime.date(year, month, day)

    def _parse_date_reference(text: str, year: int, today: datetime.date) -> datetime.date | None:
        """Try to parse an explicit date reference like 'March 15' or 'January 1st'."""
        months = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }
        match = re.match(
            r"(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?$",
            text.strip(),
            re.I,
        )
        if match:
            month_name = match.group(1).lower()
            day = int(match.group(2))
            month_num = months.get(month_name)
            if month_num and 1 <= day <= 31:
                try:
                    target = datetime.date(year, month_num, day)
                    if target <= today:
                        target = datetime.date(year + 1, month_num, day)
                    return target
                except ValueError:
                    return None
        return None

    async def flip_coin(self, params: dict[str, object]) -> dict[str, object]:
        """Flip a coin and return heads or tails."""
        result = random.choice(["Heads", "Tails"])
        return {
            "ok": True,
            "message": "%s!" % result,
            "data": {"result": result.lower()},
        }

    async def roll_dice(self, params: dict[str, object]) -> dict[str, object]:
        """Roll one or more dice in NdM format (default 1d6)."""
        try:
            original = str(params.get("_original_text", ""))

            # Parse NdM format
            ndm_match = re.search(r"(\d+)d(\d+)", original, re.I)
            single_d_match = re.search(r"d(\d+)", original, re.I)

            if ndm_match:
                count = int(ndm_match.group(1))
                sides = int(ndm_match.group(2))
            elif single_d_match:
                count = 1
                sides = int(single_d_match.group(1))
            else:
                count = 1
                sides = 6

            # Safety limits
            count = max(1, min(count, 100))
            sides = max(2, min(sides, 1000))

            rolls = [random.randint(1, sides) for _ in range(count)]
            total = sum(rolls)

            if count == 1:
                message = "You rolled a %d" % rolls[0]
            else:
                roll_str = (
                    " and ".join(str(r) for r in rolls) if count <= 5 else ", ".join(str(r) for r in rolls[:5]) + "..."
                )
                message = "You rolled %dd%d: %s, total %d" % (
                    count,
                    sides,
                    roll_str,
                    total,
                )

            return {
                "ok": True,
                "message": message,
                "data": {
                    "rolls": rolls,
                    "total": total,
                    "count": count,
                    "sides": sides,
                },
            }

        except Exception:
            log.exception("Command 'roll_dice' failed")
            return {
                "ok": False,
                "message": "Couldn't roll the dice. Try saying 'roll a d6'?",
                "data": {},
                "error": "roll_dice_failed",
            }

    async def magic_8ball(self, params: dict[str, object]) -> dict[str, object]:
        """Return a classic Magic 8-Ball response."""
        responses = [
            # Affirmative
            "It is certain.",
            "It is decidedly so.",
            "Without a doubt.",
            "Yes, definitely.",
            "You may rely on it.",
            "As I see it, yes.",
            "Most likely.",
            "Outlook good.",
            "Yes.",
            "Signs point to yes.",
            # Non-committal
            "Reply hazy, try again.",
            "Ask again later.",
            "Better not tell you now.",
            "Cannot predict now.",
            "Concentrate and ask again.",
            # Negative
            "Don't count on it.",
            "My reply is no.",
            "My sources say no.",
            "Outlook not so good.",
            "Very doubtful.",
        ]
        answer = random.choice(responses)
        return {
            "ok": True,
            "message": "The Magic 8 Ball says... %s" % answer,
            "data": {"answer": answer},
        }

    async def repeat_last_response(self, params: dict[str, object]) -> dict[str, object]:
        """Repeat the last spoken response."""
        last = None
        state = getattr(self.controller, "state", None)
        if state is not None:
            last = getattr(state, "last_response", None) or getattr(state, "last_reply", None)
        if not last:
            return {
                "ok": True,
                "message": "I don't have a previous response to repeat. Ask me something!",
                "data": {},
            }
        return {"ok": True, "message": last, "data": {"repeated": True}}

    async def farewell(self, params: dict[str, object]) -> dict[str, object]:
        """Say goodbye."""
        responses = [
            "See you later!",
            "Bye! I'll be here when you need me.",
            "Goodbye! Have a great one.",
            "Later! Just say the word when you need me.",
        ]
        return {"ok": True, "message": random.choice(responses), "data": {}}

    async def clarify_vague_check(self, params: dict[str, object]) -> dict[str, object]:
        """Ask for clarification when the user says something vague like 'check everything'."""
        return {
            "ok": True,
            "intent": "clarify",
            "message": "Check what? Say: weather, calendar, timer, or music status.",
            "data": {},
        }

    async def resume_task(self, params: dict[str, object]) -> dict[str, object]:
        """Resume the most recent failed or timed-out agent task.

        Looks up the latest resumable checkpoint from
        ``intent.task_checkpoint``.  If one exists, returns a result
        that the pipeline can use to trigger a task resume.  Otherwise,
        returns a friendly "nothing to resume" message.
        """
        try:
            from intent.task_checkpoint import get_latest_resumable

            checkpoint = get_latest_resumable()
            if checkpoint:
                log.info(
                    "Resuming task %s: %s",
                    checkpoint.task_id,
                    checkpoint.task_description,
                )
                return {
                    "ok": True,
                    "intent": "resume_task",
                    "message": "Picking up where I left off \u2014 %s" % checkpoint.task_description,
                    "data": {
                        "resume_checkpoint_id": checkpoint.task_id,
                        "task_description": checkpoint.task_description,
                    },
                }
            # No resumable checkpoint — tell the user
            log.debug("No resumable task checkpoint found")
            return {
                "ok": True,
                "intent": "resume_task",
                "message": ("No previous task to resume. " "I'm ready — what's next?"),
                "data": {},
            }
        except Exception:
            log.exception("resume_task handler failed")
            return {
                "ok": False,
                "message": "Something went wrong looking for a task to resume.",
                "data": {},
                "error": "resume_task_failed",
            }
