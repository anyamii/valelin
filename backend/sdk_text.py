"""Keep paragraph boundaries when SDK text resumes after a tool call."""


class ToolTextJoiner:
    def __init__(self) -> None:
        self._has_text = False
        self._ends_with_newline = False
        self._after_tool = False

    def tool_boundary(self) -> None:
        self._after_tool = True

    def append(self, text: str) -> str:
        if not text:
            return text
        separator = "\n\n" if (
            self._after_tool and self._has_text and not self._ends_with_newline
        ) else ""
        self._after_tool = False
        self._has_text = True
        self._ends_with_newline = text.endswith(("\n", "\r"))
        return separator + text

    def agent_event(self, event: dict) -> str:
        if event.get("type") == "block_start" and event.get("kind") == "tool":
            self.tool_boundary()
        if event.get("type") == "text_delta":
            return self.append(str(event.get("text") or ""))
        return ""
