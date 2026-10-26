"""把使用说明的标题、段落、列表和代码块生成为可离线打开的HTML。"""

from html import escape
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
STYLE = """
body{max-width:1000px;margin:32px auto;padding:0 22px;background:#f3f7fa;
font:16px/1.85 'Microsoft YaHei UI',sans-serif;color:#25394e}
article{background:white;border:1px solid #dce4ed;border-radius:10px;padding:30px}
h1,h2,h3{color:#145b63;line-height:1.5}h1{font-size:27px}h2{font-size:21px;margin-top:30px}
li{margin:6px 0}code{font-family:Consolas,'Microsoft YaHei UI',monospace;background:#edf3f6;
padding:1px 4px;border-radius:3px;overflow-wrap:anywhere}
pre{background:#edf3f6;padding:14px;white-space:pre-wrap;overflow-wrap:anywhere}
"""


def inline(text):
    """先转义原文，再标记行内代码，说明文字不能注入HTML。"""
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escape(text))


def render(source, destination):
    """仅支持本项目指南使用的简单Markdown结构，不处理外部原始HTML。"""
    blocks = []
    for block in source.read_text(encoding="utf-8-sig").strip().split("\n\n"):
        if block.startswith("```") and block.rstrip().endswith("```"):
            blocks.append("<pre>" + escape("\n".join(block.splitlines()[1:-1])) + "</pre>")
        elif match := re.fullmatch(r"(#{1,3}) ([^\n]+)", block):
            level = len(match[1])
            blocks.append(f"<h{level}>{inline(match[2])}</h{level}>")
        else:
            lines = block.splitlines()
            if all(line.startswith("- ") for line in lines):
                blocks.append(
                    "<ul>" + "".join(f"<li>{inline(line[2:])}</li>" for line in lines) + "</ul>"
                )
            elif all(re.match(r"^\d+\. ", line) for line in lines):
                items = [re.sub(r"^\d+\. ", "", line) for line in lines]
                blocks.append(
                    "<ol>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ol>"
                )
            else:
                blocks.append("<p>" + inline(block).replace("\n", "<br>") + "</p>")
    title = source.read_text(encoding="utf-8-sig").splitlines()[0].lstrip("# ")
    destination.write_text(
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body><article>"
        + "\n".join(blocks)
        + "</article></body></html>",
        encoding="utf-8",
    )


if __name__ == "__main__":
    render(ROOT / "docs/使用说明.md", ROOT / "docs/使用说明.html")
