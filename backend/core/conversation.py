"""
conversation.py — Conversation history helpers.

Provides:
  trim_history()              — keep last N turns (system message preserved)
  build_messages_with_rag()  — assemble messages list for LLM with optional RAG context
"""

from typing import List, Dict, Optional


def trim_history(
    history: List[Dict[str, str]],
    max_turns: int = 8,
) -> List[Dict[str, str]]:
    """Return a trimmed copy of conversation history.

    Always preserves the first message (system prompt).
    Keeps the most recent (max_turns - 1) messages after the system prompt.

    Args:
        history: Full conversation history list
        max_turns: Maximum total messages to keep (including system)

    Returns:
        Trimmed history list
    """
    if not history:
        return []

    system_msgs = [m for m in history if m.get("role") == "system"]
    other_msgs = [m for m in history if m.get("role") != "system"]

    if len(history) <= max_turns:
        return list(history)

    # Keep system message(s) + last (max_turns - len(system)) non-system messages
    keep_count = max(1, max_turns - len(system_msgs))
    trimmed = system_msgs + other_msgs[-keep_count:]
    print(f"[ConversationManager] History trimmed: {len(history)} → {len(trimmed)} messages")
    return trimmed


def build_messages_with_rag(
    history: List[Dict[str, str]],
    user_text: str,
    rag_context: Optional[str],
    system_prompt: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Build the messages list to send to the LLM.

    If system_prompt is provided and history is empty, prepend it.
    If rag_context is non-empty, inject it as a system-role message before
    the final user turn so the LLM sees fresh retrieved knowledge.

    Args:
        history: Existing conversation history (may include system message)
        user_text: The user's latest utterance. Normally already the last
                   message in history; only used as a fallback if it isn't.
        rag_context: Retrieved context string from RAG (may be empty/None)
        system_prompt: Optional system prompt to prepend if history is empty

    Returns:
        Messages list ready to pass to Azure OpenAI
    """
    messages: List[Dict[str, str]] = []

    # Prepend system prompt if history doesn't already have one
    has_system = any(m.get("role") == "system" for m in history)
    if system_prompt and not has_system:
        messages.append({"role": "system", "content": system_prompt})

    messages.extend(history)

    if rag_context and rag_context.strip():
        rag_msg = {
            "role": "system",
            "content": (
                "Use the following excerpts from the knowledge base to answer the user's "
                "question accurately. If the answer is clearly present in the context, "
                "use it. If not, rely on your general knowledge but do not fabricate "
                "specific details, prices, or amounts.\n\n"
                f"Knowledge Base Context:\n{rag_context}"
            ),
        }
        # The user turn is already the last message in history. Insert the RAG
        # context just before it (context → question) without duplicating the
        # user message.
        if messages and messages[-1].get("role") == "user":
            messages.insert(len(messages) - 1, rag_msg)
        else:
            messages.append(rag_msg)
            messages.append({"role": "user", "content": user_text})

    return messages
