"""
Greetings Skill

Handles basic greetings and pleasantries with time-aware responses.
"""

from __future__ import annotations

import random
from datetime import datetime

from ..base import Intent, Response, Skill


class GreetingsSkill(Skill):
    """Skill for handling greetings and basic conversation."""

    name = "greetings"
    description = "Handle greetings and basic conversation"
    priority = 30  # Lower priority - let specific skills handle first

    def patterns(self) -> list[str]:
        return [
            r"^(?:hi|hello|hey|greetings)(?:\s+viola)?",
            r"^(?:good )?(?:morning|afternoon|evening|night)",
            r"^how are you",
            r"^what'?s up",
            r"^thanks?(?:\s+you)?",
            r"^thank you",
            r"^(?:goodbye|bye|see you)",
        ]

    async def execute(self, intent: Intent) -> Response:
        """Respond to greetings."""
        text = intent.text.lower()

        # Greetings — time-aware
        if any(word in text for word in ["hi", "hello", "hey", "morning", "afternoon", "evening"]):
            hour = datetime.now().hour
            if 5 <= hour < 12:
                responses = [
                    "Good morning! What can I do for you?",
                    "Morning! Ready when you are.",
                    "Good morning. What's on your mind?",
                ]
            elif 12 <= hour < 17:
                responses = [
                    "Good afternoon! How can I help?",
                    "Afternoon! What do you need?",
                    "Hey there. What can I help with?",
                ]
            elif 17 <= hour < 21:
                responses = [
                    "Good evening! What can I do for you?",
                    "Evening! How can I help?",
                    "Hey! What are you in the mood for?",
                ]
            else:
                responses = [
                    "Hey, burning the midnight oil? What do you need?",
                    "Late night session! How can I help?",
                    "Still up? What can I do for you?",
                ]
            return Response(message=random.choice(responses), spoken=False)

        # How are you
        elif "how are you" in text or ("what" in text and "up" in text):
            responses = [
                "Doing well! What can I help with?",
                "Good! Ready when you are.",
                "Can't complain. What do you need?",
            ]
            return Response(message=random.choice(responses), spoken=False)

        # Thanks
        elif "thank" in text:
            responses = [
                "You're welcome!",
                "Happy to help!",
                "Anytime!",
                "My pleasure!",
            ]
            return Response(message=random.choice(responses), spoken=False)

        # Goodbye
        elif any(word in text for word in ["bye", "goodbye", "see you"]):
            responses = [
                "See you later!",
                "Bye! Have a good one.",
                "Later! I'll be here when you need me.",
            ]
            return Response(message=random.choice(responses), spoken=False)

        return Response(message="Hey! What can I do for you?", spoken=False)
