"""Headline extraction + optional paraphrasing."""
import os
import re

# Lines containing any of these (comma separated in .env) are dropped: channel signatures, dates...
STRIP = [s.strip() for s in os.getenv("STRIP_LINES_CONTAINING", "ቲክቫህ ኢትዮጵያ,TIKVAH").split(",") if s.strip()]
DATE_LINE = re.compile(r"^\s*(\S+\s+)?\d{1,2}\s+ቀን\s+\d{4}")      # e.g. "ጥቅምት 4 ቀን 2019 ዓ/ም"
HASHTAG_LINE = re.compile(r"^\s*(#\S+\s*)+$")


def clean_lines(text: str):
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            out.append(""); continue
        if HASHTAG_LINE.match(s) or DATE_LINE.match(s):
            continue
        if any(k.lower() in s.lower() for k in STRIP):
            continue
        out.append(s)
    return out


def split_headline_body(text: str, bold_first: str | None = None):
    """Headline = first bold paragraph if known, else first non-empty line.
    Body = the rest."""
    lines = clean_lines(text)
    paras, cur = [], []
    for ln in lines:                     # group into paragraphs
        if ln:
            cur.append(ln)
        elif cur:
            paras.append(" ".join(cur)); cur = []
    if cur:
        paras.append(" ".join(cur))
    if not paras:
        return "", ""
    headline = paras[0]
    if bold_first:
        b = " ".join(bold_first.split())
        for i, p in enumerate(paras):
            if b and (p.startswith(b) or b in p):
                headline = p if len(p) <= len(b) + 5 else b
                paras = paras[:i] + paras[i + 1:] if headline == p else paras
                body = "\n\n".join(paras)
                break
    else:
        body = "\n\n".join(paras[1:]).strip()

    # Clean any leading "Video" / "VIDEO" / "ቪዲዮ" labels/tags from headline
    headline = re.sub(r"^(?:\[?\bvideo\b\]?|\bvideo\b|\[?ቪዲዮ\]?|ቪዲዮ)\s*[-:–—|፦፡]?\s*", "", headline, flags=re.IGNORECASE).strip()
    return headline, body


def paraphrase(body: str, headline: str) -> str:
    """Rewrite the news body in Amharic with different wording. Falls back to the original text."""
    body = (body or "").strip()
    key = os.getenv("ANTHROPIC_API_KEY")
    if not body or not key:
        return body
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.getenv("PARAPHRASE_MODEL", "claude-haiku-5-5"),
            max_tokens=1500,
            messages=[{"role": "user", "content":
                "You rewrite Amharic news for a TikTok caption.\n"
                "Paraphrase the news below in natural Amharic with different wording and sentence structure.\n"
                "Rules: keep every fact, name, number and quote accurate; add nothing new; "
                "do not repeat the headline; no emojis; no hashtags; at most ~900 characters; "
                "output ONLY the paraphrased text.\n\n"
                f"HEADLINE: {headline}\n\nNEWS:\n{body}"}],
        )
        return msg.content[0].text.strip()
    except Exception as e:                      # never block the video because of the LLM
        print("paraphrase failed, using original text:", e)
        return body
