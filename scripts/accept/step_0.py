"""Step 0 acceptance.

HANDOFF: README 开头写清结果目标、假设的基准（如人工每案例 15 分钟）、放弃条件。验收：三项都有具体数字
"""
import re
import sys
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"
SECTIONS = {  # README heading -> what it must state with a number
    "Where the time actually goes": r"\d+\s*min",  # baseline minutes per case
    "Outcome targets": r"\d",
    "Kill criteria": r"\d",
}


def main() -> int:
    parts = re.split(r"^### +", README.read_text(), flags=re.M)
    body = lambda h: next((p for p in parts[1:] if p.startswith(h)), "")  # noqa: E731
    failed = [h for h, pat in SECTIONS.items() if not re.search(pat, body(h))]
    for h in failed:
        print(f"FAIL: README needs a '### {h}' section with concrete numbers")
    print("step 0 PASS" if not failed else "step 0 FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
