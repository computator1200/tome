"""Mirror docs/ into a clone of the GitHub wiki.

    git clone https://github.com/computator1200/tome.wiki.git ../tome.wiki
    python scripts/sync_wiki.py ../tome.wiki
    cd ../tome.wiki && git add -A && git commit -m "sync from docs/" && git push

docs/ is the source; every page in the wiki is overwritten from it, and wiki
pages with no counterpart in docs/ are deleted. Links are rewritten so they
work in both places: `X.md` becomes the wiki page name, and `../path` becomes a
link into the repository on main.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

REPO = "https://github.com/computator1200/tome"
RAW = "https://raw.githubusercontent.com/computator1200/tome/main"
DOCS = Path(__file__).resolve().parent.parent / "docs"

# docs file -> wiki page name
PAGES = {
    "README.md": "Home",
    "ARCHITECTURE.md": "Architecture",
    "GRAPH-MEMORY.md": "Graph-memory",
    "MODELS.md": "Models",
    "DEPLOYMENT.md": "Deployment",
    "ROADMAP.md": "Roadmap",
    "ONBOARDING.md": "Onboarding",
}

_LINK = re.compile(r"(!?)\]\(([^)\s]+)\)")
_IMAGE_EXT = (".png", ".svg", ".gif", ".jpg", ".jpeg", ".webp")


def _rewrite(match: re.Match[str]) -> str:
    bang, target = match.group(1), match.group(2)
    path, _, frag = target.partition("#")
    anchor = f"#{frag}" if frag else ""
    if path in PAGES:
        return f"{bang}]({PAGES[path]}{anchor})"
    if path.startswith("../"):
        rel = path[3:]
        if bang or rel.lower().endswith(_IMAGE_EXT):
            return f"{bang}]({RAW}/{rel})"
        return f"{bang}]({REPO}/blob/main/{rel}{anchor})"
    return match.group(0)


def sync(wiki: Path) -> None:
    missing = sorted(p.name for p in DOCS.glob("*.md") if p.name not in PAGES)
    if missing:
        sys.exit(f"no wiki page name for {', '.join(missing)}; add it to PAGES")

    for old in wiki.glob("*.md"):
        old.unlink()
    for name, page in PAGES.items():
        text = (DOCS / name).read_text(encoding="utf-8")
        header = f"<!-- Generated from docs/{name} by scripts/sync_wiki.py. Edit it there. -->\n\n"
        (wiki / f"{page}.md").write_text(header + _LINK.sub(_rewrite, text), encoding="utf-8", newline="\n")

    images = wiki / "images"
    shutil.rmtree(images, ignore_errors=True)
    shutil.copytree(DOCS / "images", images)
    print(f"wrote {len(PAGES)} pages and {len(list(images.iterdir()))} images to {wiki}")


if __name__ == "__main__":
    if len(sys.argv) != 2 or not (Path(sys.argv[1]) / ".git").exists():
        sys.exit("usage: python scripts/sync_wiki.py <path to a clone of the wiki>")
    sync(Path(sys.argv[1]))
