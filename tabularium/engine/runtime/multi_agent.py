"""Inter-Agent Context Bus and multi-agent coordination protocol (Phase 7 / DEC-2026-0006).

Implements Principle 7 ("AI opinions do not become decisions"):
Multi-agent consensus, critique, or debate constitutes advisory commentary.
Agents communicate via structured immutable messages on an audited bus.
"""

from __future__ import annotations

import datetime
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AgentRole(str, Enum):
    """Governed roles within a multi-agent orchestration DAG."""

    PLANNER = "PLANNER"
    WORKER = "WORKER"
    AUDITOR = "AUDITOR"
    GATE = "GATE"


class MessageType(str, Enum):
    """Categorization of inter-agent messages."""

    PROPOSAL = "PROPOSAL"                  # Planner: strategy / task decomposition
    EXECUTION = "EXECUTION"                # Worker: candidate work output
    CRITIQUE = "CRITIQUE"                  # Auditor: advisory evaluation / feedback
    ADVISORY = "ADVISORY"                  # Any: informational opinion
    VETO_RECOMMENDATION = "VETO_RECOMMENDATION"  # Auditor: advisory objection


@dataclass(frozen=True)
class InterAgentMessage:
    """One immutable message exchanged on the Inter-Agent Context Bus."""

    message_id: str
    sender_role: str
    recipient_role: str
    message_type: str
    content: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender_role": self.sender_role,
            "recipient_role": self.recipient_role,
            "message_type": self.message_type,
            "content": self.content,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InterAgentMessage":
        return cls(
            message_id=str(data.get("message_id", "")),
            sender_role=str(data.get("sender_role", "")),
            recipient_role=str(data.get("recipient_role", "")),
            message_type=str(data.get("message_type", "")),
            content=str(data.get("content", "")),
            created_at=str(data.get("created_at", "")),
            metadata=dict(data.get("metadata") or {}),
        )


class InterAgentBus:
    """Deterministic, append-only message bus maintaining full forensic auditability."""

    def __init__(self, initial_messages: list[InterAgentMessage] | None = None) -> None:
        self._messages: list[InterAgentMessage] = list(initial_messages or [])
        self._seq: int = len(self._messages)

    def send(self,
             sender_role: str | AgentRole,
             recipient_role: str | AgentRole,
             message_type: str | MessageType,
             content: str,
             metadata: dict[str, Any] | None = None) -> InterAgentMessage:
        """Append an immutable message to the bus."""
        self._seq += 1
        s_role = sender_role.value if isinstance(sender_role, AgentRole) else str(sender_role)
        r_role = recipient_role.value if isinstance(recipient_role, AgentRole) else str(recipient_role)
        m_type = message_type.value if isinstance(message_type, MessageType) else str(message_type)

        msg = InterAgentMessage(
            message_id=f"BUS-MSG-{self._seq:04d}",
            sender_role=s_role,
            recipient_role=r_role,
            message_type=m_type,
            content=content,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            metadata=dict(metadata or {}),
        )
        self._messages.append(msg)
        return msg

    def get_messages(self,
                     recipient_role: str | AgentRole | None = None,
                     sender_role: str | AgentRole | None = None,
                     message_type: str | MessageType | None = None) -> tuple[InterAgentMessage, ...]:
        """Filter messages by recipient, sender, or message type."""
        r_role = recipient_role.value if isinstance(recipient_role, AgentRole) else recipient_role
        s_role = sender_role.value if isinstance(sender_role, AgentRole) else sender_role
        m_type = message_type.value if isinstance(message_type, MessageType) else message_type

        filtered = [
            m for m in self._messages
            if (r_role is None or m.recipient_role in (r_role, "ALL"))
            and (s_role is None or m.sender_role == s_role)
            and (m_type is None or m.message_type == m_type)
        ]
        return tuple(filtered)

    def has_veto_recommendation(self) -> bool:
        """Check whether any Auditor has recorded an advisory veto recommendation."""
        return any(m.message_type == MessageType.VETO_RECOMMENDATION.value for m in self._messages)

    def to_list(self) -> list[dict[str, Any]]:
        """Serialize all messages in order."""
        return [m.to_dict() for m in self._messages]

    @classmethod
    def from_list(cls, data: list[dict[str, Any]] | None) -> "InterAgentBus":
        if not data:
            return cls()
        msgs = [InterAgentMessage.from_dict(item) for item in data if isinstance(item, dict)]
        return cls(initial_messages=msgs)
