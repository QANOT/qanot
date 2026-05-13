"""Group chat behaviour: zen-mode classifier + mute + signal scoring.

The pieces here run *before* a group message reaches the agent loop —
they decide whether the bot should respond at all. The default
``group_mode="mention"`` path doesn't use this code; zen mode does.
"""

from qanot.group.state import GroupChatState
from qanot.group.signals import SignalScore, collect_signals
from qanot.group.zen_classifier import GroupZenClassifier, ZenDecision

__all__ = [
    "GroupChatState",
    "SignalScore",
    "collect_signals",
    "GroupZenClassifier",
    "ZenDecision",
]
