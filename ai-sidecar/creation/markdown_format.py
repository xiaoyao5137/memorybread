"""Conservative repairs for generated emphasis; never rewrite code or literal syntax."""
import re
import unicodedata


def normalize_creation_markdown(content: str) -> str:
    lines = content.splitlines(keepends=True)
    fence = None
    output = []
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            output.append(line)
            continue
        if marker:
            fence = marker[1]
            output.append(line)
            continue
        # Literal examples, links and indented code are deliberately left alone.
        if "`" in line or "\\" in line or "](" in line or "***" in line or re.match(r"^(?: {4}|\t)(?!\s*(?:[-+*]|\d+[.)])\s)", line):
            output.append(line)
            continue
        repaired = re.sub(r"^(\s*(?:[-+*]|\d+[.)])\s+)\*\*- (?=\*\*)", r"\1", line)
        artifact = repaired != line
        if artifact and repaired.count("**") % 2:
            repaired = re.sub(r"\*\*([ \t]*(?:\r?\n)?$)", r"\1", repaired)
        def emphasis(match):
            value = match[1]
            following = match.string[match.end():match.end() + 1]
            if not following.isalnum() or value != value.strip():
                return match[0]
            # CommonMark cannot close a punctuation-ended span before a letter.
            suffix = ""
            while value and unicodedata.category(value[-1]).startswith("P"):
                suffix = value[-1] + suffix
                value = value[:-1]
            return "**" + value + "**" + suffix if value and suffix else match[0]
        repaired = re.sub(r"\*\*([^*\n]+?)\*\*", emphasis, repaired)
        output.append(repaired)
    return "".join(output)
