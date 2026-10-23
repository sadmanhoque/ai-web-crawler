import os

_client = None


def summarize(title: str, text: str) -> str:
    global _client
    text = text[:20000]
    if not os.getenv("ANTHROPIC_API_KEY"):
        return text[:1200]                       # extractive fallback
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    msg = _client.messages.create(
        model=os.getenv("SUMMARY_MODEL", "claude-haiku-4-5-20251001"),
        max_tokens=400,
        messages=[{"role": "user", "content":
                   "Summarize this web page in at most 150 words for a search index. Keep key "
                   "facts, names, numbers and terminology. No preamble.\n\n"
                   f"Title: {title}\n\n{text}"}],
    )
    return msg.content[0].text.strip()
